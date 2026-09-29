"""Bring the live feed's readings into the database, by Epic's ids.

The predictor's live feed reads the standings of every cup under way every
ten minutes and keeps a day's worth of those readings per window. This pulls
the last few days back and files each reading as a snapshot of the matching
tournament - matched on the event and window ids the harvest stores, so
nothing is guessed from a name - and the live models finally get what they
have always lacked: tournaments read every ten minutes, dozens a week, taken
by nobody.

    python src/pull_live.py                  # the last 3 days, from the feed in site.json
    python src/pull_live.py --days 7         # further back (the feed keeps a month)
    python src/pull_live.py --from URL       # a feed elsewhere, or a local one for tests
    python src/pull_live.py --dry-run        # say what would be filed

`refresh.py` runs it after the harvest's build, so a window read last night
meets its freshly built tournament. A reading whose tournament the harvest
has not built yet - the leaderboard is downloaded once the window is over,
usually the next day - waits in `data/live_pending.json` and is filed on a
later run. A reading already filed (same tournament, same minute) is not
filed twice.

The board the feed read once the cup had settled - twenty minutes past the
close, see SETTLED_MINUTES - is also filed as the final at the ranks the
harvest holds no threshold for. The feed follows a cup down to its cuts; the
harvest's first pass stops at the third page, and until this the model read
a cut's rank off whichever older edition happened to hold it.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name == "src" else HERE      # the code lives in src/, the data beside it
PENDING = ROOT / "data" / "live_pending.json"
CANDIDATES = ["../threshold-ladder", "../predictor"]      # beside this folder
DAYS = 3
AGENT = "fortnite-comp-tracker/8.0 (+https://github.com/Anonymous4724)"

# A board settles in the minutes after its window closes, while the games
# under way at the buzzer come in. A reading taken this long past the close
# is the board as it will stay - analysis/live.py reads the feed's evenings
# by the same rule - and where the harvest never read that deep, it is the
# final the model gets: the feed follows a cup to its cuts, the first harvest
# pass stops at the third page, and the deeper passes take days.
SETTLED_MINUTES = 20


def feed_from_site() -> str:
    """The feed's address, from the predictor's site.json beside this folder."""
    for candidate in CANDIDATES:
        path = (ROOT / candidate / "site.json").resolve()
        if path.exists():
            try:
                live = str(json.loads(path.read_text(encoding="utf-8")).get("live") or "")
            except ValueError:
                live = ""
            if live:
                return live.rstrip("/")
    return ""


def fetch(url: str) -> dict | None:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:                  # a day nothing was read on has no file: normal
            print(f"  {url}: HTTP {exc.code}")
        return None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"  {url}: {exc}")
        return None


def days_back(count: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(count)]


def stamp(text: str) -> str | None:
    """The feed's timestamps in the database's own spelling: 'YYYY-MM-DD HH:MM', UTC."""
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def load_pending() -> dict:
    if not PENDING.exists():
        return {}
    try:
        return json.loads(PENDING.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def save_pending(pending: dict) -> None:
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=1), encoding="utf-8")


def gather(feed: str, days: int) -> dict:
    """Every window the feed read over the last `days`, readings merged by minute."""
    windows: dict = {}
    for day in days_back(days):
        doc = fetch(f"{feed}/history/{day}.json")
        if not doc:
            continue
        for key, window in (doc.get("windows") or {}).items():
            kept = windows.setdefault(key, {**{k: window.get(k) for k in ("event", "window", "name", "region", "begin", "end")},
                                            "readings": {}})
            for reading in window.get("readings") or []:
                when = stamp(reading.get("updated"))
                if when:
                    kept["readings"][when] = reading
    return windows


def settled_points(window: dict) -> dict[int, float]:
    """The board once it stopped moving: per rank, the richest reading taken
    SETTLED_MINUTES or more past the close. Empty while the cup runs."""
    try:
        end = datetime.fromisoformat(str(window.get("end") or "").replace("Z", "+00:00"))
    except ValueError:
        return {}
    settled = end + timedelta(minutes=SETTLED_MINUTES)
    out: dict[int, float] = {}
    for when, reading in window.get("readings", {}).items():
        for r in reading.get("readings") or []:
            if not isinstance(r, list) or len(r) < 2:
                continue
            # A rank read off a page stamped at its own minute is that minute's.
            stamp = str(r[2])[:16] if len(r) > 2 and r[2] else when[:16]
            try:
                at = datetime.strptime(stamp.replace("T", " ")[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                rank, value = int(r[0]), float(r[1])
            except (TypeError, ValueError):
                continue
            if at < settled or rank < 1 or value <= 0:
                continue
            if value > out.get(rank, 0):
                out[rank] = value
    return out


def file_window(conn, window: dict, dry: bool) -> tuple[int, int]:
    """(filed, already there) for one window whose tournament exists."""
    comp = db.find_by_window(conn, window.get("event") or "", window.get("window") or "")
    if not comp:
        return -1, 0
    seen = {str(s["ts"])[:16] for s in db.get_snapshots(conn, comp["id"])}
    filed = skipped = 0
    # How many played, where the feed counted them: off the board's last
    # page, or past the API's ten thousand off Epic's percentiles, where the
    # harvest only has the page count's "ten thousand or more". It raises the
    # tournament's field, never lowers it: a count taken mid-session is who
    # had played by then.
    counted = max((int(r.get("ranked") or 0) for r in window["readings"].values()), default=0)
    if counted > int(comp.get("field_size") or 0) and not dry:
        db.update_competition(conn, comp["id"], field_size=counted)
        print(f"  field     {window.get('name') or ''} ({window.get('region') or '?'}): "
              f"{int(comp.get('field_size') or 0):,} -> {counted:,}, the feed's count")
    # The settled board stands in for the final at the ranks the harvest did
    # not read - the cut the cup was played for, above all.
    settled = settled_points(window)
    if settled and not dry:
        added = db.add_finals_missing(conn, comp["id"], settled)
        if added:
            print(f"  finals    {window.get('name') or ''} ({window.get('region') or '?'}): "
                  f"ranks {', '.join(str(r) for r in added)} from the settled board")
    for when, reading in sorted(window["readings"].items()):
        # `[rank, points]`, or `[rank, points, stamp]` where the feed read
        # that rank off a page the API stamped at another minute: the run is
        # not one board, and each minute is filed as the reading it is.
        groups: dict[str, dict[int, float]] = {}
        for r in reading.get("readings") or []:
            if not isinstance(r, list) or len(r) < 2:
                continue
            try:
                rank, value = int(r[0]), float(r[1])
            except (TypeError, ValueError):
                continue
            if rank < 1 or value <= 0:
                continue
            # The page's stamp is the API's updatedAt as the worker keeps it,
            # ISO with a T: read into the database's spelling, or it never
            # matches a minute already filed and every pull files it again.
            minute = (stamp(r[2]) if len(r) > 2 and r[2] else None) or when[:16]
            groups.setdefault(minute, {})[rank] = value
        for minute, points in sorted(groups.items()):
            if minute in seen:
                skipped += 1
                continue
            ts = when if minute == when[:16] else minute
            if not dry:
                # A sealed lobby's reading is the board at the end of its last
                # finished game, and says when a game was under way at the time.
                db.add_snapshot(conn, comp["id"], ts=ts, points=points,
                                games=reading.get("games") or None,
                                # The board's page count at the reading, and
                                # the exact count of rosters where the feed
                                # read the last page too: who had played by then.
                                pages=reading.get("pages") or None,
                                ranked=reading.get("ranked") or None,
                                note="live feed" + (" · final" if reading.get("final") else "")
                                     + (" · game under way" if reading.get("partial") else ""))
            seen.add(minute)
            filed += 1
    return filed, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="feed", help="the feed's address (default: the predictor's site.json)")
    parser.add_argument("--days", type=int, default=DAYS, help=f"how many days back (default {DAYS})")
    parser.add_argument("--dry-run", action="store_true", help="say what would be filed, file nothing")
    args = parser.parse_args()

    feed = (args.feed or feed_from_site()).rstrip("/")
    if not feed:
        print("No live feed: site.json names none. Nothing to pull.")
        return 0
    print(f"feed      : {feed}")

    windows = gather(feed, max(1, args.days))
    pending = load_pending()
    # What waited from earlier runs joins today's pull, readings merged by minute.
    for key, window in pending.items():
        kept = windows.setdefault(key, {**{k: window.get(k) for k in ("event", "window", "name", "region", "begin", "end")},
                                        "readings": {}})
        kept["readings"].update(window.get("readings") or {})
    print(f"windows   : {len(windows)} read by the feed, "
          f"{sum(len(w['readings']) for w in windows.values())} readings")

    db.init_db()
    still: dict = {}
    filed_total = skipped_total = matched = 0
    with db.session() as conn:
        for key, window in sorted(windows.items(), key=lambda kv: kv[1].get("begin") or ""):
            filed, skipped = file_window(conn, window, args.dry_run)
            label = f"{window.get('name') or key} ({window.get('region') or '?'}, {window.get('begin') or '?'})"
            if filed < 0:
                still[key] = window
                print(f"  waiting   {label}: not built yet, {len(window['readings'])} reading(s) kept for later")
                continue
            matched += 1
            filed_total += filed
            skipped_total += skipped
            print(f"  {'would file' if args.dry_run else 'filed'} {filed:3} · {skipped:3} already there  {label}")
    if not args.dry_run:
        save_pending(still)
    print(f"\n{matched} tournament(s) matched, {filed_total} reading(s) filed, {skipped_total} already there, "
          f"{len(still)} window(s) waiting for the harvest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
