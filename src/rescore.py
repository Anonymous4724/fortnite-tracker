"""A cup nobody has seen, priced by replaying recent boards under its table.

The cold rungs of the forecast read a level off the scoring table - a share of
the maximum game score, measured on cups of the same kind - and that share is
one number for a whole family of tables. It cannot tell a table that pays the
win 60 and the kill 2 from one that pays 65 and 1, and it was a tenth low on
Battle Royale skin cups, a fifth low on mobile, a sixth high on Reload.

What a board actually says is finer than a level: every roster's placement and
eliminations in every game it played. Re-scored under the new cup's table -
this placement worth that many points, these kills at that rate, capped where
the table caps them - the same games give the standings the new table would
have produced, and rank 20 of those standings is a forecast for rank 20 of the
new cup. That is the "average tournament of a top-x roster", replayed rather
than modelled: the same evenings, the same lobbies, another table.

Donors are the most recent boards of the same region, team size, game mode and
platform (mobile, console, PC), open queues only, one lobby's odd formats left
out; those played to the same number of games first, the others scaled by the
measured per-game exponent. The median across donors is the reading.

Two limits, both measured. A donor board is loaded a few pages deep and the
re-ordering pulls rosters up from below the pages held: past a third of the
rosters loaded the rank read is biased low, so the table stops there and the
forecast continues along the ladder. And the reading is only as good as the
donors are recent: a board from last week says more than one from June.

Measured by analysis/rescore.py on the 102 open queues since mid-August that
the rolling validation had to price off the scoring table or the family, each
read from boards played before it: median error 4.8 % at ranks 1-5, 3.8 % at
6-25, 3.7 % at 26-100 and 4.2 % at 101-250 against 8.1, 9.0, 10.2 and 10.2 for
the model's cold rungs, the bias within two points of zero; continued along
the ladder past the covered depth, 6.2 % at ranks 251-1,000 against 11.4. On
the cups that had already run in another region, half of the family reading
and half of this one, in log terms, beat either alone: 3.5 % against 5.9 and
6.5 on the seven day-two cups of the sample.

    python src/rescore.py --event EVENT --window WINDOW    the table for one window
    python src/rescore.py --calendar                       every cold row of the week

`calendar_snapshot` calls `cold_table` for every row of the week whose cup has
no finished edition in its region, and writes the result beside the row as
`cold`; the page reads it as one more rung, between the cup's own editions and
the scoring table. The raw boards it replays never leave this machine: what
ships is a dozen numbers per cup.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import statistics
import sys

import db
import osirion
from calibration import GAMES_EXPONENT, cold_signature, table_signature

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "src" else HERE
RAW = os.path.join(ROOT, "data", "osirion", "leaderboards")

# The ranks the table carries: the harvest's ladder, plus the cup's own cuts.
RANKS = [1, 3, 5, 10, 20, 25, 50, 100, 120, 250, 500, 1000, 2500, 5000, 10000]
# Donors read, most recent first; a board needs this many rosters loaded to be
# one; the re-ordering is trusted down to this share of the rosters loaded.
DONORS = 6
MIN_ROSTERS = 100
DEPTH_SHARE = 3
# Formats that share a name with a cup and nothing else: a test lobby, a
# ranked cup's sealed scoring, a performance evaluation played in one lobby.
ODD = re.compile(r"arena|evaluation|test cup|ranked cup", re.I)
# The dispersion of the reading, measured across the cups above: the 80th
# percentile of the error at the ranks the table covers.
REL = 0.12
# Pages read from page zero, contiguous; a page past a gap is a cut's page.
PAGE = re.compile(r"_p(\d{3})\.json\.gz$")


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "")[:120]


def platform_of(name: str) -> str:
    """Mobile, console or PC, from the name - the way the cold signature reads it."""
    return cold_signature({"name": name or "", "kind": ""})[3]


def signature(scoring: dict | None, games: int) -> str:
    """A compact spelling of a scoring table and a game count, so the page can
    tell the table a reading was made for from one the form has since changed.
    The model's own spelling, so the two sides never drift."""
    return table_signature(scoring, games)


def rosters_of(event_id: str, window_id: str) -> list[dict]:
    """Every roster on the contiguous pages of a board, with its games.

    Each roster is {"rank", "points", "games": [(placement, elims, end)]}; a
    board with no per-game history on disk comes back empty.
    """
    folder = os.path.join(RAW, slug(event_id))
    if not os.path.isdir(folder):
        return []
    prefix = slug(window_id) + "_p"
    pages = {}
    for name in os.listdir(folder):
        if name.startswith(prefix):
            found = PAGE.search(name)
            if found:
                pages[int(found.group(1))] = os.path.join(folder, name)
    out = []
    number = 0
    while number in pages:
        try:
            with gzip.open(pages[number], "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, EOFError):
            break
        entries = osirion.entries_of(payload)
        if not entries:
            break
        for entry in entries:
            games = []
            for session in osirion._sessions(entry):
                holder = session.get("trackedStats") if isinstance(session.get("trackedStats"), dict) else session
                if osirion.PLACEMENT_STAT not in holder:
                    continue
                games.append((osirion._stat(session, osirion.PLACEMENT_STAT),
                              osirion._stat(session, osirion.ELIMS_STAT),
                              str(session.get("endTime") or "")))
            out.append({"rank": entry.get("rank"), "points": entry.get("pointsEarned"), "games": games})
        number += 1
    return out


def score(games: list[tuple], scoring: dict, cap: int) -> float:
    """What these games are worth under this table: the first `cap` of them in
    the order they were played, each placement and its eliminations priced."""
    played = sorted(games, key=lambda g: g[2])
    if cap:
        played = played[:cap]
    return sum(osirion.points_for(scoring, placement, elims) for placement, elims, _ in played)


def thresholds(rosters: list[dict], scoring: dict, cap: int, ranks=RANKS) -> dict[int, float]:
    """The re-scored standings' value at each rank the table is trusted at."""
    scored = sorted((score(r["games"], scoring, cap) for r in rosters if r["games"]), reverse=True)
    deepest = len(scored) // DEPTH_SHARE
    return {rank: scored[rank - 1] for rank in ranks if rank <= deepest and scored[rank - 1] > 0}


def candidates(conn, region: str, team_mode: str, game_mode: str, platform: str,
               before: str, exclude_id=None) -> list[dict]:
    """Finished open-queue boards of this region, format and platform, newest
    first: what a cup nobody has seen is replayed from."""
    rows = conn.execute(
        "SELECT c.* FROM competition c WHERE c.source = 'osirion' AND c.region = ? "
        "AND c.team_mode = ? AND c.game_mode = ? AND (c.stage IS NULL OR c.stage = '') "
        "AND c.start_time < ? AND c.scoring != '' AND c.event_id != '' "
        "AND EXISTS (SELECT 1 FROM final_result f WHERE f.competition_id = c.id) "
        "ORDER BY c.start_time DESC LIMIT 60",
        (region, team_mode, game_mode, before)).fetchall()
    out = []
    for row in rows:
        comp = dict(row)
        if exclude_id is not None and comp["id"] == exclude_id:
            continue
        name = comp.get("family") or comp.get("name") or ""
        if ODD.search(name) or platform_of(name) != platform:
            continue
        try:
            table = json.loads(comp["scoring"]) if comp.get("scoring") else None
        except ValueError:
            table = None
        if not table or not table.get("placement"):
            continue
        out.append(comp)
    return out


def reading(conn, region: str, team_mode: str, game_mode: str, platform: str,
            scoring: dict, games: int, before: str, exclude_id=None,
            ranks=RANKS) -> dict | None:
    """The re-scored table for a cup of this format: {rank: value}, the donors
    it rests on and the deepest rank covered - or None with fewer than two."""
    cap = int(games or 0)
    pool = candidates(conn, region, team_mode, game_mode, platform, before, exclude_id)
    same = [c for c in pool if int(c.get("max_games") or 0) == cap]
    ordered = same + [c for c in pool if c not in same]
    per_rank: dict[int, list[float]] = {}
    used = []
    for donor in ordered:
        if len(used) >= DONORS:
            break
        rosters = rosters_of(donor["event_id"], donor["window_id"])
        if len(rosters) < MIN_ROSTERS:
            continue
        theirs = int(donor.get("max_games") or 0)
        table = thresholds(rosters, scoring, theirs or None, ranks)
        if not table:
            continue
        factor = (cap / theirs) ** GAMES_EXPONENT if cap and theirs else 1.0
        for rank, value in table.items():
            per_rank.setdefault(rank, []).append(value * factor)
        used.append(donor)
    if len(used) < 2:
        return None
    least = min(len(v) for v in per_rank.values())
    # A rank fewer donors reached is dropped: the median of one board is a board.
    table = {rank: statistics.median(values) for rank, values in sorted(per_rank.items())
             if len(values) >= max(2, len(used) // 2)}
    if not table:
        return None
    return {"ranks": table, "donors": len(used), "deep": max(table),
            "dates": [str(d.get("start_time") or "")[:10] for d in used],
            "names": [str(d.get("family") or d.get("name") or "") for d in used],
            "least": least}


def has_edition(conn, kind: str, region: str, team_mode: str, game_mode: str) -> bool:
    """Has this cup, in this region and format, a finished edition? Then the
    forecast reads it and the replay is not needed."""
    rows = conn.execute(
        "SELECT c.name, c.family, c.stage, c.source, c.round_no FROM competition c WHERE c.region = ? "
        "AND c.team_mode = ? AND c.game_mode = ? "
        "AND EXISTS (SELECT 1 FROM final_result f WHERE f.competition_id = c.id)",
        (region, team_mode, game_mode)).fetchall()
    return any(db.category_of(dict(row)) == kind for row in rows)


def cold_table(conn, row: dict, before: str | None = None) -> dict | None:
    """The `cold` cell of a calendar row: the re-scored table for the week's cup.

    `row` is what `calendar_snapshot.entry` builds - region, team, mode, games,
    scoring, begin, kind, name. Rows with a finished edition in their region,
    a closed lobby, no scoring table or no games get none.
    """
    scoring = row.get("scoring") if isinstance(row.get("scoring"), dict) else None
    games = int(row.get("games") or 0)
    if not scoring or not scoring.get("placement") or games <= 0 or int(row.get("stage") or 0) >= 8:
        return None
    # The odd formats are no donors, and no targets either: an evaluation
    # played in one lobby under a table of its own is not an open queue's
    # boards under another table.
    if ODD.search(row.get("name") or ""):
        return None
    if has_edition(conn, row.get("kind") or "", row.get("region") or "",
                   row.get("team") or "", row.get("mode") or ""):
        return None
    cuts = sorted({int(t[1]) for t in row.get("tiers") or [] if t[0] in ("q", "c", "i") and int(t[1]) >= 1})
    ranks = sorted(set(RANKS) | set(cuts))
    got = reading(conn, row.get("region") or "", row.get("team") or "", row.get("mode") or "",
                  platform_of(row.get("name") or ""), scoring, games,
                  before or str(row.get("begin") or ""), ranks=ranks)
    if not got:
        return None
    return {"ranks": [[int(r), round(v, 1)] for r, v in got["ranks"].items()],
            "donors": got["donors"], "deep": got["deep"], "rel": REL,
            "sig": signature(scoring, games)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", help="an event id, with --window: the table for that window")
    parser.add_argument("--window", help="its window id")
    parser.add_argument("--calendar", action="store_true",
                        help="the tables for every cold row of the week's calendar")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    conn = db.connect()
    if args.event and args.window:
        comp = db.find_by_window(conn, args.event, args.window)
        if not comp:
            print("No such window in the database.", file=sys.stderr)
            return 1
        scoring = json.loads(comp["scoring"]) if comp.get("scoring") else None
        got = reading(conn, comp["region"], comp["team_mode"], comp["game_mode"],
                      platform_of(comp.get("family") or comp["name"]), scoring or {},
                      int(comp.get("max_games") or 0), str(comp["start_time"]), exclude_id=comp["id"])
        if not got:
            print("Fewer than two donor boards on disk.")
            return 1
        finals = db.get_finals(conn, comp["id"])
        print(f"{comp['name']} · {comp['region']} · {got['donors']} donors: " + ", ".join(
            f"{n} ({d})" for n, d in zip(got["names"], got["dates"])))
        for rank, value in got["ranks"].items():
            truth = finals.get(rank)
            gap = f"  actual {truth:.0f}  ({100 * (value / truth - 1):+.1f} %)" if truth else ""
            print(f"  rank {rank:>5}: {value:7.1f}{gap}")
        return 0
    if args.calendar:
        import calendar_snapshot
        rows = calendar_snapshot.collect(args.days)
        for row in rows:
            table = cold_table(conn, row)
            if table:
                print(f"{row['begin']}  {row['region']:<5} {row['name'][:48]:<48} {table['donors']} donors, "
                      + ", ".join(f"r{r} {v:.0f}" for r, v in table["ranks"][:6]))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
