"""Draw the four figures the rest of the folder argues about, as PNG and SVG.

    python -m analysis.figures            # all four, into analysis/figures/
    python -m analysis.figures curve      # just one

Everything lands in `analysis/figures/`. The learning curve refits the pipeline a
few thousand times and takes about half a minute; the other three are instant.
"""
from __future__ import annotations

import math
import os
import sys

import matplotlib

matplotlib.use("Agg")                      # no display on a build box or in CI

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

import calibration
from analysis import data, diagnostics, fit, validate

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figures")

# Categorical slots, taken in fixed order and never cycled. Three is the cap for
# scatter-type plots where any pair can end up adjacent.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d4"

plt.rcParams.update({
    "figure.dpi": 130, "savefig.dpi": 130, "figure.facecolor": "white",
    "axes.facecolor": "white", "axes.edgecolor": GRID, "axes.labelcolor": INK,
    "axes.titlesize": 11, "axes.titleweight": "medium", "axes.labelsize": 9,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "grid.alpha": 0.9,
    "axes.axisbelow": True, "xtick.color": MUTED, "ytick.color": MUTED,
    "xtick.labelsize": 8, "ytick.labelsize": 8, "legend.fontsize": 8,
    "legend.frameon": False, "font.size": 9, "lines.linewidth": 2,
})


def save(fig, name: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    for ext in ("png", "svg"):
        path = os.path.join(OUT, f"{name}.{ext}")
        fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  analysis/figures/{name}.png + .svg")


def _tidy(ax, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, color=INK, loc="left")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


# --------------------------------------------------------------------------- #
def figure_curve(comps=None) -> None:
    """Fitted shape over the observed thresholds.

    Every tournament has its own reference share q_ref = 20 / field, so raw
    threshold-over-level points do not lie on one curve. Multiplying each by
    exp(-a * q_ref^b) removes that offset and leaves exactly exp(-a * q^b), which
    does — so a single line can be judged against every point.
    """
    comps = comps if comps is not None else data.load()
    sample = data.curve_sample(comps)
    res = fit.fit_within(sample)
    a, b = res["a"], res["b"]

    levels = {}
    for c in comps:
        lvl = calibration.reference_level(c, (a, b))
        if lvl:
            levels[c["id"]] = lvl
    sub = sample[sample["competition_id"].isin(levels)].copy()
    sub["level"] = sub["competition_id"].map(levels)
    sub["shape"] = (sub["threshold"] / sub["level"]) * np.exp(-a * sub["q_ref"] ** b)

    grid = np.logspace(math.log10(sub["q"].min() * 0.8), math.log10(sub["q"].max() * 1.2), 300)
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    ax.scatter(sub["q"], sub["shape"], s=26, color=BLUE, alpha=0.55,
               edgecolors="white", linewidths=0.6, zorder=3,
               label=f"observed thresholds (n = {len(sub)}, "
                     f"{sub['competition_id'].nunique()} tournaments)")
    ax.plot(grid, np.exp(-a * grid ** b), color=BLUE, zorder=4,
            label=f"refitted  a = {a:.3f}, b = {b:.3f}")
    ha, hb = calibration.CURVE_DEFAULT
    ax.plot(grid, np.exp(-ha * grid ** hb), color=ORANGE, linestyle="--", zorder=4,
            label=f"shipped   a = {ha}, b = {hb}")
    ax.set_xscale("log")
    _tidy(ax, "Threshold shape against depth in the standings",
          "q = rank / field size (log scale)",
          "share of the tournament's level,\nnormalised to a common reference")
    ax.legend(loc="lower left")
    ax.annotate("11 of 225 observations sit beyond q = 0.25;\n"
                "the exponent is fitted almost entirely on the left",
                xy=(0.985, 0.97), xycoords="axes fraction", ha="right", va="top",
                fontsize=8, color=MUTED)
    save(fig, "curve")


def figure_residuals(comps=None) -> None:
    """Six panels: the residual against everything it should be flat against."""
    comps = comps if comps is not None else data.load()
    df = diagnostics.shape_residuals(comps=comps)
    r = df["residual"]

    fig, axes = plt.subplots(2, 3, figsize=(12.4, 6.6))
    scatter = dict(s=20, color=BLUE, alpha=0.55, edgecolors="white", linewidths=0.5, zorder=3)

    for ax, (col, label, logx) in zip(
            axes.flat,
            [("fitted", "fitted threshold (log scale)", True),
             ("q", "q = rank / field size (log scale)", True),
             ("field_size", "field size (log scale)", True),
             ("date", "tournament date", False)]):
        ax.scatter(df[col], r, **scatter)
        ax.axhline(0, color=ORANGE, linewidth=1.4, zorder=4)
        if logx:
            ax.set_xscale("log")
        _tidy(ax, f"residual vs {col.replace('_', ' ')}", label, "log(observed / fitted)")
    for lbl in axes.flat[3].get_xticklabels():
        lbl.set_rotation(30)
        lbl.set_ha("right")

    ax = axes.flat[4]
    ax.hist(r, bins=34, color=BLUE, alpha=0.8, edgecolor="white", linewidth=0.6, zorder=3)
    ax.axvline(0, color=ORANGE, linewidth=1.4, zorder=4)
    _tidy(ax, "distribution", "log(observed / fitted)", "thresholds")

    ax = axes.flat[5]
    stats.probplot(r, dist="norm", plot=ax)
    ax.get_lines()[0].set(marker="o", markersize=3.5, markerfacecolor=BLUE,
                          markeredgecolor="white", markeredgewidth=0.4, linestyle="none")
    ax.get_lines()[1].set(color=ORANGE, linewidth=1.4)
    ax.set_title("")            # probplot writes its own centre title; ours is on the left
    norm = diagnostics.normality(r)
    _tidy(ax, f"normal Q-Q  (skew {norm['skew']:+.2f}, kurtosis {norm['excess_kurtosis']:+.1f})",
          "normal quantiles", "residual quantiles")

    fig.suptitle("Residual panel — shape residuals in log space, "
                 f"n = {len(df)} over {df['competition_id'].nunique()} tournaments",
                 x=0.008, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.965))
    save(fig, "residuals")


def figure_coverage(comps=None) -> None:
    """Do the low-high bands contain the truth as often as they claim?"""
    comps = comps if comps is not None else data.load()
    cv = validate.cross_validate(comps)
    curve = validate.calibration_curve(cv)
    got = cv.dropna(subset=["value"])

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(10.6, 4.4))
    ax.plot([0, 1], [0, 1], color=MUTED, linewidth=1.2, linestyle=":", zorder=2,
            label="perfectly calibrated")
    ax.plot(curve["nominal"], curve["empirical"], color=BLUE, zorder=4,
            label=f"observed (n = {curve.attrs['z_n']})")
    hit = float(got["covered"].mean())
    ax.scatter([validate.NOMINAL], [hit], s=70, color=ORANGE, zorder=5,
               edgecolors="white", linewidths=1.2,
               label=f"the band the app ships: {100 * hit:.0f} % actual "
                     f"vs {100 * validate.NOMINAL:.0f} % claimed")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    _tidy(ax, "Band calibration", "nominal coverage", "share of thresholds inside the band")
    ax.legend(loc="lower right")

    by_band = got.groupby("band", observed=True)["covered"].agg(["mean", "size"])
    pos = np.arange(len(by_band))
    ax2.bar(pos, 100 * by_band["mean"], color=BLUE, width=0.62, zorder=3)
    ax2.axhline(100 * validate.NOMINAL, color=ORANGE, linewidth=1.6, zorder=4,
                label=f"claimed {100 * validate.NOMINAL:.0f} %")
    for x, (share, n) in enumerate(zip(by_band["mean"], by_band["size"])):
        ax2.text(x, 100 * share + 1.6, f"{100 * share:.0f} %\nn = {n}", ha="center",
                 va="bottom", fontsize=8, color=MUTED)
    ax2.set_xticks(pos)
    ax2.set_xticklabels(by_band.index, rotation=0)
    ax2.set_ylim(0, 118)
    _tidy(ax2, "Coverage by rank band", "rank band", "thresholds inside the band (%)")
    ax2.legend(loc="lower right")
    fig.tight_layout()
    save(fig, "coverage")


def figure_learning(comps=None) -> None:
    """Error against the number of comparable editions the app has seen."""
    comps = comps if comps is not None else data.load()
    lc = validate.balanced(validate.learning_curve(comps))
    grouped = lc.groupby("peers")
    med = grouped["ape"].median()
    mean = grouped["ape"].mean()
    cov = 100 * grouped["covered"].mean()
    n_comps = grouped["competition_id"].nunique().iloc[0]

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(10.6, 4.4))
    ax.plot(med.index, med, color=BLUE, marker="o", markersize=6,
            markeredgecolor="white", markeredgewidth=1.0, zorder=4, label="median error")
    ax.plot(mean.index, mean, color=ORANGE, marker="o", markersize=6,
            markeredgecolor="white", markeredgewidth=1.0, zorder=4, label="mean error")
    ax.annotate(f"{med.iloc[0]:.1f} %", (med.index[0], med.iloc[0]), textcoords="offset points",
                xytext=(6, 8), fontsize=8, color=MUTED)
    ax.annotate(f"{med.iloc[-1]:.1f} %", (med.index[-1], med.iloc[-1]),
                textcoords="offset points", xytext=(-6, -14), fontsize=8, color=MUTED)
    ax.set_ylim(0, max(mean) * 1.24)
    _tidy(ax, "Error against history size", "comparable editions available",
          "absolute percentage error")
    ax.annotate(f"{n_comps} tournaments, the same set at every point",
                xy=(0.985, 0.97), xycoords="axes fraction", ha="right", va="top",
                fontsize=8, color=MUTED)
    ax.legend(loc="lower left")

    ax2.plot(cov.index, cov, color=BLUE, marker="o", markersize=6,
             markeredgecolor="white", markeredgewidth=1.0, zorder=4, label="actual coverage")
    ax2.axhline(100 * validate.NOMINAL, color=ORANGE, linewidth=1.6, zorder=3,
                label=f"claimed {100 * validate.NOMINAL:.0f} %")
    ax2.set_ylim(60, 105)
    _tidy(ax2, "Band coverage against history size",
          "comparable editions available", "thresholds inside the band (%)")
    ax2.legend(loc="lower left")
    fig.tight_layout()
    save(fig, "learning_curve")


FIGURES = {"curve": figure_curve, "residuals": figure_residuals,
           "coverage": figure_coverage, "learning": figure_learning}


def main(names: list[str] | None = None) -> None:
    chosen = names or list(FIGURES)
    unknown = [n for n in chosen if n not in FIGURES]
    if unknown:
        raise SystemExit(f"unknown figure(s) {unknown}; pick from {list(FIGURES)}")
    comps = data.load()
    for name in chosen:
        print(f"{name}...")
        FIGURES[name](comps)


if __name__ == "__main__":
    main(sys.argv[1:] or None)
