"""Points-threshold forecasting engine.

Two families of prediction:

1. Live (`predict_live`): during a competition, extrapolate each top's final
   threshold from the readings entered so far. Four models are evaluated,
   backtested on the readings available, then combined with a weight inversely
   proportional to their error.

2. Between competitions (`predict_next`): estimate the next competition's final
   threshold from the history of comparable ones.

No external dependency: standard library only.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta

EPS = 1e-9


# --------------------------------------------------------------------------- #
# Statistical helpers
# --------------------------------------------------------------------------- #
def linreg(xs: list[float], ys: list[float]):
    """Least-squares linear regression -> (slope, intercept, r2) or None."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < EPS:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    intercept = my - slope * mx
    r2 = (sxy ** 2) / (sxx * syy) if syy > EPS else 1.0
    return slope, intercept, r2


def _clamp(value, low, high):
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# Intra-competition extrapolation models
# --------------------------------------------------------------------------- #
def m_linear(ps, ys, target):
    """Constant average pace over the whole session."""
    fit = linreg(ps, ys)
    if not fit:
        return None
    slope, intercept, _ = fit
    return slope * target + intercept


def m_recent(ps, ys, target, window=4):
    """Pace of the last few readings (reacts to a change of tempo)."""
    k = max(2, min(window, len(ps)))
    fit = linreg(ps[-k:], ys[-k:])
    if not fit:
        return None
    slope, intercept, _ = fit
    return slope * target + intercept


def m_power(ps, ys, target):
    """y = a * p^b: catches an accumulation that speeds up or slows down."""
    pts = [(p, y) for p, y in zip(ps, ys) if p > EPS and y > EPS]
    if len(pts) < 3:
        return None
    fit = linreg([math.log(p) for p, _ in pts], [math.log(y) for _, y in pts])
    if not fit:
        return None
    b, log_a, _ = fit
    b = _clamp(b, 0.15, 3.0)
    try:
        return math.exp(log_a) * (target ** b)
    except (OverflowError, ValueError):
        return None


def estimated_games(comp: dict, progress: float) -> float:
    """Games visible on the standings at this point in the session.

    Sealed format: a certainty, since the schedule gives the exact time each game
    appears. "Maximum" format: assume the games are spread over the official
    duration, minus the tracker lag that always leaves you one game behind.
    """
    tl = timeline(comp)
    max_games = float(comp.get("max_games") or 10) or 10.0
    if not tl["total_min"]:
        return 0.0

    schedule = game_schedule(comp)
    if schedule:
        now = tl["start"] + timedelta(minutes=progress * tl["total_min"])
        return float(sum(1 for g in schedule if g["visible_at"] <= now))

    if not tl["official_min"]:
        return 0.0
    lag = float(comp.get("tracker_lag_min") or 0)
    avg = tl["official_min"] / max_games
    if avg <= 0:
        return 0.0
    return _clamp((progress * tl["total_min"] - lag) / avg, 0.0, max_games)


def make_games_model(comp: dict, games_map: dict | None = None, exponent: float = 0.93):
    """'Per game' model: usable from the very first reading.

    A top's threshold does not rise in proportion to the number of games (nobody
    strings together ten top 1s), hence the exponent below 1, refined as soon as
    there are enough points to measure it.
    """
    max_games = float(comp.get("max_games") or 0)
    if max_games <= 0:
        return None

    def g_of(p):
        if games_map:
            g = games_map.get(round(p, 6))
            if g:
                return float(g)
        return estimated_games(comp, p)

    def model(ps, ys, target):
        if not ps:
            return None
        g_now = g_of(ps[-1])
        if g_now < 0.8:                      # too early, no game finished yet
            return None
        g_target = _clamp(g_of(target), g_now, max_games)
        b = exponent
        pairs = [(g_of(p), y) for p, y in zip(ps, ys)]
        pairs = [(g, y) for g, y in pairs if g > EPS and y > EPS]
        if len(pairs) >= 3:
            fit = linreg([math.log(g) for g, _ in pairs], [math.log(y) for _, y in pairs])
            if fit:
                b = _clamp(fit[0], 0.5, 1.3)
        return ys[-1] * (g_target / g_now) ** b

    return model


def make_shape_model(shape):
    """Model based on the average shape of past curves.

    `shape`: list of (progress, fraction of the total reached).
    Prediction: current points / fraction expected at that progress.
    """
    if not shape or len(shape) < 2:
        return None

    def interp(p):
        p = _clamp(p, 0.0, 1.0)
        if p <= shape[0][0]:
            return max(shape[0][1], EPS)
        for (p0, f0), (p1, f1) in zip(shape, shape[1:]):
            if p <= p1:
                if p1 - p0 < EPS:
                    return f1
                w = (p - p0) / (p1 - p0)
                return f0 + w * (f1 - f0)
        return shape[-1][1]

    def model(ps, ys, target):
        if not ps:
            return None
        frac_now = interp(ps[-1])
        frac_target = interp(target)
        if frac_now < 0.02:          # too early, the ratio blows up
            return None
        return ys[-1] * frac_target / frac_now

    return model


# --------------------------------------------------------------------------- #
# Backtest + ensemble
# --------------------------------------------------------------------------- #
def _backtest(model, ps, ys) -> float | None:
    """Mean relative error of the model when fed only the start of the series."""
    errors = []
    for k in range(2, len(ps)):
        pred = model(ps[:k], ys[:k], ps[k])
        if pred is None or ys[k] <= EPS:
            continue
        errors.append(abs(pred - ys[k]) / ys[k])
    if not errors:
        return None
    return sum(errors) / len(errors)


MODEL_LABELS = {
    "linear": "Average pace (linear)",
    "recent": "Recent pace",
    "power": "Power curve",
    "shape": "Historical shape",
    "games": "Per game played",
}

# Prior confidence, used while there aren't enough readings to backtest: models
# that know how the points arrive (per game, or the shape of past curves) beat a
# straight line drawn against the clock.
MODEL_PRIOR = {"games": 3.0, "shape": 2.5, "power": 1.2, "linear": 1.0, "recent": 0.8}


def available_models(comp: dict, rank: int, shape=None, exponent: float | None = None) -> dict:
    """Every model applicable to one rank of a competition.

    Each model switches itself off (by returning None) when it hasn't enough
    points, so there is no need to filter on series length here.
    """
    models = {"linear": m_linear, "recent": m_recent, "power": m_power}
    shape_model = make_shape_model(shape)
    if shape_model:
        models["shape"] = shape_model
    games = make_games_model(comp, games_map(comp, rank), exponent or 0.93)
    if games:
        models["games"] = games
    return models


def predict_series(ps: list[float], ys: list[float], shape=None, target: float = 1.0,
                   games_model=None, models: dict | None = None,
                   priors: dict | None = None) -> dict:
    """Predict the value at `target` (1.0 = end of acquisition) for one series."""
    if not ps:
        return {"ok": False, "reason": "No reading for this rank."}

    if models is None:
        models = {"linear": m_linear, "recent": m_recent, "power": m_power}
        shape_model = make_shape_model(shape)
        if shape_model:
            models["shape"] = shape_model
        if games_model:
            models["games"] = games_model
    priors = priors or MODEL_PRIOR

    details, weights, values = [], [], []
    for key, model in models.items():
        pred = model(ps, ys, target)
        if pred is None:
            continue
        pred = max(pred, ys[-1])           # points can't go down
        err = _backtest(model, ps, ys)
        # weight in 1/error^2: a model twice as accurate weighs four times as much
        weight = 1.0 / (err + 0.02) ** 2 if err is not None else None
        details.append({
            "model": key,
            "label": MODEL_LABELS[key],
            "value": round(pred, 1),
            "backtest_error_pct": round(100 * err, 1) if err is not None else None,
        })
        weights.append(weight)
        values.append(pred)

    if not values:
        return {"ok": False, "reason": "Needs a 2nd reading, or the number of games "
                                       "played, to extrapolate."}

    # Models that can't be backtested (too few readings) fall back on the prior
    # confidence, scaled to the level of the ones we could measure.
    known = [w for w in weights if w is not None]
    base = statistics.median(known) if known else 1.0
    weights = [w if w is not None else base * priors.get(d["model"], 1.0)
               for w, d in zip(weights, details)]
    for d, w in zip(details, weights):
        d["weight"] = round(w, 2)

    # A model clearly worse than the best one on this series is dropped rather
    # than allowed to drag the mean towards it.
    known = [d["backtest_error_pct"] for d in details if d["backtest_error_pct"] is not None]
    if known:
        limit = 3 * min(known) + 2.0
        for d in details:
            if d["backtest_error_pct"] is not None and d["backtest_error_pct"] > limit:
                d["dropped"] = True
    kept = [(d, v, w) for d, v, w in zip(details, values, weights) if not d.get("dropped")]
    if not kept:
        kept = list(zip(details, values, weights))
    values_k = [v for _, v, _ in kept]
    weights_k = [w for _, _, w in kept]

    total_w = sum(weights_k)
    best = sum(v * w for v, w in zip(values_k, weights_k)) / total_w

    # Uncertainty: max(dispersion between models, weighted backtest error),
    # amplified by the share of the session still to come.
    spread = (max(values_k) - min(values_k)) / 2
    errs = [d["backtest_error_pct"] for d, _, _ in kept if d["backtest_error_pct"] is not None]
    err_based = best * (sum(errs) / len(errs) / 100) if errs else 0.0
    remaining = _clamp(target - ps[-1], 0.0, 1.0)
    margin = max(spread, err_based) * (1.0 + remaining)
    # with only one or two readings the real uncertainty is far wider than the
    # dispersion between models would suggest
    floor_rel = 0.15 if len(ps) == 1 else (0.08 if len(ps) == 2 else 0.02)
    margin = max(margin, best * floor_rel * (0.5 + remaining))

    return {
        "ok": True,
        "value": round(best, 1),
        "low": round(max(ys[-1], best - margin), 1),
        "high": round(best + margin, 1),
        "current": round(ys[-1], 1),
        "progress": round(100 * ps[-1], 1),
        "remaining_gain": round(best - ys[-1], 1),
        "models": sorted(details, key=lambda d: -d["weight"]),
    }


# --------------------------------------------------------------------------- #
# Building the series from a competition
# --------------------------------------------------------------------------- #
def is_sealed(comp: dict) -> bool:
    """Sealed format: every game starts at a fixed time (final, set lobby)."""
    return (comp.get("games_mode") or "max") == "sealed" and float(comp.get("slot_minutes") or 0) > 0


def game_schedule(comp: dict) -> list[dict]:
    """Game schedule of a sealed competition.

    Game k starts at start + (k-1) x interval, runs for `game_minutes`, and shows
    up on the standings `tracker_lag_min` later.
    """
    if not is_sealed(comp):
        return []
    start = datetime.fromisoformat(comp["start_time"])
    slot = float(comp["slot_minutes"])
    dur = float(comp.get("game_minutes") or 0)
    lag = float(comp.get("tracker_lag_min") or 0)
    out = []
    for k in range(1, int(comp.get("max_games") or 0) + 1):
        begins = start + timedelta(minutes=slot * (k - 1))
        out.append({
            "index": k,
            "start": begins,
            "end": begins + timedelta(minutes=dur),
            "visible_at": begins + timedelta(minutes=dur + lag),
        })
    return out


def effective_end(comp: dict) -> datetime | None:
    """When the thresholds really stop moving.

    Sealed format: the last game on the schedule, plus its duration and the
    tracker lag. "Maximum" format: a game started just before closing keeps
    scoring, so official end + duration + lag.
    """
    schedule = game_schedule(comp)
    if schedule:
        return schedule[-1]["visible_at"]
    if not comp.get("end_time"):
        return None
    end = datetime.fromisoformat(comp["end_time"])
    extra = float(comp.get("game_minutes") or 0) + float(comp.get("tracker_lag_min") or 0)
    return end + timedelta(minutes=extra)


def timeline(comp: dict) -> dict:
    """A competition's time markers, in minutes from the start."""
    start = datetime.fromisoformat(comp["start_time"])
    end = datetime.fromisoformat(comp["end_time"]) if comp.get("end_time") else None
    eff = effective_end(comp)
    if end is None and eff is not None:
        # sealed final with no end time entered: the last game is authoritative
        end = eff - timedelta(minutes=float(comp.get("tracker_lag_min") or 0))
    return {
        "start": start,
        "end": end,
        "effective_end": eff,
        "official_min": (end - start).total_seconds() / 60 if end else None,
        "total_min": (eff - start).total_seconds() / 60 if eff else None,
        "extra_min": float(comp.get("game_minutes") or 0) + float(comp.get("tracker_lag_min") or 0),
    }


def progress_series(comp: dict, rank: int):
    """-> (progress [0..1], points) for a given rank.

    Progress 1.0 is the *effective* end (official end + last game + tracker lag),
    not the closing time shown on the page.
    """
    tl = timeline(comp)
    if not tl["total_min"] or tl["total_min"] <= 0:
        return None, None
    start, total = tl["start"], tl["total_min"] * 60
    ps, ys = [], []
    for snap in comp.get("snapshots", []):
        value = snap["points"].get(rank, snap["points"].get(str(rank)))
        if value is None:
            continue
        ts = datetime.fromisoformat(snap["ts"])
        ps.append(_clamp((ts - start).total_seconds() / total, 0.0, 1.2))
        ys.append(float(value))
    if len(ps) < 1:
        return None, None
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    return [ps[i] for i in order], [ys[i] for i in order]


def final_value(comp: dict, rank: int) -> float | None:
    """Reference final threshold for this rank.

    The hand-entered definitive result wins if there is one — that's the ground
    truth — otherwise the last known reading.
    """
    finals = comp.get("finals") or {}
    value = finals.get(rank, finals.get(str(rank)))
    if value is not None:
        return float(value)
    values = [s["points"].get(rank, s["points"].get(str(rank)))
              for s in comp.get("snapshots", [])]
    values = [float(v) for v in values if v is not None]
    return max(values) if values else None


def has_finals(comp: dict) -> bool:
    return bool(comp.get("finals"))


def is_complete(comp: dict, min_progress: float = 0.9) -> bool:
    """A competition counts as finished if it was closed by hand or its definitive
    results are entered; failing that, if the last reading covers nearly the whole
    session."""
    if comp.get("finished_at") or has_finals(comp):
        return True
    if not comp.get("end_time") or not comp.get("snapshots"):
        return False
    tl = timeline(comp)
    if not tl["total_min"] or tl["total_min"] <= 0:
        return False
    last = max(datetime.fromisoformat(s["ts"]) for s in comp["snapshots"])
    return (last - tl["start"]).total_seconds() / 60 / tl["total_min"] >= min_progress


def build_shape(history: list[dict], rank: int, grid_steps: int = 20):
    """Average shape of past curves for this rank: [(progress, fraction)]."""
    curves = []
    for comp in history:
        if not is_complete(comp):
            continue
        ps, ys = progress_series(comp, rank)
        if not ps or len(ps) < 3:
            continue
        final = final_value(comp, rank)
        if not final or final <= EPS:
            continue
        fs = [y / final for y in ys]
        # With a definitive result entered, we know the curve is worth 1 at the end.
        if has_finals(comp) and ps[-1] < 1.0 - EPS:
            ps, fs = ps + [1.0], fs + [1.0]
        curves.append((ps, fs))
    if not curves:
        return None

    shape = [(0.0, 0.0)]
    for step in range(1, grid_steps + 1):
        p = step / grid_steps
        fractions = []
        for ps, fs in curves:
            if p < ps[0] or p > ps[-1] + EPS:
                continue
            for i in range(len(ps) - 1):
                if p <= ps[i + 1]:
                    span = ps[i + 1] - ps[i]
                    w = 0 if span < EPS else (p - ps[i]) / span
                    fractions.append(fs[i] + w * (fs[i + 1] - fs[i]))
                    break
        if fractions:
            shape.append((p, statistics.median(fractions)))
    if len(shape) < 3:
        return None
    # force monotone growth
    cleaned = [shape[0]]
    for p, f in shape[1:]:
        cleaned.append((p, max(f, cleaned[-1][1])))
    if cleaned[-1][0] < 1.0:
        cleaned.append((1.0, max(1.0, cleaned[-1][1])))
    return cleaned


def games_map(comp: dict, rank: int) -> dict:
    """{rounded progress: games played} for the readings where you entered it."""
    tl = timeline(comp)
    if not tl["total_min"]:
        return {}
    out = {}
    for snap in comp.get("snapshots", []):
        if not snap.get("games"):
            continue
        if snap["points"].get(rank, snap["points"].get(str(rank))) is None:
            continue
        ts = datetime.fromisoformat(snap["ts"])
        p = _clamp((ts - tl["start"]).total_seconds() / 60 / tl["total_min"], 0.0, 1.2)
        out[round(p, 6)] = snap["games"]
    return out


def predict_live(comp: dict, history: list[dict] | None = None, calib: dict | None = None) -> dict:
    """Final-threshold prediction for every rank of the competition in progress.

    `calib`: what the app has learned from the history (per-game exponent, each
    model's reliability, scoring-table-to-threshold ratio). See calibration.py.
    """
    import calibration                      # late import: avoids a cycle

    history = history or []
    out = {"ok": True, "ranks": {}, "shape_source": len(history)}
    if not comp.get("end_time") and not is_sealed(comp):
        return {"ok": False, "reason": "Fill in the competition's end time so it can "
                                       "be extrapolated to the finish."}
    priors = calibration.priors_from(calib) if calib else None
    finals = comp.get("finals") or {}
    for rank in comp["ranks"]:
        ps, ys = progress_series(comp, rank)
        exponent = calibration.exponent_for(calib, rank) if calib else None
        if not ps:
            # no reading at all: we can still estimate from the scoring table alone
            res = (calibration.prior_prediction(comp, calib, rank) if calib else None) or {
                "ok": False, "reason": "No data for this rank."}
            if res.get("ok"):
                label = (f"Scoring x {res.get('games')} games"
                         + (" (sealed)" if res.get("sealed") else " (max)"))
                res["models"] = [{"model": "prior", "label": label,
                                  "value": res["value"], "backtest_error_pct": None,
                                  "weight": 1.0}]
                res["current"] = 0
                res["progress"] = 0
                res["remaining_gain"] = res["value"]
        else:
            models = available_models(comp, rank, shape=build_shape(history, rank),
                                      exponent=exponent)
            res = predict_series(ps, ys, models=models, priors=priors)

        # Definitive result known: show the gap between the estimate and reality.
        real = finals.get(rank, finals.get(str(rank)))
        if real is not None:
            real = float(real)
            res["real"] = round(real, 1)
            if res.get("ok") and real > EPS:
                res["error_pct"] = round(100 * (res["value"] - real) / real, 1)
                res["in_range"] = res["low"] <= real <= res["high"]
        out["ranks"][rank] = res
    out["has_finals"] = bool(finals)
    return out


def trajectory(comp: dict, rank: int, steps: int = 12, history=None, calib=None) -> list[dict]:
    """Predicted curve out to the end, for drawing the dotted extension."""
    import calibration

    ps, ys = progress_series(comp, rank)
    if not ps:
        return []
    exponent = calibration.exponent_for(calib, rank) if calib else None
    priors = calibration.priors_from(calib) if calib else None
    models = available_models(comp, rank, shape=build_shape(history or [], rank),
                              exponent=exponent)
    out = []
    last_p = ps[-1]
    for i in range(1, steps + 1):
        p = last_p + (1.0 - last_p) * i / steps
        res = predict_series(ps, ys, target=p, models=models, priors=priors)
        if res.get("ok"):
            out.append({"progress": round(p, 4), "value": res["value"],
                        "low": res["low"], "high": res["high"]})
    return out


# --------------------------------------------------------------------------- #
# Cross-competition prediction
# --------------------------------------------------------------------------- #
def predict_next(history: list[dict], rank: int, recent_n: int = 5) -> dict:
    """Estimate the final threshold of the next competition of the same type.

    `history`: competitions sorted by ascending date, with their snapshots.
    """
    finals = []
    for comp in history:
        if comp.get("end_time") and not is_complete(comp):
            continue  # competition still running: its last threshold isn't final
        value = final_value(comp, rank)
        if value is None:
            continue
        finals.append((comp["start_time"], comp["name"], value))

    finals.sort(key=lambda t: t[0])
    values = [f[2] for f in finals]
    if not values:
        return {"ok": False, "reason": "No history for this rank."}
    if len(values) == 1:
        return {"ok": True, "value": round(values[0], 1), "low": round(values[0] * 0.85, 1),
                "high": round(values[0] * 1.15, 1), "n": 1, "trend_per_comp": 0.0,
                "history": [{"date": d, "name": n, "value": round(v, 1)} for d, n, v in finals],
                "note": "Only one competition in the history: wide margin by default."}

    recent = values[-recent_n:]
    mean_recent = sum(recent) / len(recent)
    median_all = statistics.median(values)

    fit = linreg(list(range(len(values))), values)
    trend_pred = None
    trend = 0.0
    if fit:
        slope, intercept, _ = fit
        trend = slope
        trend_pred = slope * len(values) + intercept

    candidates = [mean_recent, median_all] + ([trend_pred] if trend_pred is not None else [])
    best = sum(candidates) / len(candidates)
    spread = statistics.pstdev(recent) if len(recent) > 1 else abs(best) * 0.1
    margin = max(spread, abs(max(candidates) - min(candidates)) / 2, best * 0.03)

    return {
        "ok": True,
        "value": round(best, 1),
        "low": round(best - margin, 1),
        "high": round(best + margin, 1),
        "n": len(values),
        "mean_recent": round(mean_recent, 1),
        "median": round(median_all, 1),
        "trend_prediction": round(trend_pred, 1) if trend_pred is not None else None,
        "trend_per_comp": round(trend, 2),
        "history": [{"date": d, "name": n, "value": round(v, 1)} for d, n, v in finals],
    }


# --------------------------------------------------------------------------- #
# Scoring table: turning points into a concrete per-game target
# --------------------------------------------------------------------------- #
def placement_points(scoring: dict, rank: int) -> float:
    """Placement points for an end-of-game rank."""
    for row in (scoring or {}).get("placement", []):
        low, high, pts = row[0], row[1], row[2]
        if low <= rank <= high:
            return float(pts)
    return 0.0


def best_placement(scoring: dict) -> tuple[int, float]:
    rows = (scoring or {}).get("placement") or []
    if not rows:
        return 1, 0.0
    top = min(rows, key=lambda r: r[0])
    return int(top[0]), float(top[2])


def rank_for_points(scoring: dict, needed: float) -> int | None:
    """The worst rank that still pays at least `needed` placement points."""
    rows = sorted((scoring or {}).get("placement") or [], key=lambda r: r[0])
    best = None
    for low, high, pts in rows:
        if float(pts) >= needed:
            best = int(high)
    return best


def scoring_kills(scoring: dict, kills: float) -> float:
    """Eliminations actually paid for, once the tournament's cap is applied."""
    cap = (scoring or {}).get("kill_cap")
    return min(float(kills), float(cap)) if cap else float(kills)


def kill_points(scoring: dict, kills: float) -> float:
    """Points these eliminations bring in over ONE game."""
    return scoring_kills(scoring, kills) * float((scoring or {}).get("kill") or 0)


def pace_hint(scoring: dict, pts_per_game: float, kills: int = 2) -> str:
    """'~ top 12 with 2 elims, or top 6 with no elim' for a required pace."""
    if pts_per_game <= 0:
        return "target already reached"
    kill_pts = float((scoring or {}).get("kill") or 0)
    cap = (scoring or {}).get("kill_cap")
    parts, seen = [], set()
    for k in (kills, 0):
        rank = rank_for_points(scoring, pts_per_game - kill_points(scoring, k))
        label = f"{k} elim" + ("s" if k > 1 else "")
        if rank and rank in seen:
            continue                     # same rank with or without elims: no point repeating it
        if rank:
            seen.add(rank)
            parts.append(f"top {rank} with {label}" if k else f"top {rank} with no elim")
        elif k == kills:
            _, top_pts = best_placement(scoring)
            if not kill_pts:
                # with no elimination points, a win is the absolute maximum
                parts.append("win" if pts_per_game <= top_pts else "out of reach")
            else:
                need = max(0, math.ceil((pts_per_game - top_pts) / kill_pts))
                parts.append(f"out of reach (cap of {cap:g} elims per game)"
                             if cap and need > cap
                             else (f"win + {need} elims" if need else "win"))
    return " or ".join(parts) if parts else "out of reach in one game"


def max_game_score(scoring: dict, kills: int = 8) -> float:
    _, top_pts = best_placement(scoring)
    return top_pts + kill_points(scoring, kills)


# --------------------------------------------------------------------------- #
# Personal tracking: where I stand against the thresholds
# --------------------------------------------------------------------------- #
def estimate_rank(points: float, thresholds: dict) -> float | None:
    """Rank estimated from the known thresholds, interpolated on a log scale."""
    pairs = sorted(((int(r), float(v)) for r, v in thresholds.items() if v is not None),
                   key=lambda t: t[0])
    if not pairs or points is None:
        return None
    if points >= pairs[0][1]:
        return float(pairs[0][0])
    if points <= pairs[-1][1]:
        return None                       # past the last known threshold
    for (r0, v0), (r1, v1) in zip(pairs, pairs[1:]):
        if v1 <= points <= v0:
            if abs(v0 - v1) < EPS:
                return float(r1)
            w = (v0 - points) / (v0 - v1)
            lo, hi = math.log(max(r0, 1)), math.log(max(r1, 1))
            return round(math.exp(lo + w * (hi - lo)), 0)
    return None


def my_status(comp: dict, live: dict) -> dict:
    """Where I stand: current rank, gap to each top, pace to hold."""
    snaps = [s for s in comp.get("snapshots", []) if s.get("my_points") is not None]
    if not snaps:
        return {"ok": False, "reason": "Enter your own points in a reading to follow "
                                       "your run."}
    last = snaps[-1]
    mine = float(last["my_points"])
    tl = timeline(comp)
    scoring = comp.get("scoring") or {}
    max_games = int(comp.get("max_games") or 10)

    # games already played and games left
    played = last.get("games")
    if not played and tl["total_min"]:
        ts = datetime.fromisoformat(last["ts"])
        p = _clamp((ts - tl["start"]).total_seconds() / 60 / tl["total_min"], 0.0, 1.0)
        played = round(estimated_games(comp, p))
    played = int(played or 0)
    remaining = max(0, max_games - played)

    thresholds = {r: s for r, s in last["points"].items()}
    targets = []
    for rank in comp["ranks"]:
        pred = (live.get("ranks") or {}).get(rank) or {}
        goal = pred.get("real") if pred.get("real") is not None else pred.get("value")
        if goal is None:
            continue
        gap = goal - mine
        per_game = gap / remaining if remaining > 0 else None
        targets.append({
            "rank": rank,
            "goal": round(float(goal), 1),
            "gap": round(gap, 1),
            "per_game": round(per_game, 1) if per_game is not None else None,
            "hint": pace_hint(scoring, per_game) if per_game and per_game > 0 else
                    ("target reached" if gap <= 0 else "no game left"),
            "reached": gap <= 0,
        })

    return {
        "ok": True,
        "my_points": round(mine, 1),
        "ts": last["ts"],
        "games_played": played,
        "games_left": remaining,
        "per_game_so_far": round(mine / played, 1) if played else None,
        "estimated_rank": estimate_rank(mine, thresholds),
        "targets": targets,
        "max_game_score": max_game_score(scoring),
    }
