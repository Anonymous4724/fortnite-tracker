"""Build the training set from the free Osirion public API.

Three phases, each restartable, because the whole thing runs for hours:

    catalogue   one call per region: every event, its windows, its scoring rules
    fetch       the standings, page by page, saved to disk as they arrive
    build       turn what is on disk into competitions the model can learn from

Every response is written to `data/osirion/` gzipped and untouched before
anything is derived from it. That costs a few hundred megabytes and buys the
right to change your mind later: the per-game placements, the elimination
counts, the roster of every team and the size of every field are all in there,
and none of it has to be downloaded twice to answer a question this script was
not written to answer.

The standings go wide before they go deep. Reaching the API's maximum depth on
every window is days of downloading, so the fetch runs in passes: the first
covers every window shallowly, later ones come back for more pages. Stop after
any pass and the coverage is even rather than a complete third of the calendar
and nothing of the rest.

    python src/harvest_osirion.py --check          three calls, tells you what it sees
    python src/harvest_osirion.py --catalogue --fetch    download only
    python src/harvest_osirion.py --build          re-derive from what is on disk
    python src/harvest_osirion.py --passes 5,101   shallow everywhere, then as deep
                                               as the API allows

Interrupting is safe. Starting again picks up where it stopped.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import math
import os
import re
import sys
import time
from datetime import datetime

import db
import osirion

HERE = os.path.dirname(os.path.abspath(__file__))
# The code lives in `src/`, the data and the launchers one level up; a copy
# laid out flat still works, which is what keeps a move from breaking anything.
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "src" else HERE
RAW = os.path.join(ROOT, "data", "osirion")

# Ranks worth recording. Anything past the fourth page of standings is noise for
# a threshold model, and the deep ranks are what the curve extrapolates anyway.
RANKS = [1, 3, 5, 10, 20, 25, 50, 100, 120, 250, 500, 1000, 2500, 5000, 10000]

# The API refuses a page index above 100, and serves 100 entries a page, so the
# deepest rank anyone can reach through it is 10,100.
PAGE_CAP = 101


# --------------------------------------------------------------------------- #
# Running for hours, unattended
# --------------------------------------------------------------------------- #
# Windows sleeps a machine nobody has touched, and a sleeping machine has no
# network. Rather than ask the owner to disable sleep and remember to put it
# back, the run declares itself the way a download manager or a video player
# does, and the setting lapses the moment the process ends. The display is
# deliberately left out: the screen can go dark, the download continues.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def machine_awake():
    """Keep the computer from sleeping for as long as this block runs."""
    kernel = None
    if sys.platform == "win32":
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            if not kernel.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                kernel = None
        except Exception:
            kernel = None
    try:
        yield kernel is not None
    finally:
        if kernel is not None:
            kernel.SetThreadExecutionState(ES_CONTINUOUS)


class Tee:
    """Print to the console and to a file at once.

    The console is where you watch it; the file is what you read the morning
    after, once the window has been closed or the scrollback has rolled over.
    """

    def __init__(self, stream, path):
        self.stream = stream
        self.file = open(path, "a", encoding="utf-8", buffering=1)
        self.file.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M} =====\n")

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()


# --------------------------------------------------------------------------- #
# Files on disk
# --------------------------------------------------------------------------- #
def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "")[:120]


def raw_path(*parts: str) -> str:
    path = os.path.join(RAW, *[slug(p) for p in parts])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def save(path: str, payload) -> None:
    """Write the page whole or not at all.

    `refresh.py --build` may be reading this folder while a fetch is still
    running; a page that appears on disk half-written would be read as a
    truncated board and derive a wrong competition. The rename is atomic.
    """
    temp = path + ".part"
    with gzip.open(temp, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temp, path)


def load(path: str):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- #
# Phase 1 — the calendar
# --------------------------------------------------------------------------- #
def catalogue(regions: list[str]) -> dict:
    """One call per region. Cheap, so it always runs fresh."""
    events: dict[str, dict] = {}
    for region in regions:
        try:
            found = osirion.tournaments(region=region, historic=True)
        except osirion.OsirionError as exc:
            print(f"  {region:<7} failed: {exc}")
            continue
        save(raw_path("catalogue", f"{region}.json.gz"), found)
        for event in found:
            key = event.get("eventId")
            if key:
                events.setdefault(key, event)
        print(f"  {region:<7} {len(found):>4} events")
    save(raw_path("catalogue", "all.json.gz"), list(events.values()))
    return events


def windows_of(events: dict, from_year: int) -> list[dict]:
    """Flatten to a work list: one entry per (event, window) worth fetching."""
    work = []
    for event in events.values():
        for window in event.get("eventWindows") or []:
            begin = window.get("beginTime") or ""
            if not begin[:4].isdigit() or int(begin[:4]) < from_year:
                continue
            window_id = (window.get("eventWindowId") or window.get("windowId")
                         or window.get("id"))
            if not window_id:
                continue
            work.append({"event_id": event["eventId"], "window_id": window_id,
                         "begin": begin, "end": window.get("endTime") or "",
                         "round": window.get("round"), "event": event,
                         "window": window})
    # Newest first. The whole calendar is days of downloading, so whatever an
    # interrupted run did reach should be the part the model wants most: this
    # season's formats, not a cup that stopped running eighteen months ago.
    work.sort(key=lambda w: w["begin"], reverse=True)
    return work


# --------------------------------------------------------------------------- #
# Phase 2 — the standings
# --------------------------------------------------------------------------- #
def page_path(work: dict, page: int) -> str:
    return raw_path("leaderboards", work["event_id"],
                    f"{slug(work['window_id'])}_p{page:03d}.json.gz")


def read_page(work: dict, page: int):
    """A saved page, or None when it was never fetched or cannot be read."""
    path = page_path(work, page)
    if not os.path.exists(path):
        return None
    try:
        return load(path)
    except (OSError, ValueError, EOFError):
        # EOFError is what gzip raises for a file cut short — not an OSError.
        return None


def known_pages(work: dict):
    """How many pages this window has, if page 0 is already on disk."""
    first = read_page(work, 0)
    return osirion.total_pages(first) if first is not None else None


def fetch_window(work: dict, pages: int) -> int:
    """Download the pages we are missing, and no more than the window has.

    The response says how many pages there are, so a window with three of them
    costs three calls rather than three plus an empty probe - and a later,
    deeper pass reads that count off the disk and skips the window without
    asking at all.
    """
    calls, page = 0, 0
    limit = min(pages, PAGE_CAP)
    settled = known_pages(work)
    if settled is not None:
        limit = min(limit, settled)

    while page < limit:
        saved = read_page(work, page)
        if saved is None:
            try:
                saved = osirion.leaderboard_page(work["event_id"], work["window_id"], page)
            except osirion.OsirionError as exc:
                # A window that never ran 404s on page 0. Note it so no future
                # pass asks again.
                save(page_path(work, page), {"error": str(exc), "leaderboard":
                                             {"entries": [], "totalPages": page}})
                return calls + 1
            save(page_path(work, page), saved)
            calls += 1
        settled = osirion.total_pages(saved)
        if settled is not None:
            limit = min(limit, settled)
        if not osirion.entries_of(saved):
            break
        page += 1
    return calls


def fetch(work_list: list[dict], depths: list[int]) -> None:
    """Breadth first, then depth.

    Reaching the API's maximum depth on every window is days of downloading, so
    the passes go wide before they go deep: after the first one every window
    has its top few hundred ranks, which is already enough to rebuild the
    model. Later passes only fetch the pages the earlier ones did not, so
    stopping after any pass leaves a usable, evenly covered set rather than a
    complete third of the calendar and nothing of the rest.
    """
    for number, depth in enumerate(depths, 1):
        print(f"\n  pass {number}/{len(depths)} — up to {depth} pages a window")
        started, done, skipped = time.monotonic(), 0, 0
        for index, work in enumerate(work_list, 1):
            calls = fetch_window(work, depth)
            done += calls
            skipped += calls == 0
            if index % 25 == 0 or index == len(work_list):
                elapsed = time.monotonic() - started
                rate = done / elapsed if elapsed else 0
                eta = (len(work_list) - index) * (done / index) / rate / 60 if rate else 0
                print(f"    {index:>5}/{len(work_list)} windows · {done} calls · "
                      f"{rate * 60:.0f}/min · about {eta:.0f} min left in this pass")
        print(f"    pass done: {done} calls, {skipped} windows already complete")


# --------------------------------------------------------------------------- #
# Phase 3 — competitions the model can read
# --------------------------------------------------------------------------- #
def index_window(work: dict, entries: list[dict], payload: dict | None) -> dict:
    """Everything the raw pages say about a window that is not a threshold.

    Written next to the pages and never read by the model. It exists because
    the interesting questions come later — how many teams entered a cup, which
    regions grew, whether console-only events draw differently — and none of
    them are worth a second download.
    """
    event, window = work["event"], work["window"]
    countries = {}
    for entry in entries:
        for player in entry.get("players") or []:
            flag = (player.get("flagToken") or "").rsplit("_", 1)[-1]
            if flag:
                countries[flag] = countries.get(flag, 0) + 1
    return {
        "event_id": work["event_id"], "window_id": work["window_id"],
        "series": osirion.series_key(work["event_id"]),
        "season": osirion.season_of(work["event_id"]),
        "occurrence": osirion.occurrence_of(work["window_id"]),
        "round": osirion.round_of(work["window_id"]),
        "begin": work["begin"], "end": work["end"],
        "title": family_of(event),
        "regions": osirion.event_regions(event),
        "platforms": event.get("platforms") or [],
        "tags": osirion.tags(event, window),
        "team_mode": osirion.team_mode(event, entries, window),
        "match_cap": osirion.match_cap(window),
        "playlist": window.get("playlistId") or "",
        "game_mode": osirion.game_mode(event),
        "entries_seen": len(entries),
        "deepest_rank": max((e.get("rank") or 0) for e in entries) if entries else 0,
        "total_pages": (payload or {}).get("totalPages"),
        "games": osirion.match_cap(window) or max(
            (len(osirion._sessions(e)) for e in entries[:200]), default=0),
        "players_per_team": sorted({len(e.get("players") or []) for e in entries[:50]}),
        "countries": dict(sorted(countries.items(), key=lambda kv: -kv[1])[:30]),
        "payout_tiers": osirion.payout_tiers(window),
    }


def saved_entries(work: dict, pages: int) -> list[dict]:
    """Every standings row on disk for this window, in rank order."""
    rows = []
    for page in range(min(max(pages, 1), PAGE_CAP)):
        payload = read_page(work, page)
        if payload is None:
            break
        found = osirion.entries_of(payload)
        rows += found
        if not found:
            break
    rows.sort(key=lambda e: e.get("rank") or 10 ** 9)
    return rows


def thresholds_of(entries: list[dict]) -> dict[int, float]:
    """Points held by the team sitting exactly on each rank we care about.

    Zero is dropped rather than stored. Deep in a big leaderboard whole blocks of
    teams sit on nothing — they registered and never played — and "you need 0
    points to finish 5000th" is not a threshold anyone can be wrong about. The
    model is multiplicative, so it cannot produce a zero at all; keeping them
    puts a division by zero in every error measurement downstream.
    """
    by_rank = {}
    for entry in entries:
        rank, points = entry.get("rank"), entry.get("pointsEarned")
        if rank and points is not None and float(points) > 0:
            by_rank.setdefault(int(rank), float(points))
    return {r: by_rank[r] for r in RANKS if r in by_rank}


def field_of(work: dict, entries: list[dict]) -> int:
    """How many teams the leaderboard ranks — not how many we chose to download.

    This is the number that broke the model. Taking the deepest rank on disk
    makes every tournament exactly as wide as the harvest was deep: run three
    pages and all ten thousand tournaments record a field of 300, whether the
    real event ranked 400 teams or 40,000. The model's only positional variable
    is `q = rank / field`, so a field wrong by a factor of a hundred is a `q`
    wrong by a factor of a hundred, and the fitted shape bends itself around the
    error until it fits nothing.

    The API says how many pages the board has, page 0 carries it, and page 0 is
    always on disk — so the true size is recoverable without another request.
    Only the length of the final page is unknown, which is worth half a page:
    against a thousand teams that is 5 %, and at an elasticity of 0.05 it moves
    a threshold by a quarter of a percent.
    """
    deepest = max((e.get("rank") or 0) for e in entries) if entries else 0
    first = read_page(work, 0)
    total = osirion.total_pages(first) if first is not None else None
    if not total or total <= 0:
        return deepest
    # Measured, not assumed: if Epic changes the page size this follows it.
    per_page = len(osirion.entries_of(first)) if total > 1 else deepest
    if per_page <= 0:
        return deepest
    held = math.ceil(len(entries) / per_page)
    if held >= total:
        return deepest                       # the whole board is on disk
    return max(deepest, (total - 1) * per_page + per_page // 2)


def stage_of(work: dict) -> str:
    """Final, Semi-final, Round 3 — read from the window id, and only from it.

    The id is the one reliable field. Epic's `round` number beside it is a
    week counter, not a stage: `Week2Day1` carries `round: 2`, `Division2_Event3`
    carries `round: 2`, `Finals_Day3` carries `round: 3`, and a survey of the
    catalogue found no window where that number said something the id did not.
    Reading it as a stage filed week 2 of a weekly cup as its "Round 2", one
    category per week, with one edition each — the very split the whole
    classification exists to avoid. A window whose id names no round is the
    only session there is, and its stage is empty.
    """
    window = work["window"]
    return db.round_label(osirion.round_of(work.get("window_id")
                                           or window.get("eventWindowId") or ""))


def family_of(event: dict) -> str:
    """The cup's name: both title lines, because the second is not decoration.

    Epic writes "Solo | Victory Cup", "FNCS | Division 2", "Reload Duos |
    Cash Cup (Console)", "Fortnite | Performance Evaluation". Read alone the
    first line named three divisions "FNCS" and a weekly cup "Fortnite", and
    merged every cup that shares a first word. Joined, the older two-line titles
    even land on the names the newer one-line titles use — "Solo Victory Cup" —
    so a cup keeps its history across the seasons Epic restyled it.
    """
    display = event.get("displayData") or {}
    lines = [str(display.get(key) or "").strip() for key in ("titleLine1", "titleLine2")]
    name = " ".join(line for line in lines if line)
    if name:
        # Epic ran a Battle Royale and a Zero Build "Override Series" under one
        # title in the same hour; only the id tells them apart, and they are
        # not the same cup. Said in the name when the title leaves it out.
        event_id = event.get("eventId") or ""
        zb_in_id = re.search(r"(?:^|_)zb(?:_|$)|zerobuild|nobuild", event_id, re.I)
        low = name.lower()
        if zb_in_id and not ("zb" in re.split(r"[^a-z]+", low) or "zero build" in low
                             or "no build" in low):
            name += " (Zero Build)"
        return name[:80]
    for key in ("shortDescription", "title", "longFormatTitle"):
        value = display.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return (event.get("eventId") or "Tournament").replace("epicgames_", "")[:80]


# Bumped whenever family_of, stage_of or the naming of a competition changes.
# A build compares it with the stamp the last build left in the database and
# re-derives everything when they differ: an incremental build would otherwise
# file this week's editions under the new names and leave last season's under
# the old, and no category would ever join up again.
RULES_VERSION = "2026-09-05 entry requirement read off the window"


def rules_changed(conn) -> bool:
    return db.get_setting(conn, "harvest_rules") != RULES_VERSION


def stamp_rules(conn) -> None:
    db.set_setting(conn, "harvest_rules", RULES_VERSION)


def has_pages(work: dict) -> bool:
    """Cheap test: did the fetch ever reach this window?"""
    return os.path.exists(page_path(work, 0))


def build(work_list: list[dict], pages: int, limit: int | None = None,
          chunk: int = 200) -> dict:
    """Write one competition per window, with its measured thresholds.

    Reports on every window rather than on every write. An earlier version
    printed only when it wrote something, so a run over eleven thousand windows
    whose standings had not been downloaded yet said nothing at all for ten
    minutes and looked hung.

    Committed in chunks, because one transaction around eleven thousand
    tournaments is a long time to hold a lock and a lot to lose to a Ctrl-C.
    """
    counts = {"written": 0, "already": 0, "no_pages": 0, "too_few": 0,
              "scoring_ok": 0, "scoring_guessed": 0}
    todo = work_list[:limit]
    ready = [w for w in todo if has_pages(w)]
    print(f"  {len(todo)} windows, {len(ready)} with standings on disk")
    if not ready:
        counts["no_pages"] = len(todo)
        return counts

    started = time.monotonic()
    for offset in range(0, len(ready), chunk):
        with db.session() as conn:
            known = {(c["event_id"], c["window_id"])
                     for c in db.list_competitions(conn)
                     if c.get("event_id") and c.get("window_id")}
            for work in ready[offset:offset + chunk]:
                if (work["event_id"], work["window_id"]) in known:
                    counts["already"] += 1
                    continue
                entries = saved_entries(work, pages)
                finals = thresholds_of(entries)
                if len(finals) < 3:
                    counts["too_few"] += 1
                    continue
                save(raw_path("index", work["event_id"],
                              f"{slug(work['window_id'])}.json.gz"),
                     index_window(work, entries, None))

                event, window = work["event"], work["window"]
                scoring, agreement, kind = osirion.best_scoring(window, entries)
                if scoring and agreement >= 0.9:
                    counts["scoring_ok"] += 1
                else:
                    scoring, kind = None, "unverified"
                    counts["scoring_guessed"] += 1

                regions = osirion.event_regions(event)
                region = next((r for r in regions if r in db.REGIONS), regions[0][:8])
                # Epic states the game cap; counting sessions only sees what the
                # busiest team actually played, which is the same or fewer.
                games = (osirion.match_cap(window)
                         or max((len(osirion._sessions(e)) for e in entries[:200]), default=0))
                field = field_of(work, entries)

                stage = stage_of(work)
                comp_id = db.create_competition(
                    conn, name=f"{family_of(event)} - {stage}" if stage else family_of(event),
                    region=region,
                    team_mode=osirion.team_mode(event, entries, window),
                    game_mode=osirion.game_mode(event),
                    start_time=work["begin"].replace("T", " ").replace("Z", "")[:16],
                    end_time=work["end"].replace("T", " ").replace("Z", "")[:16] or None,
                    ranks=sorted(finals), max_games=games or None, scoring=scoring,
                    notes=f"osirion · scoring {kind}")
                db.update_competition(
                    conn, comp_id, source="osirion", family=family_of(event),
                    stage=stage, field_size=field or None,
                    entry=osirion.entry_requirement(window),
                    event_id=work["event_id"], window_id=work["window_id"], tracking=1,
                    finished_at=work["end"][:19].replace("T", " ") or None,
                    # The three axes, read out of Epic's ids rather than guessed
                    # from the title, plus the labels a person filters on.
                    series_key=osirion.series_key(work["event_id"]),
                    season=osirion.season_of(work["event_id"]),
                    occurrence=osirion.occurrence_of(work["window_id"]),
                    round_no=osirion.round_of(work["window_id"]),
                    tags=json.dumps(osirion.tags(event, window), ensure_ascii=False))
                db.set_finals(conn, comp_id, finals)

                tiers = osirion.payout_tiers(window, work["event_id"])
                if tiers:
                    db.set_objectives(conn,
                                      db.category_of(db.get_competition(conn, comp_id)),
                                      region, tiers[:12])
                counts["written"] += 1

        done = min(offset + chunk, len(ready))
        elapsed = time.monotonic() - started
        eta = (len(ready) - done) * elapsed / done / 60 if done else 0
        print(f"    {done:>6}/{len(ready)} · written {counts['written']} · "
              f"already there {counts['already']} · too few thresholds "
              f"{counts['too_few']} · about {eta:.0f} min left")
    return counts


# --------------------------------------------------------------------------- #
def check(regions: list[str]) -> int:
    """Three calls that answer the questions this script's estimates rest on."""
    print("\nOne catalogue call:")
    events = catalogue(regions[:1])
    if not events:
        print("  nothing came back — the API may be down or the region wrong")
        return 1
    work = windows_of(events, 2000)
    years = {}
    for item in work:
        years[item["begin"][:4]] = years.get(item["begin"][:4], 0) + 1
    print(f"  {len(events)} events, {len(work)} windows")
    print(f"  windows by year: {dict(sorted(years.items()))}")

    if not work:
        return 1
    recent = [w for w in work if w["begin"][:4] >= "2025"] or work
    sample = recent[len(recent) // 2]
    print(f"\nOne leaderboard call on {sample['event_id']} / {sample['window_id']}:")
    payload = osirion.leaderboard_page(sample["event_id"], sample["window_id"], 0)
    entries = osirion.entries_of(payload)
    pages = osirion.total_pages(payload)
    print(f"  {len(entries)} entries on page 0, {pages} page(s) in this window")
    if not entries:
        # The reader once looked for a key the API does not use and found
        # nothing, quietly, for hours. Nothing else in the check matters if
        # this fails, so it stops here rather than printing a tidy report
        # about an empty response.
        print("\n  THE READER FOUND NO ROWS in a response the API answered.")
        print(f"  Top-level keys: {sorted(payload)}")
        print("  The response shape has changed. Fix osirion.board() before")
        print("  downloading anything - a harvest against this would store")
        print("  pages nothing can read.\n")
        return 1
    first = entries[0]
    print(f"  rank 1 has {first.get('pointsEarned')} points over "
          f"{len(osirion._sessions(first))} games")
    scoring, agreement, kind = osirion.best_scoring(sample["window"], entries)
    if scoring:
        print(f"  scoring read as {kind}: kill={scoring['kill']} "
              f"cap={scoring['kill_cap']} tiers={len(scoring['placement'])}")
        print(f"  reproduces {agreement:.0%} of the published totals exactly")
    else:
        print("  no scoring rules on this window")

    print(f"  season {osirion.season_of(sample['event_id']) or '?'} · "
          f"series {osirion.series_key(sample['event_id'])} · "
          f"edition {osirion.occurrence_of(sample['window_id'])} · "
          f"round {osirion.round_of(sample['window_id'])}")
    print(f"  tags: {', '.join(osirion.tags(sample['event'], sample['window'])) or 'none'}")

    per_page = len(entries)
    windows_2025 = len([w for w in work if w["begin"][:4] >= "2025"])
    print(f"\nWhat it would cost, at {per_page} entries a page and {windows_2025} "
          f"windows from 2025 on:")
    print("  (an upper bound: a window stops at its own last page, and this one "
          f"has {pages})")
    for depth in (3, 10, 30, PAGE_CAP):
        calls = windows_2025 * depth
        print(f"  {depth:>3} pages a window (to rank {depth * per_page:>6}): "
              f"up to {calls:>7} calls, about {calls / osirion.LEADERBOARD_PER_MIN / 60:>5.1f} h")
    print("  Money: nothing. This API is free and takes no key.\n")
    return 0


def purge_manual() -> int:
    """Drop the hand-typed history, once the harvested set has replaced it."""
    with db.session() as conn:
        rows = [c for c in db.list_competitions(conn)
                if c.get("source") in ("manual", "history", "import")]
        harvested = len([c for c in db.list_competitions(conn)
                         if c.get("source") == "osirion"])
        print(f"\n  {len(rows)} hand-entered competition(s) would be deleted.")
        print(f"  {harvested} harvested competition(s) are in the database.")
        if harvested < 50:
            print("  Refusing: too few harvested tournaments to replace them.\n")
            return 1
        if input("  Delete the hand-entered ones? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  Cancelled.\n")
            return 0
        for comp in rows:
            db.delete_competition(conn, comp["id"])
    print(f"  {len(rows)} deleted. Manual entry still works — this only cleared "
          f"what was there.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--regions", default=",".join(osirion.REGIONS))
    parser.add_argument("--from-year", type=int, default=2025)
    parser.add_argument("--passes", default="3,10,30,101",
                        help="depths to fetch, shallowest first (API caps at 101)")
    parser.add_argument("--limit", type=int, help="stop after this many windows")
    parser.add_argument("--log", default=os.path.join(RAW, "harvest.log"),
                        help="where to copy the progress; empty string for none")
    parser.add_argument("--allow-sleep", action="store_true",
                        help="do not keep the computer awake while downloading")
    parser.add_argument("--rate", type=int, default=osirion.LEADERBOARD_PER_MIN,
                        help=f"leaderboard requests a minute, capped at "
                             f"{osirion.LEADERBOARD_LIMIT} by the API")
    parser.add_argument("--only", default="",
                        help="keep windows whose event id or title contains this")
    parser.add_argument("--check", action="store_true", help="three calls, then a report")
    parser.add_argument("--catalogue", action="store_true", help="phase 1 only")
    parser.add_argument("--fetch", action="store_true", help="phase 2 only")
    parser.add_argument("--build", action="store_true", help="phase 3 only")
    parser.add_argument("--rebuild", action="store_true",
                        help="delete the harvested tournaments and derive them again")
    parser.add_argument("--purge-manual", action="store_true",
                        help="delete hand-entered tournaments once harvesting is done")
    args = parser.parse_args()
    regions = [r.strip().upper() for r in args.regions.split(",") if r.strip()]
    rate = osirion.set_rate(args.rate)

    if args.log:
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        sys.stdout = Tee(sys.stdout, args.log)
        print(f"  progress is also written to {args.log}")

    if args.purge_manual:
        return purge_manual()
    if args.check:
        return check(regions)

    everything = not (args.catalogue or args.fetch or args.build)
    if (args.build or everything) and not args.rebuild:
        with db.session() as conn:
            if rules_changed(conn):
                print("\n  The naming rules have changed since the last build "
                      f"({RULES_VERSION}).\n  Re-deriving every harvested tournament so "
                      "old and new editions file under the same names — about seven "
                      "minutes.")
                args.rebuild = True
    if args.rebuild:
        with db.session() as conn:
            stale = [c["id"] for c in db.list_competitions(conn) if c.get("source") == "osirion"]
            for comp_id in stale:
                db.delete_competition(conn, comp_id)
        print(f"\n  {len(stale)} harvested tournament(s) removed; deriving them again.")
        print("  Hand-entered tournaments are untouched.")
        args.build = True
    started = datetime.now()
    keep_awake = contextlib.nullcontext(False) if args.allow_sleep else machine_awake()

    stack = contextlib.ExitStack()
    if stack.enter_context(keep_awake):
        print("  the computer will not fall asleep while this runs")
    elif not args.allow_sleep and sys.platform == "win32":
        print("  could not hold sleep off — set Power & battery > Sleep to Never")

    print("\nCatalogue")
    events_file = raw_path("catalogue", "all.json.gz")
    if everything or args.catalogue or not os.path.exists(events_file):
        events = catalogue(regions)
    else:
        events = {e["eventId"]: e for e in load(events_file) if e.get("eventId")}
        print(f"  {len(events)} events read from disk")
    work = windows_of(events, args.from_year)
    print(f"  {len(work)} windows from {args.from_year} on")
    if args.only:
        needle = args.only.lower()
        work = [w for w in work
                if needle in w["event_id"].lower() or needle in family_of(w["event"]).lower()]
        print(f"  {len(work)} of them match {args.only!r}")
    if args.limit:
        work = work[:args.limit]
        print(f"  limited to the {len(work)} most recent")

    depths = sorted({min(max(int(p), 1), 101)
                     for p in args.passes.split(",") if p.strip().isdigit()})
    if everything or args.fetch:
        print(f"\nStandings, in {len(depths)} pass(es): {depths}, at {rate} requests a minute")
        fetch(work, depths)

    if everything or args.build:
        print("\nBuilding competitions")
        counts = build(work, max(depths), limit=args.limit)
        print(f"\n  written {counts['written']}, already there {counts['already']}, "
              f"no standings downloaded {counts['no_pages']}, "
              f"too few thresholds {counts['too_few']}")
        print(f"  scoring verified against the standings {counts['scoring_ok']}, "
              f"unverified {counts['scoring_guessed']}")
        if counts["no_pages"] and not counts["written"]:
            print("\n  Nothing was built because nothing has been downloaded yet.")
            print("  Run the fetch first:  python src/harvest_osirion.py --catalogue --fetch\n")
        elif not args.limit:
            # A complete pass under the current rules: the next build can be
            # incremental again.
            with db.session() as conn:
                stamp_rules(conn)

    stack.close()
    print(f"\nDone in {(datetime.now() - started).seconds // 60} min.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
