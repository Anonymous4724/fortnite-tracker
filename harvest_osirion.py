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

    python harvest_osirion.py --check          three calls, tells you what it sees
    python harvest_osirion.py                  everything, from 2025 on
    python harvest_osirion.py --pages 20       deeper standings, slower
    python harvest_osirion.py --build          re-derive from what is already saved

Interrupting is safe. Starting again picks up where it stopped.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from datetime import datetime

import db
import osirion

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "osirion")

# Ranks worth recording. Anything past the fourth page of standings is noise for
# a threshold model, and the deep ranks are what the curve extrapolates anyway.
RANKS = [1, 3, 5, 10, 20, 25, 50, 100, 120, 250, 500, 1000, 2500]


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
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


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
    work.sort(key=lambda w: w["begin"])
    return work


# --------------------------------------------------------------------------- #
# Phase 2 — the standings
# --------------------------------------------------------------------------- #
def page_path(work: dict, page: int) -> str:
    return raw_path("leaderboards", work["event_id"],
                    f"{slug(work['window_id'])}_p{page:03d}.json.gz")


def fetch_window(work: dict, pages: int) -> int:
    """Download the pages we are missing. Returns how many calls were made."""
    calls = 0
    for page in range(pages):
        path = page_path(work, page)
        if os.path.exists(path):
            continue
        try:
            payload = osirion.leaderboard_page(work["event_id"], work["window_id"], page)
        except osirion.OsirionError as exc:
            # An empty window (cancelled, or never played) 404s on page 0. Note
            # it and move on rather than retrying it on every future run.
            save(path, {"error": str(exc), "leaderboardData": []})
            calls += 1
            break
        entries = payload.get("leaderboardData") or []
        save(path, payload)
        calls += 1
        if not entries:
            break
    return calls


def fetch(work_list: list[dict], pages: int) -> None:
    started, done = time.monotonic(), 0
    for index, work in enumerate(work_list, 1):
        done += fetch_window(work, pages)
        if index % 10 == 0 or index == len(work_list):
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed else 0
            left = (len(work_list) - index) * pages
            eta = left / rate / 60 if rate else 0
            print(f"  {index:>5}/{len(work_list)} windows · {done} calls · "
                  f"{rate * 60:.0f}/min · about {eta:.0f} min left")


# --------------------------------------------------------------------------- #
# Phase 3 — competitions the model can read
# --------------------------------------------------------------------------- #
def entries_of(work: dict, pages: int) -> list[dict]:
    """Every standings row on disk for this window, in rank order."""
    rows = []
    for page in range(pages):
        path = page_path(work, page)
        if not os.path.exists(path):
            break
        try:
            payload = load(path)
        except (OSError, ValueError):
            break
        rows += payload.get("leaderboardData") or []
    rows.sort(key=lambda e: e.get("rank") or 10 ** 9)
    return rows


def thresholds_of(entries: list[dict]) -> dict[int, float]:
    """Points held by the team sitting exactly on each rank we care about."""
    by_rank = {}
    for entry in entries:
        rank, points = entry.get("rank"), entry.get("pointsEarned")
        if rank and points is not None:
            by_rank.setdefault(int(rank), float(points))
    return {r: by_rank[r] for r in RANKS if r in by_rank}


def stage_of(work: dict) -> str:
    """Round 1, Final, Day 3 — whatever the window calls itself."""
    window = work["window"]
    for key in ("round", "windowName", "name"):
        value = window.get(key)
        if isinstance(value, int):
            return f"Round {value}"
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    tail = work["window_id"].rsplit("_", 1)[-1]
    return tail[:40] if tail else "Round 1"


def family_of(event: dict) -> str:
    display = event.get("displayData") or {}
    for key in ("titleLine1", "shortDescription", "title"):
        value = display.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return (event.get("eventId") or "Tournament").replace("epicgames_", "")[:80]


def build(work_list: list[dict], pages: int, limit: int | None = None) -> dict:
    """Write one competition per (window, region), with its measured thresholds."""
    counts = {"written": 0, "skipped": 0, "scoring_ok": 0, "scoring_guessed": 0}
    with db.session() as conn:
        known = {(c["event_id"], c["window_id"])
                 for c in db.list_competitions(conn)
                 if c.get("event_id") and c.get("window_id")}
        for work in work_list[:limit]:
            key = (work["event_id"], work["window_id"])
            if key in known:
                counts["skipped"] += 1
                continue
            entries = entries_of(work, pages)
            finals = thresholds_of(entries)
            if len(finals) < 3:
                counts["skipped"] += 1
                continue

            event, window = work["event"], work["window"]
            scoring, agreement, kind = osirion.best_scoring(window, entries)
            if scoring and agreement >= 0.9:
                counts["scoring_ok"] += 1
            else:
                scoring, kind = None, "unverified"
                counts["scoring_guessed"] += 1

            regions = osirion.event_regions(event)
            region = next((r for r in regions if r in db.REGIONS), regions[0][:8])
            games = max((len(osirion._sessions(e)) for e in entries[:200]), default=0)
            field = max((e.get("rank") or 0) for e in entries) if entries else 0

            comp_id = db.create_competition(
                conn, name=f"{family_of(event)} - {stage_of(work)}", region=region,
                team_mode=osirion.team_mode(event, entries),
                game_mode=osirion.game_mode(event),
                start_time=work["begin"].replace("T", " ").replace("Z", "")[:16],
                end_time=work["end"].replace("T", " ").replace("Z", "")[:16] or None,
                ranks=sorted(finals), max_games=games or None, scoring=scoring,
                notes=f"osirion · scoring {kind}")
            db.update_competition(conn, comp_id, source="osirion",
                                  family=family_of(event), stage=stage_of(work),
                                  field_size=field or None, event_id=work["event_id"],
                                  window_id=work["window_id"], tracking=1,
                                  finished_at=work["end"][:19].replace("T", " ") or None)
            db.set_finals(conn, comp_id, finals)

            tiers = osirion.payout_tiers(window)
            if tiers:
                db.set_objectives(conn, db.category_of(db.get_competition(conn, comp_id)),
                                  region, tiers[:12])
            counts["written"] += 1
            if counts["written"] % 50 == 0:
                print(f"  {counts['written']} competitions written")
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
    entries = payload.get("leaderboardData") or []
    print(f"  {len(entries)} entries on page 0, totalPages={payload.get('totalPages')}")
    if entries:
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

    per_page = len(entries) or 1
    print(f"\nWhat a full run would cost, at {per_page} entries a page:")
    for pages in (5, 10, 20):
        windows_2025 = len([w for w in work if w["begin"][:4] >= "2025"])
        calls = windows_2025 * pages * len(regions) // max(len(regions), 1)
        print(f"  {pages:>2} pages/window (to rank {pages * per_page:>5}): "
              f"{calls:>6} calls, about {calls / osirion.LEADERBOARD_PER_MIN / 60:.1f} h")
    print("\n  Money: nothing. This API is free and takes no key.\n")
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
    parser.add_argument("--pages", type=int, default=10,
                        help="leaderboard pages per window (the API caps at 101)")
    parser.add_argument("--limit", type=int, help="stop after this many windows")
    parser.add_argument("--check", action="store_true", help="three calls, then a report")
    parser.add_argument("--catalogue", action="store_true", help="phase 1 only")
    parser.add_argument("--fetch", action="store_true", help="phase 2 only")
    parser.add_argument("--build", action="store_true", help="phase 3 only")
    parser.add_argument("--purge-manual", action="store_true",
                        help="delete hand-entered tournaments once harvesting is done")
    args = parser.parse_args()
    regions = [r.strip().upper() for r in args.regions.split(",") if r.strip()]

    if args.purge_manual:
        return purge_manual()
    if args.check:
        return check(regions)

    everything = not (args.catalogue or args.fetch or args.build)
    started = datetime.now()

    print("\nCatalogue")
    events_file = raw_path("catalogue", "all.json.gz")
    if everything or args.catalogue or not os.path.exists(events_file):
        events = catalogue(regions)
    else:
        events = {e["eventId"]: e for e in load(events_file) if e.get("eventId")}
        print(f"  {len(events)} events read from disk")
    work = windows_of(events, args.from_year)
    print(f"  {len(work)} windows from {args.from_year} on")
    if args.limit:
        work = work[:args.limit]

    if everything or args.fetch:
        print(f"\nStandings, {args.pages} pages a window")
        fetch(work, args.pages)

    if everything or args.build:
        print("\nBuilding competitions")
        counts = build(work, args.pages)
        print(f"  written {counts['written']}, already there {counts['skipped']}, "
              f"scoring verified {counts['scoring_ok']}, "
              f"scoring unverified {counts['scoring_guessed']}")

    print(f"\nDone in {(datetime.now() - started).seconds // 60} min.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
