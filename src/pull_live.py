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


def file_window(conn, window: dict, dry: bool) -> tuple[int, int]:
    """(filed, already there) for one window whose tournament exists."""
    comp = db.find_by_window(conn, window.get("event") or "", window.get("window") or "")
    if not comp:
        return -1, 0
    seen = {str(s["ts"])[:16] for s in db.get_snapshots(conn, comp["id"])}
    filed = skipped = 0
    for when, reading in sorted(window["readings"].items()):
        if when[:16] in seen:
            skipped += 1
            continue
        points = {int(r[0]): float(r[1]) for r in reading.get("readings") or []
                  if isinstance(r, list) and len(r) == 2 and float(r[1]) > 0}
        if not points:
            continue
        if not dry:
            # A sealed lobby's reading is the board at the end of its last
            # finished game, and says when a game was under way at the time.
            db.add_snapshot(conn, comp["id"], ts=when, points=points,
                            games=reading.get("games") or None,
                            note="live feed" + (" · final" if reading.get("final") else "")
                                 + (" · game under way" if reading.get("partial") else ""))
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
