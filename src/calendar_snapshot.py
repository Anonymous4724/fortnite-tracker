"""What is on in the next week, written where the predictor can read it.

The predictor is a page. A page cannot ask Osirion anything: the API answers the
request but sends no `Access-Control-Allow-Origin`, so every browser refuses to
hand the response to the script that asked for it — measured, from a local file
and from a real origin alike. No amount of client-side cleverness gets around
that; it is the browser refusing, not the network failing.

So the calendar is fetched here, on a machine that has no such rule, and written
beside the predictor as `calendar.js`. GitHub Pages then serves it from the same
origin as the page, which needs no permission at all. A `<script>` rather than a
JSON file for the same reason `model.js` is one: a page opened from `file://`
may not `fetch` its neighbour but may always load a script, so `standalone.html`
gets the same list, frozen at the time it was built.

What is written is not Osirion's response. It is the handful of fields the form
needs — name, region, team size, mode, games, scoring, when it starts, and the
cuts the cup pays out on — derived from it and nothing else. That is the
difference between an app that uses an API and a mirror that republishes one,
and it is deliberate.

The cuts are what turn a forecast into an answer: "top 2,000 goes through to
Round 2" is a rank, and a rank is what the model prices. Each row carries them
as `tiers`, a short list of `[code, number, detail]`:

    ["q", 2000, "Event 2 Round 2"]   the top 2,000 qualify, for that window
    ["p", 0.25, "Final"]             the top quarter of the field qualify
    ["c", 50, 25]                    prize money from 50th place, 25 dollars there
    ["i", 500, ""]                   a cosmetic down to 500th

Every qualification cut is kept; of the prize tiers only the first place and
the widest, since the question is "what gets me into the money", not the
whole payout table.

A cup with no finished edition in its region gets one more cell, `cold`: the
week's recent boards of the same format replayed under its own scoring table,
the median standing at each rank - see rescore.py. It needs the database and
the boards on disk, so a run without them (the repository's own workflow,
which refreshes the calendar between two runs of the machine that has them)
carries the cells the previous calendar wrote for the rows it still lists.

    python src/calendar_snapshot.py              the next 7 days, every region
    python src/calendar_snapshot.py --days 14    a longer window
    python src/calendar_snapshot.py --out PATH   somewhere other than the predictor
    python src/calendar_snapshot.py --dry-run    print what it would write
    python src/calendar_snapshot.py --no-replay  the rows without the replay cells

Run it from a scheduled task next to the harvest; commit the result. The live
feed reads the published calendar to know which cups to follow, so a cup
announced after the last run is a cup nobody followed: the workflow above is
what keeps the list fresh between two runs here.
"""
from __future__ import annotations

import argparse
import json
import re
import os
import sys
from datetime import datetime, timedelta, timezone

import db
import osirion
from harvest_osirion import family_of

HERE = os.path.dirname(os.path.abspath(__file__))
# The code lives in `src/`, the data and the launchers one level up; a copy
# laid out flat still works, which is what keeps a move from breaking anything.
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "src" else HERE
DAYS = 7

# Tournaments already under way are kept for a grace period: somebody opening
# the predictor mid-session wants the event they are playing, not the next one.
STARTED_GRACE_HOURS = 6


def when(text: str):
    """Epic's timestamps, or None when the field is missing or malformed."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def entry(event: dict, window: dict, region: str) -> dict | None:
    """One row of the form, derived from one event window."""
    begin, end = when(window.get("beginTime")), when(window.get("endTime"))
    if not begin:
        return None
    family = family_of(event)
    # Built as the harvest builds it, then asked of the app: the category key
    # is what joins this row to the model's tables, and a second spelling of it
    # here would drift out of step with the one the forecast looks up. The
    # stage comes from the window id alone — Epic's `round` number is a week
    # counter, see harvest_osirion.stage_of — and `source` says so, which is
    # what makes db.round_of trust the number and ignore any label.
    shaped = {"name": family, "family": family, "source": "osirion",
              "round_no": osirion.round_of(window.get("eventWindowId") or ""),
              "series_key": osirion.series_key(event.get("eventId") or "")}
    stage_no = db.round_of(shaped)
    label = db.round_label(stage_no)
    shaped["stage"] = label
    return {
        "kind": db.category_of(shaped),
        "name": f"{family} — {label}" if label else family,
        # Epic's own ids, so a live feed can ask for this window's standings
        # and the page can tell which row a feed entry belongs to.
        "event": event.get("eventId") or "",
        "window": window.get("eventWindowId") or "",
        # 9 is a final, 8 a semi-final, 0 the only session there is. The page
        # reads this to decide whether the lobby is closed — a final is played
        # by a fixed set of qualified teams in one lobby, so the field is the
        # lobby size and the format is sealed. Kept as a number so the page
        # does not have to parse a label that gets translated.
        "stage": stage_no,
        "region": region,
        "team": osirion.playlist_team_mode(window) or "",
        "mode": mode_of(event, window),
        "games": osirion.match_cap(window) or 0,
        "begin": begin.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if end else "",
        "scoring": osirion.scoring_from_window(window, cumulative=True),
        "tiers": tier_summary(osirion.payout_tiers(window, event.get("eventId") or "")),
        # Who may enter, as Epic's requirement spells it: the model reads the
        # previous edition with the same bar, and the page says what the bar is.
        "entry": osirion.entry_requirement(window),
    }


# The page shows chips, not a payout table: this many at most, best first.
TIERS_SHOWN = 6


def mode_of(event: dict, window: dict) -> str:
    """The game mode, or "Other" for a window that is not a tournament playlist.

    Every Battle Royale, Zero Build, Reload and Blitz cup is played on a
    `Playlist_ShowdownTournament_*` playlist that names the team size. A
    window on anything else - the Arena test cup plays on `Playlist_VK_Play` -
    is a format the model has never measured, and listing it as Battle Royale
    would price a box fight off a Battle Royale ladder.
    """
    playlist = (window.get("playlistId") or "").lower()
    if "showdown" in playlist or osirion.playlist_team_mode(window):
        return osirion.game_mode(event)
    return "Other"


def tier_summary(tiers: list[dict]) -> list[list]:
    """The cuts worth a chip, as compact rows the page reads.

    Qualification cuts are all kept — a cup rarely has more than three. Of the
    cash tiers only first place and the widest cut survive: a final lists a
    prize for every one of fifty places, and the two that answer a question
    are "what does it take to win" and "what gets me into the money". Same for
    cosmetics: the widest cut only. Series points are bookkeeping, dropped.
    """
    rows: list[list] = []
    for tier in tiers:
        if tier["kind"] != "qualify":
            continue
        if tier["rank"]:
            rows.append(["q", int(tier["rank"]), tier["label"]])
        elif tier["share"]:
            rows.append(["p", float(tier["share"]), tier["label"]])
    for kind, code in (("cash", "c"), ("item", "i")):
        ranked = [t for t in tiers if t["kind"] == kind and t["rank"]]
        if not ranked:
            continue
        widest = max(ranked, key=lambda t: t["rank"])
        first = min(ranked, key=lambda t: t["rank"])
        for tier in ([first, widest] if kind == "cash" and first is not widest else [widest]):
            detail = tier["amount"] if kind == "cash" and tier.get("currency") == "USD" \
                else (tier["label"] if kind == "cash" else "")
            if isinstance(detail, float) and detail == int(detail):
                detail = int(detail)
            rows.append([code, int(tier["rank"]), detail])
    return rows[:TIERS_SHOWN]


# What a label has to say for the cut to open a later window of the same event.
STAGE_WORDS = re.compile(r"\b(round|final|finals|semi|day|grand)\b", re.I)


def fed_by(event: dict) -> dict:
    """How many teams each later window of an event is played by.

    A second round's field is not a sign-up count: it is exactly the cut of the
    round before it - "the top 20 go through to Event 2 Round 2" - and Epic
    writes that cut in the earlier window's payout table, naming the window it
    opens. Read across the event, it says which windows are a single lobby
    (twenty Reload duos) and which are still an open queue of eight hundred.
    """
    out: dict = {}
    event_id = event.get("eventId") or ""
    for window in event.get("eventWindows") or []:
        for tier in osirion.payout_tiers(window, event_id):
            if tier.get("kind") == "qualify" and tier.get("rank") and not tier.get("share"):
                label = str(tier.get("label") or "")
                if label:
                    out[label] = max(out.get(label, 0), int(tier["rank"]))
    return out


def fed_next(event: dict) -> dict:
    """The same cuts, by the window that follows the cut's own in time.

    Epic's label sometimes names the window in words the id does not carry -
    "Event 3 Final" for a window called Round 2 - and then no label matches.
    A cut that opens a round or a final opens the event's next window as a
    rule, so that window is the fallback, keyed by its id - unless the next
    window is a sibling rather than a sequel: Heat 2 after Heat 1 is the same
    stage played again by other teams, and its field is nobody's cut. A cut
    whose label names no round - "FNCS Division 1", the promotion a Division
    2 cup plays for - opens another event altogether and feeds nothing here.
    """
    out: dict = {}
    event_id = event.get("eventId") or ""
    windows = sorted((w for w in event.get("eventWindows") or [] if when(w.get("beginTime"))),
                     key=lambda w: when(w.get("beginTime")))

    def shape(window: dict) -> str:
        token = osirion.humanise_token(window.get("eventWindowId") or "", event_id)
        return re.sub(r"\d+", "", token).strip().lower()

    for here, following in zip(windows, windows[1:]):
        rises = osirion.round_of(following.get("eventWindowId") or "") > osirion.round_of(here.get("eventWindowId") or "")
        if not rises and shape(here) == shape(following):
            continue
        ranks = [int(t["rank"]) for t in osirion.payout_tiers(here, event_id)
                 if t.get("kind") == "qualify" and t.get("rank") and not t.get("share")
                 and STAGE_WORDS.search(str(t.get("label") or ""))]
        if ranks:
            out[following.get("eventWindowId") or ""] = max(ranks)
    return out


def fed_field(fed: dict, token: str) -> int:
    """The cut that feeds the window named `token`, or 0 when none does.

    Epic names the window a cut opens in words of its own - "FNCS Division 1
    Week 2 Final", "FNCSSolo Qual 1 Round 3" - while the window id humanises
    to "Week 2 Final" or "Qual 1 Round 3". The two agree on the end, so the
    label is matched whole, then by its ending, then by carrying the token
    inside it, all with spaces and case set aside; never by the words alone,
    since "Event 2 Round 2" shares every word with "Event 1 Round 2".
    """
    def compact(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())
    wanted = compact(token)
    if not wanted:
        return 0
    best = 0
    for label, rank in fed.items():
        have = compact(label)
        if have == wanted:
            return int(rank)
        if have.endswith(wanted) or wanted in have:
            best = max(best, int(rank))
    return best


def collect(days: int = DAYS, regions=None) -> list[dict]:
    """Every window starting inside the next `days`, one row each."""
    now = datetime.now(timezone.utc)
    floor = now - timedelta(hours=STARTED_GRACE_HOURS)
    ceiling = now + timedelta(days=days)
    seen, rows = set(), []
    for asked in (regions or osirion.REGIONS):
        try:
            events = osirion.tournaments(asked, historic=False)
        except osirion.OsirionError as exc:
            print(f"  {asked}: {exc}", file=sys.stderr)
            continue
        for event in events:
            # The region is the event's own, not the one asked for: a query for
            # EU also returns the on-site LAN events, which belong to no region
            # and would otherwise be listed once per query under eight names.
            # Read the way the harvest reads it, so the row joins the model.
            own = osirion.event_regions(event)
            region = next((r for r in own if r in db.REGIONS), own[0][:8])
            fed, following = fed_by(event), fed_next(event)
            for window in event.get("eventWindows") or []:
                key = (event.get("eventId"), window.get("eventWindowId"))
                if key in seen:
                    continue
                begin = when(window.get("beginTime"))
                if not begin or not (floor <= begin <= ceiling):
                    continue
                row = entry(event, window, region)
                # Kept out of the list for the same reason they are kept out of
                # the training set: the model has nothing useful to say about
                # them, and offering a forecast it cannot make is worse than not
                # offering the tournament.
                if row and db.is_excluded({"family": row["kind"], "name": row["name"]}):
                    continue
                if row and row["games"]:
                    row["field"] = fed_field(fed, osirion.humanise_token(
                        window.get("eventWindowId") or "", event.get("eventId") or "")) \
                        or following.get(window.get("eventWindowId") or "", 0)
                    seen.add(key)
                    rows.append(row)
        print(f"  {asked:<7} {len(rows)} windows so far", flush=True)
    rows.sort(key=lambda r: (r["begin"], r["region"], r["name"]))
    return rows


def replay_cells(rows: list[dict], previous: dict | None, want: bool = True) -> int:
    """Hang the replay table on every row that can carry one; count them.

    With the database and the boards at hand the tables are computed; without
    them - the machine running this is not the one that harvests - the cells
    of the previous calendar are kept for the rows still listed, so a refresh
    of the list never takes a reading away. A cell is carried only for the
    same scoring table and game count, which its signature spells.
    """
    kept = {}
    for row in (previous or {}).get("events") or []:
        if isinstance(row.get("cold"), dict):
            kept[(row.get("event"), row.get("window"))] = row["cold"]
    written = 0
    conn = None
    if want and os.path.exists(db.DB_PATH):
        try:
            import rescore
            conn = db.connect()
        except Exception as exc:                                     # noqa: BLE001
            print(f"  (no replay tables: {exc})", file=sys.stderr)
            conn = None
    for row in rows:
        table = None
        if conn is not None:
            try:
                table = rescore.cold_table(conn, row)
            except Exception as exc:                                 # noqa: BLE001
                print(f"  (replay failed for {row.get('name')}: {exc})", file=sys.stderr)
                table = None
        if table is None:
            table = kept.get((row.get("event"), row.get("window")))
            if table and table.get("sig") and table["sig"] != rescore_signature(row):
                table = None
        if table:
            row["cold"] = table
            written += 1
    if conn is not None:
        conn.close()
    return written


def rescore_signature(row: dict) -> str:
    """The signature a replay table of this row would carry."""
    from calibration import table_signature
    scoring = row.get("scoring") if isinstance(row.get("scoring"), dict) else None
    return table_signature(scoring, row.get("games"))


def previous_calendar(path: str) -> dict | None:
    """The calendar already written at `path`, or None."""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    start, stop = text.find("{"), text.rfind("}")
    if start < 0 or stop < 0:
        return None
    try:
        return json.loads(text[start:stop + 1])
    except ValueError:
        return None


def packed(rows: list[dict], days: int) -> dict:
    """The rows with their scoring tables shared rather than repeated.

    A scoring table is twenty-odd placement rows and a handful of events share
    each one, so repeating them costs more than the rest of the file put
    together. They go in a list; each row holds an index.
    """
    tables: list[str] = []
    for row in rows:
        scoring = row.pop("scoring", None)
        if not scoring or not scoring.get("placement"):
            row["scoring"] = None
            continue
        blob = json.dumps(scoring, sort_keys=True, separators=(",", ":"))
        if blob not in tables:
            tables.append(blob)
        row["scoring"] = tables.index(blob)
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "days": days,
        "scorings": [json.loads(t) for t in tables],
        "events": rows,
    }


def predictor_dir() -> str | None:
    """The predictor repository, found the way update_predictor.py finds it."""
    parent = os.path.dirname(ROOT)
    for name in sorted(os.listdir(parent)):
        path = os.path.join(parent, name)
        if os.path.isdir(path) and os.path.exists(os.path.join(path, "build.py")) \
                and os.path.exists(os.path.join(path, "src", "app.html")):
            return path
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=DAYS,
                        help=f"how far ahead to look (default {DAYS})")
    parser.add_argument("--out", help="where to write calendar.js")
    parser.add_argument("--regions", default="",
                        help="comma-separated, default every region Osirion lists")
    parser.add_argument("--dry-run", action="store_true",
                        help="print a summary and write nothing")
    parser.add_argument("--no-replay", action="store_true",
                        help="write the rows without the replay tables (see rescore.py)")
    args = parser.parse_args()

    regions = [r.strip().upper() for r in args.regions.split(",") if r.strip()] or None
    print(f"Reading the next {args.days} days from Osirion")
    rows = collect(args.days, regions)
    if not rows:
        print("Nothing starts in that window — writing nothing, keeping the old file.",
              file=sys.stderr)
        return 1

    out = args.out
    if not out:
        found = predictor_dir()
        if not found and not args.dry_run:
            print("No predictor folder beside this one — pass --out.", file=sys.stderr)
            return 1
        out = os.path.join(found, "calendar.js") if found else ""

    # The replay tables, before the scoring tables are packed away: a cup
    # with no edition in its region, its table replayed on the week's boards.
    replayed = replay_cells(rows, previous_calendar(out) if out else None, want=not args.no_replay)

    payload = packed(rows, args.days)
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    text = f"window.CALENDAR = {blob};\n"

    by_region: dict = {}
    for row in payload["events"]:
        by_region[row["region"]] = by_region.get(row["region"], 0) + 1
    print(f"\n{len(rows)} windows, {len(payload['scorings'])} distinct scoring tables, "
          f"{len(text.encode('utf-8')) / 1024:.0f} KB")
    print("  " + "  ".join(f"{k} {v}" for k, v in sorted(by_region.items())))
    named = sum(1 for r in payload["events"] if r["scoring"] is not None)
    print(f"  {named} of {len(rows)} carry a scoring table the form can prefill")
    cuts = sum(1 for r in payload["events"] if any(t[0] in ("q", "p") for t in r["tiers"]))
    print(f"  {cuts} of {len(rows)} say who qualifies, so the page can ask the right rank")
    print(f"  {replayed} carry a replay table: a cup with no edition in its region, "
          f"priced off recent boards replayed under its scoring")

    if args.dry_run:
        for row in payload["events"][:8]:
            print(f"    {row['begin']}  {row['region']:<5} {row['name'][:52]}")
        return 0

    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\nWrote {out}")
    print("  Rebuild the page and commit both:  python build.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
