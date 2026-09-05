"""Does the field size belong in the shape of a leaderboard, and how strongly?

The shipped model says it does, and says exactly how much:

    threshold(r) = level * exp(-a * (q**b - q_ref**b)),   q = rank / field

Factor it with q = r/field and q_ref = 20/field:

    q**b - q_ref**b = (20/field)**b * ((r/20)**b - 1)

so the amplitude of the whole shape is `a * (20/field)**b`. The field does not
nudge the ladder, it multiplies it — and the exponent that governs the ladder's
steepness in rank is forced to be the same exponent that governs its dependence
on the field. Nothing in the physics of a tournament requires those two numbers
to be equal. That they are is an artefact of writing the model in `q`.

So this script unties them and lets the data set both:

    threshold(r) = level * exp(-a * (20/field)**c * ((r/20)**b - 1))

    c = b   the shipped model, exactly
    c = 0   the field drops out of the shape entirely; the ladder is a
            function of rank against the reference rank, and the field is
            left to act on the level alone, where the measured elasticity
            of about 0.05 already lives

Fitting `c` is the whole question, and it has a property worth stating plainly,
because it answers the obvious objection about what the field even counts. A
leaderboard row is a roster, not a person and not a permanent team: a duo whose
partner leaves mid-session appears twice, and teams that registered without ever
playing appear at the bottom. So the row count is an upper bound on the number
of teams genuinely competing, and it may be an upper bound by a fair margin.

That does not touch `c`. Rescaling every field by the same factor k turns
`(20/field)**c` into `k**-c * (20/field)**c`, a constant that is absorbed
whole into `a`. Counting players instead of teams, or rosters instead of
finishers, moves the fitted amplitude and leaves the exponent where it was.
Only a distortion that grows with tournament size could bend `c`, and the
report below says how large the field range actually is so that the size of
that loophole can be judged.

    python -m analysis.shape
"""
from __future__ import annotations

import math
import statistics

import numpy as np

import calibration
from analysis import data

REF = calibration.REFERENCE_RANK

B_GRID = np.round(np.arange(0.10, 1.61, 0.02), 3)
C_GRID = np.round(np.arange(0.00, 1.21, 0.02), 3)

# Enough gaps to pin two exponents to a couple of decimals; past this the grid
# search costs minutes to sharpen a number in its third decimal.
MAX_GAPS = 60_000
DRAWS = 60
SEED = 20260904


def gaps(comps: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(rank, field, log(threshold(rank)/threshold(20)), tournament id) as arrays.

    Every pair is measured inside one tournament and against that tournament's
    own rank-20 threshold, so the level cancels exactly and nothing here depends
    on the anchor cascade, the scoring table or the number of games. Only the
    shape is under test.

    Rank 1 is kept, unlike in `calibration.fit_curve` where it is dropped for
    being noisy. It is also where the two readings disagree most, and a
    comparison that threw it away would be comparing them where the answer does
    not matter.
    """
    rank, field, ratio, who, base20 = [], [], [], [], []
    for comp in comps:
        size = comp.get("field_size")
        finals = {int(r): float(v) for r, v in (comp.get("finals") or {}).items()
                  if float(v) > 0}
        base = finals.get(REF)
        if not size or size <= REF or not base:
            continue
        for r, value in finals.items():
            if r == REF:
                continue
            rank.append(r), field.append(int(size))
            ratio.append(math.log(value / base)), who.append(comp["id"])
            base20.append(base)
    return (np.array(rank, dtype=float), np.array(field, dtype=float),
            np.array(ratio, dtype=float), np.array(who), np.array(base20, dtype=float))


def fit(rank, field, ratio, b_grid=B_GRID, c_grid=C_GRID):
    """(a, b, c, residual sd) by grid search on the exponents, `a` in closed form.

    For fixed (b, c) the model is linear in `a`, so only the two exponents are
    searched and the amplitude falls out of a least-squares formula. One
    estimator, one grid, so nothing in the comparison below turns on two
    different fitting procedures having been used.
    """
    best = None
    for b in b_grid:
        shape = (rank / REF) ** b - 1.0
        for c in c_grid:
            x = shape * (REF / field) ** c
            den = float(x @ x)
            if den <= 0:
                continue
            a = -float(x @ ratio) / den
            resid = ratio + a * x
            err = float(resid @ resid)
            if best is None or err < best[0]:
                best = (err, a, float(b), float(c), float(resid.std(ddof=1)))
    if best is None:
        return 0.0, 0.0, 0.0, float("inf")
    return best[1], best[2], best[3], best[4]


def fit_fixed_c(rank, field, ratio, c):
    """The same fit with `c` held, for scoring the two named special cases."""
    return fit(rank, field, ratio, c_grid=np.array([c]))


def bootstrap_c(rank, field, ratio, who, b0, c0, draws=DRAWS, seed=SEED):
    """A confidence interval for `c`, resampled over tournaments.

    Over tournaments and not over rows, for the reason the rest of this folder
    resamples that way: every rank of one evening is wrong in the same
    direction, so rows are nothing like independent and a row bootstrap would
    report an interval several times too narrow.

    Each draw searches a neighbourhood of the full-sample optimum rather than
    the whole grid — sixty full searches would be an afternoon, and the
    question is how far `c` wanders, not where it starts.
    """
    rng = np.random.default_rng(seed)
    # Sorted once and split, not one mask per tournament: a mask each is a full
    # pass over sixty thousand rows for every one of eight thousand tournaments,
    # which is the same quadratic trap that made the cross-validation take hours.
    order = np.argsort(who, kind="stable")
    grouped = who[order]
    edges = np.flatnonzero(np.r_[True, grouped[1:] != grouped[:-1]])
    index = np.split(order, edges[1:])
    near_b = np.round(np.arange(max(0.10, b0 - 0.16), b0 + 0.17, 0.04), 3)
    near_c = np.round(np.arange(max(0.00, c0 - 0.20), c0 + 0.21, 0.04), 3)
    found = []
    for _ in range(draws):
        picked = rng.integers(0, len(index), size=len(index))
        rows = np.concatenate([index[i] for i in picked])
        _, _, c, _ = fit(rank[rows], field[rows], ratio[rows],
                         b_grid=near_b, c_grid=near_c)
        found.append(c)
    return float(np.percentile(found, 2.5)), float(np.percentile(found, 97.5))


def fit_shipped(rank, field, ratio, b_grid=B_GRID):
    """The shipped form, c held equal to b — the reading the free fit vindicated.

    One exponent instead of two, so this is quick enough to run inside every
    group of a breakdown.
    """
    best = None
    for b in b_grid:
        x = ((rank / REF) ** b - 1.0) * (REF / field) ** b
        den = float(x @ x)
        if den <= 0:
            continue
        a = -float(x @ ratio) / den
        resid = ratio + a * x
        err = float(resid @ resid)
        if best is None or err < best[0]:
            best = (err, a, float(b), resid)
    if best is None:
        return 0.0, 0.0, np.zeros_like(ratio)
    return best[1], best[2], best[3]


def spread(resid) -> tuple[float, float]:
    """(standard deviation, median absolute) of a residual, in log points.

    Both, because on this data they disagree and only one of them means
    anything. Section D measures a 99th percentile at 191 %: under a tail that
    heavy a standard deviation is a measurement of the tail, not of the fit, and
    every verdict below would be decided by a few hundred broken evenings out of
    sixty thousand rows. `validate.py` reports median absolute error for exactly
    this reason, and a diagnostic that answers in a different currency than the
    validation it is trying to explain cannot be compared with it.
    """
    r = np.asarray(resid, dtype=float)
    return float(r.std(ddof=1)), float(np.median(np.abs(r)))


def as_pct(value: float) -> float:
    """A log-point spread read as a percentage."""
    return 100 * (math.exp(value) - 1)


def fit_trimmed(rank, field, ratio, cut=0.10, rounds=2):
    """The shipped form fitted with the worst `cut` of residuals set aside.

    Least squares puts the curve where it minimises the sum of squares, so an
    evening whose ladder is twice what it should be pulls harder than fifty
    ordinary ones. Refitting on the inner ninety per cent asks a different and
    more useful question: where does the curve go if it is allowed to describe
    the tournaments that behave, rather than to compromise with the ones that do
    not?
    """
    keep = np.ones(len(ratio), dtype=bool)
    a = b = 0.0
    for _ in range(rounds + 1):
        a, b, resid = fit_shipped(rank[keep], field[keep], ratio[keep])
        full = ratio + a * (((rank / REF) ** b - 1.0) * (REF / field) ** b)
        limit = np.quantile(np.abs(full), 1 - cut)
        keep = np.abs(full) <= limit
    full = ratio + a * (((rank / REF) ** b - 1.0) * (REF / field) ** b)
    return a, b, full


def per_group(rank, field, ratio, keys, floor=300):
    """Refit the shape inside each group; return (pooled residual sd, group count).

    The shipped model asserts one universal shape: the way points fall down a
    leaderboard is a property of Fortnite's scoring, not of the particular cup.
    That is a testable claim and this is the test. If the residual collapses once
    each format fits its own exponent, the shape is not universal — it belongs to
    the format, and one curve for everything is averaging away the thing it is
    supposed to describe.

    Groups thinner than `floor` gaps keep the global shape rather than fitting
    their own, so the comparison cannot be won by giving a handful of rows their
    own free parameters.
    """
    resid = np.array(fit_shipped(rank, field, ratio)[2], dtype=float)
    fitted = 0
    order = np.argsort(keys, kind="stable")
    run = keys[order]
    starts = np.flatnonzero(np.r_[True, run[1:] != run[:-1]])
    for sel in np.split(order, starts[1:]):
        if len(sel) < floor:
            continue
        resid[sel] = fit_shipped(rank[sel], field[sel], ratio[sel])[2]
        fitted += 1
    return spread(resid)[1], fitted


def observed_top(comps: list[dict]) -> list[tuple[int, float]]:
    """(field, threshold(1)/threshold(20)) — the observable the readings argue over."""
    out = []
    for comp in comps:
        size = comp.get("field_size")
        finals = {int(r): float(v) for r, v in (comp.get("finals") or {}).items()
                  if float(v) > 0}
        if size and finals.get(1) and finals.get(REF):
            out.append((int(size), finals[1] / finals[REF]))
    return out


def report() -> None:
    comps = data.load()
    rank, field, ratio, who, base20 = gaps(comps)
    if len(rank) < 50:
        print("Not enough rank-to-rank gaps to fit a shape.")
        return

    if len(rank) > MAX_GAPS:
        keep = np.random.default_rng(SEED).choice(len(rank), MAX_GAPS, replace=False)
        rank, field, ratio, who, base20 = (rank[keep], field[keep], ratio[keep],
                                           who[keep], base20[keep])

    span = field.max() / field.min()
    print(f"{len(rank)} rank-to-rank gaps from {len(np.unique(who))} tournaments")
    print(f"field sizes {int(field.min())} to {int(field.max())} "
          f"({span:.0f}x), median {int(np.median(field))}")
    if span < 5:
        print("\n  WARNING: the fields barely differ. The two readings below are")
        print("  nearly the same function on this data and it cannot separate them.")
    print()

    a, b, c, sd = fit(rank, field, ratio)
    lo, hi = bootstrap_c(rank, field, ratio, who, b, c)
    print("  free fit    exp(-a * (20/field)**c * ((r/20)**b - 1))")
    print(f"      a = {a:.3f}   b = {b:.2f}   c = {c:.2f}  [{lo:.2f}, {hi:.2f}] "
          f"95 % over tournaments")
    _, _, resid_free = fit_shipped(rank, field, ratio)
    free_sd, free_med = spread(resid_free)
    print(f"      typical error {free_med:.4f} log points (~{as_pct(free_med):.1f} %), "
          f"sd {free_sd:.4f} (~{as_pct(free_sd):.1f} %)")
    print("      The two disagree because the tail is heavy — see D. The first is the")
    print("      one to read, and the one validate.py reports in.\n")

    held_fits = {}
    for label, held in (("shipped   c = b", b), ("no field  c = 0", 0.0)):
        a_h, b_h, _, sd_h = fit_fixed_c(rank, field, ratio, held)
        held_fits[label] = (a_h, b_h)
        worse = 100 * (sd_h - sd) / sd if sd else 0.0
        print(f"  {label}:  a = {a_h:.3f}  b = {b_h:.2f}  sd {sd_h:.4f} "
              f"({worse:+.1f} % vs free)")

    print()
    if hi < 0.10:
        print("  -> c is indistinguishable from zero. The field does not belong in the")
        print("     shape at all; it belongs on the level, where the elasticity already")
        print("     measured at about 0.05 lives. The shipped form ties the ladder's")
        print("     steepness to the registration count and that tie is not real.")
    elif lo <= b <= hi:
        print("  -> c is consistent with b. The shipped form survives: the same exponent")
        print("     really does govern both the ladder and its field dependence.")
    else:
        print(f"  -> c sits between the two: neither 0 nor b = {b:.2f}. The field belongs in")
        print("     the shape, but far more weakly than the shipped form asserts.")
    print("  A uniform mis-count of the field — players for teams, rosters for")
    print("  finishers — rescales a and leaves c untouched, so none of the above")
    print("  turns on what exactly a leaderboard row represents.")

    top = observed_top(comps)
    if len(top) >= 20:
        top.sort()
        half = len(top) // 2
        sf = statistics.median([f for f, _ in top[:half]])
        bf = statistics.median([f for f, _ in top[half:]])
        small = statistics.median([t for _, t in top[:half]])
        big = statistics.median([t for _, t in top[half:]])
        print(f"\n  threshold(1) / threshold({REF}), measured against predicted:")
        print(f"      {'field':>9}  {'measured':>9}  {'free fit':>9}  {'shipped':>9}")
        a_h, b_h = held_fits["shipped   c = b"]
        for size, seen in ((sf, small), (bf, big)):
            free = math.exp(-a * (REF / size) ** c * ((1 / REF) ** b - 1))
            ship = math.exp(-a_h * (REF / size) ** b_h * ((1 / REF) ** b_h - 1))
            print(f"      {int(size):>9}  {seen:>9.3f}  {free:>9.3f}  {ship:>9.3f}")
        print("  This ratio needs no model to measure and no field to compute. A form")
        print("  that cannot hold it flat cannot forecast the top of a leaderboard.")

    # ----------------------------------------------------------------- #
    # Where the scatter lives
    # ----------------------------------------------------------------- #
    # Knowing the form is right settles nothing on its own: a right form fitted
    # to a 29 % residual forecasts no better than 29 %. So the second half of
    # this report asks where that residual comes from, because the two candidate
    # answers call for opposite work. Either it is concentrated in tournaments
    # that should never have entered the training set, and the fix is a filter —
    # or the shape genuinely differs between formats, and one universal curve is
    # averaging away the thing it claims to describe.
    print("\n" + "-" * 70)
    print("  WHERE THE SCATTER IS")
    print("-" * 70)
    a_all, b_all, resid_all = fit_shipped(rank, field, ratio)
    whole_sd, whole = spread(resid_all)
    print(f"  everything: b = {b_all:.2f}, typical error {whole:.4f} "
          f"(~{as_pct(whole):.1f} %), sd {whole_sd:.4f} (~{as_pct(whole_sd):.1f} %)")
    a_t, b_t, resid_t = fit_trimmed(rank, field, ratio)
    trim_sd, trim_med = spread(resid_t)
    print(f"  refitted with the worst tenth set aside: a = {a_t:.3f}, b = {b_t:.2f}, "
          f"typical error {trim_med:.4f} (~{as_pct(trim_med):.1f} %)")
    print(f"      least squares under a heavy tail puts the curve where the outliers")
    print(f"      want it. The move from {as_pct(whole):.1f} % to {as_pct(trim_med):.1f} % "
          f"is what that costs the\n      tournaments that behave.\n")

    by_id = {c["id"]: c for c in comps}
    censored = int((field >= field.max() - 1).sum())
    print("  A. drop tournaments that should not be in a training set")
    for label, keep in (
            ("field at the page-cap ceiling", field < field.max() - 1),
            ("field under 200 rosters", field >= 200),
            ("rank 20 worth under 10 points", base20 >= 10),
            ("all three", (field < field.max() - 1) & (field >= 200) & (base20 >= 10))):
        if int(keep.sum()) < 500:
            continue
        med = spread(fit_shipped(rank[keep], field[keep], ratio[keep])[2])[1]
        print(f"      without {label:<32} {int(keep.sum()):>6} gaps  "
              f"{med:.4f}  (~{as_pct(med):4.1f} %)  {100 * (med - whole) / whole:+.0f} %")
    print(f"      ({censored} of {len(field)} gaps sit at the {int(field.max())} "
          f"ceiling, where the field is censored, not measured)\n")

    print("  B. let each format fit its own shape")
    groupings = [
        ("team size", lambda c: c.get("team_mode") or "?"),
        ("game mode", lambda c: c.get("game_mode") or "?"),
        ("mode + team size", lambda c: f"{c.get('game_mode')}/{c.get('team_mode')}"),
        ("number of games", lambda c: str(c.get("max_games") or 0)),
        ("region", lambda c: c.get("region") or "?"),
        ("category", lambda c: c.get("kind") or "?"),
    ]
    for label, key_of in groupings:
        keys = np.array([key_of(by_id[i]) for i in who])
        sd, fitted = per_group(rank, field, ratio, keys)
        print(f"      one shape per {label:<18} {fitted:>4} groups  "
              f"{sd:.4f}  (~{as_pct(sd):4.1f} %)  {100 * (sd - whole) / whole:+.0f} %")

    print("\n  A large negative number in A means the training set is polluted and the")
    print("  fix is a filter. A large negative number in B means the shape is not")
    print("  universal and belongs to the format. Small numbers everywhere mean the")
    print("  scatter is real tournament-to-tournament variation, and no rearrangement")
    print("  of this model will forecast below it.")

    resid = np.asarray(resid_all, dtype=float)

    # C. Which part of the ladder is wrong. Averaged over the whole board a
    # residual says nothing about where a forecast will fail, and the ranks are
    # not interchangeable: rank 1 is one team having an exceptional night, the
    # deep end is where hundreds of teams tie on the same handful of points.
    # `calibration.fit_curve` already drops rank 1 for being noisy; this says
    # whether that is worth doing and whether anything else deserves the same.
    print("\n  C. which rank the scatter sits at")
    print(f"      {'rank':>6}  {'gaps':>7}  {'typical':>8}  {'bias':>8}")
    for lo, hi, label in ((1, 1, "1"), (2, 5, "3-5"), (6, 15, "6-15"),
                          (16, 40, "16-40"), (41, 110, "41-110"), (111, 10 ** 9, "111+")):
        sel = (rank >= lo) & (rank <= hi)
        if int(sel.sum()) < 200:
            continue
        part = resid[sel]
        print(f"      {label:>6}  {int(sel.sum()):>7}  {as_pct(spread(part)[1]):>7.1f} % "
              f"{as_pct(float(np.median(part))):>+7.1f} %")
    print("      bias is the average signed error: a column of one sign means the")
    print("      curve is systematically too high or too low there, not merely noisy.")

    # D. Is every evening a little wrong, or a minority catastrophically so?
    # The two call for different work — a better curve against a filter — and a
    # pooled standard deviation cannot tell them apart.
    order = np.argsort(who, kind="stable")
    grouped, edges = who[order], None
    edges = np.flatnonzero(np.r_[True, grouped[1:] != grouped[:-1]])
    per_comp = np.array([float(np.sqrt((resid[idx] ** 2).mean()))
                         for idx in np.split(order, edges[1:])])
    print("\n  D. spread of the per-tournament error")
    for q in (50, 75, 90, 99):
        value = float(np.percentile(per_comp, q))
        print(f"      {q:>2}th percentile  {value:.4f}  (~{100 * (math.exp(value) - 1):5.1f} %)")
    # Counted, not read off a percentile ratio. A tenth of the sample being
    # broken puts the 90th percentile exactly on the boundary, where it reads as
    # clean — so the question "how many evenings are far worse than typical" is
    # asked directly instead.
    middle = float(np.median(per_comp))
    bad = per_comp > 3 * middle
    share = float(bad.mean())
    print(f"      {int(bad.sum())} of {len(per_comp)} tournaments ({100 * share:.1f} %) "
          f"sit above 3x the median evening")
    if share > 0.02:
        cut = float(np.median(per_comp[~bad]))
        print(f"      -> a fat tail. Set them aside and the rest fit at {cut:.4f} "
              f"(~{100 * (math.exp(cut) - 1):.1f} %),")
        print("         so most evenings are fine and a minority are something else.")
        print("         Find out what they are before touching the curve.")
    else:
        print("      -> no tail worth chasing: the error is spread evenly over evenings,")
        print("         which is what irreducible variation looks like, and no filter")
        print("         will remove it.")

    # E. The curve exists to answer about ranks nobody measured. But the harvest
    # measures the same handful of ranks in every tournament, so for those the
    # question can be answered by looking them up instead of modelling them:
    # what did this rank sit at, relative to rank 20, in the other editions?
    # That is what the carry-forward baseline does implicitly, and it is worth
    # knowing what it costs before deciding whether the curve deserves to be in
    # front of it.
    print("\n  E. a lookup table instead of a curve")
    print("     (median log-ratio at each rank among the other editions of the group,")
    print("      each edition scored against a table it did not contribute to)")
    print(f"      {'grouped by':<28} {'usable':>8}  {'typical':>8}   vs the curve")
    for label, key_of in (
            ("nothing — one table", lambda c: ""),
            ("mode + team size", lambda c: f"{c.get('game_mode')}/{c.get('team_mode')}"),
            ("mode + team size + games", lambda c: f"{c.get('game_mode')}/"
                                                   f"{c.get('team_mode')}/{c.get('max_games')}"),
            ("category", lambda c: c.get("kind") or "?")):
        table_key = np.array([f"{key_of(by_id[i])}|{int(r)}" for i, r in zip(who, rank)])
        held = np.full(len(ratio), np.nan)
        # Sorted once and split. A mask per key is a full pass over sixty
        # thousand rows for each of twenty thousand groups; the same trap that
        # has already cost this project an afternoon twice over.
        order = np.argsort(table_key, kind="stable")
        run = table_key[order]
        starts = np.flatnonzero(np.r_[True, run[1:] != run[:-1]])
        for sel in np.split(order, starts[1:]):
            if len(sel) < 3:
                continue                      # too thin to leave one out of
            values = ratio[sel]
            for pos in range(len(sel)):
                others = np.delete(values, pos)
                held[sel[pos]] = values[pos] - np.median(others)
        good = ~np.isnan(held)
        if int(good.sum()) < 500:
            continue
        med = spread(held[good])[1]
        # The curve, scored on the same rows, so the comparison is like for like.
        curve_here = spread(resid[good])[1]
        print(f"      {label:<28} {int(good.sum()):>8}  {as_pct(med):>7.1f} %  "
              f"{100 * (med - curve_here) / curve_here:+.0f} %  "
              f"(against {as_pct(curve_here):.1f} % for the curve)")
    print("      A large negative number here means the curve is being asked to do")
    print("      a job the data can already answer by lookup, and that the curve")
    print("      belongs behind the table as the fallback for unmeasured ranks.")


if __name__ == "__main__":
    report()
