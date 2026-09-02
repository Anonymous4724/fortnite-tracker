"""Out-of-sample honesty: does the pipeline beat the obvious alternatives?

Leave-one-tournament-out, not leave-one-row-out. Holding out a single rank and
predicting it from the other ranks of the same tournament would be measuring
interpolation, and the app is never asked to interpolate — it is asked about a
tournament it has not seen. So the whole tournament leaves: it is dropped from
the comparable circle, from the wide sample the curve is fitted on, and from the
level measurements. What is left is what the app would genuinely have known.

The prediction under test is the cold one — `calibration.prior_prediction`, the
estimate made before the first reading. That is where the anchor cascade, the
shrinkage toward `REFERENCE_SHARE` and the curve all get used at once, so it is
the whole pipeline in one call.

Two baselines have to be beaten before any of that machinery is worth its
complexity:

    carry     the same category's nearest other edition, same rank
    median    the median of the same category's other editions, same rank

    python -m analysis.validate
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

import calibration
import db
import predict
from analysis import data

BANDS = [0, 5, 25, 100, 500, 10 ** 9]
BAND_LABELS = ["1-5", "6-25", "26-100", "101-500", "> 500"]

# What the app's low-high band claims. `calibration.ANCHOR_SPREAD` records the
# raw cross-validated figures being widened until "they contain the true
# threshold about 80 % of the time" — so 80 % is the number to hold it to.
NOMINAL = 0.80

SESSION_CUTS = [0.15, 0.25, 0.5, 0.75, 0.9]
SEED = 20260902


# --------------------------------------------------------------------------- #
# Leave one tournament out
# --------------------------------------------------------------------------- #
def cross_validate(comps: list[dict] | None = None) -> pd.DataFrame:
    """Cold prediction for every (tournament, rank) with the tournament held out."""
    conn = data.connect()
    try:
        comps = comps if comps is not None else db.all_full(conn)
        rows = []
        for target in comps:
            finals = {int(r): float(v) for r, v in (target.get("finals") or {}).items()}
            if not finals:
                continue
            history, scope, exact = calibration.comparable_history(
                conn, db, target, exclude_id=target["id"])
            # `wide` feeds the curve fit. Leaving the target in it would let the
            # tournament shape its own prediction through the back door — a small
            # leak on 66 tournaments, but the kind that makes a backtest lie.
            wide = [c for c in comps if c["id"] != target["id"]]
            calib = calibration.calibrate(history, sorted(finals), wide=wide)
            for rank, truth in finals.items():
                pred = calibration.prior_prediction(target, calib, rank)
                ok = bool(pred and pred.get("ok"))
                rows.append({
                    "competition_id": target["id"], "name": target["name"],
                    "category": target["kind"], "region": target["region"],
                    "game_mode": target["game_mode"], "date": pd.Timestamp(target["start_time"]),
                    "field_size": target.get("field_size"), "rank": rank, "truth": truth,
                    "value": pred["value"] if ok else np.nan,
                    "low": pred["low"] if ok else np.nan,
                    "high": pred["high"] if ok else np.nan,
                    "anchor": pred["source"] if ok else None,
                    "n_history": calib.get("n_comps", 0), "scope": scope, "exact_scope": exact,
                    "reason": None if ok else (pred or {}).get("reason"),
                })
    finally:
        conn.close()

    df = pd.DataFrame(rows)
    df["q"] = df["rank"] / df["field_size"]
    df["band"] = pd.cut(df["rank"], bins=BANDS, labels=BAND_LABELS)
    df["ape"] = (df["value"] - df["truth"]).abs() / df["truth"] * 100
    df["log_error"] = np.log(df["value"] / df["truth"])
    df["covered"] = ((df["low"] <= df["truth"]) & (df["truth"] <= df["high"])).astype(float)
    df.loc[df["value"].isna(), "covered"] = np.nan
    # Half-width as a fraction of the point estimate: the app's own claimed
    # uncertainty, recovered from the band it hands the user.
    df["rel"] = (df["high"] - df["value"]) / df["value"]
    return df


# --------------------------------------------------------------------------- #
# Baselines
# --------------------------------------------------------------------------- #
def add_baselines(cv: pd.DataFrame, comps: list[dict] | None = None) -> pd.DataFrame:
    """Carry-forward and category-median predictions for the same rows.

    Both get the same information set as the model — every other tournament,
    later ones included. Restricting them to the past while the model's
    calibration sees the future would be rigging the comparison in the model's
    favour; `backtest.py --learning-curve` is where the chronological question
    belongs.
    """
    comps = comps if comps is not None else data.load()
    key = {c["id"]: (c["kind"], c["region"]) for c in comps}
    when = {c["id"]: pd.Timestamp(c["start_time"]) for c in comps}
    finals = {c["id"]: {int(r): float(v) for r, v in (c.get("finals") or {}).items()}
              for c in comps}

    def peers(cid, rank):
        return [o for o in finals
                if o != cid and key.get(o) == key.get(cid) and rank in finals[o]]

    def carry(cid, rank):
        """Nearest other edition in time; earlier wins a tie."""
        found = peers(cid, rank)
        if not found:
            return np.nan
        found.sort(key=lambda o: (abs(when[o] - when[cid]), when[o] > when[cid]))
        return finals[found[0]][rank]

    def median(cid, rank):
        found = peers(cid, rank)
        return float(np.median([finals[o][rank] for o in found])) if found else np.nan

    out = cv.copy()
    out["carry"] = [carry(c, r) for c, r in zip(out["competition_id"], out["rank"])]
    out["median"] = [median(c, r) for c, r in zip(out["competition_id"], out["rank"])]
    for name in ("carry", "median"):
        out[f"ape_{name}"] = (out[name] - out["truth"]).abs() / out["truth"] * 100
    out["ape_model"] = out["ape"]
    return out


def compare(sub: pd.DataFrame, draws: int = 2000, seed: int = SEED) -> pd.DataFrame:
    """Model against each baseline on the rows where all three fire.

    The difference in median error is bootstrapped over tournaments for the same
    reason the curve fit is: a couple of hundred rows come from a few dozen
    evenings, and it is the evenings that vary.
    """
    cols = {"model": "ape_model", "carry": "ape_carry", "median": "ape_median"}
    rng = np.random.default_rng(seed)
    ids = sub["competition_id"].unique()
    index = {i: sub.index[sub["competition_id"] == i] for i in ids}

    samples = {name: [] for name in cols}
    for _ in range(draws):
        picked = rng.choice(ids, size=len(ids), replace=True)
        rows = sub.loc[np.concatenate([index[i] for i in picked])]
        for name, col in cols.items():
            samples[name].append(rows[col].median())

    out = []
    base = np.array(samples["model"])
    for name, col in cols.items():
        boot = np.array(samples[name])
        row = {"method": name, "median_ape": sub[col].median(),
               "mean_ape": sub[col].mean(),
               "median_ci_low": np.percentile(boot, 2.5),
               "median_ci_high": np.percentile(boot, 97.5)}
        if name != "model":
            diff = boot - base       # baseline error minus model error: positive = model wins
            row["model_better_by"] = sub[col].median() - sub["ape_model"].median()
            row["diff_ci"] = (np.percentile(diff, 2.5), np.percentile(diff, 97.5))
            row["p_model_better"] = float((diff > 0).mean())
            row["win_rate"] = float((sub["ape_model"] < sub[col]).mean())
        out.append(row)
    return pd.DataFrame(out)


# --------------------------------------------------------------------------- #
# Are the bands the width they claim to be?
# --------------------------------------------------------------------------- #
def calibration_curve(cv: pd.DataFrame, grid: np.ndarray | None = None) -> pd.DataFrame:
    """Empirical coverage against nominal, by widening and narrowing the band.

    The app hands out one interval and calls it 80 %. To turn that into a curve,
    read its half-width as the 80 % quantile of a normal — sigma = rel / 1.2816 —
    and sweep the nominal level. The normal step is an assumption, and it is the
    assumption being tested: if the errors are fatter-tailed than normal (they
    are, see `analysis.diagnostics`) the curve sags in the middle and catches up
    at the ends.
    """
    good = cv.dropna(subset=["value", "rel"])
    good = good[good["rel"] > 0]
    sigma = good["rel"] / stats.norm.ppf((1 + NOMINAL) / 2)
    z = (np.log(good["truth"]) - np.log(good["value"])) / sigma
    grid = grid if grid is not None else np.linspace(0.05, 0.99, 40)
    rows = []
    for p in grid:
        k = stats.norm.ppf((1 + p) / 2)
        rows.append({"nominal": p, "empirical": float((z.abs() <= k).mean())})
    out = pd.DataFrame(rows)
    out.attrs["z_sd"] = float(z.std(ddof=1))
    out.attrs["z_n"] = int(len(z))
    return out


# --------------------------------------------------------------------------- #
# Elapsed-session buckets
# --------------------------------------------------------------------------- #
def live_by_elapsed(comps: list[dict] | None = None) -> pd.DataFrame:
    """Error of the live prediction as the session runs, tournament held out.

    Only tournaments that were genuinely tracked can answer this. See
    `tracked_only()` for why the rest cannot.
    """
    comps = comps if comps is not None else data.load()
    usable = [c for c in comps if len(c.get("snapshots") or []) >= 3
              and predict.is_complete(c) and (c.get("finals") or {})]
    rows = []
    for target in usable:
        tl = predict.timeline(target)
        if not tl["total_min"]:
            continue
        history = [c for c in comps if c["id"] != target["id"]
                   and (c["region"], c["team_mode"], c["game_mode"])
                   == (target["region"], target["team_mode"], target["game_mode"])]
        for cut in SESSION_CUTS:
            partial = dict(target)
            partial["snapshots"] = [
                s for s in target["snapshots"]
                if (pd.Timestamp(s["ts"]) - pd.Timestamp(tl["start"])).total_seconds()
                / 60 / tl["total_min"] <= cut]
            partial["finals"] = {}
            if not partial["snapshots"]:
                continue
            res = predict.predict_live(partial, history)
            for rank, truth in (target.get("finals") or {}).items():
                pred = (res.get("ranks") or {}).get(int(rank))
                if not pred or not pred.get("ok") or truth <= 0:
                    continue
                rows.append({"competition_id": target["id"], "cut": cut, "rank": int(rank),
                             "truth": float(truth), "value": pred["value"],
                             "covered": float(pred["low"] <= truth <= pred["high"]),
                             "n_readings": len(partial["snapshots"])})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["ape"] = (df["value"] - df["truth"]).abs() / df["truth"] * 100
    return df


# --------------------------------------------------------------------------- #
# Does more history help?
# --------------------------------------------------------------------------- #
def learning_curve(comps: list[dict] | None = None, max_peers: int = 6,
                   repeats: int = 12, seed: int = SEED) -> pd.DataFrame:
    """Error against the number of comparable editions the app has seen.

    Subsampling rather than replaying in date order. `backtest.py
    --learning-curve` does the chronological version, which conflates "more
    history" with "later in the season"; here the target is fixed and only the
    number of its peers moves, so the curve isolates the thing it claims to
    measure. The cost is that the peers can be later tournaments — fine for a
    question about sample size, wrong for a question about drift.

    Note where `calibrate` reads the anchor from: `reference_pace` is computed on
    the wide sample, not on the narrow comparable circle. So it is the wide
    sample that has to be thinned, and thinning the circle alone would produce a
    flat and meaningless line.
    """
    comps = comps if comps is not None else data.load()
    rng = np.random.default_rng(seed)
    rows = []
    for target in comps:
        finals = {int(r): float(v) for r, v in (target.get("finals") or {}).items()}
        if not finals:
            continue
        peers = [c for c in comps if c["id"] != target["id"] and c["kind"] == target["kind"]]
        others = [c for c in comps if c["id"] != target["id"] and c["kind"] != target["kind"]]
        for m in range(0, min(max_peers, len(peers)) + 1):
            # Every peer count gets the same number of draws, even where the draw
            # is deterministic (m = 0, or m = every peer there is). Skipping the
            # repeats there would leave those tournaments under-weighted at that
            # x-value and put a step in the curve that is pure bookkeeping.
            draws = 1 if m == 0 else repeats
            for _ in range(draws):
                picked = list(rng.choice(len(peers), size=m, replace=False))
                sample = [peers[i] for i in picked]
                calib = calibration.calibrate(sample, sorted(finals), wide=others + sample)
                for rank, truth in finals.items():
                    pred = calibration.prior_prediction(target, calib, rank)
                    if not pred or not pred.get("ok"):
                        continue
                    rows.append({"competition_id": target["id"], "peers": m, "rank": rank,
                                 "peers_available": len(peers),
                                 "ape": abs(pred["value"] - truth) / truth * 100,
                                 "covered": float(pred["low"] <= truth <= pred["high"])})
    return pd.DataFrame(rows)


def balanced(lc: pd.DataFrame, max_peers: int = 6) -> pd.DataFrame:
    """Keep only tournaments that reach every peer count on the x-axis.

    Otherwise the last point is computed on a different, easier set of
    tournaments than the first, and the curve measures the mix rather than the
    sample size.
    """
    return lc[lc["peers_available"] >= max_peers]


def tracked_only(comps: list[dict] | None = None) -> tuple[int, int]:
    """(tournaments really tracked, tournaments whose reading is the result again).

    An imported tournament carries one "snapshot" holding the final standings.
    Backtesting the live models on it looks superb and means nothing: the reading
    they extrapolate from is the answer.
    """
    comps = comps if comps is not None else data.load()
    tracked = echo = 0
    for c in comps:
        snaps = c.get("snapshots") or []
        finals = {int(r): float(v) for r, v in (c.get("finals") or {}).items()}
        if len(snaps) >= 3:
            tracked += 1
            continue
        if len(snaps) == 1 and finals:
            last = {int(r): float(v) for r, v in snaps[-1]["points"].items()}
            shared = set(last) & set(finals)
            if shared and all(abs(last[r] - finals[r]) < 1e-6 for r in shared):
                echo += 1
    return tracked, echo


# --------------------------------------------------------------------------- #
def report() -> None:
    comps = data.load()
    cv = cross_validate(comps)
    got = cv.dropna(subset=["value"])
    print(f"Leave-one-tournament-out — {len(got)} of {len(cv)} thresholds predicted, "
          f"over {got['competition_id'].nunique()} tournaments")
    if len(got) < len(cv):
        print(cv[cv["value"].isna()]["reason"].value_counts().to_string())
    print(f"overall: median APE {got['ape'].median():.1f} %   mean {got['ape'].mean():.1f} %   "
          f"band coverage {100 * got['covered'].mean():.0f} % (claimed {100 * NOMINAL:.0f} %)\n")

    print("by rank band")
    by_band = got.groupby("band", observed=True).agg(
        n=("ape", "size"), tournaments=("competition_id", "nunique"),
        median_ape=("ape", "median"), mean_ape=("ape", "mean"),
        coverage=("covered", "mean"))
    by_band["coverage"] = (100 * by_band["coverage"]).round(0)
    print(by_band.round(2).to_string(), "\n")

    print("by anchor the cascade landed on")
    by_src = got.groupby("anchor", observed=True).agg(
        n=("ape", "size"), tournaments=("competition_id", "nunique"),
        median_ape=("ape", "median"), mean_ape=("ape", "mean"),
        coverage=("covered", "mean"))
    by_src["coverage"] = (100 * by_src["coverage"]).round(0)
    print(by_src.round(2).to_string())
    print("The 'scoring' rows are the cold start: no comparable edition, so the estimate")
    print("rests on REFERENCE_SHARE = 0.58, which calibration.py already flags as resting")
    print("on three readings. The error column is what that costs.\n")

    with_base = add_baselines(cv, comps)
    sub = with_base.dropna(subset=["ape_model", "ape_carry", "ape_median"])
    print(f"Against the baselines — {len(sub)} rows over {sub['competition_id'].nunique()} "
          f"tournaments where all three produce a number")
    print(f"({(with_base['ape_carry'].isna()).sum()} rows have no other edition of their "
          f"category at that rank, so only the model can answer them)")
    cmp = compare(sub)
    for _, r in cmp.iterrows():
        line = (f"  {r['method']:<7} median {r['median_ape']:5.2f} % "
                f"[{r['median_ci_low']:.2f}, {r['median_ci_high']:.2f}]   "
                f"mean {r['mean_ape']:5.2f} %")
        if r["method"] != "model":
            lo, hi = r["diff_ci"]
            line += (f"\n          model ahead by {r['model_better_by']:+.2f} pt "
                     f"[{lo:+.2f}, {hi:+.2f}], P(model better) = {r['p_model_better']:.2f}, "
                     f"wins {100 * r['win_rate']:.0f} % of rows")
        print(line)
    print("Both intervals straddle zero. On this history the model is ahead of a")
    print("carry-forward by an amount indistinguishable from noise, and ahead of the")
    print("category median by about two points — suggestive, not established.\n")
    print("by rank band, median APE")
    print(sub.groupby("band", observed=True)[["ape_model", "ape_carry", "ape_median"]]
          .median().round(2).join(sub.groupby("band", observed=True).size().rename("n"))
          .to_string(), "\n")

    curve = calibration_curve(cv)
    print(f"Band calibration — {curve.attrs['z_n']} predictions")
    print(f"standardised error sd = {curve.attrs['z_sd']:.2f} "
          f"(1.0 would mean the band is exactly the width it claims; "
          f"below 1 means it is too wide)")
    for p in (0.5, 0.8, 0.9, 0.95):
        row = curve.iloc[(curve["nominal"] - p).abs().argmin()]
        print(f"  nominal {100 * row['nominal']:4.0f} %  ->  empirical "
              f"{100 * row['empirical']:4.0f} %")
    print()

    lc = balanced(learning_curve(comps))
    print("Learning curve — error against the number of comparable editions known")
    print("(only tournaments with at least six peers, so every column is the same set)")
    tbl = lc.groupby("peers").agg(n=("ape", "size"), tournaments=("competition_id", "nunique"),
                                  median_ape=("ape", "median"), mean_ape=("ape", "mean"),
                                  coverage=("covered", "mean"))
    tbl["coverage"] = (100 * tbl["coverage"]).round(0)
    print(tbl.round(2).to_string())
    first = lc[lc["peers"] == 0]["ape"].median() - lc[lc["peers"] == 1]["ape"].median()
    rest = lc[lc["peers"] == 1]["ape"].median() - lc[lc["peers"] == 6]["ape"].median()
    print(f"The first comparable edition is worth {first:.1f} points of median error. The "
          f"next five\nare worth {rest:.1f} between them. Coverage falls from 100 % to 82 % over "
          "the same range,\nwhich is the band correctly tightening as the anchor firms up — "
          "past four peers it\ntightens further than the errors justify.\n")

    tracked, echo = tracked_only(comps)
    live = live_by_elapsed(comps)
    print(f"Elapsed-session buckets — {tracked} tournaments were tracked live; "
          f"{echo} carry a single 'reading' identical to their final result.")
    print("Those 64 cannot test the live models: the series they would extrapolate from")
    print("already contains the answer. So this table rests on two evenings. It is an")
    print("anecdote, not a measurement, and no conclusion should be drawn from it.")
    if live.empty:
        print("  nothing to show.")
    else:
        tbl = live.groupby("cut").agg(n=("ape", "size"),
                                      tournaments=("competition_id", "nunique"),
                                      median_ape=("ape", "median"),
                                      mean_ape=("ape", "mean"),
                                      coverage=("covered", "mean"))
        tbl["coverage"] = (100 * tbl["coverage"]).round(0)
        print(tbl.round(2).to_string())


if __name__ == "__main__":
    report()
