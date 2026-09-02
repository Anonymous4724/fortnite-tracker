"""Residual analysis: where the curve is wrong, and how wrong the error bars are.

Two residuals, because they answer different questions.

**Shape residual** — the tournament's level is read off its own thresholds the way
`calibration.reference_level` does it (median over ranks 5/10/20/25), and only
the shape has to explain the rest. This is the curve on trial.

**Forecast residual** — the level is borrowed from comparable tournaments through
the anchor cascade, which is what the app actually does before a tournament
starts. This is the pipeline on trial, and it carries the tournament-level error
the shape residual has already absorbed.

The intra-class correlation is computed on both, and the gap between them is the
answer to "how much does anchoring buy you".

    python -m analysis.diagnostics
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

import calibration
from analysis import data, fit, validate


# --------------------------------------------------------------------------- #
# Residual construction
# --------------------------------------------------------------------------- #
def shape_residuals(curve: tuple[float, float] | None = None,
                    comps: list[dict] | None = None) -> pd.DataFrame:
    """log(observed / predicted) with the level anchored on the tournament itself."""
    comps = comps if comps is not None else data.load()
    if curve is None:
        r = fit.fit_within(data.curve_sample(comps))
        curve = (r["a"], r["b"])
    rows = []
    for c in comps:
        finals = {int(r): float(v) for r, v in (c.get("finals") or {}).items()}
        field = c.get("field_size")
        if not field or len(finals) < 2:
            continue
        level = calibration.reference_level(c, curve)
        if not level:
            continue
        for rank, value in finals.items():
            if rank == 1 or value <= 0:
                continue
            fitted = level * calibration.shape_ratio(rank, field, curve)
            rows.append({"competition_id": c["id"], "name": c["name"], "region": c["region"],
                         "game_mode": c["game_mode"], "date": pd.Timestamp(c["start_time"]),
                         "rank": rank, "field_size": field, "q": rank / field,
                         "threshold": value, "fitted": fitted,
                         "residual": math.log(value / fitted)})
    out = pd.DataFrame(rows)
    out.attrs["curve"] = curve
    return out


# --------------------------------------------------------------------------- #
# Intra-class correlation
# --------------------------------------------------------------------------- #
def icc(df: pd.DataFrame, value: str = "residual",
        group: str = "competition_id") -> dict:
    """One-way random-effects ICC, plus what it does to a naive standard error.

    Unequal group sizes, so the ANOVA uses the usual k0 correction rather than
    the mean cluster size. A negative between-group variance estimate is possible
    and means "no detectable clustering"; it is clamped to zero and reported as
    such rather than dressed up.
    """
    d = df.dropna(subset=[value])
    sizes = d.groupby(group)[value].size()
    k, n = len(sizes), len(d)
    if k < 2 or n <= k:
        return {"icc": float("nan"), "n": n, "groups": k}
    means = d.groupby(group)[value].mean()
    grand = d[value].mean()
    ms_between = float(((means - grand) ** 2 * sizes).sum()) / (k - 1)
    ms_within = float(((d[value] - d[group].map(means)) ** 2).sum()) / (n - k)
    k0 = (n - (sizes ** 2).sum() / n) / (k - 1)
    var_between = max(0.0, (ms_between - ms_within) / k0)
    rho = var_between / (var_between + ms_within)
    mean_size = n / k
    design_effect = 1 + (mean_size - 1) * rho
    return {"icc": rho, "sd_between": math.sqrt(var_between), "sd_within": math.sqrt(ms_within),
            "n": n, "groups": k, "mean_size": mean_size,
            "design_effect": design_effect, "se_inflation": math.sqrt(design_effect)}


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def normality(values: np.ndarray) -> dict:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    return {"n": len(v), "sd": float(v.std(ddof=1)),
            "skew": float(stats.skew(v)), "excess_kurtosis": float(stats.kurtosis(v)),
            "shapiro_p": float(stats.shapiro(v).pvalue) if len(v) <= 5000 else float("nan"),
            "jarque_bera_p": float(stats.jarque_bera(v).pvalue)}


def heteroskedasticity(df: pd.DataFrame) -> dict:
    """Breusch-Pagan against the three things the variance could plausibly track.

    Squared residual on log q, log field size and log fitted value. All three are
    collinear by construction — q is rank over field — so the LM statistic is
    read as "is there any dependence at all", not as three separate slopes.
    """
    d = df.dropna(subset=["residual", "q", "field_size", "fitted"])
    x = np.column_stack([np.ones(len(d)), np.log(d["q"]), np.log(d["field_size"]),
                         np.log(d["fitted"])])
    u = d["residual"].to_numpy() ** 2
    beta = np.linalg.lstsq(x, u, rcond=None)[0]
    resid = u - x @ beta
    r2 = 1 - float(resid @ resid) / float(((u - u.mean()) ** 2).sum())
    lm = len(d) * r2
    out = {"lm": lm, "df": x.shape[1] - 1, "p": float(stats.chi2.sf(lm, x.shape[1] - 1))}
    for name, col in (("q", "q"), ("field_size", "field_size"), ("fitted", "fitted")):
        rho = stats.spearmanr(d[col], d["residual"].abs())
        out[f"spearman_{name}"] = (float(rho.statistic), float(rho.pvalue))
    return out


def _slope(df: pd.DataFrame, column: str, log: bool = True) -> tuple[float, float]:
    """OLS slope of the residual on one covariate, with its p-value.

    A residual that still tilts against a covariate means the model has left
    structure on the table there.
    """
    d = df.dropna(subset=["residual", column])
    x = np.log(d[column].to_numpy(dtype=float)) if log else d[column].to_numpy(dtype=float)
    r = stats.linregress(x, d["residual"])
    return float(r.slope), float(r.pvalue)


# --------------------------------------------------------------------------- #
def report() -> None:
    comps = data.load()
    df = shape_residuals(comps=comps)
    a, b = df.attrs["curve"]
    print(f"Shape residuals — curve refitted at a = {a:.3f}, b = {b:.3f}")
    print(f"{len(df)} thresholds over {df['competition_id'].nunique()} tournaments; "
          f"level read off ranks {calibration.REFERENCE_ANCHORS} as the app does\n")

    norm = normality(df["residual"])
    print(f"residual sd {norm['sd']:.4f} in log points (~{100 * norm['sd']:.1f} % on the "
          f"threshold), mean {df['residual'].mean():+.4f}")
    print(f"skew {norm['skew']:+.2f}   excess kurtosis {norm['excess_kurtosis']:+.2f}   "
          f"Shapiro p = {norm['shapiro_p']:.2g}   Jarque-Bera p = {norm['jarque_bera_p']:.2g}")
    print("Comfortably non-normal, and it is a left tail, not a fat middle: a handful of")
    print("thresholds sit far below the curve, none sits far above.\n")

    print("largest residuals")
    worst = df.reindex(df["residual"].abs().sort_values(ascending=False).index).head(6)
    for _, r in worst.iterrows():
        print(f"  {r['name'][:38]:<38} {r['region']:<4} rank {r['rank']:>4}  "
              f"observed {r['threshold']:>6.1f}  curve says {r['fitted']:>6.1f}  "
              f"{100 * (math.exp(r['residual']) - 1):+6.1f} %")
    print("Three of these are the rank-50 threshold of a lower FNCS division, where the")
    print("ladder drops 30-40 % in one step from rank 25 while every comparable tournament")
    print("loses 5 %. That is not a curve failure; it is very likely a truncated standings")
    print("page read as a threshold. Worth checking before those rows shape anything.\n")

    trimmed = df[df["residual"].abs() < 0.20]
    tn = normality(trimmed["residual"])
    print(f"dropping the {len(df) - len(trimmed)} residuals beyond +/-20 %: sd "
          f"{tn['sd']:.4f}, skew {tn['skew']:+.2f}, excess kurtosis "
          f"{tn['excess_kurtosis']:+.2f}, Shapiro p = {tn['shapiro_p']:.2g}")
    print("Still not normal, but the asymmetry is gone. The working error is around "
          f"{100 * tn['sd']:.0f} %,\nnot the {100 * norm['sd']:.0f} % the raw sd suggests, "
          "with a few genuine outliers on top.\n")

    print("residual tilt against each covariate (log scale)")
    for label, column in (("q = rank / field", "q"), ("field size", "field_size"),
                          ("fitted value", "fitted")):
        slope, p = _slope(df, column)
        print(f"  {label:<18} slope {slope:+.4f} per log unit, p = {p:.3f}")
    date_slope, date_p = _slope(
        df.assign(days=(df["date"] - df["date"].min()).dt.days + 1), "days")
    print(f"  {'date':<18} slope {date_slope:+.4f} per log day, p = {date_p:.3f}")
    print("The history covers about three months, so the date column is a check that")
    print("nothing drifted, not a test for drift.\n")

    het = heteroskedasticity(df)
    print(f"Breusch-Pagan LM = {het['lm']:.1f} on {het['df']} df, p = {het['p']:.4f}")
    for name in ("q", "field_size", "fitted"):
        rho, p = het[f"spearman_{name}"]
        print(f"  |residual| vs {name:<11} Spearman rho = {rho:+.3f}, p = {p:.3f}")
    print("The spread widens deeper into the standings and narrows at higher thresholds.")
    print("Constant-variance least squares in log space is therefore the wrong weighting,")
    print("mildly: the deep ranks get more say in the exponent than their precision earns.\n")

    print("residual sd by rank band")
    bands = df.assign(band=pd.cut(df["rank"], bins=validate.BANDS,
                                  labels=validate.BAND_LABELS))
    print(bands.groupby("band", observed=True)["residual"]
          .agg(n="size", sd=lambda s: s.std(ddof=1), median="median")
          .round(4).to_string(na_rep="n < 2"), "\n")

    shape_icc = icc(df)
    print("Intra-class correlation — thresholds from one tournament are not independent")
    print(f"  shape residual   ICC = {shape_icc['icc']:.3f}   "
          f"between-tournament sd {shape_icc['sd_between']:.4f}, "
          f"within {shape_icc['sd_within']:.4f}")
    print(f"                   {shape_icc['groups']} tournaments, "
          f"{shape_icc['mean_size']:.1f} ranks each -> design effect "
          f"{shape_icc['design_effect']:.2f}, standard errors x "
          f"{shape_icc['se_inflation']:.2f}")

    cv = validate.cross_validate(comps).dropna(subset=["log_error"])
    fc_icc = icc(cv, value="log_error")
    print(f"  forecast residual ICC = {fc_icc['icc']:.3f}   "
          f"between-tournament sd {fc_icc['sd_between']:.4f}, "
          f"within {fc_icc['sd_within']:.4f}")
    print(f"                   {fc_icc['groups']} tournaments, "
          f"{fc_icc['mean_size']:.1f} ranks each -> design effect "
          f"{fc_icc['design_effect']:.2f}, standard errors x "
          f"{fc_icc['se_inflation']:.2f}")
    print()
    print("The two numbers are the whole story. Once a tournament's level is read off its own")
    print("standings, almost nothing tournament-specific is left — the shape travels. Once the")
    print("level has to be guessed from comparable editions, the tournament is most of the")
    print(f"error: {100 * fc_icc['icc']:.0f} % of the forecast variance is shared by every rank in it. "
          f"So a headline\nlike 'median error 6 %, n = {fc_icc['n']}' is really n = "
          f"{fc_icc['groups']} evenings, and a standard error\ncomputed on "
          f"{fc_icc['n']} rows is understated by about {fc_icc['se_inflation']:.1f}x.")
    print()

    sample = data.curve_sample(comps)
    within = fit.fit_within(sample)
    boot = fit.bootstrap_within(sample, draws=500)
    print("Same thing measured directly on the curve fit, by resampling tournaments:")
    for i, name in enumerate("ab"):
        ratio = boot[:, i].std(ddof=1) / (within["se_a"] if i == 0 else within["se_b"])
        print(f"  {name}: clustered sd {boot[:, i].std(ddof=1):.3f} against Jacobian se "
              f"{(within['se_a'] if i == 0 else within['se_b']):.3f}  ->  x{ratio:.1f}")
    print("Bigger than the design effect above, because resampling tournaments also")
    print("resamples the rank ladder, and the exponent is identified by the ladder.")


if __name__ == "__main__":
    report()
