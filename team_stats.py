"""Team statistics and rank progression, with no API request.

Everything is computed from the match detail already in the database:

* **Points match by match.** The API returns `points: 0` on a lot of events;
  we recompute them from the inferred scoring table, elimination cap included.
  That is what makes the rest possible.

* **Rank after each match.** A team's rank after its nth match is where it
  would stand if the tournament stopped there: we compare its running total
  against everyone else's at the same moment. When the API gives the time of
  each match, "the same moment" is meant literally; otherwise we compare
  against everyone's nth match, which amounts to the same thing in an open
  where everybody plays inside the same window.

* **Tournament stats.** Elims per game, average and best placement, points per
  game, time alive, consistency — and the same again for the whole field and
  for the top 100, to place the team against them.
"""
from __future__ import annotations

import bisect
import statistics
from collections import defaultdict

import predict


def match_points(scoring: dict, placement, kills) -> float:
    """Points for one match under the scoring table, elim cap applied."""
    pts = predict.placement_points(scoring, int(placement or 0)) if placement else 0.0
    return pts + predict.kill_points(scoring, kills or 0)


# --------------------------------------------------------------------------- #
# Rank progression
# --------------------------------------------------------------------------- #
def _cumulative(rows: list[dict], scoring: dict) -> dict[int, list[tuple]]:
    """Per team: (match number, time, points, running total)."""
    by_team: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        by_team[row["rank"]].append(row)
    out = {}
    for rank, matches in by_team.items():
        matches.sort(key=lambda m: m["match_number"] or 0)
        total, series = 0.0, []
        for m in matches:
            pts = m["points"] if (m["points"] or 0) > 0 \
                else match_points(scoring, m["placement"], m["kills"])
            total += pts
            series.append((m["match_number"], m["started_at"], pts, total))
        out[rank] = series
    return out


def progression(conn, db, comp_id: int, rank: int) -> list[dict]:
    """Where the team stood after each of its matches."""
    comp = db.get_competition(conn, comp_id)
    if comp is None:
        return []
    scoring = comp["scoring"]
    rows = [dict(r) for r in conn.execute(
        "SELECT rank, match_number, started_at, placement, kills, points, time_alive "
        "FROM team_match WHERE competition_id = ?", (comp_id,))]
    if not rows:
        return []

    cum = _cumulative(rows, scoring)
    mine = cum.get(rank)
    if not mine:
        return []

    finals = {r: series[-1][3] for r, series in cum.items() if series}
    detail = {m["match_number"]: m for m in rows if m["rank"] == rank}

    out = []
    for index, (number, started, pts, total) in enumerate(mine, start=1):
        # every other team's total at the same stage: its nth match, or its
        # final total if it played fewer (the tournament "stops" for it).
        others = []
        for other, series in cum.items():
            if other == rank:
                continue
            others.append(series[index - 1][3] if len(series) >= index else finals[other])
        others.sort()
        # rank = 1 + the number of teams strictly ahead
        place = len(others) - bisect.bisect_left(others, total) + 1
        m = detail.get(number, {})
        out.append({
            "match_number": number,
            "started_at": started,
            "placement": m.get("placement"),
            "kills": m.get("kills"),
            "time_alive": m.get("time_alive"),
            "points": round(pts, 1),
            "total": round(total, 1),
            "rank_after": place,
            "moved": None,
        })
    for previous, current in zip(out, out[1:]):
        current["moved"] = previous["rank_after"] - current["rank_after"]
    return out


# --------------------------------------------------------------------------- #
# Tournament statistics
# --------------------------------------------------------------------------- #
def _profile(matches: list[dict], scoring: dict) -> dict:
    if not matches:
        return {}
    places = [m["placement"] for m in matches if m["placement"]]
    kills = [m["kills"] or 0 for m in matches]
    alive = [m["time_alive"] for m in matches if m["time_alive"]]
    points = [m["points"] if (m["points"] or 0) > 0
              else match_points(scoring, m["placement"], m["kills"]) for m in matches]
    return {
        "games": len(matches),
        "kills_per_game": round(statistics.mean(kills), 2) if kills else 0,
        "points_per_game": round(statistics.mean(points), 1) if points else 0,
        "avg_placement": round(statistics.mean(places), 1) if places else None,
        "best_placement": min(places) if places else None,
        "wins": sum(1 for p in places if p == 1),
        "top10": sum(1 for p in places if p <= 10),
        "avg_alive": round(statistics.mean(alive) / 60, 1) if alive else None,
        "regularity": round(statistics.pstdev(points), 1) if len(points) > 1 else 0,
    }


def team_report(conn, db, comp_id: int, rank: int) -> dict:
    """The team, the whole field and the top 100, side by side."""
    comp = db.get_competition(conn, comp_id)
    scoring = comp["scoring"] if comp else {}
    rows = [dict(r) for r in conn.execute(
        "SELECT rank, match_number, placement, kills, points, time_alive "
        "FROM team_match WHERE competition_id = ?", (comp_id,))]
    mine = [m for m in rows if m["rank"] == rank]
    top = [m for m in rows if m["rank"] <= 100]

    # The API sometimes truncates a team's match list. Better to say so than to
    # show a running total that doesn't land on its official score.
    official = conn.execute(
        "SELECT score, games FROM standing WHERE competition_id = ? AND rank = ?",
        (comp_id, rank)).fetchone()
    recomputed = sum(m["points"] if (m["points"] or 0) > 0
                     else match_points(scoring, m["placement"], m["kills"]) for m in mine)
    complete = bool(official) and abs(recomputed - official["score"]) < 0.5 \
        and (official["games"] is None or official["games"] == len(mine))
    return {
        "team": _profile(mine, scoring),
        "field": _profile(rows, scoring),
        "top100": _profile(top, scoring),
        "complete": complete,
        "official_score": official["score"] if official else None,
        "recomputed": round(recomputed, 1),
        "official_games": official["games"] if official else None,
    }
