"""The model: what a rank will cost, before the tournament starts.

A cascade, most direct reading first. `prior_prediction` walks it and stops at
the first rung that can answer:

1. **The previous edition, read straight** (`direct_tables`). Same cup, same
   region, same rank, last time — with a band measured from how much that rank
   moved between consecutive editions. First because it kept winning: a strong
   evening lifts every rank together, and a number read whole keeps that where a
   level times a ratio loses it.

2. **The cup's level times its measured shape** (`reference_pace`,
   `shape_tables`). The rank-20 threshold from the previous edition, times what
   each rank was worth relative to rank 20 across the cup's editions — a lookup,
   not a curve. Category first, then the family across regions.

3. **The level times the fitted curve** (`fit_curve`, `shape_ratio`), for ranks
   no edition has measured:

       threshold(rank) = level * exp(-a * (q**b - q_ref**b)),   q = rank / field

4. **The scoring table alone** (`anchor_level`, the scoring branch), for a cup
   nobody has seen: a share of the most a team could score, read from cups of
   the same kind, platform and stage, shrunk toward `REFERENCE_SHARE`.

Everything the cascade reads comes from `broad_stats`, computed once over every
finished tournament and cached in the database. The live models in `predict.py`
also draw their prior weights from here (`model_errors`).

Measured as a forecast — the newest 600 tournaments from the 6,632 before them:
5.7 % median error where the cup has run before, 19 % where it has not, and a
band that claims 80 % and covers 82 %. `analysis/validate.py` prints all of it;
`docs/methodology.md` says what it means.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime

import predict

CUTS = (0.25, 0.5, 0.75)
DEFAULT_EXPONENT = 0.93

# Shape of the threshold curve, measured on 30 finished Epic tournaments:
#
#     threshold(rank) = level x exp(-a x (q^b - q_ref^b))     q = rank / field
#
# The "level" is the value at the reference rank. Why 20 and not 1: a tournament
# winner often owes their score to one exceptional game, which makes for an
# unstable point to lean on. In a typical category the pace moves 7.7 % from one
# edition to the next at rank 1 and 7.2 % at rank 5, but only 3.4 % at rank 20;
# by rank 50 it is back up to 5.0 %. Cross-validation follows the same shape:
# 9.2 % error anchoring on the top 1 against 6.3 to 6.7 % between the top 10 and
# the top 25. Rank 1 is one team having a good night; rank 20 is a population.
#
# Reading four ranks (5/10/20/25) and taking the median gives 6.4 %. The margin
# over any single rank between 10 and 25 is inside the noise — only rank 1 is
# beaten outright. analysis/anchor.py measures all of this; the numbers rest on
# 26 editions across 10 categories, so treat the ordering as the result and the
# levels as indicative.
REFERENCE_RANK = 20
REFERENCE_ANCHORS = (5, 10, 20, 25)
CURVE_DEFAULT = (1.255, 0.370)

# Level reached at the reference rank, as a share of the theoretical maximum for
# one game (win + elimination cap) times the number of games. It is the bridge
# used when only the scoring table is known — the case of a format never played
# before.
#
# This is the weak link in the model, and it should be said plainly: the constant
# rests on no solid measurement.
#
# First value tried: 0.43, taken from the Reload Elite Series ASIA on the
# assumption that it shared the Victory Cups' scoring table. That assumption was
# wrong — against three real Reload Duos Victory Cup thresholds (414 and 380 in
# ASIA, 414 in ME at the top 120) it underestimated by 25 to 30 %. Those three
# readings put the true value between 0.57 and 0.61 depending on field size.
#
# We settle on 0.58, knowing it is a prior drawn from three points and a single
# format. Hence the +/-40 % range: when an estimate rests on this, what it mostly
# says is that it doesn't know. As soon as a comparable tournament enters the
# history — imported, tracked to the end, or entered by hand — the measurement
# replaces this constant and the range closes up.
REFERENCE_SHARE = 0.58

# Uncertainty specific to each anchor source. The raw cross-validated figures give
# a range too narrow to be honest; widened, they contain the true threshold about
# 80 % of the time.
ANCHOR_SPREAD = {"category": 0.19, "family": 0.21, "mode": 0.24, "scoring": 0.40}

# The exponent grid `fit_curve` searches. Below 0.2 the curve is flat enough to
# be indistinguishable from a constant; above 1.2 it is steeper than any observed
# leaderboard. Landing on either edge means the fit never bracketed its optimum,
# which `LAST_FIT` records so callers can say so out loud.
B_RANGE = (0.20, 1.20)
LAST_FIT: dict = {"railed": None}

# Most editions any one comparable circle keeps, newest first. A cup that has run
# three hundred times does not need three hundred loaded from disk to produce a
# median, and the widest circle — a whole region and mode — holds thousands once
# the API harvest has run. See `comparable_history`.
CIRCLE_CAP = 40

# The measured shape table. `SHAPE_MIN` editions before a rank is trusted to
# speak for itself; below that the curve answers. `SHAPE_FALLBACK_REL` is the
# curve's own shape uncertainty, which a thin table is blended toward, and
# `SHAPE_FLOOR` stops three editions that happened to agree from claiming they
# know a rank to within nothing.
SHAPE_MIN = 3
SHAPE_FALLBACK_REL = 0.06

# One lobby holds this many teams: a final played by qualified teams in a single
# lobby cannot rank more than this, whatever the sign-up count says. Reload and
# Blitz lobbies seat forty players, not a hundred. The predictor carries the
# same two tables.
LOBBY_CAP = {"Solo": 100, "Duo": 50, "Trio": 33, "Squad": 25}
LOBBY_CAP_SMALL = {"Solo": 40, "Duo": 20, "Trio": 13, "Squad": 10}
# A closed lobby's ladder is read by share of the lobby, in these buckets of
# q = rank / field (upper edges). The finals in the training set are 20 to
# 100 teams and keep ranks 1, 3, 5, 10, 20, 25, 50 and 100, so a rank-by-rank
# table would be mostly holes; by share, rank 1 of 20 and rank 5 of 100 are the
# same question and land in the same bucket.
LOBBY_BUCKETS = (0.03, 0.06, 0.12, 0.25, 0.4, 0.55, 0.7, 0.9)
LOBBY_LAST = 0.9                   # beyond this share, the last places: teams that left
LOBBY_MIN = 3                      # finals a bucket needs before it is believed
LOBBY_REL_FLOOR = 0.25             # never narrower than this: a cup never seen differs
# The API pages a board a hundred pages deep at most, so a harvested field of
# this size or more says "ten thousand or more", not a count. A field at the
# cap is unknown for the purpose of comparing two editions' fields.
FIELD_CAP = 9900
# When the previous edition admitted a different ranked tier - Unreal alone
# one week, Diamond upwards the next - and no edition with the same bar exists
# to read instead, its band widens by this much: the field is another size.
WIDEN_ENTRY = 1.5
# How far along the curve a known field is allowed to move the previous
# edition's value, in log terms: a fifth either way, about +22 % / -18 %.
# Measured on the newest 600 tournaments read from the 6,632 before them, the
# full move cuts the error at these rows from 6.3 % to 5.0 %, and capped here
# to 4.7 %: beyond a fifth the curve extrapolates further than the data goes.
# The move's own uncertainty - the fit puts the true slope anywhere from half
# the move to all of it - is added to the band as this share of the move.
FIELD_MOVE_CAP = 0.2
FIELD_MOVE_REL = 0.3

LOBBY_THIN = 5                     # below this many finals the band widens
LOBBY_WIDEN = {"team": 1.0, "mode": 1.15, "any": 1.3, "thin": 1.3}
SHAPE_FLOOR = 0.02

# Which scopes a measured shape may be read from, by where the level was
# anchored. The mode is deliberately absent. A ratio at rank 1000 depends on how
# many teams there are, and a mode groups a five-hundred-team cup with a ten-
# thousand-team one; its median ratio describes neither. Measured, that scope
# doubled the error past rank 500 while every other band improved. The curve
# knows about field size and takes over there, which is what it is for.
SHAPE_SCOPES = {"category": ("category", "family"), "family": ("family",)}

# Deepest rank a measured table is allowed to answer. Past it the table lost to
# the curve — 28.4 % against 17.3 % beyond rank 500 — for two reasons that
# compound: few editions are harvested deep enough to reach those ranks, and the
# ratio there depends heavily on the field size, which a table does not carry and
# the curve does. So the curve takes the deep end, which is what it is for.
SHAPE_MAX_RANK = 500

# How many editions of a cup speak for its current level. See `_recent`.
RECENT_EDITIONS = 1

# How a threshold moves when the number of games moves. Not 1.0: a session twice
# as long does not double what twentieth place scores, because the field improves
# with it. `DEFAULT_EXPONENT` is the same number the per-game live model uses.
GAMES_EXPONENT = DEFAULT_EXPONENT


# --------------------------------------------------------------------------- #
# Scale of a scoring table
# --------------------------------------------------------------------------- #
def scoring_scale(scoring: dict) -> float:
    """Value of one "reference game" under this scoring table.

    A top 10 plus two eliminations: a good game without being exceptional. The
    exact choice doesn't matter as long as it is *proportional* to the scoring
    table — that proportionality is what makes two different tables comparable.
    """
    placement = predict.placement_points(scoring, 10)
    if placement <= 0:                       # short table (20-team final)
        placement = predict.placement_points(scoring, 5)
    if placement <= 0:
        _, placement = predict.best_placement(scoring)
    return max(placement + 2 * float((scoring or {}).get("kill") or 0), 1e-6)


def session_scale(comp: dict) -> float:
    """Points a reference player would score over the whole session."""
    return scoring_scale(comp.get("scoring") or {}) * float(comp.get("max_games") or 10)


# --------------------------------------------------------------------------- #
# Measurements on a finished competition
# --------------------------------------------------------------------------- #
def measured_exponent(comp: dict, rank: int) -> float | None:
    """Exponent b such that threshold ~ a x games^b, measured on the readings."""
    gm = predict.games_map(comp, rank)
    ps, ys = predict.progress_series(comp, rank)
    if not ps or len(ps) < 3:
        return None
    pairs = []
    for p, y in zip(ps, ys):
        g = gm.get(round(p, 6)) or predict.estimated_games(comp, p)
        if g and g > 0 and y > 0:
            pairs.append((float(g), y))
    final = predict.final_value(comp, rank)
    max_games = float(comp.get("max_games") or 0)
    if final and max_games:
        pairs.append((max_games, final))     # the finishing point carries double the information
    if len(pairs) < 3:
        return None
    fit = predict.linreg([math.log(g) for g, _ in pairs], [math.log(y) for _, y in pairs])
    if not fit:
        return None
    return max(0.5, min(1.3, fit[0]))


def measured_ratio(comp: dict, rank: int) -> float | None:
    """Final threshold relative to what a reference player would score.

    With no known scoring table there is nothing to relate it to: an imported
    tournament whose points system we don't know would be measured against the
    generic table, and would skew the calibration instead of enriching it. It
    still serves elsewhere — for the shape of the curve — but not here.
    """
    if comp.get("scoring_known") is False:
        return None
    final = predict.final_value(comp, rank)
    scale = session_scale(comp)
    if not final or scale <= 0:
        return None
    return final / scale


def model_errors(comp: dict, history: list[dict]) -> dict:
    """Each model's error on this competition, at several cut-off points."""
    tl = predict.timeline(comp)
    if not tl["total_min"]:
        return {}
    out: dict[str, list[float]] = {}
    for cut in CUTS:
        partial = dict(comp)
        partial["snapshots"] = [
            s for s in comp.get("snapshots", [])
            if (datetime.fromisoformat(s["ts"]) - tl["start"]).total_seconds() / 60
            / tl["total_min"] <= cut
        ]
        partial["finals"] = {}
        if len(partial["snapshots"]) < 1:
            continue
        for rank in comp["ranks"]:
            truth = predict.final_value(comp, rank)
            if not truth or truth <= 0:
                continue
            ps, ys = predict.progress_series(partial, rank)
            if not ps:
                continue
            models = predict.available_models(
                partial, rank, shape=predict.build_shape(history, rank))
            for key, model in models.items():
                value = model(ps, ys, 1.0)
                if value is None:
                    continue
                out.setdefault(key, []).append(abs(max(value, ys[-1]) - truth) / truth)
    return out


# --------------------------------------------------------------------------- #
# Full calibration
# --------------------------------------------------------------------------- #
def _shape_rows(seen: dict) -> dict:
    out = {}
    for rank, values in seen.items():
        if len(values) < SHAPE_MIN:
            continue
        mid = statistics.median(values)
        if mid <= 0:
            continue
        devs = sorted(abs(v / mid - 1) for v in values)
        rel = devs[min(len(devs) - 1, int(0.8 * len(devs)))]
        out[str(rank)] = {"median": round(mid, 4), "n": len(values),
                          "rel": round(max(rel, SHAPE_FLOOR), 4)}
    return out


def broad_for(base: dict, peers: list[dict], comp: dict, curve=None) -> dict:
    """`base`, with everything `comp` looks up about its own family taken from `peers`.

    `analysis.validate.learning_curve` thins a tournament's comparable editions to
    measure what each extra one is worth. Only that tournament's own category and
    family entries may change when they are thinned — every other group in `base`
    is untouched by it — so rebuilding the whole wide sample per draw is a day of
    work for two dictionary entries.

    The mode-level and scoring-level fallbacks keep `base`'s numbers, which still
    see the category's other editions. That is a small leak, and it lives in a
    diagnostic rather than in the model: it makes the flat part of the learning
    curve slightly optimistic and does not touch a forecast.
    """
    part = broad_stats(peers, curve=curve or base.get("curve"))
    names = {"category": str((comp.get("kind"), comp.get("region"))),
             "family": str(comp.get("kind"))}
    out = dict(base)
    for block in ("reference_pace", "field_sizes", "shape"):
        into = dict(base.get(block) or {})
        for scope, name in names.items():
            found = ((part.get(block) or {}).get(scope) or {}).get(name)
            into[scope] = {name: found} if found else {}
        out[block] = into
    return out


def shape_tables(history: list[dict]) -> dict:
    """`measured_shape`, grouped the three ways the level anchor is grouped.

    Deliberately the same keys as `reference_pace`, so the shape can be read at
    whichever scope the level was read at. A forecast that took its level from
    the family and its shape from the category would be describing two different
    populations and calling the result one tournament.
    """
    out: dict = {}
    for key, extract in (("category", lambda c: (c.get("kind"), c.get("region"))),
                         ("family", lambda c: c.get("kind")),
                         ("mode", lambda c: (c.get("game_mode"), c.get("team_mode")))):
        groups: dict = {}
        for comp in history:
            base = predict.final_value(comp, REFERENCE_RANK)
            if not base or base <= 0:
                continue
            bucket = groups.setdefault(str(extract(comp)), {})
            for rank in comp.get("ranks") or ():
                rank = int(rank)
                if rank == REFERENCE_RANK:
                    continue
                value = predict.final_value(comp, rank)
                if value and value > 0:
                    bucket.setdefault(rank, []).append(value / base)
        out[key] = {name: rows for name, seen in groups.items()
                    if (rows := _shape_rows(seen))}
    return out


def bracket(table: dict, rank: int, usable) -> tuple | None:
    """The measured ranks either side of `rank`, or None if it is not inside them.

    The harvest keeps a threshold at ranks 1, 3, 5, 10, 20, 25, 50, 100 and so
    on, so a table never has rank 8. Asked for it, the cascade used to fall
    through to the curve — which, in a twenty-team lobby where rank 8 is
    two-fifths of the way down, said more than rank 5 did. Between two
    measured ranks the ladder is read off the measured ranks: log-linear in
    the rank, which is the interpolation the curve itself is a straight line
    under. Beyond the deepest measured rank the caller falls through as before.
    """
    rank = int(rank)
    lo = hi = None
    for key, entry in (table or {}).items():
        try:
            at = int(key)
        except (TypeError, ValueError):
            continue
        if not usable(entry):
            continue
        if at < rank and (lo is None or at > lo):
            lo = at
        elif at > rank and (hi is None or at < hi):
            hi = at
    if lo is None or hi is None:
        return None
    f = (math.log(rank) - math.log(lo)) / (math.log(hi) - math.log(lo))
    return lo, hi, f


def interpolated(v_lo: float, v_hi: float, f: float) -> float:
    return math.exp((1 - f) * math.log(v_lo) + f * math.log(v_hi))


def shape_from(comp: dict, calib: dict, rank: int, source: str) -> tuple[float, float] | None:
    """(ratio, relative uncertainty) at this rank, or None to fall back to the curve.

    Read at the scope the level came from, then wider. A level anchored on the
    scoring table has no scope of its own, so it starts at the mode.

    Three editions is not enough to trust a measured dispersion on its own, so
    the measured spread and the curve's are blended in variance, the measurement
    winning as the sample grows. A rank the table does not hold is read between
    the two it does, when it lies between them — see `bracket`.
    """
    if int(rank) > SHAPE_MAX_RANK:
        return None
    tables = (calib or {}).get("shape") or {}
    order = SHAPE_SCOPES.get(source, ())
    names = {"category": str((comp.get("kind"), comp.get("region"))),
             "family": str(comp.get("kind")),
             "mode": str((comp.get("game_mode"), comp.get("team_mode")))}

    def usable(entry):
        return bool(entry) and entry.get("n", 0) >= SHAPE_MIN and bool(entry.get("median"))

    def read(entry):
        n = entry["n"]
        weight = n / (n + 4)
        rel = math.sqrt(weight * entry["rel"] ** 2 + (1 - weight) * SHAPE_FALLBACK_REL ** 2)
        return float(entry["median"]), rel

    for scope in order:
        table = (tables.get(scope) or {}).get(names[scope]) or {}
        entry = table.get(str(int(rank)))
        if usable(entry):
            return read(entry)
        found = bracket(table, rank, usable)
        if found:
            lo, hi, f = found
            (r_lo, rel_lo), (r_hi, rel_hi) = read(table[str(lo)]), read(table[str(hi)])
            return interpolated(r_lo, r_hi, f), max(rel_lo, rel_hi)
    return None


def counted_field(comp: dict) -> int:
    """The field as a count, or 0 when it is the API's ceiling rather than a count.

    A harvested board is paged a hundred pages deep at most, so a harvested
    field of FIELD_CAP or more says "ten thousand or more". A field typed in,
    or read off a qualification cut, is a count whatever its size.
    """
    field = int(comp.get("field_size") or 0)
    if field <= 0:
        return 0
    if str(comp.get("source") or "") == "osirion" and field >= FIELD_CAP:
        return 0
    return field


def direct_tables(history: list[dict]) -> dict:
    """What the previous edition of each cup scored at each rank, read straight.

    This is the carry-forward baseline, made the first rung of the model — and
    it is there because it kept winning. The level-times-shape decomposition
    has a defect no amount of tuning removes: a strong evening lifts rank 1 and
    rank 20 together, and a forecast that multiplies one edition's level by a
    ratio taken from other editions throws that correlation away. Two errors
    compose where a direct reading carries one. Measured on the newest six
    hundred tournaments, forecast chronologically: 13.2 % against 5.5 % in the
    top five, 9.4 % against 5.7 % overall.

    Category scope only — the same cup in the same region. A reading from
    another region is not a previous edition, it is a different population.

    `rel` is the 80th percentile of the relative move between consecutive
    editions at that rank: the honest width of "next time will be like last
    time", measured on this cup rather than assumed.
    """
    by_name: dict = {}
    for comp in history:
        name = str((comp.get("kind"), comp.get("region")))
        entry = str(comp.get("entry") or "")
        field = counted_field(comp)
        for rank in comp.get("ranks") or ():
            value = predict.final_value(comp, int(rank))
            if value and value > 0:
                by_name.setdefault(name, {}).setdefault(int(rank), []).append(
                    (str(comp.get("start_time") or ""), value, entry, field))
    out: dict = {}
    for name, ranks in by_name.items():
        table = {}
        for rank, dated in ranks.items():
            dated.sort()
            values = [v for _, v, _, _ in dated]
            moves = sorted(abs(b / a - 1) for a, b in zip(values, values[1:]) if a > 0)
            rel = moves[min(len(moves) - 1, int(0.8 * len(moves)))] if moves else None
            latest = dated[-1]
            row = {"value": round(values[-1], 2), "n": len(values),
                   "rel": round(max(rel, SHAPE_FLOOR), 4) if rel is not None else None,
                   "field": latest[3] or 0, "entry": latest[2], "date": latest[0][:10]}
            # The last edition under each other entry bar: what to read when
            # the cup coming up asks for that bar rather than the latest one.
            alt = {}
            for date, value, entry, field in dated:
                if entry and entry != latest[2]:
                    alt[entry] = [round(value, 2), field or 0, date[:10]]
            if alt:
                row["alt"] = alt
            table[str(rank)] = row
        out[name] = table
    return out


def direct_from(comp: dict, calib: dict, rank: int) -> dict | None:
    """The previous edition, read straight: {value, rel, n, note, field_effect}, or None.

    Which edition is "previous" depends on who was let in. The same cup one
    week admitting Unreal alone and the next Diamond upwards is two fields of
    different sizes, and a threshold at rank 4,000 follows the field: so when
    the cup coming up states its entry bar, the last edition with the *same*
    bar is read, and when none exists the latest edition is read with its
    band widened. Then the field: when both the cup's field and the edition's
    are counts - typed in, or a later round's cut, never a harvested board's
    ten-thousand ceiling, see `counted_field` - the value moves along the
    curve from the edition's share of its field to this cup's share of its
    own, and `field_effect` says by how much it did.

    A rank the edition did not publish is read between the two it did, when it
    lies between them — see `bracket`; deeper than its deepest rank, None.
    """
    table = ((calib or {}).get("direct") or {}).get(str((comp.get("kind"), comp.get("region"))))
    want = str(comp.get("entry") or "")
    field_now = counted_field(comp)
    curve = (calib or {}).get("curve") or CURVE_DEFAULT

    def usable(entry):
        return bool(entry) and bool(entry.get("value"))

    def read(entry):
        n = entry["n"]
        pairs = max(n - 1, 0)
        measured = entry.get("rel")
        fallback = ANCHOR_SPREAD["category"]
        if measured is None:
            rel = fallback * 1.5                        # one edition: widened, as elsewhere
        else:
            weight = pairs / (pairs + 2)
            rel = math.sqrt(weight * measured ** 2 + (1 - weight) * fallback ** 2)
        value, field, note = float(entry["value"]), int(entry.get("field") or 0), None
        theirs = str(entry.get("entry") or "")
        if want and theirs and want != theirs:
            alt = (entry.get("alt") or {}).get(want)
            if alt:
                value, field = float(alt[0]), int(alt[1] or 0)
                note = ("same_entry",)
            else:
                rel *= WIDEN_ENTRY
                note = ("other_entry", theirs)
        effect = 0.0
        if field_now > 0 and field > 0 and field != field_now:
            move = field_move(rank, field_now, field, curve)
            value *= math.exp(move)
            rel = math.sqrt(rel ** 2 + (FIELD_MOVE_REL * move) ** 2)
            effect = 100 * (math.exp(move) - 1)
        return value, rel, n, note, effect

    entry = (table or {}).get(str(int(rank)))
    if usable(entry):
        value, rel, n, note, effect = read(entry)
        return {"value": value, "rel": rel, "n": n, "note": note, "field_effect": effect}
    found = bracket(table, rank, usable)
    if not found:
        return None
    lo, hi, f = found
    (v_lo, rel_lo, n_lo, note, e_lo), (v_hi, rel_hi, n_hi, _, e_hi) = read(table[str(lo)]), read(table[str(hi)])
    return {"value": interpolated(v_lo, v_hi, f), "rel": max(rel_lo, rel_hi), "n": min(n_lo, n_hi),
            "note": note, "field_effect": (1 - f) * e_lo + f * e_hi}


def broad_stats(wide: list[dict], curve: tuple | list | None = None) -> dict:
    """The part of a calibration that depends on the wide sample and nothing else.

    Split out because it is the expensive part and because it is the *same* part
    for every tournament being calibrated. `fit_curve` sweeps the exponent over
    every threshold in the sample: at sixty-six tournaments that is milliseconds,
    at ten thousand it is the better part of a minute, and anything that calls
    `calibrate` in a loop pays it once per iteration for an answer that does not
    change. Compute it once, hand it back.

    `wide` is filtered here, so callers pass whatever they have. `curve` skips
    the fit itself, for callers that vary the sample by a handful of tournaments
    and know the shape cannot have moved — `analysis.validate.learning_curve`
    resamples the level tables eighty times per tournament and the exponent
    sweep is the only part that would not survive it.
    """
    done = [c for c in wide if predict.is_complete(c)]
    curve = tuple(curve) if curve else fit_curve(done)
    return {"curve": list(curve), "reference_pace": reference_pace(done, curve),
            "field_sizes": field_sizes(done), "shape": shape_tables(done),
            "direct": direct_tables(done), "n_curve": len(done)}


def calibrate(history: list[dict], ranks: list[int], wide: list[dict] | None = None,
              broad: dict | None = None) -> dict:
    """Aggregate the measurements over every comparable finished competition.

    `history` is the narrow circle — the genuinely comparable tournaments, which
    feed the ratios and the model errors. `wide` is everything we know: the shape
    of the curve depends on neither the scoring table nor the region, so it has
    everything to gain from being fitted on the lot.

    `broad` short-circuits the wide half with an already-computed `broad_stats`.
    Callers that calibrate many circles against one database pass it; leaving it
    None reproduces the old behaviour exactly.
    """
    done = [c for c in history if predict.is_complete(c)]
    if broad is None:
        broad = broad_stats([c for c in (wide or history) if predict.is_complete(c)] or done)
    curve = tuple(broad["curve"])
    exps: dict[int, list[float]] = {}
    ratios: dict[int, list[float]] = {}
    errors: dict[str, list[float]] = {}

    for comp in done:
        others = [c for c in done if c["id"] != comp["id"]]
        for key, errs in model_errors(comp, others).items():
            errors.setdefault(key, []).extend(errs)
        for rank in set(ranks) | set(comp.get("ranks", [])):
            e = measured_exponent(comp, rank)
            if e is not None:
                exps.setdefault(rank, []).append(e)
            r = measured_ratio(comp, rank)
            if r is not None:
                ratios.setdefault(rank, []).append(r)

    def agg(values):
        return {"median": round(statistics.median(values), 4), "n": len(values),
                "spread": round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0}

    return {
        "n_comps": len(done),
        "shape": broad["shape"],
        "direct": broad.get("direct") or {},
        "curve": list(curve),
        "reference_pace": broad["reference_pace"],
        "field_sizes": broad["field_sizes"],
        "n_curve": broad["n_curve"],
        "exponent": {str(r): agg(v) for r, v in exps.items() if v},
        "ratio": {str(r): agg(v) for r, v in ratios.items() if v},
        "model_error_pct": {k: round(100 * sum(v) / len(v), 2) for k, v in errors.items() if v},
        "computed_from": [c["name"] for c in done][:20],
    }


# --------------------------------------------------------------------------- #
# A rank's threshold: one universal shape, one scale to anchor
# --------------------------------------------------------------------------- #
def fit_curve(history: list[dict]) -> tuple[float, float]:
    """Fit (a, b) by comparing ranks *within* a tournament.

    Each tournament contributes its internal gaps, never its absolute level: the
    shape therefore fits without being dragged around by the highest-scoring
    tournaments. Rank 1 is left out — it is precisely the one that adds the noise.
    """
    LAST_FIT["railed"] = None
    data = []
    for comp in history:
        field = comp.get("field_size")
        finals = comp.get("finals") or {}
        base = finals.get(REFERENCE_RANK)
        if not field or not base:
            continue
        qref = REFERENCE_RANK / field
        for rank, value in finals.items():
            rank = int(rank)
            if rank in (1, REFERENCE_RANK) or not value or value <= 0:
                continue
            data.append((rank / field, qref, math.log(value / base)))
    if len(data) < 12:
        return CURVE_DEFAULT
    best = None
    # The sweep runs over exactly the range the check below accepts. It used to
    # stop at 0.90 while accepting up to 1.20, so a sample whose best exponent
    # lay above 0.90 was quietly handed the edge of the grid and no one was told
    # the optimum had never been bracketed.
    lo, hi = int(B_RANGE[0] * 100), int(B_RANGE[1] * 100)
    for step in range(lo, hi + 1):                 # b, swept by hundredths
        b = step / 100
        den = sum((qr ** b - q ** b) ** 2 for q, qr, _ in data)
        if den <= 0:
            continue
        a = sum((qr ** b - q ** b) * y for q, qr, y in data) / den
        err = sum((y - a * (qr ** b - q ** b)) ** 2 for q, qr, y in data)
        if best is None or err < best[0]:
            best = (err, a, b)
    if not best or not (0.3 <= best[1] <= 4.0 and B_RANGE[0] <= best[2] <= B_RANGE[1]):
        return CURVE_DEFAULT
    if best[2] in B_RANGE:
        # Railed. The data wanted to go further and the grid would not let it,
        # which nearly always means `q` is wrong — a field size that records how
        # deep the harvest went rather than how many teams were ranked will do
        # it every time.
        LAST_FIT["railed"] = best[2]
    return round(best[1], 3), round(best[2], 3)


def field_move(rank: int, field_now: int, field_then: int, curve) -> float:
    """Log of how much a threshold at `rank` moves when the field changes.

    The curve's own quantile term and nothing else: a rank is a share of the
    field, and the same share of a bigger field is a stronger team. The
    reference-rank term of `shape_ratio` is left out on purpose - it moves
    with the field too, and taking the ratio of two `shape_ratio`s says that
    a smaller field lifts the top ranks, which the data refuses. Capped at
    FIELD_MOVE_CAP either way.
    """
    a, b = curve or CURVE_DEFAULT
    if rank < 1 or not field_now or not field_then or field_now <= 0 or field_then <= 0:
        return 0.0
    q_now = min(max(rank / field_now, 1e-9), 1.0)
    q_then = min(max(rank / field_then, 1e-9), 1.0)
    move = -a * (q_now ** b - q_then ** b)
    return max(-FIELD_MOVE_CAP, min(FIELD_MOVE_CAP, move))


def shape_ratio(rank: int, field: int, curve) -> float:
    """Share of the reference level still standing at this rank."""
    a, b = curve or CURVE_DEFAULT
    if not field or field <= 0 or rank < 1:
        return 1.0
    q = min(max(rank / field, 1e-9), 1.0)
    qref = min(max(REFERENCE_RANK / field, 1e-9), 1.0)
    return math.exp(-a * (q ** b - qref ** b))


def reference_level(comp: dict, curve=None) -> float | None:
    """The tournament's level: its threshold at the reference rank.

    Read off several ranks at once and taken as a median, which makes it immune
    to one aberrant reading. A tournament entered by hand often gives only a
    single threshold, at an arbitrary rank: the curve brings it back to the
    reference rank, so every entry counts whatever tier was measured.
    """
    field = comp.get("field_size")
    finals = comp.get("finals") or {}
    if not field or not finals:
        return None
    curve = curve or CURVE_DEFAULT
    # The reference rank, when it was actually measured, is the level — no curve
    # in the way. The median over 5/10/20/25 was worth its noise reduction when a
    # tournament was typed in by hand and often carried one arbitrary rank; on
    # harvested standings rank 20 is nearly always there, and mixing it with three
    # curve-corrected readings imports the curve's error into the one number that
    # did not have any. Measured on the harvest: the curve scatters 9.1 % at rank
    # 3-5 against 1.6 % at 16-40, so the median of the four is several times
    # noisier than the exact reading it contains.
    exact = finals.get(REFERENCE_RANK)
    if exact and exact > 0:
        return float(exact)
    seen = []
    for rank in REFERENCE_ANCHORS:
        value = finals.get(rank)
        if value and value > 0:
            seen.append(value / shape_ratio(rank, field, curve))
    if not seen:                                  # no reference rank available:
        for rank, value in finals.items():        # fall back on whatever we have
            rank = int(rank)
            if rank > 1 and value and value > 0:
                seen.append(value / shape_ratio(rank, field, curve))
    return statistics.median(seen) if seen else None


# The words that sort cups into kinds, and the platforms that sort players
# into populations. Read from the name, because the name is what both the
# database and the calendar have; a tag the harvest derived from a field the
# calendar does not carry would give the two sides different keys.
COLD_KINDS = (("fncs", "fncs"), ("champion series", "fncs"), ("cash cup", "cash-cup"),
              ("elite series", "elite-series"), ("victory cup", "victory-cup"),
              ("practice", "practice"), ("blitz", "blitz"))


def cold_signature(comp: dict) -> tuple:
    """What a cup nobody has seen before is most like.

    The cold start used to group by game mode and team size alone, which puts a
    creator's console Solo cup in the same bucket as an FNCS Solo final. Kind of
    cup, platform and whether it is a closed final each move the level the
    scoring table implies, and together they cut the cold-start error from
    12.0 % to 9.9 % on 6,539 tournaments — measured, not assumed.
    """
    # Name and category label only — the two fields the predictor's form also
    # has. The family is inside the label already, and reading it here as well
    # would give this side a word the other side cannot see.
    name = f"{comp.get('name') or ''} {comp.get('kind') or ''}".lower()
    kind = next((tag for needle, tag in COLD_KINDS if needle in name), "other")
    platform = "mobile" if "mobile" in name else ("console" if "console" in name else "pc")
    # From the category label, which every caller has — the export's form
    # carries no round number — and which is the one place the stage is
    # decided. A window whose id said Final and a hand-typed "Final" both end
    # up as "· Final" there.
    label = str(comp.get("kind") or "")
    stage = "final" if label.endswith((" Final", "Semi-final")) else "open"
    return (comp.get("game_mode") or "", comp.get("team_mode") or "", kind, platform, stage)


def reference_pace(history: list[dict], curve=None) -> dict:
    """Level per game, by scope — and its bridge to the scoring table."""
    out = {}
    # The scoring-table -> points bridge is not the same everywhere: a Reload cup
    # with its elimination cap and a classic Battle Royale don't have the same
    # economy. So we measure it per game mode, with a global fallback.
    shares: dict = {}
    for comp in history:
        level = reference_level(comp, curve)
        games = comp.get("max_games") or 0
        scoring = comp.get("scoring") or {}
        if not level or games <= 0 or not comp.get("scoring_known") \
                or not scoring.get("placement"):
            continue
        best = predict.max_game_score(scoring, kills=scoring.get("kill_cap") or 8)
        if best > 0:
            share = level / (games * best)
            # Two levels only, and never a mix across game types: a Reload cup
            # with its respawns and its elimination cap has nothing in common
            # with a Battle Royale. Solo and Duo split too — 100 players per lobby
            # against 50 teams.
            game = comp.get("game_mode") or ""
            team = comp.get("team_mode") or ""
            # Four keys, most specific first; `anchor_level` walks them in the
            # same order and stops at the first with a measurement behind it.
            signature = cold_signature(comp)
            shares.setdefault(str(signature + (comp.get("region") or "",)), []).append(share)
            shares.setdefault(str(signature), []).append(share)
            shares.setdefault(str((game, team)), []).append(share)
            shares.setdefault(str((game, "")), []).append(share)
    out["share_of_max"] = {k: {"median": round(statistics.median(v), 3), "n": len(v)}
                          for k, v in shares.items() if v}
    out["lobby"] = lobby_tables(history)

    for key, extract in (("category", lambda c: (c.get("kind"), c.get("region"))),
                         ("family", lambda c: c.get("kind")),
                         ("mode", lambda c: (c.get("game_mode"), c.get("team_mode")))):
        groups: dict = {}
        for comp in history:
            level = reference_level(comp, curve)
            games = comp.get("max_games") or 0
            if not level or games <= 0:
                continue
            # The level itself and the games it was played over, kept apart.
            # Dividing here and multiplying back in `anchor_level` was a
            # round-trip that cost accuracy for nothing whenever the target runs
            # the same number of games as its history — which is the usual case,
            # and exactly where the model was losing to a plain median of the
            # other editions.
            groups.setdefault(extract(comp), []).append(
                (str(comp.get("start_time") or ""), level, games))
        out[key] = {str(k): _recent(v) for k, v in groups.items() if v}
    return out


def _recent(dated: list[tuple]) -> dict:
    """Median level over the most recent editions, and the games they were run over.

    Carrying last week's threshold forward beats a median over the whole history
    of a cup — measured, 3.65 % against 4.66 % at the reference rank. One reading
    cannot beat many unless the level drifts, so the older editions are not
    evidence about tonight, they are evidence about last season.

    RECENT_EDITIONS sits between the two: enough readings to damp a freak
    evening, few enough to stay in the current meta. It is a working value picked
    between the one that carry-forward uses and the everything that lost to it,
    and it deserves to be measured properly rather than argued about.
    """
    dated.sort()
    kept = dated[-RECENT_EDITIONS:]
    levels = [level for _, level, _ in kept]
    games = [g for _, _, g in kept if g > 0] or [1.0]
    return {"median": round(statistics.median(levels), 2), "n": len(levels),
            "seen": len(dated), "games": round(statistics.median(games), 2)}


def field_sizes(history: list[dict]) -> dict:
    """Typical field size, by scope."""
    out = {}
    for key, extract in (("category", lambda c: (c.get("kind"), c.get("region"))),
                         ("family", lambda c: c.get("kind")),
                         ("mode", lambda c: (c.get("game_mode"), c.get("team_mode"),
                                             c.get("region")))):
        groups: dict = {}
        for comp in history:
            field = comp.get("field_size")
            if field and field > 0:
                groups.setdefault(extract(comp), []).append(int(field))
        out[key] = {str(k): {"median": int(statistics.median(v)), "n": len(v),
                             # how much the field moves from one edition to the
                             # next: measured at 4 % median on the FNCS
                             "spread": round(statistics.pstdev(v) / statistics.mean(v), 3)
                                       if len(v) > 1 else 0.20}
                    for k, v in groups.items() if v}
    return out


def field_sensitivity(rank: int, field: int, curve) -> float:
    """How much the threshold moves when the team count moves by 1 %.

        d ln(threshold) / d ln(field) = a . b . (q^b - q_ref^b)

    The result is small — 0.05 for a top 300 out of 36,000 — because the target
    rank and the reference rank are both taken relative to the same field: being
    wrong by half on the team count shifts the estimate by only 4 %. So there is
    no need to know the field size accurately in advance.
    """
    a, b = curve or CURVE_DEFAULT
    if not field or field <= 0 or rank < 1:
        return 0.0
    q = min(max(rank / field, 1e-9), 1.0)
    qref = min(max(REFERENCE_RANK / field, 1e-9), 1.0)
    return abs(a * b * (q ** b - qref ** b))


def guess_field(comp: dict, calib: dict) -> tuple[int, str, float] | None:
    """How many teams to expect, when it hasn't been entered.

    A rank on its own means nothing: 50th out of 500 and 50th out of 20,000 are
    not the same level. Rather than falling back on a model that does without it
    — and that was wrong by 25 % — we borrow the size from comparable
    tournaments, and say so.
    """
    table = calib.get("field_sizes") or {}
    for key, name in (("category", str((comp.get("kind"), comp.get("region")))),
                      ("family", str(comp.get("kind"))),
                      ("mode", str((comp.get("game_mode"), comp.get("team_mode"),
                                    comp.get("region"))))):
        entry = (table.get(key) or {}).get(name)
        if entry and entry["n"] >= 1:
            return entry["median"], key, entry.get("spread", 0.20)
    return None


def lobby_cap(team_mode, game_mode) -> int:
    mode = str(game_mode or "").lower()
    small = "reload" in mode or "blitz" in mode
    return (LOBBY_CAP_SMALL if small else LOBBY_CAP).get(str(team_mode or ""), 0)


def single_lobby(comp: dict, field: int) -> bool:
    """One lobby: a final played by a fixed set of qualified teams."""
    cap = lobby_cap(comp.get("team_mode"), comp.get("game_mode"))
    return bool(cap) and 0 < int(field or 0) <= cap


def lobby_tables(history: list[dict]) -> dict:
    """What a rank is worth inside a closed lobby, by share of the lobby.

    The open-queue cascade anchors on rank 20 and shapes the rest with a curve
    fitted on fields of thousands. Inside a lobby of twenty, rank 20 is the last
    team and that curve makes the winner worth ten times the anchor - which is
    how a Reload final priced rank 5 at 1,667 points where 300 was the most
    anyone could score. So closed lobbies get their own reading: every final in
    the training set that fits in one lobby, each of its thresholds divided by
    the most a team could score over the games played, bucketed by the share of
    the lobby the rank is. Keyed like the cold start - game mode and team size,
    then game mode alone, then everything - and each entry is [q, share, rel, n]
    with q the bucket's median share and rel the robust relative spread across
    finals, which is the band a cup never seen before honestly gets.
    """
    points: dict = {}
    for comp in history:
        field = int(comp.get("field_size") or 0)
        games = float(comp.get("max_games") or 0)
        scoring = comp.get("scoring") or {}
        if not single_lobby(comp, field) or games <= 0 or not comp.get("scoring_known") \
                or not scoring.get("placement"):
            continue
        best = predict.max_game_score(scoring, kills=scoring.get("kill_cap") or 8)
        if best <= 0:
            continue
        game = comp.get("game_mode") or ""
        team = comp.get("team_mode") or ""
        for rank, value in (comp.get("finals") or {}).items():
            rank, value = int(rank), float(value or 0)
            if rank < 1 or rank > field or value <= 0:
                continue
            # The last places are not a ladder: a team that left after two
            # games sits there with next to nothing, and reading the middle of
            # the board through them would price rank 15 of 20 off rank 20.
            if rank / field > LOBBY_LAST:
                continue
            pair = (rank / field, value / (games * best))
            for key in (str((game, team)), str((game, "")), "*"):
                points.setdefault(key, []).append(pair)

    def buckets(pairs):
        rows = []
        for edge in LOBBY_BUCKETS:
            inside = [(q, y) for q, y in pairs if q <= edge and (q, y) not in used]
            if len(inside) < LOBBY_MIN:
                used.update(inside)
                continue
            used.update(inside)
            ys = [y for _, y in inside]
            med = statistics.median(ys)
            mad = statistics.median(abs(y - med) for y in ys)
            rel = 1.4826 * mad / med if med > 0 else 0.0
            rows.append([round(statistics.median(q for q, _ in inside), 4),
                         round(med, 4), round(max(rel, LOBBY_REL_FLOOR), 3), len(inside)])
        return rows

    out = {}
    for key, pairs in points.items():
        used: set = set()
        rows = buckets(pairs)
        if len(rows) >= 2:
            out[key] = rows
    return out


def lobby_read(rows: list, q: float) -> tuple[float, float, int]:
    """(share of max, rel, n) at share q, log-linear between the two buckets
    either side. The top bucket holds above its own q; below the deepest, the
    board is read as flat and the band widens, because nothing was measured
    there and the last places are where a lobby's ladder collapses."""
    if q <= rows[0][0]:
        return rows[0][1], rows[0][2], rows[0][3]
    if q >= rows[-1][0]:
        return rows[-1][1], rows[-1][2] * 1.5, rows[-1][3]
    for lo, hi in zip(rows, rows[1:]):
        if lo[0] <= q <= hi[0]:
            f = (math.log(q) - math.log(lo[0])) / (math.log(hi[0]) - math.log(lo[0]))
            share = math.exp(math.log(lo[1]) + f * (math.log(hi[1]) - math.log(lo[1])))
            return share, max(lo[2], hi[2]), min(lo[3], hi[3])
    return rows[-1][1], rows[-1][2], rows[-1][3]


def lobby_estimate(comp: dict, calib: dict, rank: int, field: int) -> dict | None:
    """A closed lobby the model has never seen, from the finals it has."""
    table = (calib.get("reference_pace") or {}).get("lobby") or {}
    games = float(comp.get("max_games") or 0)
    scoring = comp.get("scoring") or {}
    if games <= 0 or comp.get("scoring_known") is False or not scoring.get("placement"):
        return None
    best = predict.max_game_score(scoring, kills=scoring.get("kill_cap") or 8)
    if best <= 0:
        return None
    game = comp.get("game_mode") or ""
    team = comp.get("team_mode") or ""
    for key, widen in ((str((game, team)), LOBBY_WIDEN["team"]),
                       (str((game, "")), LOBBY_WIDEN["mode"]), ("*", LOBBY_WIDEN["any"])):
        rows = table.get(key) or []
        if len(rows) >= 2:
            break
    else:
        return None
    share, rel, n = lobby_read(rows, rank / field)
    if n < LOBBY_THIN:
        widen *= LOBBY_WIDEN["thin"]
    return {"value": share * best * games, "rel": rel * widen, "n": n}


def anchor_level(comp: dict, calib: dict) -> dict | None:
    """The tournament's level, estimated without having seen it."""
    games = float(comp.get("max_games") or 0)
    if games <= 0:
        return None

    pace = calib.get("reference_pace") or calib.get("winner_pace") or {}

    def measured(key, name):
        """A single edition already counts — with a widened range."""
        entry = (pace.get(key) or {}).get(name)
        if not entry or entry.get("n", 0) < 1:
            return None
        # Widened when the cup has run once, not when the level is read off one
        # edition by choice: `seen` is how many there were, `n` how many the
        # reading used, and RECENT_EDITIONS = 1 would otherwise widen every cup.
        rel = ANCHOR_SPREAD[key] * (1.5 if entry.get("seen", entry["n"]) == 1 else 1.0)
        # The stored level is what these editions actually reached, over the
        # number of games they actually played. A target running the same number
        # gets it unchanged — the factor below is exactly 1 — and only a target
        # running a different number is scaled, at the exponent measured for the
        # per-game model rather than the 1.0 that dividing and multiplying
        # implies. Thresholds do not double when the games do.
        theirs = float(entry.get("games") or 0) or games
        value = entry["median"] * (games / theirs) ** GAMES_EXPONENT
        return {"value": value, "rel": rel, "source": key, "n": entry["n"]}

    for key, name in (("category", str((comp.get("kind"), comp.get("region")))),
                      ("family", str(comp.get("kind")))):
        found = measured(key, name)
        if found:
            return found

    # The scoring table comes before the game mode's pace: two tournaments in the
    # same mode can have unrelated scoring tables, and that is exactly the case
    # for a format never played before.
    scoring = comp.get("scoring") or {}
    if comp.get("scoring_known") is not False and scoring.get("placement"):
        best = predict.max_game_score(scoring, kills=scoring.get("kill_cap") or 8)
        table = pace.get("share_of_max") or {}
        game = comp.get("game_mode") or ""
        team = comp.get("team_mode") or ""

        # Same game type first, with the same team mode where possible. Never a
        # different game type: that mixing is what underestimated Reload cups by
        # a third, by borrowing from Battle Royale.
        entry, widen = {}, 1.15
        signature = cold_signature(comp)
        for key, factor in ((str(signature + (comp.get("region") or "",)), 0.9),
                            (str(signature), 1.0),
                            (str((game, team)), 1.0), (str((game, "")), 1.2)):
            found = table.get(key)
            if found and found.get("n", 0) >= 1:
                entry, widen = found, factor
                break

        # One or two measurements aren't enough to chase off the prior: we blend
        # them, the more freely the more of them there are.
        n = entry.get("n", 0)
        if n:
            weight = n / (n + 2)
            share = weight * entry["median"] + (1 - weight) * REFERENCE_SHARE
            if n < 3:
                widen = max(widen, 1.3)
        else:
            share = REFERENCE_SHARE

        if best > 0:
            return {"value": share * best * games,
                    "rel": ANCHOR_SPREAD["scoring"] * (1.0 if n >= 3 else widen),
                    "source": "scoring" if n >= 3 else
                              ("scoring (thin sample)" if n else "scoring (prior)"),
                    "n": n}

    return measured("mode", str((comp.get("game_mode"), comp.get("team_mode"))))


def exponent_for(calib: dict, rank: int) -> float:
    """Exponent learned for this rank, falling back on the mean then the default."""
    table = (calib or {}).get("exponent") or {}
    entry = table.get(str(rank))
    if entry and entry["n"] >= 1:
        return entry["median"]
    medians = [e["median"] for e in table.values() if e.get("n")]
    return statistics.median(medians) if medians else DEFAULT_EXPONENT


def priors_from(calib: dict) -> dict:
    """Prior weights for the models, derived from their historical error."""
    errs = (calib or {}).get("model_error_pct") or {}
    if not errs:
        return dict(predict.MODEL_PRIOR)
    priors = {}
    for key in predict.MODEL_LABELS:
        err = errs.get(key)
        priors[key] = 1.0 / ((err / 100 if err is not None else 0.15) + 0.02) ** 2
    # brought back onto the same scale as the default priors
    top = max(priors.values()) or 1.0
    return {k: round(3.0 * v / top, 3) for k, v in priors.items()}


def prior_prediction(comp: dict, calib: dict, rank: int) -> dict | None:
    """A rank's threshold BEFORE the first reading, in two pieces.

    **The shape** — how much of the winner's threshold is left at a given rank —
    depends on neither the scoring table nor the region: `exp(-a q^b)` with q the
    share of the field. Measured on the history, it reproduces the thresholds to
    within 4.8 %, as well at the bottom of the standings as at the top.

    **The scale** — what the winner scores — depends on the tournament. We take it
    from comparable editions where there are any, otherwise from the scoring table
    alone, and it is by far the more uncertain term: it is what sets the range.
    """
    field = int(comp.get("field_size") or 0)
    if field < 0:
        field = 0
    guessed, field_spread = None, 0.0
    if not field:
        found = guess_field(comp, calib)
        if not found:
            return {"ok": False, "reason":
                    "The number of ranked teams is missing, and no comparable "
                    "tournament allows us to assume it. Without it a rank means "
                    "nothing: 50th of 500 and 50th of 20,000 have nothing in common."}
        field, guessed, field_spread = found
    if rank < 1:
        return None

    # Rung zero: this cup, this region, this rank, last time. Nothing is
    # modelled — see `direct_tables` for why that beats modelling it.
    direct = direct_from(comp, calib, rank)
    if direct:
        value, rel, n = direct["value"], direct["rel"], direct["n"]
        return {
            "ok": True, "shape_source": "direct",
            "value": round(value, 1),
            "low": round(max(0.0, value * (1 - rel)), 1),
            "high": round(value * (1 + rel), 1),
            "n": n, "source": "previous edition",
            "games": int(comp.get("max_games") or 0),
            "field": field, "share": round(100 * rank / field, 2),
            "guessed_field": guessed,
            # Here: how much the known field moved the edition's value, in
            # per cent - not, as on the other rungs, what a 30 % error would.
            "field_effect": round(direct["field_effect"], 1),
            "entry_note": direct["note"],
            "level": round(value, 1), "ref_rank": rank,
            "sealed": predict.is_sealed(comp),
        }

    top = anchor_level(comp, calib)

    # A closed lobby without editions of its own: read off the finals of its
    # mode, by share of the lobby, rather than off an anchor and a curve that
    # were both measured on open queues of thousands. Editions of the cup
    # itself, where there are any, still come first.
    if single_lobby(comp, field) and (not top or top["source"] not in ("category", "family")):
        lobby = lobby_estimate(comp, calib, rank, field)
        if lobby:
            value, rel = lobby["value"], lobby["rel"]
            return {
                "ok": True, "shape_source": "lobby",
                "value": round(value, 1),
                "low": round(max(0.0, value * (1 - rel)), 1),
                "high": round(value * (1 + rel), 1),
                "n": lobby["n"], "source": "closed lobby",
                "games": int(comp.get("max_games") or 0),
                "field": field, "share": round(100 * rank / field, 2),
                "guessed_field": guessed, "field_effect": 0.0,
                "level": round(value, 1), "ref_rank": rank,
                "sealed": predict.is_sealed(comp),
            }

    if not top:
        return {"ok": False, "reason": why_no_anchor(comp, calib)}

    curve = calib.get("curve") or CURVE_DEFAULT
    slope = field_sensitivity(rank, field, curve)
    # Measured first, modelled second. Where comparable editions have actually
    # been to this rank, their median says what it was worth; the curve answers
    # only for ranks none of them reached.
    found = shape_from(comp, calib, rank, top["source"])
    if found:
        ratio, shape_rel = found
        shape_source = "measured"
        # No separate field term here. The dispersion above is what these very
        # editions did at this very rank, fields and all, so adding a field
        # correction on top would be counting the same variation twice.
        field_part = 0.0
    else:
        ratio, shape_rel = shape_ratio(rank, field, curve), SHAPE_FALLBACK_REL
        shape_source = "curve"
        # An error on the team count only reaches the threshold in proportion to
        # the slope, which is 0.05 near the top of the standings. So a guessed
        # field costs almost nothing.
        field_part = slope * (field_spread if guessed else 0.15)
    value = top["value"] * ratio
    rel = math.sqrt(top["rel"] ** 2 + shape_rel ** 2 + field_part ** 2)
    return {
        "ok": True,
        "shape_source": shape_source,
        "value": round(value, 1),
        "low": round(max(0.0, value * (1 - rel)), 1),
        "high": round(value * (1 + rel), 1),
        "n": top["n"],
        "source": top["source"],
        "games": int(comp.get("max_games") or 0),
        "field": field,
        "share": round(100 * rank / field, 2),
        "guessed_field": guessed,
        # what a 30 % error on the team count would cost
        "field_effect": round(100 * slope * 0.30, 1),
        "level": round(top["value"], 1),
        "ref_rank": REFERENCE_RANK,
        "sealed": predict.is_sealed(comp),
    }


def why_no_anchor(comp: dict, calib: dict) -> str:
    """Say what is missing rather than returning a blank screen."""
    if float(comp.get("max_games") or 0) <= 0:
        return "The number of games is missing."
    scoring = comp.get("scoring") or {}
    if not scoring.get("placement"):
        return ("No reference at all: no comparable edition in the history, and no "
                "scoring table entered. Fill in the placement points, or enter the "
                "real threshold below to create the first reference.")
    if comp.get("scoring_known") is False:
        return ("The scoring table isn't confirmed for this tournament, and no "
                "comparable edition exists. Pick or enter a scoring table.")
    return ("No usable reference. Enter the real threshold below: it becomes the "
            "first one.")


# --------------------------------------------------------------------------- #
# Which tournaments are genuinely comparable?
# --------------------------------------------------------------------------- #
def comparable_history(conn, db, comp: dict, exclude_id: int | None = None):
    """From the narrowest circle outwards, stopping at the first usable one.

    A Division 1 and a Division 5 carry the same series name but have neither the
    same level nor, sometimes, the same scoring table; a Reload Zero Build cup has
    nothing to do with a Battle Royale. So the exact category — the tournament
    name stripped of its week number and region — comes before everything else,
    and we only widen for want of anything better.
    """
    kind = db.category_of(comp)
    # The label is what the page shows; the key is what decides sameness. They
    # differ where the label cannot: two cups of the same name in different
    # modes read alike and are not comparable.
    key = db.category_key(comp)

    # Listed once, not once per circle: four circles are tried before the widest
    # one answers, and re-reading every competition each time is four full table
    # scans for a list that cannot have changed between them. `catalogue` rather
    # than `list_competitions` because every test below asks about region, stage
    # or mode and none of them opens a scoring table.
    everything = db.catalogue(conn)

    def collect(match):
        rows = [o for o in everything
                if not (exclude_id and o["id"] == exclude_id) and match(o)]
        rows.sort(key=lambda c: c["start_time"])
        out = []
        # Newest first, stopping at CIRCLE_CAP: the widest circle is
        # region + team size + mode, which the API harvest fills with thousands of
        # tournaments, and loading them all — with their snapshots — to take a
        # median is minutes of work for a number that stopped moving decades of
        # editions ago. The shrinkage weight n/(n+2) is within 5 % of 1 by n = 40,
        # and the level itself is read off the wide sample, not from here.
        for other in reversed(rows):
            full = db.get_competition_full(conn, other["id"])
            if full and full["snapshots"]:
                out.append(full)
            if len(out) >= CIRCLE_CAP:
                break
        out.reverse()
        return out

    same_format = (lambda o: o["region"] == comp["region"]
                   and o["team_mode"] == comp["team_mode"]
                   and o["game_mode"] == comp["game_mode"])

    # (label, exact, test). "exact" marks the two circles drawn on the
    # competition's own category; the wider ones borrow from neighbours and
    # callers widen their bands accordingly. Carrying the flag beats having
    # them recognise the label, which is free text and gets reworded.
    levels = [
        (f"{kind} in {comp['region']}", True,
         lambda o: db.category_key(o) == key),
        (f"{kind}, all regions", True,
         lambda o: db.series_of(o) == key[0] and db.round_of(o) == key[1]
         and o["team_mode"] == comp["team_mode"] and o["game_mode"] == comp["game_mode"]),
    ]
    if comp.get("series_id"):
        levels.append((f"same series, stage {comp.get('stage') or '-'}", False,
                       lambda o: o["series_id"] == comp["series_id"]
                       and o["stage"] == comp["stage"]))
    levels.append((f"{comp['region']} - {comp['team_mode']} - {comp['game_mode']}",
                   False, same_format))

    for label, exact, match in levels:
        hist = collect(match)
        if any(predict.is_complete(c) for c in hist):
            return hist, label, exact
    # nothing finished anywhere: hand back the narrowest circle, empty or not
    return collect(levels[0][2]), levels[0][0], levels[0][1]


# --------------------------------------------------------------------------- #
# Database cache
# --------------------------------------------------------------------------- #
def cache_key(scope: dict) -> str:
    return "|".join(str(scope.get(k) or "") for k in
                    ("level", "kind", "series_id", "stage", "region", "team_mode", "game_mode"))


# Reserved cache key: the wide-sample half of a calibration, which every scope
# shares. Not a scope name — no scope can produce a key starting with "*".
BROAD_KEY = "*broad*"


def _fingerprint(conn) -> int:
    """A cheap number that changes whenever the wide sample could have.

    Every write in the app calls `invalidate`, but the harvester writes straight
    through `db`, so the cache cannot rely on being told. Counting two tables is
    two index scans and catches anything that adds, removes or re-scores a
    tournament.
    """
    comps = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM competition").fetchone()
    finals = conn.execute("SELECT COUNT(*) FROM final_result").fetchone()[0]
    return int(comps[0]) * 1_000_003 + int(comps[1]) * 1_009 + int(finals)


def shared_broad(conn, db) -> dict:
    """`broad_stats` over the whole database, computed once and cached.

    Without this, every scope that misses the cache refits the curve over every
    threshold in the database — 2,000 scopes paying a minute each for the same
    three tables. The fit depends on the wide sample alone, so one row serves
    the lot.
    """
    mark = _fingerprint(conn)
    row = conn.execute("SELECT * FROM calibration_cache WHERE key = ?", (BROAD_KEY,)).fetchone()
    if row and row["n_comps"] == mark:
        try:
            return json.loads(row["payload"])
        except (ValueError, TypeError):
            pass
    broad = broad_stats(db.all_full(conn))
    conn.execute(
        "INSERT INTO calibration_cache (key, n_comps, payload, computed_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET n_comps = excluded.n_comps, "
        "payload = excluded.payload, computed_at = excluded.computed_at",
        (BROAD_KEY, mark, json.dumps(broad, ensure_ascii=False), db.norm_ts(None)),
    )
    return broad


def load_or_compute(conn, db, scope: dict, history: list[dict], ranks: list[int]) -> dict:
    """Return the calibration, recomputing only if the history has changed.

    `wide` is everything known, not just the comparable circle: the curve shape
    is a property of Fortnite scoring, not of one tournament family, so it is
    fitted on every result available. Passing it here matters — a cached
    calibration computed without it used to differ from a fresh one.
    """

    key = cache_key(scope)
    n_done = sum(1 for c in history if predict.is_complete(c))
    if key == BROAD_KEY:                       # not a scope; see shared_broad
        raise ValueError("BROAD_KEY is reserved")
    row = conn.execute("SELECT * FROM calibration_cache WHERE key = ?", (key,)).fetchone()
    if row and row["n_comps"] == n_done:
        try:
            payload = json.loads(row["payload"])
            if set(payload.get("ratio", {})) >= {str(r) for r in ranks if r} or not ranks:
                return payload
        except (ValueError, TypeError):
            pass
    calib = calibrate(history, ranks, broad=shared_broad(conn, db))
    conn.execute(
        "INSERT INTO calibration_cache (key, n_comps, payload, computed_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET n_comps = excluded.n_comps, "
        "payload = excluded.payload, computed_at = excluded.computed_at",
        (key, n_done, json.dumps(calib, ensure_ascii=False), db.norm_ts(None)),
    )
    return calib


def invalidate(conn) -> None:
    conn.execute("DELETE FROM calibration_cache")
