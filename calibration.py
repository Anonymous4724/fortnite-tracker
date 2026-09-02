"""Calibration: what the app learns from your own history.

Three things are measured on competitions that have already finished, then fed
back into the predictions for the ones that follow:

1. **The exponent of the "per game" model** — how much a top's threshold rises
   when the number of games doubles. Defaults to 0.93, and gets measured as soon
   as you have one comparable finished competition.

2. **How reliable each model is** — which model is least wrong, by stage and by
   moment in the session. Serves as a prior weight while there are still too few
   readings to backtest the competition in progress.

3. **The relationship between the scoring table and the thresholds reached** —
   how many times a reference game's value you have to score to land in a given
   top. That ratio is what lets us estimate a tournament *before the first
   reading*, and transpose the history onto a different scoring table.

The result is cached in the database and recomputed when the number of
comparable finished competitions changes.
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
def calibrate(history: list[dict], ranks: list[int], wide: list[dict] | None = None) -> dict:
    """Aggregate the measurements over every comparable finished competition.

    `history` is the narrow circle — the genuinely comparable tournaments, which
    feed the ratios and the model errors. `wide` is everything we know: the shape
    of the curve depends on neither the scoring table nor the region, so it has
    everything to gain from being fitted on the lot.
    """
    done = [c for c in history if predict.is_complete(c)]
    broad = [c for c in (wide or history) if predict.is_complete(c)] or done
    curve = fit_curve(broad)
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
        "curve": list(curve),
        "reference_pace": reference_pace(broad, curve),
        "field_sizes": field_sizes(broad),
        "n_curve": len(broad),
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
    for step in range(20, 91):                     # b, swept by hundredths
        b = step / 100
        den = sum((qr ** b - q ** b) ** 2 for q, qr, _ in data)
        if den <= 0:
            continue
        a = sum((qr ** b - q ** b) * y for q, qr, y in data) / den
        err = sum((y - a * (qr ** b - q ** b)) ** 2 for q, qr, y in data)
        if best is None or err < best[0]:
            best = (err, a, b)
    if not best or not (0.3 <= best[1] <= 4.0 and 0.2 <= best[2] <= 1.2):
        return CURVE_DEFAULT
    return round(best[1], 3), round(best[2], 3)


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
            shares.setdefault(str((game, team)), []).append(share)
            shares.setdefault(str((game, "")), []).append(share)
    out["share_of_max"] = {k: {"median": round(statistics.median(v), 3), "n": len(v)}
                          for k, v in shares.items() if v}

    for key, extract in (("category", lambda c: (c.get("kind"), c.get("region"))),
                         ("family", lambda c: c.get("kind")),
                         ("mode", lambda c: (c.get("game_mode"), c.get("team_mode")))):
        groups: dict = {}
        for comp in history:
            level = reference_level(comp, curve)
            games = comp.get("max_games") or 0
            if not level or games <= 0:
                continue
            groups.setdefault(extract(comp), []).append(level / games)
        out[key] = {str(k): {"median": round(statistics.median(v), 2), "n": len(v)}
                    for k, v in groups.items() if v}
    return out


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
        rel = ANCHOR_SPREAD[key] * (1.5 if entry["n"] == 1 else 1.0)
        return {"value": entry["median"] * games, "rel": rel,
                "source": key, "n": entry["n"]}

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
        for key, factor in ((str((game, team)), 1.0), (str((game, "")), 1.2)):
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
    top = anchor_level(comp, calib)
    if not top:
        return {"ok": False, "reason": why_no_anchor(comp, calib)}

    curve = calib.get("curve") or CURVE_DEFAULT
    value = top["value"] * shape_ratio(rank, field, curve)
    # The anchor's uncertainty and the shape's compose. The field's adds to them,
    # but propagated: an error on the team count only reaches the threshold in
    # proportion to the slope, which is 0.05 near the top of the standings. So a
    # guessed field costs almost nothing.
    slope = field_sensitivity(rank, field, curve)
    field_part = slope * (field_spread if guessed else 0.15)
    rel = math.sqrt(top["rel"] ** 2 + 0.06 ** 2 + field_part ** 2)
    return {
        "ok": True,
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

    def collect(match):
        out = []
        for other in db.list_competitions(conn):
            if exclude_id and other["id"] == exclude_id:
                continue
            if not match(other):
                continue
            full = db.get_competition_full(conn, other["id"])
            if full and full["snapshots"]:
                out.append(full)
        out.sort(key=lambda c: c["start_time"])
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
         lambda o: db.category_of(o) == kind and o["region"] == comp["region"]),
        (f"{kind}, all regions", True,
         lambda o: db.category_of(o) == kind),
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


def load_or_compute(conn, db, scope: dict, history: list[dict], ranks: list[int]) -> dict:
    """Return the calibration, recomputing only if the history has changed.

    `wide` is everything known, not just the comparable circle: the curve shape
    is a property of Fortnite scoring, not of one tournament family, so it is
    fitted on every result available. Passing it here matters — a cached
    calibration computed without it used to differ from a fresh one.
    """

    key = cache_key(scope)
    n_done = sum(1 for c in history if predict.is_complete(c))
    row = conn.execute("SELECT * FROM calibration_cache WHERE key = ?", (key,)).fetchone()
    if row and row["n_comps"] == n_done:
        try:
            payload = json.loads(row["payload"])
            if set(payload.get("ratio", {})) >= {str(r) for r in ranks if r} or not ranks:
                return payload
        except (ValueError, TypeError):
            pass
    calib = calibrate(history, ranks, wide=db.all_full(conn))
    conn.execute(
        "INSERT INTO calibration_cache (key, n_comps, payload, computed_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET n_comps = excluded.n_comps, "
        "payload = excluded.payload, computed_at = excluded.computed_at",
        (key, n_done, json.dumps(calib, ensure_ascii=False), db.norm_ts(None)),
    )
    return calib


def invalidate(conn) -> None:
    conn.execute("DELETE FROM calibration_cache")
