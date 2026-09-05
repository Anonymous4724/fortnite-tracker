"""Draw the four figures the rest of the folder argues about, as PNG and SVG.

    python -m analysis.figures            # all four, into analysis/figures/
    python -m analysis.figures curve      # just one

Everything lands in `analysis/figures/`. On the full harvest the coverage
figure replays a few hundred tournaments and the learning curve refits the
pipeline for each of them at every history size: count on a few minutes.

Scatter layers are rasterised inside the SVG. Sixty thousand points as vector
circles made a 48 MB file that no browser wanted to open; the axes, text and
lines stay crisp, the dots become an embedded image.
"""
from __future__ import annotations

import math
import os
import sys

from analysis import _require

_require("matplotlib")                     # the only script here that needs it

import matplotlib

matplotlib.use("Agg")                      # no display on a build box or in CI

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

import calibration
import export_model
from analysis import data, diagnostics, fit, validate

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figures")

# Beyond this many times the reference level the curve figure stops drawing
# points and counts them instead: a handful of boards with twenty entrants and
# a 1-point rank 20 would otherwise push the axis to 11 and flatten the other
# sixty thousand observations into a stripe.
CURVE_Y_CAP = 3.0

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


def shipped_curve() -> tuple[tuple[float, float], str]:
    """The curve the site runs on, and what to call it in a legend.

    `model.json` is what the export last wrote and what the predictor loads;
    the constant in `calibration` is only the cold fallback for a database too
    small to fit. Showing the fallback as "shipped" is how a figure ends up
    contradicting the site it illustrates.
    """
    curve = export_model.previous_curve()
    if curve and "a" in curve and "b" in curve:
        return (float(curve["a"]), float(curve["b"])), "shipped (model.json)"
    return calibration.CURVE_DEFAULT, "fallback default (no model.json yet)"


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
    shown = sub[sub["shape"] <= CURVE_Y_CAP]
    above = len(sub) - len(shown)
    deep = int((sub["q"] > 0.25).sum())

    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    ax.scatter(shown["q"], shown["shape"], s=14, color=BLUE, alpha=0.35,
               edgecolors="white", linewidths=0.4, zorder=3, rasterized=True,
               label=f"observed thresholds (n = {len(sub)}, "
                     f"{sub['competition_id'].nunique()} tournaments)")
    ax.plot(grid, np.exp(-a * grid ** b), color=BLUE, zorder=4,
            label=f"refitted  a = {a:.3f}, b = {b:.3f}")
    (ha, hb), shipped_label = shipped_curve()
    ax.plot(grid, np.exp(-ha * grid ** hb), color=ORANGE, linestyle="--", zorder=4,
            label=f"{shipped_label}  a = {ha:.3f}, b = {hb:.3f}")
    ax.set_xscale("log")
    ax.set_ylim(0, CURVE_Y_CAP)
    _tidy(ax, "Threshold shape against depth in the standings",
          "q = rank / field size (log scale)",
          "share of the tournament's level,\nnormalised to a common reference")
    ax.legend(loc="lower left")     # the only empty corner: nothing is cheap and deep
    note = (f"{deep} of {len(sub)} observations ({100 * deep / len(sub):.0f} %) sit "
            f"beyond q = 0.25")
    if above:
        note += (f";\n{above} sit above {CURVE_Y_CAP:g} and are not drawn — boards "
                 f"of a few dozen entrants\nwhere rank {calibration.REFERENCE_RANK} "
                 f"is worth a handful of points")
    ax.annotate(note, xy=(0.015, 0.97), xycoords="axes fraction", ha="left", va="top",
                fontsize=8, color=MUTED)
    save(fig, "curve")


def figure_residuals(comps=None) -> None:
    """Six panels: the residual against everything it should be flat against."""
    comps = comps if comps is not None else data.load()
    df = diagnostics.shape_residuals(comps=comps)
    r = df["residual"]

    fig, axes = plt.subplots(2, 3, figsize=(12.4, 6.6))
    scatter = dict(s=12, color=BLUE, alpha=0.35, edgecolors="white", linewidths=0.4,
                   zorder=3, rasterized=True)

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
                          markeredgecolor="white", markeredgewidth=0.4, linestyle="none",
                          rasterized=True)
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
