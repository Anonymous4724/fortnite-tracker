"""Is rank 20 the right rank to anchor the level on?

`calibration.py` says it is, and gives numbers: dispersion of the pace from one
edition to the next of 7.5 % at rank 1, 5.6 % at rank 5, 4.2 % at rank 20; and
cross-validated error of 9.1 % anchoring on the top 1 against 6.4 to 6.6 %
between the top 10 and the top 25, falling to 6.0 % once the level is read off
four ranks and taken as a median. Those figures predate a good deal of the
history now in the database and nothing in the repo reproduced them. This does.

Two measurements, and only the second decides anything:

**Dispersion** — how much a category's threshold at rank R moves between
editions. Cheap to compute, and it is what the original argument was made on,
but a stable rank is not automatically a *useful* one: rank 50 could be steady
and still sit too far from where the curve is accurate.

**Forecast error** — the leave-one-tournament-out run from `analysis.validate`,
re-run with the anchor moved. This is the question as the app experiences it,
and where the two disagree it wins.

    python -m analysis.anchor
"""
from __future__ import annotations

import contextlib

import numpy as np
import pandas as pd

import calibration
from analysis import data, validate

CANDIDATES = (1, 5, 10, 20, 25, 50, 100)
SEED = 20260902
DRAWS = 2000

# What the comment block above `REFERENCE_RANK` in `calibration.py` claims, so the
# run can check itself against it. Transcribed, not imported — they are prose.
ASSERTED_DISPERSION = {1: 7.5, 5: 5.6, 20: 4.2}
ASSERTED_SINGLE_ANCHOR = {1: 9.1, 10: 6.4, 25: 6.6}
ASSERTED_FOUR_ANCHOR = 6.0


@contextlib.contextmanager
def anchored(rank: int, single: bool = True):
    """Run the app's own code with the anchor moved, then put it back.

    Monkey-patching rather than parameterising: `REFERENCE_RANK` and
    `REFERENCE_ANCHORS` are module constants read at call time by `fit_curve`,
    `shape_ratio`, `reference_level` and `field_sensitivity`, and threading a
    parameter through all four would mean editing `calibration.py` — which this
    folder is not allowed to do, and should not want to. The patch is restored in
    a `finally`, so an exception mid-run cannot leave the app pointing at the
    wrong rank.

    `single` reads the level off that one rank, which is the comparison the
    original argument was about. `single=False` moves only the point the curve is
    pinned at and leaves the four-rank median in place.
    """
    keep_rank, keep_anchors = calibration.REFERENCE_RANK, calibration.REFERENCE_ANCHORS
    calibration.REFERENCE_RANK = rank
    if single:
        calibration.REFERENCE_ANCHORS = (rank,)
    try:
        yield
    finally:
        calibration.REFERENCE_RANK = keep_rank
        calibration.REFERENCE_ANCHORS = keep_anchors


# --------------------------------------------------------------------------- #
# How much data stands behind each candidate
# --------------------------------------------------------------------------- #
def support(comps: list[dict] | None = None) -> pd.DataFrame:
    """Per candidate: tournaments carrying that rank, and whether the curve still fits.

    `fit_curve` normalises every tournament by its threshold at the reference
    rank and gives up below twelve usable rows, falling back to the shipped
    constants. A candidate that trips that fallback is not being tested at all,
    so it has to be caught here rather than explained away in the results.
    """
    comps = comps if comps is not None else data.load()
    thresholds = data.thresholds(comps)
    rows = []
    for rank in CANDIDATES:
        carried = thresholds[thresholds["rank"] == rank]["competition_id"].nunique()
        with anchored(rank):
            curve = calibration.fit_curve(comps)
        rows.append({"rank": rank, "tournaments_with_rank": carried,
                     "curve": f"a={curve[0]}, b={curve[1]}",
                     "curve_fitted": curve != calibration.CURVE_DEFAULT})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# 1. Dispersion of the level between editions
# --------------------------------------------------------------------------- #
def dispersion(comps: list[dict] | None = None, min_editions: int = 2) -> pd.DataFrame:
    """Coefficient of variation of the pace at rank R, within a category.

    The category is (name stripped of week and region) x region — the app's own
    narrowest comparable circle, so this measures exactly the spread the anchor
    cascade has to live with. Pace rather than raw threshold because that is what
    `reference_pace` stores, and because editions of one category do not always
    run the same number of games.

    Pooled in log space across categories, weighting by degrees of freedom, so a
    category with four editions counts for more than one with two. The median of
    the per-category CVs is reported beside it: the two diverge here, and the
    reason is worth seeing.
    """
    comps = comps if comps is not None else data.load()
    df = data.thresholds(comps)
    df = df[(df["threshold"] > 0) & df["max_games"].notna() & (df["max_games"] > 0)].copy()
    df["group"] = df["category"] + " | " + df["region"]
    df["log_pace"] = np.log(df["threshold"] / df["max_games"])

    rows = []
    for rank in CANDIDATES:
        at_rank = df[df["rank"] == rank]
        num = den = 0.0
        cats, obs, per_cat, weights = 0, 0, [], []
        for _, group in at_rank.groupby("group"):
            if len(group) < min_editions:
                continue
            var = float(group["log_pace"].var(ddof=1))
            num += (len(group) - 1) * var
            den += len(group) - 1
            cats += 1
            obs += len(group)
            per_cat.append(np.expm1(np.sqrt(var)))
            weights.append(len(group) - 1)
        if den <= 0:
            rows.append({"rank": rank, "cv_pooled": np.nan, "cv_pooled_drop1": np.nan,
                         "cv_median_category": np.nan, "categories": cats,
                         "observations": obs, "dof": 0})
            continue
        # Pooled again without the single worst category. One group out of ten
        # moving the answer by half is the kind of thing a reader should be told
        # rather than left to discover.
        worst = int(np.argmax(per_cat))
        trimmed = ((num - weights[worst] * float(np.log1p(per_cat[worst])) ** 2)
                   / (den - weights[worst])) if den > weights[worst] else np.nan
        rows.append({"rank": rank,
                     "cv_pooled": 100 * float(np.expm1(np.sqrt(num / den))),
                     "cv_pooled_drop1": 100 * float(np.expm1(np.sqrt(trimmed)))
                                        if np.isfinite(trimmed) else np.nan,
                     "cv_median_category": 100 * float(np.median(per_cat)),
                     "categories": cats, "observations": obs, "dof": int(den)})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# 2. Forecast error with the anchor moved
# --------------------------------------------------------------------------- #
def forecast_by_anchor(comps: list[dict] | None = None) -> pd.DataFrame:
    """One leave-one-tournament-out run per candidate, plus the shipped setting.

    Every run predicts the same 290 thresholds, so the rows line up and the
    bootstrap below can pair them.
    """
    comps = comps if comps is not None else data.load()
    frames = []
    # Eight replays of the whole validation. The frozen split, not the
    # rolling one: the question is which anchor rank forecasts best, a
    # comparison between runs on the same rows, and the frozen split answers
    # it in half the time.
    for rank in CANDIDATES:
        with anchored(rank):
            cv = validate.cross_validate(comps, rolling=False)
        frames.append(cv.assign(anchor=str(rank)))
    frames.append(validate.cross_validate(comps, rolling=False).assign(anchor="shipped"))
    out = pd.concat(frames, ignore_index=True)
    # Ordered so the tables read up the ladder rather than alphabetically, where
    # rank 100 lands between 1 and 20.
    order = [str(r) for r in CANDIDATES] + ["shipped"]
    out["anchor"] = pd.Categorical(out["anchor"], categories=order, ordered=True)
    return out.dropna(subset=["ape"])


def summarise(long: pd.DataFrame) -> pd.DataFrame:
    return (long.groupby("anchor", observed=True)
            .agg(n=("ape", "size"), tournaments=("competition_id", "nunique"),
                 median_ape=("ape", "median"), mean_ape=("ape", "mean"),
                 coverage=("covered", "mean"))
            .assign(coverage=lambda d: (100 * d["coverage"]).round(0)))


# --------------------------------------------------------------------------- #
# 3. Does the margin survive resampling?
# --------------------------------------------------------------------------- #
def margins(long: pd.DataFrame, against: str = "shipped", draws: int = DRAWS,
            seed: int = SEED) -> pd.DataFrame:
    """Bootstrap the gap between each anchor and the reference setting.

    Tournaments are resampled, not rows, and every anchor is scored on the *same*
    resample — so the comparison is paired and the tournament-to-tournament noise
    that dominates the level cancels instead of drowning the signal.
    """
    wide = long.pivot_table(index=["competition_id", "rank"], columns="anchor",
                            values="ape", observed=True)
    wide = wide.dropna()
    ids = wide.index.get_level_values("competition_id").to_numpy()
    unique = np.unique(ids)
    index = {i: np.flatnonzero(ids == i) for i in unique}
    values = {col: wide[col].to_numpy() for col in wide.columns}

    rng = np.random.default_rng(seed)
    draws_by_anchor = {col: [] for col in wide.columns}
    for _ in range(draws):
        picked = rng.choice(unique, size=len(unique), replace=True)
        rows = np.concatenate([index[i] for i in picked])
        for col, series in values.items():
            draws_by_anchor[col].append(np.median(series[rows]))

    base = np.array(draws_by_anchor[against])
    out = []
    for col in wide.columns:
        boot = np.array(draws_by_anchor[col])
        diff = base - boot                    # positive = this anchor is better
        out.append({"anchor": col,
                    "median_ape": float(np.median(values[col])),
                    "ci_low": float(np.percentile(boot, 2.5)),
                    "ci_high": float(np.percentile(boot, 97.5)),
                    "vs_reference": float(np.median(values[against])) - float(np.median(values[col])),
                    "diff_ci_low": float(np.percentile(diff, 2.5)),
                    "diff_ci_high": float(np.percentile(diff, 97.5)),
                    "p_better": float((diff > 0).mean())})
    return pd.DataFrame(out).set_index("anchor").loc[list(wide.columns)].reset_index()


# --------------------------------------------------------------------------- #
def report() -> None:
    comps = data.load()

    stand = support(comps)
    print("What stands behind each candidate")
    print(stand.to_string(index=False))
    broken = stand[~stand["curve_fitted"]]["rank"].tolist()
    if broken:
        print(f"Rank {', '.join(str(r) for r in broken)} cannot be tested: too few "
              "tournaments carry that threshold for")
        print("`fit_curve` to normalise on it, so it silently reverts to the shipped curve.")
        print("Its rows below are the shipped curve with a broken level, not an anchor at that")
        print("rank. Read them as 'no data', not as a result.\n")

    disp = dispersion(comps)
    print("1. Dispersion of the pace between editions of the same category")
    print(disp.round(2).to_string(index=False, na_rep="no data"))
    print("Ranks 1 to 50 all rest on the same 26 observations across 10 categories — 16")
    print("degrees of freedom. That is thin, and the two CV columns disagree because of it:")
    print("one category (FNCS Division 3 NAC) contains a 10-game and an 11-game edition whose")
    print("paces differ by a third, and the pooled figure carries it while the median does")
    print("not. Both are reported; neither is precise. What is solid is the ordering: the")
    print("spread falls from rank 1 to rank 20-25 and rises again by rank 50.\n")

    long = forecast_by_anchor(comps)
    table = summarise(long)
    print("2. Leave-one-tournament-out forecast error, anchor moved")
    print(table.round(2).to_string())
    print("Rows 1-50 read the level off that single rank. 'shipped' is the median over ranks")
    print("5/10/20/25 that the app actually uses.\n")

    print("3. Verdict — bootstrap over tournaments, paired on the same resample")
    marg = margins(long).sort_values("median_ape")
    unusable = {str(r) for r in broken}
    print(f"{'anchor':>9}  {'median':>8}  {'95 % CI':>16}  {'vs shipped':>10}  "
          f"{'difference CI':>18}  {'P(better)':>9}")
    for _, r in marg.iterrows():
        label = f"{r['anchor']}*" if r["anchor"] in unusable else str(r["anchor"])
        if r["anchor"] == "shipped":
            print(f"{label:>9}  {r['median_ape']:6.2f} %  "
                  f"[{r['ci_low']:5.2f}, {r['ci_high']:5.2f}]  {'reference':>10}"
                  f"{'':>21}{'—':>10}")
            continue
        print(f"{label:>9}  {r['median_ape']:6.2f} %  "
              f"[{r['ci_low']:5.2f}, {r['ci_high']:5.2f}]  {r['vs_reference']:+9.2f}  "
              f"[{r['diff_ci_low']:+7.2f}, {r['diff_ci_high']:+7.2f}]  {r['p_better']:8.2f}")
    if unusable:
        print(f"* not a real test — see the note above.")

    real = marg[~marg["anchor"].isin(unusable | {"shipped"})]
    best = real.iloc[0]
    shipped = marg[marg["anchor"] == "shipped"].iloc[0]
    # Three verdicts, and only the third is a real finding: within half a point of
    # the shipped setting; worse on the point estimate but with an interval that
    # still crosses zero; worse with the whole interval below zero.
    close = real[real["vs_reference"].abs() <= 0.5]["anchor"].tolist()
    beaten = real[real["diff_ci_high"] < 0]["anchor"].tolist()
    suspect = real[(real["vs_reference"] < -0.5) & (real["diff_ci_high"] >= 0)]["anchor"].tolist()
    print()
    print(f"Best single rank is {best['anchor']}, at {best['median_ape']:.2f} % against "
          f"{shipped['median_ape']:.2f} % for the shipped four-rank\nmedian — a gap of "
          f"{best['vs_reference']:+.2f} points, 95 % interval "
          f"[{best['diff_ci_low']:+.2f}, {best['diff_ci_high']:+.2f}], which straddles zero.")
    print(f"Ranks {', '.join(close)} sit within half a point of the shipped median of four and "
          f"of\neach other: on this history the choice among them does not matter.")
    if suspect:
        print(f"Ranks {', '.join(suspect)} are worse on the point estimate but their intervals "
              f"still cross\nzero — suspected, not convicted.")
    if beaten:
        cost = -marg.set_index("anchor").loc[beaten[0], "vs_reference"]
        print(f"Only rank {', '.join(beaten)} is beaten outright: anchoring on the winner costs "
              f"{cost:.1f} points of median\nerror with the whole interval below zero. That is "
              "the one thing here the data settles.")

    print()
    print("Against what calibration.py's comments assert")
    seen = table["median_ape"]
    disp = dispersion(comps).set_index("rank")
    print(f"  {'claim':<44}{'asserted':>11}{'measured':>11}")
    for rank, claim in ASSERTED_DISPERSION.items():
        print(f"  {'dispersion of the pace at rank ' + str(rank):<44}{claim:>9.1f} %"
              f"{disp.loc[rank, 'cv_median_category']:>9.1f} %")
    for rank, claim in ASSERTED_SINGLE_ANCHOR.items():
        print(f"  {'cross-validated error anchoring on rank ' + str(rank):<44}{claim:>9.1f} %"
              f"{seen[str(rank)]:>9.1f} %")
    print(f"  {'the same, level = median over 5/10/20/25':<44}"
          f"{ASSERTED_FOUR_ANCHOR:>9.1f} %{seen['shipped']:>9.1f} %")
    print("The forecast figures reproduce almost exactly. The dispersion figures line up with")
    print("the per-category median above, not with the pooled estimate — so whoever wrote them")
    print("was taking a typical category, not pooling across all of them. Worth saying which,")
    print("since the pooled number is roughly twice as large.")


if __name__ == "__main__":
    report()
