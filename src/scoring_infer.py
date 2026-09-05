"""Recover a tournament's scoring table from the standings.

Two sources, tried in this order.

**1. Per-game points.** When the API fills them in, every game hands you an
equation `points = placement + elims x value` directly, and a median slope is
enough.

**2. Each team's total.** The common case: the API returns `points: 0` on every
game, but the standings give the total score, and the per-game detail gives
placements and eliminations. Each team is then one equation

    score = sum placement(pi) + elim_value x sum min(elimsi, cap)

With a few thousand teams the system is heavily overdetermined: once the value
of an elimination and the cap are fixed, the placement table falls out in one
least-squares solve. So we try every plausible (value, cap) pair and keep the one
that **reproduces the most scores to the unit**.

The cap is not a detail: Reload cups cap the eliminations that score (often 10
per game). Without it a team on 23 eliminations is credited with twice what it
actually scored, and the whole table goes crooked.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

# Elimination values actually used by Epic and by third-party organisers.
KILL_CANDIDATES = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0)
MAX_PLACEMENT = 120          # past this, a placement never scores anything
MIN_TEAMS = 40               # below this the system is not constrained enough


# --------------------------------------------------------------------------- #
# Source 1: per-game points
# --------------------------------------------------------------------------- #
def collect_matches(rows: list[dict]) -> list[tuple[int, int, float]]:
    """(placement, eliminations, points) for every usable game."""
    out = []
    for row in rows:
        for session in row.get("sessions") or []:
            placement = session.get("placement")
            kills = session.get("kills")
            points = session.get("points")
            if placement is None or kills is None or points is None:
                continue
            try:
                out.append((int(placement), int(kills), float(points)))
            except (TypeError, ValueError):
                continue
    return out


def infer_kill_points(matches) -> float | None:
    """Median slope of points against eliminations, at equal placement."""
    by_placement = defaultdict(list)
    for placement, kills, points in matches:
        by_placement[placement].append((kills, points))

    slopes = []
    for samples in by_placement.values():
        by_kills = defaultdict(list)
        for kills, points in samples:
            by_kills[kills].append(points)
        levels = sorted((k, statistics.median(v)) for k, v in by_kills.items())
        for (k0, p0), (k1, p1) in zip(levels, levels[1:]):
            if k1 > k0:
                slopes.append((p1 - p0) / (k1 - k0))
    if not slopes:
        return None
    value = statistics.median(slopes)
    # scoring tables use simple values: round to the half point
    return round(value * 2) / 2


def infer_placement_points(matches, kill_points: float) -> dict[int, float]:
    """Placement points, once the eliminations' contribution is taken out."""
    by_placement = defaultdict(list)
    for placement, kills, points in matches:
        by_placement[placement].append(points - kill_points * kills)
    out = {}
    for placement, values in by_placement.items():
        value = statistics.median(values)
        out[placement] = round(value * 2) / 2
    return out


# --------------------------------------------------------------------------- #
# Source 2: the standings totals
# --------------------------------------------------------------------------- #
def collect_teams(rows: list[dict]) -> list[tuple[list[int], list[int], float]]:
    """(placements, eliminations, score) for teams whose detail is complete.

    Teams whose recorded games reproduce neither the game count nor the
    elimination total the standings announce are dropped: their equation would be
    wrong, and would corrupt everything else with it.
    """
    teams = []
    for row in rows:
        score = row.get("score")
        sessions = row.get("sessions") or []
        if score is None or not sessions:
            continue
        placements, kills = [], []
        for session in sessions:
            p, k = session.get("placement"), session.get("kills")
            if p is None or k is None:
                placements = []
                break
            placements.append(int(p))
            kills.append(int(k))
        if not placements:
            continue
        games, total_kills = row.get("games"), row.get("kills")
        if games is not None and int(games) != len(placements):
            continue
        if total_kills is not None and int(total_kills) != sum(kills):
            continue
        teams.append((placements, kills, float(score)))
    return teams


def _lu(matrix: list[list[float]]):
    """LU decomposition with partial pivoting, so we factor only once."""
    n = len(matrix)
    a = [row[:] for row in matrix]
    perm = list(range(n))
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-9:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        perm[col], perm[pivot] = perm[pivot], perm[col]
        for r in range(col + 1, n):
            f = a[r][col] / a[col][col]
            a[r][col] = f
            for cx in range(col + 1, n):
                a[r][cx] -= f * a[col][cx]
    return a, perm


def _solve(factors, rhs: list[float]) -> list[float]:
    a, perm = factors
    n = len(a)
    y = [rhs[perm[i]] for i in range(n)]
    for i in range(n):
        for j in range(i):
            y[i] -= a[i][j] * y[j]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        acc = y[i]
        for j in range(i + 1, n):
            acc -= a[i][j] * x[j]
        x[i] = acc / a[i][i]
    return x


def _round_table(values: list[float], step: float = 1.0) -> list[float]:
    """Scoring tables are round numbers; snapping to the grid kills the noise."""
    out = []
    for v in values:
        r = round(v / step) * step
        out.append(0.0 if r < step / 2 else r)
    return out


def infer_from_totals(rows: list[dict]) -> dict:
    """Solve the scoring table from total scores plus the per-game detail."""
    teams = collect_teams(rows)
    if len(teams) < MIN_TEAMS:
        return {"ok": False, "reason":
                f"Too few teams with complete detail ({len(teams)}).",
                "n_teams": len(teams)}

    depth = min(max(max(ps) for ps, _, _ in teams), MAX_PLACEMENT)
    index = {p: i for i, p in enumerate(range(1, depth + 1))}

    # Sparse "how many times each placement" vector for every team.
    vectors = []
    for placements, kills, score in teams:
        counts = defaultdict(int)
        for p in placements:
            if p in index:
                counts[index[p]] += 1
        vectors.append((sorted(counts.items()), kills, score))

    # The normal matrix depends on the placements alone, so one factorization
    # serves every (elim value, cap) pair tried below.
    n = len(index)
    normal = [[0.0] * n for _ in range(n)]
    for counts, _, _ in vectors:
        for i, ci in counts:
            row = normal[i]
            for j, cj in counts:
                row[j] += ci * cj
    factors = _lu(normal)
    if factors is None:
        return {"ok": False, "reason": "Placements too uniform to conclude anything.",
                "n_teams": len(teams)}

    biggest = max(max(ks) for _, ks, _ in vectors)
    caps = [None] + [c for c in range(3, min(biggest, 30) + 1)]

    best = None
    for kill in KILL_CANDIDATES:
        for cap in caps:
            rhs = [0.0] * n
            for counts, kills, score in vectors:
                target = score - kill * sum(min(k, cap) if cap else k for k in kills)
                for i, ci in counts:
                    rhs[i] += ci * target
            table = _round_table(_solve(factors, rhs))
            exact = 0
            for counts, kills, score in vectors:
                got = sum(ci * table[i] for i, ci in counts) \
                    + kill * sum(min(k, cap) if cap else k for k in kills)
                if abs(got - score) < 0.5:
                    exact += 1
            if best is None or exact > best[0]:
                best = (exact, kill, cap, table)
            if exact == len(vectors):
                break
        if best and best[0] == len(vectors):
            break

    exact, kill, cap, table = best
    if exact < 0.5 * len(vectors):
        return {"ok": False, "n_teams": len(teams), "accuracy": round(100 * exact / len(vectors), 1),
                "reason": "No simple scoring table reproduces the scores "
                          f"(at best {round(100 * exact / len(vectors))} % of teams)."}

    points = {p: table[i] for p, i in index.items()}
    return {"ok": True, "kill": kill, "kill_cap": cap, "points": points,
            "n_teams": len(teams), "accuracy": round(100 * exact / len(vectors), 1),
            "source": "totals"}


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def fill_gaps(points_by_placement: dict[int, float]) -> dict[int, float]:
    """Fill in ranks never observed when their neighbours are worth the same.

    On a small standings nobody may have finished 13th or 14th; if 12th and 15th
    pay the same, the tier necessarily covers them.
    """
    ranks = sorted(points_by_placement)
    out = dict(points_by_placement)
    for low, high in zip(ranks, ranks[1:]):
        if high - low > 1 and points_by_placement[low] == points_by_placement[high]:
            for rank in range(low + 1, high):
                out[rank] = points_by_placement[low]
    return out


def compress(points_by_placement: dict[int, float]) -> list[list]:
    """{1:60, 2:54, 3:54} -> [[1,1,60],[2,3,54]]: tiers, not one row per rank."""
    table = []
    for placement in sorted(points_by_placement):
        value = points_by_placement[placement]
        if table and table[-1][2] == value and placement == table[-1][1] + 1:
            table[-1][1] = placement
        else:
            table.append([placement, placement, value])
    return [row for row in table if row[2] > 0]


def infer(rows: list[dict]) -> dict:
    """Inferred scoring table, with enough context to judge how much to trust it."""
    matches = collect_matches(rows)
    scored = [m for m in matches if m[2] > 0]

    # Path 1: per-game points, when the API actually fills them in.
    if len(scored) >= 30:
        kill_points = infer_kill_points(scored)
        if kill_points is not None:
            placement = fill_gaps(infer_placement_points(scored, kill_points))
            table = compress(placement)
            if table:
                lookup = {r: v for low, high, v in table for r in range(low, high + 1)}
                errors = [abs(lookup.get(p, 0) + kill_points * k - pts) for p, k, pts in scored]
                exact = sum(1 for e in errors if e < 0.01) / len(errors)
                if exact >= 0.5:
                    return _package(table, kill_points, None, len(matches), len(scored),
                                    round(100 * exact, 1), "games")

    # Path 2: the standings totals.
    solved = infer_from_totals(rows)
    if not solved.get("ok"):
        reason = solved.get("reason") or "Scoring table not found."
        if matches and not scored:
            reason += " (the API gives no per-game points)"
        return {"ok": False, "reason": reason, "n_matches": len(matches),
                "n_teams": solved.get("n_teams", 0)}

    table = compress(fill_gaps(solved["points"]))
    if not table:
        return {"ok": False, "reason": "No placement tier recovered.",
                "n_matches": len(matches)}
    return _package(table, solved["kill"], solved["kill_cap"], len(matches),
                    solved["n_teams"], solved["accuracy"], "totals")


def _package(table, kill, kill_cap, n_matches, n_sample, accuracy, source) -> dict:
    return {
        "ok": True,
        "kill": kill,
        "kill_cap": kill_cap,
        "placement": table,
        "n_matches": n_matches,
        "n_sample": n_sample,
        "accuracy": accuracy,
        "source": source,
        "best_placement_points": max((row[2] for row in table), default=0),
        "deepest_scoring_rank": max((row[1] for row in table), default=0),
    }
