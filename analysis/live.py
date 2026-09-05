"""Replay the harvested boards through time: what a threshold is worth mid-cup.

    python -m analysis.live              # every board with verified scoring
    python -m analysis.live --pages 3    # read deeper than the top 100 rosters
    python -m analysis.live --limit 200  # a quick look

Every leaderboard entry Osirion returns carries its per-game history — each
session's end time, placement and eliminations — and the scoring table has
been verified against the published totals. So the standings at any moment of
the session can be rebuilt exactly, for every tournament in the harvest, and
the question the live page asks can be measured rather than assumed: *at this
point of the session, what share of its final value has the threshold at rank
r reached?*

Two clocks, because the formats keep two kinds of time. A closed lobby plays
its games one after another, everyone in the same match, so the natural clock
is games played: the k-th of n. An open queue has thousands of teams playing
at their own pace, so the clock is the wall clock: elapsed over the window's
length. Both are reported; the page uses games for sealed formats and minutes
for open ones, which is what a player can actually read off the screen.

It also answers the question that decides what a reading is worth: does a
threshold read at one rank say anything about another? Write

    rho_r = observed_r / (share x final_r)

for the multiplicative error the pace curve alone makes at rank r. If a board
runs hot everywhere at once, rho is one number per board and a reading at rank
5 prices rank 20 as well; if the ranks wander independently, carrying the
reading across the ladder adds noise instead of removing it. The regression
slope of log rho_b on log rho_a is exactly how much of a reading to carry, and
it comes out very different for the two formats — see `carry`.

Writes `analysis/pace.json`, which export_model.py carries into model.json as
`pace` — the share curve, its dispersion, how much of a reading carries across
ranks, and how many boards it all rests on.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone

import db
import osirion
import harvest_osirion
from harvest_osirion import slug

PACE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pace.json")
RANKS = (1, 3, 5, 10, 20, 25, 50, 100)
# The ranks the pooled numbers rest on. One page of rosters is the top 100, so
# on a board of two thousand teams the "final" threshold at rank 100 is the
# last row read rather than the last row there is, and at half time hardly any
# of those teams have finished: rank 100 correlates at -3 with everything,
# which is an artefact of the depth read, not a fact about tournaments. Ranks
# 1 to 25 are whole on every board.
POOLED_RANKS = (1, 3, 5, 10, 20, 25)
STEPS = tuple(round(0.1 * i, 1) for i in range(1, 11))     # τ = 0.1 … 1.0
# One lobby: Battle Royale seats 100 players, Reload 40. A board with no more
# rosters than a lobby holds is a closed final; the rest is an open queue.
CLOSED_MAX = 50

# The share a threshold has reached at half the session, used only to turn the
# observed standings into rho for `carry`. Close to 0.5 on both clocks — the
# pace is linear — and the slope `carry` measures is insensitive to it, since
# dividing every rank of every board by the same number moves nothing.
CURVE_AT_HALF = {"open": 0.50, "closed": 0.504}


def when(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def page_file(comp: dict, page: int) -> str:
    return os.path.join(harvest_osirion.RAW, "leaderboards", slug(comp["event_id"]),
                        f"{slug(comp['window_id'])}_p{page:03d}.json.gz")


def sessions_of(comp: dict, pages: int):
    """(team -> [(session id, end time, points)]) for the rosters on disk."""
    scoring = comp["scoring"]
    if isinstance(scoring, str):
        scoring = json.loads(scoring)
    teams, disagree = [], 0
    for page in range(pages):
        path = page_file(comp, page)
        if not os.path.exists(path):
            break
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError, EOFError):
            break
        for entry in osirion.entries_of(payload):
            games = []
            for s in osirion._sessions(entry):
                end = when(s.get("endTime"))
                if not end:
                    continue
                points = osirion.points_for(
                    scoring, osirion._stat(s, osirion.PLACEMENT_STAT, 999),
                    osirion._stat(s, "TEAM_ELIMS_STAT_INDEX", 0))
                games.append((s.get("sessionId") or "", end, points))
            total = sum(p for _, _, p in games)
            if abs(total - float(entry.get("pointsEarned") or 0)) > 0.5:
                disagree += 1
            teams.append(games)
    return teams, disagree


def by_time(comp: dict, teams: list) -> dict:
    """Share of the final threshold at each tenth of the window, by rank."""
    begin = when(comp["start_time"].replace(" ", "T") + "+00:00")
    end = when((comp.get("end_time") or "").replace(" ", "T") + "+00:00")
    if not begin or not end or end <= begin:
        return {}
    span = end - begin
    finals = sorted((sum(p for _, _, p in games) for games in teams), reverse=True)
    out = {}
    for tau in STEPS:
        cut = begin + span * tau
        totals = sorted((sum(p for _, t, p in games if t <= cut) for games in teams), reverse=True)
        for rank in RANKS:
            if rank <= len(finals) and finals[rank - 1] > 0:
                out[(rank, tau)] = totals[rank - 1] / finals[rank - 1]
    return out


def by_game(teams: list) -> dict:
    """Share of the final threshold after game k of n, in a lobby that plays
    the same matches: a session id shared by half the board is one game."""
    end_of, count = {}, collections.Counter()
    for games in teams:
        for sid, end, _ in games:
            end_of[sid] = max(end_of.get(sid, end), end)
            count[sid] += 1
    shared = sorted((sid for sid in end_of if count[sid] >= 0.5 * len(teams)), key=end_of.get)
    if len(shared) < 3:
        return {}
    finals = sorted((sum(p for _, _, p in games) for games in teams), reverse=True)
    out = {}
    for k in range(1, len(shared) + 1):
        played = set(shared[:k])
        totals = sorted((sum(p for sid, _, p in games if sid in played) for games in teams),
                        reverse=True)
        for rank in RANKS:
            if rank <= len(finals) and finals[rank - 1] > 0:
                out[(rank, round(k / len(shared), 2))] = totals[rank - 1] / finals[rank - 1]
    return out


def nearest_step(x: float) -> float:
    return min(STEPS, key=lambda s: abs(s - x))


def summarise(shares: dict) -> dict:
    """{rank: {step: {"median", "p10", "p90", "n"}}} over the boards."""
    table: dict = {}
    for (rank, step), values in shares.items():
        values = sorted(values)
        if len(values) < 3:
            continue
        table.setdefault(str(rank), {})[f"{step:.1f}"] = {
            "median": round(statistics.median(values), 3),
            "p10": round(values[int(0.1 * len(values))], 3),
            "p90": round(values[min(len(values) - 1, int(0.9 * len(values)))], 3),
            "n": len(values)}
    return table


def curve(shares: dict) -> dict:
    """One share per step, pooled over POOLED_RANKS: those ranks agree to a few
    hundredths and the page needs a number, not a table."""
    out = {}
    for step in STEPS:
        pooled = [v for rank in POOLED_RANKS for v in shares.get((rank, step), [])]
        if len(pooled) >= 5:
            out[f"{step:.1f}"] = round(statistics.median(pooled), 3)
    return out


def carry(rows: list, at: float = 0.5) -> dict:
    """How much of a reading at one rank belongs to another, measured.

    `rows` is one dict per board, {rank: rho at that rank}, where

        rho_r = observed_r / (share x final_r)

    is the multiplicative error the pace curve alone makes there. Regress
    log rho_b on log rho_a across boards, pooled over every ordered pair of
    ranks, each rank first centred on its own median so that a curve that is
    slightly off at one rank does not read as agreement. The slope is the
    share of a reading to carry to another rank: 1 means the board moves as
    one and a reading prices the whole ladder, 0 means the ranks wander
    independently and a reading prices only itself.

    Centring each *board* on its own median would destroy the answer — it
    removes exactly the common factor being looked for — and did, in the first
    version of this function, which reported no carry anywhere.
    """
    ranks = [r for r in POOLED_RANKS if sum(1 for row in rows if r in row and row[r] > 0) >= 5]
    if len(ranks) < 2:
        return {}
    middle = {}
    for rank in ranks:
        values = [row[rank] for row in rows if rank in row and row[rank] > 0]
        middle[rank] = statistics.median(values)
    xs, ys = [], []
    for a in ranks:
        for b in ranks:
            if a == b:
                continue
            for row in rows:
                if row.get(a, 0) > 0 and row.get(b, 0) > 0:
                    xs.append(math.log(row[a] / middle[a]))
                    ys.append(math.log(row[b] / middle[b]))
    if len(xs) < 20:
        return {}
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
    sx, sy = statistics.pstdev(xs) or 1e-9, statistics.pstdev(ys) or 1e-9
    return {"slope": round(max(0.0, min(1.0, slope)), 3),
            "correlation": round(slope * sx / sy, 3),
            "boards": len(rows), "pairs": len(xs)}


def dispersion(shares: dict) -> dict:
    """Half-width of the p10–p90 band as a share of the median, by step: the
    pace uncertainty the page quotes. Rank 1 is left out — a winner often owes
    their evening to one exceptional game, the same reason `fit_curve` drops
    it — and so are the ranks one page cannot see whole."""
    out = {}
    for step in STEPS:
        rel = []
        for rank in [r for r in POOLED_RANKS if r != 1]:
            values = sorted(shares.get((rank, step), []))
            if len(values) >= 5:
                med = statistics.median(values)
                if med > 0:
                    lo, hi = values[int(0.1 * len(values))], values[min(len(values) - 1, int(0.9 * len(values)))]
                    rel.append((hi - lo) / 2 / med)
        if rel:
            out[f"{step:.1f}"] = round(statistics.median(rel), 3)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", type=int, default=1, help="pages to read per board (100 rosters each)")
    parser.add_argument("--limit", type=int, help="stop after this many boards")
    parser.add_argument("--since", default="2026-01-01", help="oldest start date to replay")
    args = parser.parse_args()

    with db.session() as conn:
        comps = [dict(r) for r in conn.execute(
            "SELECT id, name, region, start_time, end_time, field_size, max_games, scoring, "
            "event_id, window_id FROM competition WHERE source = 'osirion' AND notes LIKE "
            "'%scoring cumulative%' AND start_time >= ? AND field_size IS NOT NULL "
            "ORDER BY start_time DESC", (args.since,))]
    if args.limit:
        comps = comps[:args.limit]
    print(f"{len(comps)} boards with a verified scoring table since {args.since}")

    time_shares = {"open": collections.defaultdict(list), "closed": collections.defaultdict(list)}
    game_shares = collections.defaultdict(list)
    counted = collections.Counter()
    # One row per board for `carry`, which needs the ranks of a board kept
    # together: pooling them into per-rank lists loses which board each came
    # from, and the pairing is the whole measurement.
    rho_rows = {"open": [], "closed": []}
    HALF = 0.5
    for i, comp in enumerate(comps, 1):
        teams, disagree = sessions_of(comp, args.pages)
        if len(teams) < 15 or disagree > 0.1 * len(teams):
            counted["skipped"] += 1
            continue
        kind = "closed" if (comp["field_size"] or 0) <= CLOSED_MAX else "open"
        timed = by_time(comp, teams)
        for key, share in timed.items():
            time_shares[kind][key].append(share)
        counted[kind] += 1
        played = {}
        if kind == "closed":
            for (rank, k), share in by_game(teams).items():
                game_shares[(rank, nearest_step(k))].append(share)
                if nearest_step(k) == HALF:
                    played[rank] = share
        # rho at half the session, per rank, on the clock that format keeps
        source = played if kind == "closed" else {r: s for (r, tau), s in timed.items() if tau == HALF}
        half = CURVE_AT_HALF.get(kind)
        row = {rank: share / half for rank, share in source.items() if share > 0} if half else {}
        if len(row) >= 2:
            rho_rows[kind].append(row)
        if i % 200 == 0:
            print(f"  ... {i}/{len(comps)}", flush=True)

    print(f"\n{counted['open']} open queues and {counted['closed']} closed lobbies replayed, "
          f"{counted['skipped']} skipped (thin board or scoring disagreement)\n")
    for label, shares in (("open queue, by elapsed time", time_shares["open"]),
                          ("closed lobby, by elapsed time", time_shares["closed"]),
                          ("closed lobby, by games played", game_shares)):
        print(f"{label} — share of the final threshold (median over boards)")
        print("rank  " + "  ".join(f"{s:.1f}" for s in STEPS))
        for rank in RANKS:
            cells = []
            for step in STEPS:
                v = shares.get((rank, step), [])
                cells.append(f"{statistics.median(v):.2f}" if len(v) >= 3 else "  - ")
            print(f"{rank:>4}  " + "  ".join(f"{c:>4}" for c in cells))
        print("  linear:  " + "  ".join(f"{s:.2f}" for s in STEPS) + "\n")

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "note": "share of the final threshold reached at a point of the session; "
                "open queues on the wall clock, closed lobbies on games played",
        "boards": {"open": counted["open"], "closed": counted["closed"]},
        "curve": {"open_by_time": curve(time_shares["open"]),
                  "closed_by_game": curve(game_shares)},
        "carry": {"open_by_time": carry(rho_rows["open"]),
                  "closed_by_game": carry(rho_rows["closed"])},
        "dispersion": {"open_by_time": dispersion(time_shares["open"]),
                       "closed_by_game": dispersion(game_shares)},
        "open_by_time": summarise(time_shares["open"]),
        "closed_by_game": summarise(game_shares),
    }
    for kind in ("open_by_time", "closed_by_game"):
        print(f"{kind}: share  " + "  ".join(f"{k}:{v:.2f}" for k, v in payload["curve"][kind].items()))
        print(f"{' ' * len(kind)}  ±      " + "  ".join(f"{k}:{v:.2f}" for k, v in payload["dispersion"][kind].items()))
        got = payload["carry"][kind]
        if got:
            print(f"{' ' * len(kind)}  carry  {got['slope']:.2f} of a reading belongs to another rank "
                  f"(correlation {got['correlation']:+.2f} over {got['boards']} boards)")
    with open(PACE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    print(f"Wrote {os.path.relpath(PACE_PATH)} — the export carries it into model.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
