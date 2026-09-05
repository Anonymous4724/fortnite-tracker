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

import json
import os
import random
from datetime import date


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

# How many tournaments are actually replayed, and how many feed the curve fit
# inside each replay.
#
# Leave-one-out over ten thousand tournaments is not ten thousand times more
# informative than over six hundred — it is the same estimate with a standard
# error a quarter the size, bought at four hundred times the compute. The
# binding cost is `calibration.fit_curve`, which sweeps the exponent over every
# threshold in the wide sample: a minute a call, once per held-out tournament,
# is thirty hours.
#
# So two samples are drawn, and they are drawn *disjoint*: the tournaments being
# predicted are removed from the pool the shape is fitted on. That buys the
# expensive half back — one fit for the whole run instead of one per target —
# without the leak that hoisting it would otherwise create, because no target
# can appear in a fit that no target is in. It also makes the backtest
# pessimistic rather than optimistic: production fits the shape on everything,
# this fits it on WIDE_CAP.
#
# Below SAMPLE tournaments nothing is sampled and the shape is refitted for each
# held-out tournament, exactly as before — which is what reproduces the figures
# measured on the original 66.
SAMPLE = 600

# Forecasting split by default: hold out the newest tournaments, train on what
# came before. The random split is kept as --random for the learning curve and
# for comparison, but it is not how a forecast should be scored.
CHRONO = True
# Everything that is not a target. Capping this at fifteen hundred was a mistake
# that ran for a whole afternoon: the model read its level, field and shape
# tables from the pool while the carry-forward and median baselines read every
# tournament in the database, so the comparison handed them five times the
# history and then reported the model losing. The shape table needs SHAPE_MIN
# editions of a category before it will speak, and a category with four editions
# across seven thousand tournaments rarely has three inside fifteen hundred — so
# the model was being measured with its own best machinery switched off.
#
# The pool is one call, not one per target, so its size costs a single fit. There
# was never a reason to make it small.
WIDE_CAP = 10 ** 9

# The learning curve replays each tournament eighty-odd times — seven peer counts
# by twelve draws — so it gets a smaller sample of its own.
LEARNING_SAMPLE = 120


# --------------------------------------------------------------------------- #
# Leave one tournament out
# --------------------------------------------------------------------------- #
def split(comps: list[dict], sample: int = SAMPLE, wide_cap: int = WIDE_CAP,
          seed: int = SEED, chrono: bool = CHRONO) -> tuple[list[dict], list[dict] | None]:
    """(tournaments to predict, pool to fit the shape on) — disjoint, or (all, None).

    Returning None for the pool is the small-database path: refit per target,
    which is both affordable and exactly what leave-one-out asks for.

    `chrono` is the forecasting split: the most recent tournaments are held out
    and everything before them is the pool. Nothing in the pool postdates
    anything in the targets. That is the only arrangement that measures what
    the app does — forecast tonight from what has already happened — and it
    removes a bias that ran both ways in the random split: carry-forward got to
    read next week's edition, while the model's "most recent editions" were the
    newest in the database rather than the newest before the target.
    """
    if len(comps) <= sample:
        return comps, None
    if chrono:
        dated = sorted(comps, key=lambda c: str(c.get("start_time") or ""))
        return dated[-sample:], dated[:-sample]
    rng = random.Random(seed)
    order = rng.sample(range(len(comps)), len(comps))
    targets = [comps[i] for i in order[:sample]]
    pool = [comps[i] for i in order[sample:sample + wide_cap]]
    return targets, pool


def information_note(comps, targets, pool) -> str:
    """What the model was allowed to see, said out loud next to the baselines.

    The baselines read every tournament in the database. If the model reads
    fewer, the comparison below is not about the model and the report has to say
    so rather than let a handicap read as a result.
    """
    if pool is None or len(pool) >= len(comps) - len(targets):
        return "Model and baselines both read every tournament except the held-out one."
    return (f"WARNING: the model reads {len(pool)} tournaments, the baselines read "
            f"{len(comps)}.\n  The comparison is not like for like — raise WIDE_CAP.")


def cross_validate(comps: list[dict] | None = None, progress: bool = True) -> pd.DataFrame:
    """Cold prediction for every (tournament, rank) with the tournament held out."""
    conn = data.connect()
    try:
        comps = comps if comps is not None else db.all_full(conn)
        targets, pool = split(comps)
        # One fit for the run when the pool holds no target, one fit per target
        # otherwise. See the note on SAMPLE.
        shared = calibration.broad_stats(pool) if pool is not None else None
        rows = []
        for done, target in enumerate(targets):
            if progress and pool is not None and done and not done % 50:
                print(f"  ... {done}/{len(targets)} tournaments replayed", flush=True)
            # A threshold of zero is not something a multiplicative model can be
            # right or wrong about, and dividing by it turns every mean in this
            # file into inf. Dropped here and counted in the report, rather than
            # silently carried into an average.
            finals = {int(r): float(v) for r, v in (target.get("finals") or {}).items()
                      if float(v) > 0}
            if not finals:
                continue
            history, scope, exact = calibration.comparable_history(
                conn, db, target, exclude_id=target["id"])
            if shared is not None:
                calib = calibration.calibrate(history, sorted(finals), broad=shared)
            else:
                # `wide` feeds the curve fit. Leaving the target in it would let
                # the tournament shape its own prediction through the back door —
                # a small leak on 66 tournaments, but the kind that makes a
                # backtest lie.
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
def add_baselines(cv: pd.DataFrame, comps: list[dict] | None = None,
                  history: list[dict] | None = None) -> pd.DataFrame:
    """Carry-forward and category-median predictions for the same rows.

    `history` is what the baselines may read. In the chronological split it is
    the pool — the past — and carry-forward becomes what it is in real life, the
    previous edition. Given everything, it becomes the *nearest* edition in
    either direction, which reads next week's result to forecast this week's:
    measured, that alone is worth a point of median error nobody can have on
    the night.
    """
    comps = comps if comps is not None else data.load()
    history = history if history is not None else comps
    key = {c["id"]: (c["kind"], c["region"]) for c in comps}
    when = {c["id"]: pd.Timestamp(c["start_time"]) for c in comps}
    readable = {c["id"] for c in history}
    finals = {c["id"]: {int(r): float(v) for r, v in (c.get("finals") or {}).items()
                        if float(v) > 0}
              for c in comps if c["id"] in readable}

    # Bucketed on (category, rank) up front. Scanning every tournament for every
    # row is a hundred thousand rows against ten thousand tournaments — a billion
    # comparisons for an answer that a dictionary hands over.
    bucket: dict[tuple, list[int]] = {}
    for cid, table in finals.items():
        for rank in table:
            bucket.setdefault((key.get(cid), rank), []).append(cid)

    def peers(cid, rank):
        return [o for o in bucket.get((key.get(cid), rank), ()) if o != cid]

    def carry(cid, rank):
        """Nearest other edition in time; earlier wins a tie."""
        found = peers(cid, rank)
        if not found:
            return np.nan
        return finals[min(found, key=lambda o: (abs(when[o] - when[cid]),
                                                when[o] > when[cid]))][rank]

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
    # One groupby instead of one boolean mask per tournament: the mask version is
    # `len(ids)` full passes over the frame, which is an hour once there are ten
    # thousand tournaments in it.
    groups = sub.groupby("competition_id", sort=False).indices
    ids = np.array(list(groups))
    positions = [groups[i] for i in ids]
    values = {name: sub[col].to_numpy() for name, col in cols.items()}

    samples = {name: [] for name in cols}
    for _ in range(draws):
        picked = rng.integers(0, len(ids), size=len(ids))
        rows = np.concatenate([positions[i] for i in picked])
        for name in cols:
            samples[name].append(float(np.median(values[name][rows])))

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
    targets, pool = split(comps, sample=LEARNING_SAMPLE, wide_cap=WIDE_CAP, seed=seed)
    background = pool if pool is not None else comps
    # Every draw rebuilds the level tables, and only those: the exponent sweep is
    # fitted once. Six peers moving in a sample of fifteen hundred cannot shift
    # the shape, and refitting it eighty times per tournament is what turns this
    # function from a minute into a day.
    base = calibration.broad_stats(background)
    curve = base["curve"]
    # `kind` is indexed once; the scan version is len(comps) per target.
    by_kind: dict = {}
    for c in comps:
        by_kind.setdefault(c["kind"], []).append(c)
    rows = []
    for target in targets:
        finals = {int(r): float(v) for r, v in (target.get("finals") or {}).items()
                  if float(v) > 0}
        if not finals:
            continue
        peers = [c for c in by_kind.get(target["kind"], ()) if c["id"] != target["id"]]
        others = [c for c in background
                  if c["id"] != target["id"] and c["kind"] != target["kind"]]
        for m in range(0, min(max_peers, len(peers)) + 1):
            # Every peer count gets the same number of draws, even where the draw
            # is deterministic (m = 0, or m = every peer there is). Skipping the
            # repeats there would leave those tournaments under-weighted at that
            # x-value and put a step in the curve that is pure bookkeeping.
            draws = 1 if m == 0 else repeats
            for _ in range(draws):
                picked = list(rng.choice(len(peers), size=m, replace=False))
                sample = [peers[i] for i in picked]
                # Level, field and shape for this target's own family, rebuilt
                # from the drawn peers alone — which is the whole experiment. The
                # rest of the wide sample cannot change when the peers are
                # thinned, so it is carried over rather than recomputed.
                broad = calibration.broad_for(base, sample, target, curve=curve)
                calib = calibration.calibrate(sample, sorted(finals), broad=broad)
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
def data_health(comps: list[dict]) -> list[str]:
    """Complaints about the training data itself, before anything is measured.

    Every number below this line is conditional on the data being what it claims
    to be, and the expensive way to discover otherwise is to publish an error
    rate and have someone ask why the model loses to carrying last week forward.
    So the cheap checks run first and say what they find.
    """
    out = []
    zeros = sum(1 for c in comps for v in (c.get("finals") or {}).values() if float(v) <= 0)
    total = sum(len(c.get("finals") or {}) for c in comps)
    if zeros:
        out.append(f"{zeros} of {total} stored thresholds are zero or negative and are "
                   f"dropped:\n    a multiplicative model cannot express them and every "
                   f"relative error against\n    one is infinite.")

    # The fingerprint of a field size that records the harvest rather than the
    # event. Same test the exporter refuses on, defined once in db.
    found = db.suspicious_fields(comps)
    if found:
        common, share = found
        out.append(
            f"{100 * share:.0f} % of tournaments record a field of exactly {common},\n"
            f"    which is also the largest in the database. That is what a harvest\n"
            f"    stopped after a fixed number of pages looks like, and the model knows\n"
            f"    a rank only as q = rank / field, so every number below is meaningless.\n"
            f"    Re-derive the field from each leaderboard's own page count — the pages\n"
            f"    are already on disk, nothing is downloaded again:\n"
            f"        python harvest_osirion.py --rebuild")

    if calibration.LAST_FIT.get("railed") is not None:
        out.append(f"the fitted exponent b stopped at {calibration.LAST_FIT['railed']}, "
                   f"the edge of the search grid.\n    The optimum was never bracketed — "
                   f"the shape below is the boundary, not a fit.")
    return out


SUMMARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validation.json")


def write_summary(summary: dict) -> None:
    """The headline numbers, for the model to carry and the page to show.

    The predictor used to quote an error and a coverage typed into its source
    by hand, and both outlived the model they described. Now they travel with
    the model: export_model.py copies this file into model.json as `quality`,
    and the page prints whatever is there — or a dash when nothing is.
    """
    with open(SUMMARY_PATH, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    print(f"\nWrote {os.path.relpath(SUMMARY_PATH)} — the export carries it into model.json.")


def report() -> None:
    comps = data.load()
    targets, pool = split(comps)
    if pool is not None and CHRONO:
        first = min(str(t.get("start_time") or "") for t in targets)[:10]
        print(f"{len(comps)} tournaments in the database. Forecasting the newest "
              f"{len(targets)} — every tournament from {first} on —\nfrom the "
              f"{len(pool)} that came before. Nothing sees the future: not the model, "
              f"not the baselines.\n")
    elif pool is not None:
        print(f"{len(comps)} tournaments in the database. Replaying a random "
              f"{len(targets)} of them,\nwith the curve fitted on a disjoint "
              f"{len(pool)} so no tournament is in its own fit.\n"
              f"Seed {SEED} — the same sample every run.\n")
    cv = cross_validate(comps)
    complaints = data_health(comps)
    note = information_note(comps, targets, pool)
    if note.startswith("WARNING"):
        complaints.insert(0, note)
    if complaints:
        print("=" * 70)
        print("  BEFORE READING ANY NUMBER BELOW")
        print("=" * 70)
        for line in complaints:
            print(f"  - {line}")
        print("=" * 70 + "\n")

    got = cv.dropna(subset=["value"])
    summary = {
        "generated": date.today().isoformat(),
        "split": "chronological" if (pool is not None and CHRONO) else "random",
        "targets": int(len(targets)),
        "pool": int(len(pool)) if pool is not None else int(len(comps)),
        "from": (min(str(t.get("start_time") or "") for t in targets)[:10]
                 if (pool is not None and CHRONO) else None),
        "thresholds": int(len(got)),
        "median_ape": round(float(got["ape"].median()), 2),
        "mean_ape": round(float(got["ape"].mean()), 2),
        "coverage": round(float(got["covered"].mean()), 3),
        "nominal": NOMINAL,
    }
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
    # The cold start is every row the cascade answered without an edition of
    # the cup: the scoring table, thin or not, and the closed-lobby reading,
    # which is the scoring table's counterpart for a final in one lobby.
    cold = got[got["anchor"].astype(str).str.startswith("scoring")
               | (got["anchor"] == "closed lobby")]
    summary["cold_median_ape"] = round(float(cold["ape"].median()), 2) if len(cold) else None
    summary["cold_share"] = round(len(cold) / len(got), 3) if len(got) else 0.0
    summary["lobby_median_ape"] = (round(float(got.loc[got["anchor"] == "closed lobby", "ape"].median()), 2)
                                   if (got["anchor"] == "closed lobby").any() else None)
    summary["lobby_rows"] = int((got["anchor"] == "closed lobby").sum())
    if "scoring" in by_src.index:
        print("The 'scoring' rows are the cold start: no comparable edition, so the "
              "estimate\nrests on REFERENCE_SHARE = "
              f"{calibration.REFERENCE_SHARE}. The error column is what that costs.")
    else:
        print("No cold starts in this sample: every tournament drawn had a comparable")
        print("edition to anchor on, so REFERENCE_SHARE was never reached.")
    print()

    with_base = add_baselines(cv, comps, history=pool if pool is not None else comps)
    sub = with_base.dropna(subset=["ape_model", "ape_carry", "ape_median"])
    print(f"Against the baselines — {len(sub)} rows over {sub['competition_id'].nunique()} "
          f"tournaments where all three produce a number")
    print(f"  {information_note(comps, targets, pool)}")
    print(f"({(with_base['ape_carry'].isna()).sum()} rows have no other edition of their "
          f"category at that rank, so only the model can answer them)")
    cmp = compare(sub)
    summary["baselines"] = {
        str(r["method"]): {"median_ape": round(float(r["median_ape"]), 2),
                           "model_better_by": (None if r["method"] == "model"
                                               else round(float(r["model_better_by"]), 2))}
        for _, r in cmp.iterrows()}
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
    # Read the verdict off the intervals. Written as prose it was true of 66
    # tournaments and false of ten thousand, and a backtest that narrates a
    # result it did not measure is worse than one that prints nothing.
    for _, r in cmp.iterrows():
        if r["method"] == "model":
            continue
        lo, hi = r["diff_ci"]
        if lo > 0:
            verdict = (f"beats {r['method']} by {r['model_better_by']:.2f} points, whole "
                       f"interval above zero")
        elif hi < 0:
            verdict = (f"LOSES to {r['method']} by {-r['model_better_by']:.2f} points, whole "
                       f"interval below zero")
        else:
            verdict = (f"is {abs(r['model_better_by']):.2f} points "
                       f"{'ahead of' if r['model_better_by'] > 0 else 'behind'} "
                       f"{r['method']}, interval straddles zero — not established")
        print(f"  -> the model {verdict}.")
    print()
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
    if lc.empty:
        print("  no tournament has six comparable editions; nothing to plot.\n")
    else:
        tbl = lc.groupby("peers").agg(
            n=("ape", "size"), tournaments=("competition_id", "nunique"),
            median_ape=("ape", "median"), mean_ape=("ape", "mean"),
            coverage=("covered", "mean"))
        tbl["coverage"] = (100 * tbl["coverage"]).round(0)
        print(tbl.round(2).to_string())
        # Read off the table rather than typed underneath it: these three numbers
        # were once prose, and prose does not get recomputed when the database
        # grows by two orders of magnitude.
        at = {m: lc[lc["peers"] == m] for m in (0, 1, 6)}
        if all(not at[m].empty for m in at):
            first = at[0]["ape"].median() - at[1]["ape"].median()
            rest = at[1]["ape"].median() - at[6]["ape"].median()
            wide, tight = 100 * at[0]["covered"].mean(), 100 * at[6]["covered"].mean()
            print(f"The first comparable edition is worth {first:.1f} points of median "
                  f"error. The next five\nare worth {rest:.1f} between them. Coverage "
                  f"goes from {wide:.0f} % to {tight:.0f} % over the same range,\n"
                  "which is the band tightening as the anchor firms up.\n")

    tracked, echo = tracked_only(comps)
    live = live_by_elapsed(comps)
    print(f"Elapsed-session buckets — {tracked} tournaments were tracked live; "
          f"{echo} carry a single 'reading' identical to their final result.")
    print(f"Those {echo} cannot test the live models: the series they would extrapolate")
    print(f"from already contains the answer. So this table rests on {tracked} "
          f"evening{'s' if tracked != 1 else ''}, which is an")
    print("anecdote rather than a measurement.")
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

    write_summary(summary)


if __name__ == "__main__":
    report()
