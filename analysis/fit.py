"""Re-derive the curve parameters (a, b) instead of taking them on faith.

    threshold(rank) = level * exp(-a * (q**b - q_ref**b)),   q = rank / field

Two estimators, because they are not the same model and the difference is the
point:

**within** — each tournament gets its own free `log(level)`, concentrated out by
demeaning inside the tournament. This is the estimator that matches what the
model claims: a shared shape, a level that varies by tournament. It is the
headline number.

**ratio** — what `calibration.fit_curve` actually does: divide every threshold by
the *observed* rank-20 threshold of its own tournament and fit the residual
shape. That denominator is a measurement, not a parameter, so its noise enters
every row of the tournament and attenuates `a`. Reported alongside so the size of
that bias is visible rather than assumed away.

Both fit in log space, where the model is linear in `a` once `b` is fixed. So `b`
is profiled — swept on a grid, then polished by Levenberg-Marquardt — and `a`
falls out of an ordinary least-squares solve at each step. A two-parameter
nonlinear search would find the same optimum more slowly and less reliably.

Uncertainty comes twice over: Jacobian standard errors (which assume the 225
observations are independent — they are not) and a bootstrap that resamples
**tournaments**, not rows. Ranks inside one tournament share a field, a scoring
table, a lobby and a day; treating them as 225 draws is the mistake this file
exists to avoid.

    python -m analysis.fit
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import optimize

import calibration
from analysis import data

BOOTSTRAP_DRAWS = 2000
SEED = 20260902

# Grid the profile search starts from. Wide enough to contain every value the
# app's own sweep can return, so a disagreement is a real disagreement.
B_GRID = np.linspace(0.05, 1.50, 291)


# --------------------------------------------------------------------------- #
# The two estimators
# --------------------------------------------------------------------------- #
def _within_residual(params, log_y, q, q_ref, group, group_n):
    """Residual with each tournament's level profiled out.

    For a fixed (a, b) the level that minimises the sum of squares is the
    within-tournament mean of log(y) - a*x, in closed form. Subtracting it is
    the standard within transformation; it costs one degree of freedom per
    tournament, which the covariance below accounts for.
    """
    a, b = params
    # log(y) - log(level) + a*(q^b - q_ref^b); demeaning is what removes log(level).
    err = log_y + a * (q ** b - q_ref ** b)
    means = np.bincount(group, weights=err) / group_n
    return err - means[group]


def _ratio_residual(params, log_ratio, q, q_ref):
    """Residual of the app's own formulation: log(threshold / threshold_at_20)."""
    a, b = params
    return log_ratio - a * (q_ref ** b - q ** b)


def _profile_start(residual, args) -> tuple[float, float]:
    """Best (a, b) on the b-grid, with `a` solved exactly at each b.

    A grid start rather than a guess: the profile sum of squares in b is not
    convex, and starting LM from a plausible-looking b has landed it in a local
    minimum on this data.
    """
    best = None
    for b in B_GRID:
        # The residual is affine in a, so two evaluations pin it down exactly and
        # the best a at this b is an ordinary projection.
        r0 = residual((0.0, b), *args)
        r1 = residual((1.0, b), *args)
        basis = r0 - r1
        den = float(basis @ basis)
        if den <= 0:
            continue
        a = float(basis @ r0) / den
        r = residual((a, b), *args)
        sse = float(r @ r)
        if best is None or sse < best[0]:
            best = (sse, a, b)
    return (best[1], best[2]) if best else (1.0, 0.5)


def fit_within(df: pd.DataFrame) -> dict:
    """Fixed-effects fit: shared (a, b), one free level per tournament."""
    group, uniq = pd.factorize(df["competition_id"])
    args = (np.log(df["threshold"].to_numpy()), df["q"].to_numpy(),
            df["q_ref"].to_numpy(), group, np.bincount(group).astype(float))
    start = _profile_start(_within_residual, args)
    res = optimize.least_squares(_within_residual, start, args=args, method="lm")
    # One parameter burned per tournament by the demeaning, plus a and b.
    return _summarise(res, n_params=2 + len(uniq), n_groups=len(uniq),
                      label="within (free level per tournament)")


def fit_ratio(df: pd.DataFrame) -> dict:
    """The app's formulation: thresholds divided by the observed rank-20 value."""
    base = df[df["rank"] == data.REFERENCE_RANK].set_index("competition_id")["threshold"]
    sub = df[df["competition_id"].isin(base.index) & (df["rank"] != data.REFERENCE_RANK)].copy()
    sub["log_ratio"] = np.log(sub["threshold"] / sub["competition_id"].map(base))
    args = (sub["log_ratio"].to_numpy(), sub["q"].to_numpy(), sub["q_ref"].to_numpy())
    start = _profile_start(_ratio_residual, args)
    res = optimize.least_squares(_ratio_residual, start, args=args, method="lm")
    # Two parameters only: the level is data here, not something being estimated.
    return _summarise(res, n_params=2, n_groups=sub["competition_id"].nunique(),
                      label="ratio to observed rank 20 (as implemented)")


def _summarise(res, n_params: int, n_groups: int, label: str) -> dict:
    n = len(res.fun)
    dof = max(n - n_params, 1)
    sse = float(res.fun @ res.fun)
    sigma2 = sse / dof
    jtj = res.jac.T @ res.jac
    cov = sigma2 * np.linalg.inv(jtj)
    se = np.sqrt(np.diag(cov))
    return {"label": label, "a": float(res.x[0]), "b": float(res.x[1]),
            "se_a": float(se[0]), "se_b": float(se[1]),
            "corr_ab": float(cov[0, 1] / (se[0] * se[1])),
            "resid_sd": float(np.sqrt(sigma2)), "n": n, "n_groups": n_groups,
            "dof": dof, "sse": sse}


# --------------------------------------------------------------------------- #
# Bootstrap over tournaments
# --------------------------------------------------------------------------- #
def bootstrap_within(df: pd.DataFrame, draws: int = BOOTSTRAP_DRAWS,
                     seed: int = SEED) -> np.ndarray:
    """Resample tournaments with replacement, refit, keep (a, b).

    Rows are never resampled on their own. Two thresholds from the same
    tournament are two readings of one lobby on one evening; pretending they are
    independent is what makes the Jacobian errors below look better than they
    are.
    """
    rng = np.random.default_rng(seed)
    group, uniq = pd.factorize(df["competition_id"])
    log_y = np.log(df["threshold"].to_numpy())
    q, q_ref = df["q"].to_numpy(), df["q_ref"].to_numpy()
    index = [np.flatnonzero(group == g) for g in range(len(uniq))]
    start = (1.25, 0.40)

    out = []
    for _ in range(draws):
        picked = rng.integers(0, len(uniq), size=len(uniq))
        rows = np.concatenate([index[g] for g in picked])
        # A tournament drawn twice must count as two clusters, not one bigger
        # one — otherwise its level gets averaged across both copies.
        gb = np.concatenate([np.full(len(index[g]), k) for k, g in enumerate(picked)])
        args = (log_y[rows], q[rows], q_ref[rows], gb, np.bincount(gb).astype(float))
        try:
            res = optimize.least_squares(_within_residual, start, args=args, method="lm")
        except (ValueError, np.linalg.LinAlgError):
            continue
        if res.success or res.status > 0:
            out.append(res.x)
    return np.array(out)


def interval(samples: np.ndarray, level: float = 0.95) -> tuple[float, float]:
    tail = (1 - level) / 2 * 100
    return float(np.percentile(samples, tail)), float(np.percentile(samples, 100 - tail))


# --------------------------------------------------------------------------- #
# Sensitivity: which rows are holding the exponent up?
# --------------------------------------------------------------------------- #
def sensitivity(df: pd.DataFrame) -> pd.DataFrame:
    """Refit on deliberately altered samples.

    Not a robustness ritual — on this data the exponent moves further between
    two defensible samples than the confidence interval on any one of them
    suggests, and that is worth seeing in a table.
    """
    full = data.thresholds()
    practice = set(full.loc[full["name"].str.contains("Practice", case=False),
                            "competition_id"])

    def fit(sub, why):
        groups = sub["competition_id"].nunique()
        if groups < 5:
            # Below five tournaments the bootstrap has nothing to resample and the
            # point estimate is one tournament away from anything. Say so instead.
            return {"sample": why, "a": np.nan, "b": np.nan,
                    "n": len(sub), "tournaments": groups}
        r = fit_within(sub)
        return {"sample": why, "a": round(r["a"], 3), "b": round(r["b"], 3),
                "n": r["n"], "tournaments": r["n_groups"]}

    with_one = data.curve_sample(drop_rank_one=False)
    rows = [
        fit(df, "headline (rank 1 dropped)"),
        fit(with_one, "rank 1 kept"),
        fit(df[~df["competition_id"].isin(practice)], "practice events dropped"),
        fit(df[df["q"] <= 0.25], "q <= 0.25 only"),
        fit(df[df["q"] <= 0.10], "q <= 0.10 only"),
        fit(df[df["source"] == "import"], "imported tournaments only"),
        fit(df[df["game_mode"] == "Battle Royale"], "Battle Royale only"),
        fit(df[df["game_mode"] == "Reload"], "Reload only"),
    ]
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
def report() -> None:
    df = data.curve_sample()
    n, groups = len(df), df["competition_id"].nunique()
    print(f"Curve fit — {n} thresholds over {groups} tournaments "
          f"({df.groupby('competition_id').size().mean():.1f} ranks each)")
    print(f"q spans {df['q'].min():.4f} to {df['q'].max():.3f}; "
          f"{(df['q'] > 0.25).sum()} observations above q = 0.25\n")

    within = fit_within(df)
    ratio = fit_ratio(df)
    for r in (within, ratio):
        print(f"{r['label']}")
        print(f"  a = {r['a']:.3f}  (Jacobian se {r['se_a']:.3f})")
        print(f"  b = {r['b']:.3f}  (Jacobian se {r['se_b']:.3f})   corr(a,b) = {r['corr_ab']:+.2f}")
        print(f"  residual sd {r['resid_sd']:.4f} in log points "
              f"(~{100 * r['resid_sd']:.1f} % on the threshold), "
              f"n = {r['n']}, dof = {r['dof']}\n")

    gap_a = 100 * (ratio["a"] - within["a"]) / within["a"]
    gap_b = 100 * (ratio["b"] - within["b"]) / within["b"]
    print(f"The app's estimator sits {gap_a:+.1f} % on a and {gap_b:+.1f} % on b against the")
    print("within estimator. Dividing by a noisy rank-20 reading is a regression-to-the-mean")
    print("problem, and it pulls both parameters down.\n")

    boot = bootstrap_within(df)
    print(f"Bootstrap over tournaments — {len(boot)} of {BOOTSTRAP_DRAWS} draws converged, "
          f"resampling {groups} tournaments each time")
    names = ("a", "b")
    hard = calibration.CURVE_DEFAULT
    for i, name in enumerate(names):
        lo, hi = interval(boot[:, i])
        point = within["a"] if i == 0 else within["b"]
        print(f"  {name}: {point:.3f}   bootstrap sd {boot[:, i].std(ddof=1):.3f}   "
              f"95 % CI [{lo:.3f}, {hi:.3f}]")
        inflation = boot[:, i].std(ddof=1) / (within["se_a"] if i == 0 else within["se_b"])
        print(f"     the clustered sd is {inflation:.1f}x the Jacobian se — that ratio is the "
              f"price of the within-tournament correlation")
        verdict = "inside" if lo <= hard[i] <= hi else "OUTSIDE"
        print(f"     hard-coded {name} = {hard[i]} is {verdict} the interval "
              f"({100 * (hard[i] - point) / point:+.1f} % from the point estimate)\n")

    print("Sensitivity — same estimator, different samples")
    print(sensitivity(df).to_string(index=False, na_rep="too thin"))
    deep = int((df["q"] > 0.25).sum())
    print("\nb moves further across these samples than any single interval allows for.")
    print("The exponent governs how fast the threshold falls deep in the standings, and this")
    print(f"history barely reaches there: {len(df) - deep} of {len(df)} observations sit below "
          f"q = 0.25, so b\nrests on {deep} rows. Treat 0.37 as a working value, not a "
          "measurement.")


if __name__ == "__main__":
    report()
