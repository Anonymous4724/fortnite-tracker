"""The model: what a rank will cost, before the tournament starts.

A cascade, most direct reading first. `prior_prediction` walks it and stops at
the first rung that can answer:

1. **The previous edition, read straight** (`direct_tables`). Same cup, same
   region, same format, same rank, last time — with a band measured from how
   much that rank moved between consecutive editions. First because it kept
   winning: a strong evening lifts every rank together, and a number read whole
   keeps that where a level times a ratio loses it. A cup that has never run
   in this format reads its last edition in another one, band widened.

2. **The cup's level times its measured shape** (`reference_pace`,
   `shape_tables`). The rank-20 threshold from the previous edition, times what
   each rank was worth relative to rank 20 across the cup's editions — a lookup,
   not a curve. Category first, then the family across regions.

3. **The level times the fitted curve** (`fit_curves`, `shape_ratio`), for ranks
   no edition has measured:

       threshold(rank) = level * exp(-a * (q**b - q_ref**b)),   q = rank / field

   with (a, b) fitted once per band of field size — a lobby of three hundred
   and a queue of ten thousand do not empty at the same pace.

4. **The scoring table alone** (`anchor_level`, the scoring branch), for a cup
   nobody has seen: a share of the most a team could score, read from cups of
   the same kind, platform and stage, shrunk toward `REFERENCE_SHARE`.

A single lobby the model has no editions of is read off the finals of its
mode instead, by share of the lobby (`lobby_estimate`); and the last places
of a single lobby get no number at all (`last_places_reason`).

Everything the cascade reads comes from `broad_stats`, computed once over every
finished tournament and cached in the database. The live models in `predict.py`
also draw their prior weights from here (`model_errors`).

Measured as a forecast — each of the newest 600 tournaments from everything
that finished before its day: `analysis/validate.py` prints all of it and
`docs/methodology.md` says what it means.
"""
from __future__ import annotations

import json
import math
import re
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

# The exponent grid `fit_curve` searches. Landing on either edge means the fit
# never bracketed its optimum, which `LAST_FIT` records so callers can say so
# out loud. The grid used to stop at 0.2 and 1.2, and the fits by field size
# below sat on both edges: a ten-thousand-player queue wants 0.12, a lobby of
# three hundred wants 1.23.
B_RANGE = (0.10, 2.00)
LAST_FIT: dict = {"railed": None}

# Most editions any one comparable circle keeps, newest first. A cup that has run
# three hundred times does not need three hundred loaded from disk to produce a
# median, and the widest circle — a whole region and mode — holds thousands once
# the API harvest has run. See `comparable_history`.
CIRCLE_CAP = 40

# The measured shape table. `SHAPE_MIN` editions before a rank is trusted to
# speak for itself; below that the curve answers. `SHAPE_FALLBACK_REL` is the
# curve's own shape uncertainty, which a thin table is blended toward, and
# `SHAPE_FLOOR` stops three editions that happened to agree from claiming they
# know a rank to within nothing.
SHAPE_MIN = 3
SHAPE_FALLBACK_REL = 0.06

# One lobby holds this many teams: a final played by qualified teams in a single
# lobby cannot rank more than this, whatever the sign-up count says. Reload and
# Blitz lobbies seat forty players, not a hundred. The predictor carries the
# same two tables.
LOBBY_CAP = {"Solo": 100, "Duo": 50, "Trio": 33, "Squad": 25}
LOBBY_CAP_SMALL = {"Solo": 40, "Duo": 20, "Trio": 13, "Squad": 10}
# A closed lobby's ladder is read by share of the lobby, in these buckets of
# q = rank / field (upper edges). The finals in the training set are 20 to
# 100 teams and keep ranks 1, 3, 5, 10, 20, 25, 50 and 100, so a rank-by-rank
# table would be mostly holes; by share, rank 1 of 20 and rank 5 of 100 are the
# same question and land in the same bucket.
LOBBY_BUCKETS = (0.03, 0.06, 0.12, 0.25, 0.4, 0.55, 0.7, 0.9)
LOBBY_LAST = 0.9                   # beyond this share, the last places: teams that left
LOBBY_MIN = 3                      # finals a bucket needs before it is believed
LOBBY_REL_FLOOR = 0.25             # never narrower than this: a cup never seen differs
# The API pages a board a hundred pages deep at most, so a harvested field of
# this size or more says "ten thousand or more", not a count. A field at the
# cap is unknown for the purpose of comparing two editions' fields.
FIELD_CAP = 9900
# One curve does not fit every field. The same `q = rank / field` is a different
# place in a division of three hundred qualified teams, where everyone plays
# every game, and in an open queue of ten thousand, where half the field plays
# one. Fitted on all boards at once the exponent lands on 0.88 - too steep for
# the big queues past rank 500 and too flat for the small lobbies - and the
# forecast comes out a fifth low at rank 2,500 of the one and a quarter low at
# rank 250 of the other. So the curve is fitted once per band of field size
# (upper edges below; the last band is the API's ceiling and everything above
# it), and the cascade reads the band the cup falls in. The pooled fit stays
# as the fallback for a band with too few boards. Measured on 600 held-out
# cups, the banded curves cut the error at the deep ranks from 16.3 % to
# 14.0 % and, where no edition of the cup exists, from 16.9 % to 14.1 %.
FIELD_BANDS = (300, 1000, 3000, FIELD_CAP)
CURVE_MIN_BOARDS = 200
# When the previous edition admitted a different ranked tier - Unreal alone
# one week, Diamond upwards the next - and no edition with the same bar exists
# to read instead, its band widens by this much: the field is another size.
# The same widening applies when the last edition was played in another
# format - Trio where this one is Duo, Reload where this one is Battle Royale
# - and no edition in this cup's own format exists to read instead.
WIDEN_ENTRY = 1.5
# How far along the curve a known field is allowed to move the previous
# edition's value, in log terms: a fifth either way, about +22 % / -18 %.
# Measured on the newest 600 tournaments read from the 6,632 before them, the
# full move cuts the error at these rows from 6.3 % to 5.0 %, and capped here
# to 4.7 %: beyond a fifth the curve extrapolates further than the data goes.
# The move's own uncertainty - the fit puts the true slope anywhere from half
# the move to all of it - is added to the band as this share of the move.
FIELD_MOVE_CAP = 0.2
FIELD_MOVE_REL = 0.3

LOBBY_THIN = 5                     # below this many finals the band widens
LOBBY_WIDEN = {"team": 1.0, "mode": 1.15, "any": 1.3, "thin": 1.3}
SHAPE_FLOOR = 0.02

# Which scopes a measured shape may be read from, by where the level was
# anchored. The mode alone is deliberately absent. A ratio at rank 1000 depends
# on how many teams there are, and a mode groups a five-hundred-team cup with a
# ten-thousand-team one; its median ratio describes neither. Measured, that
# scope doubled the error past rank 500 while every other band improved.
#
# The ladder is the pooled shape that does carry the field: every open-queue
# board of the same band of field size (LADDER_BANDS, below) and game mode,
# its thresholds relative to rank 20, one median per rank. It answers where the
# cup's own editions have not been to the rank - and for a level read off the
# scoring table alone, which has no editions at all. The curve used to answer
# there, and it is too steep at the deep end: measured on 416 open-queue
# boards played after 15 August, read from the ones before, the curve's
# ratio to rank 100 ran 9 % low at rank 500, 13 % low at rank 1,000 and 8 %
# low at 2,500 (a fifth to a third low for fields of one to three thousand);
# the ladder ran 0.2 %, 1.3 % and 3.3 % high, and its median error was 4.4,
# 7.6 and 13.8 % against the curve's 9.9, 15.7 and 15.5. The curve stays as
# the fallback for a band and rank the ladder has too few boards at.
SHAPE_SCOPES = {"category": ("category", "family", "ladder"),
                "family": ("family", "ladder"),
                "scoring": ("ladder",), "mode": ("ladder",)}

# Deepest rank a cup's own tables are allowed to answer. Past it the table lost
# to the curve — 28.4 % against 17.3 % beyond rank 500 — for two reasons that
# compound: few editions are harvested deep enough to reach those ranks, and the
# ratio there depends heavily on the field size, which a category's table does
# not carry. The ladder carries it and is read to any depth it has boards at.
SHAPE_MAX_RANK = 500
# The ladder's bands of field size, finer than the curve's: rank 1,000 is the
# last place of a 1,100-team queue and the middle of a 2,900-team one, and
# one median for both - the curve's band - read 0.21 of rank 20 where the two
# ends read 0.10 and 0.40, with a spread of 87 % to say so. Eleven bands
# (upper edges; the last is the API's ceiling and everything above it) leave
# a hundred boards or more in each and, out of sample, read rank 500 5.6 %
# off against 6.0 with the curve's four, rank 1,000 9.6 against 10.7.
LADDER_BANDS = (300, 450, 650, 1000, 1400, 2000, 3000, 4500, 6500, FIELD_CAP)
# The ranks the ladder is built on: the harvest's fixed fifteen, which every
# board carries. A cut's rank - 300, 2,000 - is on file for the cups played
# for it alone, and a median over those cups' boards is not the band's: at
# rank 300 it read above rank 250. Between two fixed ranks the ladder is read
# log-linear, like the other tables.
LADDER_RANKS = (1, 3, 5, 10, 20, 25, 50, 100, 120, 250, 500, 1000, 2500, 5000, 10000)
# Boards a rank of the ladder needs before it speaks; the game mode's row is
# read first, then the band's row across game modes. Checked on the rolling
# validation (5,874 thresholds): the ladder halves the shape error at ranks
# 26-500 (6.9 -> 3.5 %) and takes the bias out of the deep end (-10.8 -> +5.6
# past rank 500) where the level came from the scoring table.
LADDER_MIN = 20

# How many editions of a cup speak for its current level. See `_recent`.
RECENT_EDITIONS = 1

# Seasons. A new season moves every cup's level at once - a new loot pool, a
# new crowd - and the move is shared: at the turn of season 42 the cups that
# had run in both seasons came back 4 to 6 % higher at the top hundred and 9
# to 12 % higher past rank 500, in every region; at the turn of season 41,
# nothing moved. So an edition read across a season boundary is moved by what
# the season's earlier cups showed, per band of rank (upper edges below; 0 is
# everything deeper), shrunk toward zero by n / (n + SEASON_SHRINK) so that
# three noisy pairs do not swing every cup of the week. Measured out of
# sample on the last three boundaries (the shift known only from cups played
# on earlier days), the first edition of a season is read 3.8 -> 3.6 % off
# at the top hundred, 5.3 -> 4.8 % between 100 and 500, 8.7 -> 8.1 % deeper;
# on a boundary that did not move it costs about a point in the middle band.
# A rank worth under SEASON_FLOOR of the same edition's rank 20 is the bottom
# of a small board, not a threshold, and is left out of the measurement.
SEASON_BANDS = ((1, 100), (101, 500), (501, 0))
SEASON_SHRINK = 10
SEASON_FLOOR = 0.25
# The band's own dispersion once the shift is taken out, at the 80th
# percentile like `direct_tables`' rel, is what a cross-season reading adds
# to its band; before any cup of the new season has run, the dispersion
# every past boundary showed, pooled - or these, for a history without one.
SEASON_SPREAD = {"100": 0.06, "500": 0.09, "0": 0.12}

# How a threshold moves when the number of games moves. Not 1.0: a session twice
# as long does not double what twentieth place scores, because the field improves
# with it. `DEFAULT_EXPONENT` is the same number the per-game live model uses.
GAMES_EXPONENT = DEFAULT_EXPONENT


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
def _shape_rows(seen: dict, least: int = SHAPE_MIN) -> dict:
    out = {}
    for rank, values in seen.items():
        if len(values) < least:
            continue
        mid = statistics.median(values)
        if mid <= 0:
            continue
        devs = sorted(abs(v / mid - 1) for v in values)
        rel = devs[min(len(devs) - 1, int(0.8 * len(devs)))]
        out[str(rank)] = {"median": round(mid, 4), "n": len(values),
                          "rel": round(max(rel, SHAPE_FLOOR), 4)}
    return out


# --------------------------------------------------------------------------- #
# How the tables are keyed
# --------------------------------------------------------------------------- #
# A cup is the same cup only in the same format. "FNCS Division 1 Practice"
# ran as Trio lobbies of thirty-three one season and Duo lobbies of fifty the
# next; the Performance Evaluation alternates Battle Royale and Reload weeks.
# The narrow circle (`comparable_history`) has always keyed on both modes; the
# wide tables keyed on the name and the region alone, so the first Duo week
# read the last Trio edition as its previous edition, straight, and priced
# rank 250 of a 500-team field off rank 250 of a 150-team one. Measured on
# 600 held-out cups, keying the tables on the format too and reading the
# other format only as a widened fallback (see `direct_from`) cut the error
# on the finals that had changed lobby size from 30 % to 5 %.
def category_name(comp: dict) -> str:
    return str((comp.get("kind"), comp.get("region"),
                comp.get("team_mode") or "", comp.get("game_mode") or ""))


def family_name(comp: dict) -> str:
    return str((comp.get("kind"), comp.get("team_mode") or "", comp.get("game_mode") or ""))


def category_kin(comp: dict) -> str:
    """The cup in the region, whatever the format: what the fallback walks."""
    return str((comp.get("kind"), comp.get("region")))


def broad_for(base: dict, peers: list[dict], comp: dict, curve=None) -> dict:
    """`base`, with everything `comp` looks up about its own family taken from `peers`.

    `analysis.validate.learning_curve` thins a tournament's comparable editions to
    measure what each extra one is worth. Only that tournament's own category and
    family entries may change when they are thinned — every other group in `base`
    is untouched by it — so rebuilding the whole wide sample per draw is a day of
    work for two dictionary entries.

    The mode-level and scoring-level fallbacks keep `base`'s numbers, which still
    see the category's other editions. That is a small leak, and it lives in a
    diagnostic rather than in the model: it makes the flat part of the learning
    curve slightly optimistic and does not touch a forecast.
    """
    part = broad_stats(peers, curve=curve or base.get("curve"), curves=base.get("curves"))
    names = {"category": category_name(comp), "family": family_name(comp)}
    out = dict(base)
    for block in ("reference_pace", "field_sizes", "shape"):
        into = dict(base.get(block) or {})
        for scope, name in names.items():
            found = ((part.get(block) or {}).get(scope) or {}).get(name)
            into[scope] = {name: found} if found else {}
        out[block] = into
    return out


def shape_tables(history: list[dict]) -> dict:
    """`measured_shape`, grouped the three ways the level anchor is grouped.

    Deliberately the same keys as `reference_pace`, so the shape can be read at
    whichever scope the level was read at. A forecast that took its level from
    the family and its shape from the category would be describing two different
    populations and calling the result one tournament.
    """
    out: dict = {}
    for key, extract in (("category", category_name),
                         ("family", family_name),
                         ("mode", lambda c: str((c.get("game_mode"), c.get("team_mode"))))):
        groups: dict = {}
        for comp in history:
            base = predict.final_value(comp, REFERENCE_RANK)
            if not base or base <= 0:
                continue
            bucket = groups.setdefault(extract(comp), {})
            for rank in comp.get("ranks") or ():
                rank = int(rank)
                if rank == REFERENCE_RANK:
                    continue
                value = predict.final_value(comp, rank)
                if value and value > 0:
                    bucket.setdefault(rank, []).append(value / base)
        out[key] = {name: rows for name, seen in groups.items()
                    if (rows := _shape_rows(seen))}
    # The ladder: the same ratios pooled by band of field size and game mode,
    # over open queues only - a closed lobby's ladder is another thing, read by
    # `lobby_estimate`. Each board counts in its game mode's row and in the
    # band's row across game modes, the fallback for a mode with few boards.
    ladders: dict = {}
    for comp in history:
        field = int(comp.get("field_size") or 0)
        if field <= 0 or single_lobby(comp, field):
            continue
        base = predict.final_value(comp, REFERENCE_RANK)
        if not base or base <= 0:
            continue
        for name in ladder_names(field, comp.get("game_mode") or ""):
            bucket = ladders.setdefault(name, {})
            for rank in comp.get("ranks") or ():
                rank = int(rank)
                if rank == REFERENCE_RANK or rank not in LADDER_RANKS:
                    continue
                value = predict.final_value(comp, rank)
                if value and value > 0:
                    bucket.setdefault(rank, []).append(value / base)
    out["ladder"] = {name: rows for name, seen in ladders.items()
                     if (rows := _shape_rows(seen, LADDER_MIN))}
    return out


def ladder_band(field: int) -> int:
    """Which of LADDER_BANDS a field falls in: the index of its upper edge,
    or one past the end at or above the API's ceiling."""
    field = int(field or 0)
    for i, edge in enumerate(LADDER_BANDS):
        if field < edge:
            return i
    return len(LADDER_BANDS)


def ladder_names(field: int, game_mode: str) -> list[str]:
    """The ladder rows a field of this size reads, most specific first: its
    band of field size in this game mode, then the band across game modes.
    None below the first band's edge: a queue of under three hundred is a
    mixed bag of small formats, and the curve fitted on them read better."""
    band = ladder_band(field)
    if band == 0:
        return []
    return [str((band, game_mode or "")), str((band, ""))]


def shape_scopes(source: str) -> tuple:
    """The scopes a level anchored on `source` may read its shape from. The
    scoring rung names its sample size in its source - "scoring (prior)" -
    and every spelling reads the same scopes."""
    source = str(source or "")
    return SHAPE_SCOPES.get(source) or SHAPE_SCOPES.get(source.split(" ")[0], ())


def bracket(table: dict, rank: int, usable) -> tuple | None:
    """The measured ranks either side of `rank`, or None if it is not inside them.

    The harvest keeps a threshold at ranks 1, 3, 5, 10, 20, 25, 50, 100 and so
    on, so a table never has rank 8. Asked for it, the cascade used to fall
    through to the curve — which, in a twenty-team lobby where rank 8 is
    two-fifths of the way down, said more than rank 5 did. Between two
    measured ranks the ladder is read off the measured ranks: log-linear in
    the rank, which is the interpolation the curve itself is a straight line
    under. Beyond the deepest measured rank the caller falls through as before.
    """
    rank = int(rank)
    lo = hi = None
    for key, entry in (table or {}).items():
        try:
            at = int(key)
        except (TypeError, ValueError):
            continue
        if not usable(entry):
            continue
        if at < rank and (lo is None or at > lo):
            lo = at
        elif at > rank and (hi is None or at < hi):
            hi = at
    if lo is None or hi is None:
        return None
    f = (math.log(rank) - math.log(lo)) / (math.log(hi) - math.log(lo))
    return lo, hi, f


def interpolated(v_lo: float, v_hi: float, f: float) -> float:
    return math.exp((1 - f) * math.log(v_lo) + f * math.log(v_hi))


def shape_from(comp: dict, calib: dict, rank: int, source: str,
               field: int | None = None) -> tuple[float, float, str] | None:
    """(ratio, relative uncertainty, scope) at this rank, or None for the curve.

    Read at the scope the level came from, then wider, then off the ladder of
    every open queue of this field size and game mode. A level anchored on the
    scoring table has no editions of its own, so it starts at the ladder.

    Three editions is not enough to trust a measured dispersion on its own, so
    the measured spread and the curve's are blended in variance, the measurement
    winning as the sample grows. A rank the table does not hold is read between
    the two it does, when it lies between them — see `bracket`. `field` is the
    field the forecast settled on, typed or guessed: what picks the ladder.
    """
    tables = (calib or {}).get("shape") or {}
    names = {"category": category_name(comp), "family": family_name(comp),
             "mode": str((comp.get("game_mode"), comp.get("team_mode")))}
    field = int(field if field is not None else (comp.get("field_size") or 0))

    def usable_for(least):
        return lambda entry: bool(entry) and entry.get("n", 0) >= least and bool(entry.get("median"))

    def read(entry):
        n = entry["n"]
        weight = n / (n + 4)
        rel = math.sqrt(weight * entry["rel"] ** 2 + (1 - weight) * SHAPE_FALLBACK_REL ** 2)
        return float(entry["median"]), rel

    def off(table, least):
        usable = usable_for(least)
        entry = table.get(str(int(rank)))
        if usable(entry):
            return read(entry)
        found = bracket(table, rank, usable)
        if found:
            lo, hi, f = found
            (r_lo, rel_lo), (r_hi, rel_hi) = read(table[str(lo)]), read(table[str(hi)])
            return interpolated(r_lo, r_hi, f), max(rel_lo, rel_hi)
        return None

    for scope in shape_scopes(source):
        if scope == "ladder":
            if field <= 0:
                continue
            for name in ladder_names(field, comp.get("game_mode") or ""):
                got = off((tables.get("ladder") or {}).get(name) or {}, LADDER_MIN)
                if got:
                    return got[0], got[1], "ladder"
            continue
        # A cup's own tables answer down to SHAPE_MAX_RANK only, see there.
        if int(rank) > SHAPE_MAX_RANK:
            continue
        got = off((tables.get(scope) or {}).get(names[scope]) or {}, SHAPE_MIN)
        if got:
            return got[0], got[1], scope
    return None


def counted_field(comp: dict) -> int:
    """The field as a count, or 0 when it is the API's ceiling rather than a count.

    A harvested board is paged a hundred pages deep at most, so a harvested
    field of FIELD_CAP or more says "ten thousand or more". A field typed in,
    or read off a qualification cut, is a count whatever its size.
    """
    field = int(comp.get("field_size") or 0)
    if field <= 0:
        return 0
    if str(comp.get("source") or "") == "osirion" and field >= FIELD_CAP:
        return 0
    return field


# The previous edition, smoothed. A cup's latest edition is one evening, and
# at the top of the board one evening is one team's great night: read straight
# it carries that night into next week's forecast. The editions before it,
# when they were played the same way - same number of games, same scoring
# table, same entry bar, same season - say the same thing with more nights
# behind it. So the reading is an exponentially weighted average of that run,
# in log terms, the latest edition weighing DIRECT_ALPHA and each one before
# it that share of what is left; a run of one is the latest edition, as
# before. Measured on the cups played since June with three editions or more
# in their run, against the latest edition read straight: 2.83 -> 2.65 %
# median error at ranks 1-25, 1.45 -> 1.28 at 26-100, 2.19 -> 2.10 at
# 101-500, 4.96 -> 4.47 deeper. A trend - the last move carried forward, even
# damped - lost everywhere (2.83 -> 3.25 at the top): cups do not drift, they
# wobble. At most DIRECT_RUN editions are read.
# Through the rolling validation (each tournament forecast from what had
# finished before its day), the 1,711 thresholds of 193 cups the smoothing
# changes: median error unchanged (2.84 %), mean 7.24 -> 6.59 % (95 % interval
# of the gain, resampling cups: 0.33 to 1.02 points), 90th percentile 14.0 ->
# 13.4 %. The weight is the one that keeps the median: 0.8 gains less on the
# mean (6.71 %), 0.6 and 0.5 start to cost it (2.96 %, 3.06 %).
DIRECT_ALPHA = 0.7
DIRECT_RUN = 6


def format_of(comp: dict) -> str:
    """How an edition was played, as far as its thresholds are concerned: the
    number of games and the scoring table. Two editions that differ here are
    two ladders, whatever their name."""
    scoring = comp.get("scoring") or {}
    if isinstance(scoring, str):
        try:
            scoring = json.loads(scoring)
        except ValueError:
            scoring = {}
    return f"{int(comp.get('max_games') or 0)}|{table_signature(scoring, 0)}"


def smoothed_run(dated: list[tuple]) -> tuple[float, int]:
    """(value, editions read) for one rank: the latest edition, averaged with
    the ones just before it played the same way - see DIRECT_ALPHA.

    `dated` is sorted oldest first; each item is (date, value, entry, field,
    season, format). The run stops at the first edition, going back, that
    was played under another entry bar, format or season.
    """
    latest = dated[-1]
    run = [latest]
    for item in reversed(dated[:-1]):
        if len(run) >= DIRECT_RUN or (item[2], item[4], item[5]) != (latest[2], latest[4], latest[5]):
            break
        run.append(item)
    run.reverse()
    level = math.log(run[0][1])
    for item in run[1:]:
        level = DIRECT_ALPHA * math.log(item[1]) + (1 - DIRECT_ALPHA) * level
    return math.exp(level), len(run)


def direct_tables(history: list[dict], seasons: list | None = None) -> dict:
    """What the previous edition of each cup scored at each rank, read straight.

    This is the carry-forward baseline, made the first rung of the model — and
    it is there because it kept winning. The level-times-shape decomposition
    has a defect no amount of tuning removes: a strong evening lifts rank 1 and
    rank 20 together, and a forecast that multiplies one edition's level by a
    ratio taken from other editions throws that correlation away. Two errors
    compose where a direct reading carries one. Measured on the newest six
    hundred tournaments, forecast chronologically: 13.2 % against 5.5 % in the
    top five, 9.4 % against 5.7 % overall.

    Category scope only — the same cup in the same region and the same
    format. A reading from another region is not a previous edition, it is a
    different population; one from another team size or game mode is another
    ladder altogether, and `direct_from` reads it only as a widened fallback.

    `rel` is the 80th percentile of the relative move between consecutive
    editions at that rank: the honest width of "next time will be like last
    time", measured on this cup rather than assumed.
    """
    by_name: dict = {}
    for comp in history:
        name = category_name(comp)
        entry = str(comp.get("entry") or "")
        field = counted_field(comp)
        season = season_of(comp, seasons)
        fmt = format_of(comp)
        for rank in comp.get("ranks") or ():
            value = predict.final_value(comp, int(rank))
            if value and value > 0:
                by_name.setdefault(name, {}).setdefault(int(rank), []).append(
                    (str(comp.get("start_time") or ""), value, entry, field, season, fmt))
    out: dict = {}
    for name, ranks in by_name.items():
        table = {}
        for rank, dated in ranks.items():
            dated.sort(key=lambda d: (d[0], d[1]))
            values = [d[1] for d in dated]
            moves = sorted(abs(b / a - 1) for a, b in zip(values, values[1:]) if a > 0)
            rel = moves[min(len(moves) - 1, int(0.8 * len(moves)))] if moves else None
            latest = dated[-1]
            # The edition read is dated and placed in its season: a rank the
            # latest edition did not publish is read off an older one, and
            # the model has to know when that one was played to carry it
            # across a turn of season - see `season_move`. The value is the
            # latest edition's, smoothed with the ones before it that were
            # played the same way - see `smoothed_run`.
            value, run = smoothed_run(dated)
            row = {"value": round(value, 2), "n": len(values),
                   "rel": round(max(rel, SHAPE_FLOOR), 4) if rel is not None else None,
                   "field": latest[3] or 0, "entry": latest[2], "date": latest[0][:10],
                   "season": latest[4], "run": run}
            # The last edition under each other entry bar: what to read when
            # the cup coming up asks for that bar rather than the latest one.
            alt = {}
            for date, value, entry, field, season, _ in dated:
                if entry and entry != latest[2]:
                    alt[entry] = [round(value, 2), field or 0, date[:10], season]
            if alt:
                row["alt"] = alt
            table[str(rank)] = row
        out[name] = table
    return out


def direct_kin(direct: dict) -> dict:
    """The formats each cup has run in, per cup and region, newest first.

    What `direct_from` walks when the cup's own format has no table: every
    other format of the same cup in the same region, the one played most
    recently first, ties broken on the format's name so that two ports agree.
    Each entry is [table name, latest edition date, team mode, game mode].
    """
    import ast
    kin: dict = {}
    for name, table in (direct or {}).items():
        kind, region, team, game = ast.literal_eval(name)
        latest = max((row.get("date") or "") for row in table.values()) if table else ""
        kin.setdefault(str((kind, region)), []).append([name, latest, team, game])
    for rows in kin.values():
        # Newest first; among equal dates the format whose names sort last.
        # Plain string order on the three fields, so a port sorting the same
        # three strings lands on the same table.
        rows.sort(key=lambda r: (r[1], r[2], r[3]), reverse=True)
    return kin


def season_number(label) -> int | None:
    """"S42" -> 42; nothing for a row that names no season."""
    found = re.search(r"(\d+)", str(label or ""))
    return int(found.group(1)) if found else None


def season_table(history: list[dict]) -> list:
    """Every season the history names and the day it was first seen on,
    [[number, "YYYY-MM-DD"], ...] oldest first: what an edition that names no
    season - typed by hand - is dated into."""
    first: dict = {}
    for comp in history:
        number = season_number(comp.get("season"))
        day = str(comp.get("start_time") or "")[:10]
        if number is None or len(day) < 10:
            continue
        if number not in first or day < first[number]:
            first[number] = day
    return [[number, first[number]] for number in sorted(first)]


def season_of(comp: dict, seasons: list | None) -> int | None:
    """The season a tournament belongs to: the one its id names, else the
    latest season that had begun on its day, else none."""
    number = season_number(comp.get("season"))
    if number is not None:
        return number
    day = str(comp.get("start_time") or "")[:10]
    found = None
    if len(day) >= 10:
        for entry in seasons or []:
            if str(entry[1]) <= day:
                found = int(entry[0])
    return found


def season_band(rank: int) -> str:
    """The band of rank a season's move is measured over, named by its upper
    edge: "100", "500", "0" for everything deeper."""
    for lo, hi in SEASON_BANDS:
        if rank >= lo and (not hi or rank <= hi):
            return str(hi)
    return "0"


def season_shifts(history: list[dict], seasons: list) -> dict:
    """How the level moved at each turn of season, per band of rank.

    One pair per category: its last edition of one season against its first
    of the next (consecutive seasons, the same number of games), at every
    rank both hold. Returns {"boundaries": [[from, to, {band: [shift, spread,
    n]}], ...], "fallback": {band: spread}}: `shift` the median log-move,
    shrunk by n / (n + SEASON_SHRINK); `spread` the 80th percentile of the
    moves' distance from it; `fallback` that spread pooled over every
    boundary, for a turn of season no cup has crossed yet.
    """
    known = [int(entry[0]) for entry in seasons or []]
    by_name: dict = {}
    for comp in history:
        by_name.setdefault(category_name(comp), []).append(comp)
    moves: dict = {}
    for comps in by_name.values():
        comps = sorted(comps, key=lambda c: str(c.get("start_time") or ""))
        for a, b in zip(comps, comps[1:]):
            since, until = season_of(a, seasons), season_of(b, seasons)
            if since is None or until is None or until <= since:
                continue
            if any(since < n < until for n in known):
                continue
            if int(a.get("max_games") or 0) != int(b.get("max_games") or 0):
                continue
            ref_a = predict.final_value(a, REFERENCE_RANK)
            ref_b = predict.final_value(b, REFERENCE_RANK)
            if not ref_a or not ref_b:
                continue
            for rank in set(a.get("ranks") or ()) & set(b.get("ranks") or ()):
                va, vb = predict.final_value(a, int(rank)), predict.final_value(b, int(rank))
                if not va or not vb or va < SEASON_FLOOR * ref_a or vb < SEASON_FLOOR * ref_b:
                    continue
                moves.setdefault((since, until), {}).setdefault(
                    season_band(int(rank)), []).append(math.log(vb / va))
    boundaries, pooled = [], {}
    for (since, until), bands in sorted(moves.items()):
        table = {}
        for band, values in bands.items():
            n = len(values)
            shift = statistics.median(values) * n / (n + SEASON_SHRINK)
            gaps = sorted(abs(v - shift) for v in values)
            spread = gaps[min(n - 1, int(0.8 * n))]
            table[band] = [round(shift, 4), round(max(spread, SHAPE_FLOOR), 4), n]
            pooled.setdefault(band, []).extend(gaps)
        boundaries.append([since, until, table])
    fallback = {}
    for band in SEASON_SPREAD:
        gaps = sorted(pooled.get(band) or [])
        fallback[band] = (round(max(gaps[min(len(gaps) - 1, int(0.8 * len(gaps)))], SHAPE_FLOOR), 4)
                          if gaps else SEASON_SPREAD[band])
    return {"boundaries": boundaries, "fallback": fallback}


def season_move(calib: dict | None, since, until, rank: int) -> tuple[float, float, int]:
    """(shift, spread, pairs): how a reading from season `since` is carried
    to season `until` at this rank - the turns of season between them summed,
    in log terms, each unknown one counted at the fallback spread and no
    shift. Nothing when the seasons are the same, or either is unknown."""
    if since is None or until is None or int(until) <= int(since):
        return 0.0, 0.0, 0
    since, until = int(since), int(until)
    shifts = (calib or {}).get("season_shifts") or {}
    known = [int(entry[0]) for entry in (calib or {}).get("seasons") or []]
    band = season_band(rank)
    steps = [n for n in known if since < n <= until]
    if until not in known:
        steps.append(until)
    table = {(int(b[0]), int(b[1])): b[2] for b in shifts.get("boundaries") or []}
    fallback = float((shifts.get("fallback") or {}).get(band, SEASON_SPREAD[band]))
    shift, var, pairs, previous = 0.0, 0.0, 0, since
    for step in steps:
        row = (table.get((previous, step)) or {}).get(band)
        if row:
            shift += float(row[0])
            var += float(row[1]) ** 2
            pairs += int(row[2])
        else:
            var += fallback ** 2
        previous = step
    return shift, math.sqrt(var), pairs


def direct_from(comp: dict, calib: dict, rank: int, field: int | None = None) -> dict | None:
    """The previous edition, read straight: {value, rel, n, note, field_effect}, or None.

    Which edition is "previous" depends on who was let in. The same cup one
    week admitting Unreal alone and the next Diamond upwards is two fields of
    different sizes, and a threshold at rank 4,000 follows the field: so when
    the cup coming up states its entry bar, the last edition with the *same*
    bar is read, and when none exists the latest edition is read with its
    band widened. Then the field: when both the cup's field and the edition's
    are counts - typed in, or a later round's cut, never a harvested board's
    ten-thousand ceiling, see `counted_field` - the value moves along the
    curve from the edition's share of its field to this cup's share of its
    own, and `field_effect` says by how much it did.

    A rank the edition did not publish is read between the two it did, when it
    lies between them — see `bracket`; deeper than its deepest rank, None.

    The tables are kept per format. When the cup has never run in this one -
    the first Duo week after a Trio season - the last edition in any other
    format is read instead, band widened by `WIDEN_ENTRY` like a different
    entry bar, unless the cup is a single lobby: a lobby of fifty read off a
    lobby of thirty-three was wrong by 30 % where the closed-lobby rung is
    wrong by 5 %, so a lobby that changed size falls through to that rung.
    `field` is the field the caller settled on, typed or guessed, which is
    what decides whether the cup is one lobby.
    """
    tables = (calib or {}).get("direct") or {}
    want = str(comp.get("entry") or "")
    field_now = counted_field(comp)
    curve = curve_for(calib, field if field is not None else field_now)
    # The season the cup coming up is played in: what an edition read from
    # an earlier season is carried into, see `season_move`.
    until = season_of(comp, (calib or {}).get("seasons"))

    def usable(entry):
        return bool(entry) and bool(entry.get("value"))

    def read(entry):
        n = entry["n"]
        pairs = max(n - 1, 0)
        measured = entry.get("rel")
        fallback = ANCHOR_SPREAD["category"]
        if measured is None:
            rel = fallback * 1.5                        # one edition: widened, as elsewhere
        else:
            weight = pairs / (pairs + 2)
            rel = math.sqrt(weight * measured ** 2 + (1 - weight) * fallback ** 2)
        value, field, note = float(entry["value"]), int(entry.get("field") or 0), None
        theirs = str(entry.get("entry") or "")
        # The season and date of the edition actually read: the latest, or
        # the last one under the bar asked for.
        since, played = entry.get("season"), str(entry.get("date") or "")
        if want and theirs and want != theirs:
            alt = (entry.get("alt") or {}).get(want)
            # The last edition under the bar asked for, when it was played in
            # the same season as the latest: measured, an edition a season
            # back under the right bar reads worse than last week's under
            # the wrong one, at every rank.
            theirs_season = alt[3] if alt and len(alt) > 3 else None
            if alt and (theirs_season is None or since is None or int(theirs_season) == int(since)):
                value, field = float(alt[0]), int(alt[1] or 0)
                played = str(alt[2]) if len(alt) > 2 and alt[2] else played
                since = theirs_season if theirs_season is not None else since
                note = ("same_entry",)
            else:
                rel *= WIDEN_ENTRY
                note = ("other_entry", theirs)
        effect = 0.0
        if field_now > 0 and field > 0 and field != field_now:
            move = field_move(rank, field_now, field, curve)
            value *= math.exp(move)
            rel = math.sqrt(rel ** 2 + (FIELD_MOVE_REL * move) ** 2)
            effect = 100 * (math.exp(move) - 1)
        # Read across a turn of season: moved by what the season's earlier
        # cups showed, band widened by what is left once that is taken out.
        shift, spread, crossed = season_move(calib, since, until, rank)
        season = {"effect": 0.0, "pairs": 0, "since": None, "until": None, "date": played}
        if spread > 0:
            value *= math.exp(shift)
            rel = math.sqrt(rel ** 2 + spread ** 2)
            season = {"effect": 100 * (math.exp(shift) - 1), "pairs": crossed,
                      "since": int(since), "until": int(until), "date": played}
        return value, rel, n, note, effect, season

    def from_table(table):
        entry = (table or {}).get(str(int(rank)))
        if usable(entry):
            value, rel, n, note, effect, season = read(entry)
            return {"value": value, "rel": rel, "n": n, "note": note, "field_effect": effect,
                    "season": season}
        found = bracket(table, rank, usable)
        if not found:
            return None
        lo, hi, f = found
        (v_lo, rel_lo, n_lo, note, e_lo, s_lo), (v_hi, rel_hi, n_hi, _, e_hi, s_hi) = \
            read(table[str(lo)]), read(table[str(hi)])
        # Between two ranks read off two editions, the season note is the
        # lower rank's: the nearer of the two to what was asked.
        return {"value": interpolated(v_lo, v_hi, f), "rel": max(rel_lo, rel_hi), "n": min(n_lo, n_hi),
                "note": note, "field_effect": (1 - f) * e_lo + f * e_hi,
                "season": dict(s_lo, effect=(1 - f) * s_lo["effect"] + f * s_hi["effect"])}

    own = category_name(comp)
    found = from_table(tables.get(own))
    if found:
        return found
    if own in tables or single_lobby(comp, field if field is not None else field_now):
        # The cup has run in this format and never published this rank: the
        # cascade goes on to the level and the shape, not to another format.
        return None
    kin = ((calib or {}).get("direct_kin") or {}).get(category_kin(comp)) or []
    for name, _, team, game in kin:
        found = from_table(tables.get(name))
        if found:
            found["rel"] *= WIDEN_ENTRY
            found["note"] = ("other_format", team, game)
            return found
    return None


def broad_stats(wide: list[dict], curve: tuple | list | None = None,
                curves: list | None = None) -> dict:
    """The part of a calibration that depends on the wide sample and nothing else.

    Split out because it is the expensive part and because it is the *same* part
    for every tournament being calibrated. `fit_curve` sweeps the exponent over
    every threshold in the sample: at sixty-six tournaments that is milliseconds,
    at ten thousand it is the better part of a minute, and anything that calls
    `calibrate` in a loop pays it once per iteration for an answer that does not
    change. Compute it once, hand it back.

    `wide` is filtered here, so callers pass whatever they have. `curve` (and
    `curves`, the fits by field band) skip the fit itself, for callers that vary
    the sample by a handful of tournaments and know the shape cannot have moved
    — `analysis.validate.learning_curve` resamples the level tables eighty
    times per tournament and the exponent sweep is the only part that would
    not survive it.
    """
    done = [c for c in wide if predict.is_complete(c)]
    curve = tuple(curve) if curve else fit_curve(done)
    curves = list(curves) if curves is not None else fit_curves(done)
    seasons = season_table(done)
    direct = direct_tables(done, seasons)
    return {"curve": list(curve), "curves": curves,
            "reference_pace": reference_pace(done, curve, curves, seasons),
            "field_sizes": field_sizes(done), "shape": shape_tables(done),
            "direct": direct, "direct_kin": direct_kin(direct), "n_curve": len(done),
            # The seasons the history spans, and how the level moved at each
            # turn of one: what an edition read across a boundary is moved by.
            "seasons": seasons, "season_shifts": season_shifts(done, seasons)}


def field_band(field: int) -> int:
    """Which band of field size a cup falls in: the index of its upper edge in
    FIELD_BANDS, or one past the end for a field at or above the API's ceiling."""
    field = int(field or 0)
    for i, edge in enumerate(FIELD_BANDS):
        if field < edge:
            return i
    return len(FIELD_BANDS)


def fit_curves(history: list[dict]) -> list:
    """One curve per band of field size, where the band has boards enough.

    Each entry is [lower edge, upper edge, a, b]; the upper edge of the last
    band is 0, meaning none. A band below `CURVE_MIN_BOARDS` boards is left
    out and the pooled curve answers for it.
    """
    out = []
    edges = (0,) + FIELD_BANDS + (0,)
    pooled, railed = LAST_FIT.get("railed"), []
    for i in range(len(FIELD_BANDS) + 1):
        lo, hi = edges[i], edges[i + 1]
        boards = [c for c in history if field_band(c.get("field_size") or 0) == i
                  and (c.get("field_size") or 0) > 0]
        if len(boards) < CURVE_MIN_BOARDS:
            continue
        fit = fit_curve(boards)
        if LAST_FIT.get("railed") is not None:
            railed.append((lo, hi, LAST_FIT["railed"]))
        if fit == CURVE_DEFAULT:
            continue                    # the sweep found nothing it believed
        out.append([lo, hi, fit[0], fit[1]])
    # The pooled fit's verdict is the one callers read; the bands' go alongside.
    LAST_FIT["railed"] = pooled
    LAST_FIT["railed_bands"] = railed
    return out


def curve_for(calib: dict | None, field: int | None) -> tuple:
    """The curve for a field of this size: its band's fit, else the pooled one."""
    pooled = tuple((calib or {}).get("curve") or CURVE_DEFAULT)
    field = int(field or 0)
    if field <= 0:
        return pooled
    band = field_band(field)
    edges = (0,) + FIELD_BANDS + (0,)
    lo, hi = edges[band], edges[band + 1]
    for row in (calib or {}).get("curves") or ():
        if int(row[0]) == lo and int(row[1]) == hi:
            return float(row[2]), float(row[3])
    return pooled


def calibrate(history: list[dict], ranks: list[int], wide: list[dict] | None = None,
              broad: dict | None = None) -> dict:
    """Aggregate the measurements over every comparable finished competition.

    `history` is the narrow circle — the genuinely comparable tournaments, which
    feed the ratios and the model errors. `wide` is everything we know: the shape
    of the curve depends on neither the scoring table nor the region, so it has
    everything to gain from being fitted on the lot.

    `broad` short-circuits the wide half with an already-computed `broad_stats`.
    Callers that calibrate many circles against one database pass it; leaving it
    None reproduces the old behaviour exactly.
    """
    done = [c for c in history if predict.is_complete(c)]
    if broad is None:
        broad = broad_stats([c for c in (wide or history) if predict.is_complete(c)] or done)
    curve = tuple(broad["curve"])
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
        "tables_version": TABLES_VERSION,
        "shape": broad["shape"],
        "direct": broad.get("direct") or {},
        "direct_kin": broad.get("direct_kin") or {},
        "curve": list(curve),
        "curves": list(broad.get("curves") or []),
        "reference_pace": broad["reference_pace"],
        "field_sizes": broad["field_sizes"],
        "n_curve": broad["n_curve"],
        "seasons": broad.get("seasons") or [],
        "season_shifts": broad.get("season_shifts") or {},
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
    LAST_FIT["railed"] = None
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

    def at(step):
        """(error, a) at b = step / 100: a is closed-form given b."""
        b = step / 100
        gaps = [(qr ** b - q ** b, y) for q, qr, y in data]
        den = sum(g * g for g, _ in gaps)
        if den <= 0:
            return None
        a = sum(g * y for g, y in gaps) / den
        return sum((y - a * g) ** 2 for g, y in gaps), a

    # The sweep runs over exactly the range the check below accepts. It used to
    # stop at 0.90 while accepting up to 1.20, so a sample whose best exponent
    # lay above 0.90 was quietly handed the edge of the grid and no one was told
    # the optimum had never been bracketed. Coarse first, every fifth
    # hundredth, then every hundredth around the best of those: the error is
    # smooth in b, so the finest optimum lies within a coarse step of the
    # coarse one, and the sweep costs a quarter of what a flat one did - it is
    # run six times per calibration now, once pooled and once per field band.
    lo, hi = int(B_RANGE[0] * 100), int(B_RANGE[1] * 100)
    seen: dict = {}
    best = None
    for step in list(range(lo, hi + 1, 5)) + [hi]:
        if step not in seen:
            seen[step] = at(step)
        if seen[step] and (best is None or seen[step][0] < best[0]):
            best = (seen[step][0], seen[step][1], step)
    if best:
        for step in range(max(lo, best[2] - 5), min(hi, best[2] + 5) + 1):
            if step not in seen:
                seen[step] = at(step)
            if seen[step] and seen[step][0] < best[0]:
                best = (seen[step][0], seen[step][1], step)
        best = (best[0], best[1], best[2] / 100)
    if not best or not (0.3 <= best[1] <= 4.0 and B_RANGE[0] <= best[2] <= B_RANGE[1]):
        return CURVE_DEFAULT
    if best[2] in B_RANGE:
        # Railed. The data wanted to go further and the grid would not let it,
        # which nearly always means `q` is wrong — a field size that records how
        # deep the harvest went rather than how many teams were ranked will do
        # it every time.
        LAST_FIT["railed"] = best[2]
    return round(best[1], 3), round(best[2], 3)


def field_move(rank: int, field_now: int, field_then: int, curve) -> float:
    """Log of how much a threshold at `rank` moves when the field changes.

    The curve's own quantile term and nothing else: a rank is a share of the
    field, and the same share of a bigger field is a stronger team. The
    reference-rank term of `shape_ratio` is left out on purpose - it moves
    with the field too, and taking the ratio of two `shape_ratio`s says that
    a smaller field lifts the top ranks, which the data refuses. Capped at
    FIELD_MOVE_CAP either way.
    """
    a, b = curve or CURVE_DEFAULT
    if rank < 1 or not field_now or not field_then or field_now <= 0 or field_then <= 0:
        return 0.0
    q_now = min(max(rank / field_now, 1e-9), 1.0)
    q_then = min(max(rank / field_then, 1e-9), 1.0)
    move = -a * (q_now ** b - q_then ** b)
    return max(-FIELD_MOVE_CAP, min(FIELD_MOVE_CAP, move))


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
    # The reference rank, when it was actually measured, is the level — no curve
    # in the way. The median over 5/10/20/25 was worth its noise reduction when a
    # tournament was typed in by hand and often carried one arbitrary rank; on
    # harvested standings rank 20 is nearly always there, and mixing it with three
    # curve-corrected readings imports the curve's error into the one number that
    # did not have any. Measured on the harvest: the curve scatters 9.1 % at rank
    # 3-5 against 1.6 % at 16-40, so the median of the four is several times
    # noisier than the exact reading it contains.
    exact = finals.get(REFERENCE_RANK)
    if exact and exact > 0:
        return float(exact)
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


# The words that sort cups into kinds, and the platforms that sort players
# into populations. Read from the name, because the name is what both the
# database and the calendar have; a tag the harvest derived from a field the
# calendar does not carry would give the two sides different keys.
COLD_KINDS = (("fncs", "fncs"), ("champion series", "fncs"), ("cash cup", "cash-cup"),
              ("elite series", "elite-series"), ("victory cup", "victory-cup"),
              ("practice", "practice"), ("blitz", "blitz"))


def cold_signature(comp: dict) -> tuple:
    """What a cup nobody has seen before is most like.

    The cold start used to group by game mode and team size alone, which puts a
    creator's console Solo cup in the same bucket as an FNCS Solo final. Kind of
    cup, platform and whether it is a closed final each move the level the
    scoring table implies, and together they cut the cold-start error from
    12.0 % to 9.9 % on 6,539 tournaments — measured, not assumed.
    """
    # Name and category label only — the two fields the predictor's form also
    # has. The family is inside the label already, and reading it here as well
    # would give this side a word the other side cannot see.
    name = f"{comp.get('name') or ''} {comp.get('kind') or ''}".lower()
    kind = next((tag for needle, tag in COLD_KINDS if needle in name), "other")
    platform = "mobile" if "mobile" in name else ("console" if "console" in name else "pc")
    # From the category label, which every caller has — the export's form
    # carries no round number — and which is the one place the stage is
    # decided. A window whose id said Final and a hand-typed "Final" both end
    # up as "· Final" there. A later round of an open cup - "· Round 2" - is
    # its own stage: a few hundred qualified teams playing a shorter session
    # score a different share of the maximum than the open round did, and
    # pooling the two under-priced the open rounds and over-priced the later
    # ones by a fifth each way. Splitting them cut the cold-start error on
    # 600 held-out cups from 22.3 % to 19.9 %.
    label = str(comp.get("kind") or "")
    if label.endswith((" Final", "Semi-final")):
        stage = "final"
    elif LATER_ROUND.search(label):
        stage = "later"
    else:
        stage = "open"
    return (comp.get("game_mode") or "", comp.get("team_mode") or "", kind, platform, stage)


# "· Round 2" and up at the end of a category label. Round 1 never appears
# there - `db.round_of` folds it into the open round.
LATER_ROUND = re.compile(r" · Round ([2-9]|[1-9][0-9]+)$")


def reference_pace(history: list[dict], curve=None, curves: list | None = None,
                   seasons: list | None = None) -> dict:
    """Level per game, by scope — and its bridge to the scoring table."""
    out = {}

    def level_of(comp):
        # Read at the curve of the cup's own field band, like a forecast is.
        return reference_level(comp, curve_for({"curve": curve, "curves": curves},
                                               comp.get("field_size")))

    # The scoring-table -> points bridge is not the same everywhere: a Reload cup
    # with its elimination cap and a classic Battle Royale don't have the same
    # economy. So we measure it per game mode, with a global fallback.
    shares: dict = {}
    for comp in history:
        # A single lobby has no rank 20 to speak of - a heat of sixteen has its
        # winner's score read back through the curve to a "level" one tenth of
        # it - and it has its own table (`lobby_tables`). Left in here, the
        # mobile heats outnumbered the mobile cups one week and the cold start
        # for an open mobile cup dropped to a tenth of the maximum overnight.
        if single_lobby(comp, int(comp.get("field_size") or 0)):
            continue
        level = level_of(comp)
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
            # Four keys, most specific first; `anchor_level` walks them in the
            # same order and stops at the first with a measurement behind it.
            signature = cold_signature(comp)
            shares.setdefault(str(signature + (comp.get("region") or "",)), []).append(share)
            shares.setdefault(str(signature), []).append(share)
            shares.setdefault(str((game, team)), []).append(share)
            shares.setdefault(str((game, "")), []).append(share)
    out["share_of_max"] = {k: {"median": round(statistics.median(v), 3), "n": len(v)}
                          for k, v in shares.items() if v}
    out["lobby"] = lobby_tables(history)

    for key, extract in (("category", category_name),
                         ("family", family_name),
                         ("mode", lambda c: str((c.get("game_mode"), c.get("team_mode"))))):
        groups: dict = {}
        for comp in history:
            level = level_of(comp)
            games = comp.get("max_games") or 0
            if not level or games <= 0:
                continue
            # The level itself and the games it was played over, kept apart.
            # Dividing here and multiplying back in `anchor_level` was a
            # round-trip that cost accuracy for nothing whenever the target runs
            # the same number of games as its history — which is the usual case,
            # and exactly where the model was losing to a plain median of the
            # other editions.
            groups.setdefault(extract(comp), []).append(
                (str(comp.get("start_time") or ""), level, games, season_of(comp, seasons)))
        out[key] = {str(k): _recent(v) for k, v in groups.items() if v}
    return out


def _recent(dated: list[tuple]) -> dict:
    """Median level over the most recent editions, and the games they were run over.

    Carrying last week's threshold forward beats a median over the whole history
    of a cup — measured, 3.65 % against 4.66 % at the reference rank. One reading
    cannot beat many unless the level drifts, so the older editions are not
    evidence about tonight, they are evidence about last season.

    RECENT_EDITIONS sits between the two: enough readings to damp a freak
    evening, few enough to stay in the current meta. It is a working value picked
    between the one that carry-forward uses and the everything that lost to it,
    and it deserves to be measured properly rather than argued about.
    """
    dated.sort(key=lambda d: (d[0], d[1]))
    kept = dated[-RECENT_EDITIONS:]
    levels = [d[1] for d in kept]
    games = [d[2] for d in kept if d[2] > 0] or [1.0]
    # When, and in which season, the newest of the editions read was played:
    # a level read across a turn of season is moved, see `season_move`.
    latest = kept[-1]
    return {"median": round(statistics.median(levels), 2), "n": len(levels),
            "seen": len(dated), "games": round(statistics.median(games), 2),
            "date": latest[0][:10], "season": latest[3] if len(latest) > 3 else None}


def field_sizes(history: list[dict]) -> dict:
    """Typical field size, by scope."""
    out = {}
    for key, extract in (("category", category_name),
                         ("family", family_name),
                         ("mode", lambda c: str((c.get("game_mode"), c.get("team_mode"),
                                                 c.get("region"))))):
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
    for key, name in (("category", category_name(comp)),
                      ("family", family_name(comp)),
                      ("mode", str((comp.get("game_mode"), comp.get("team_mode"),
                                    comp.get("region"))))):
        entry = (table.get(key) or {}).get(name)
        if entry and entry["n"] >= 1:
            return entry["median"], key, entry.get("spread", 0.20)
    return None


def lobby_cap(team_mode, game_mode) -> int:
    mode = str(game_mode or "").lower()
    small = "reload" in mode or "blitz" in mode
    return (LOBBY_CAP_SMALL if small else LOBBY_CAP).get(str(team_mode or ""), 0)


def single_lobby(comp: dict, field: int) -> bool:
    """One lobby: a final played by a fixed set of qualified teams."""
    cap = lobby_cap(comp.get("team_mode"), comp.get("game_mode"))
    return bool(cap) and 0 < int(field or 0) <= cap


def lobby_tables(history: list[dict]) -> dict:
    """What a rank is worth inside a closed lobby, by share of the lobby.

    The open-queue cascade anchors on rank 20 and shapes the rest with a curve
    fitted on fields of thousands. Inside a lobby of twenty, rank 20 is the last
    team and that curve makes the winner worth ten times the anchor - which is
    how a Reload final priced rank 5 at 1,667 points where 300 was the most
    anyone could score. So closed lobbies get their own reading: every final in
    the training set that fits in one lobby, each of its thresholds divided by
    the most a team could score over the games played, bucketed by the share of
    the lobby the rank is. Keyed like the cold start - game mode and team size,
    then game mode alone, then everything - and each entry is [q, share, rel, n]
    with q the bucket's median share and rel the robust relative spread across
    finals, which is the band a cup never seen before honestly gets.
    """
    points: dict = {}
    for comp in history:
        field = int(comp.get("field_size") or 0)
        games = float(comp.get("max_games") or 0)
        scoring = comp.get("scoring") or {}
        if not single_lobby(comp, field) or games <= 0 or not comp.get("scoring_known") \
                or not scoring.get("placement"):
            continue
        best = predict.max_game_score(scoring, kills=scoring.get("kill_cap") or 8)
        if best <= 0:
            continue
        game = comp.get("game_mode") or ""
        team = comp.get("team_mode") or ""
        for rank, value in (comp.get("finals") or {}).items():
            rank, value = int(rank), float(value or 0)
            if rank < 1 or rank > field or value <= 0:
                continue
            # The last places are not a ladder: a team that left after two
            # games sits there with next to nothing, and reading the middle of
            # the board through them would price rank 15 of 20 off rank 20.
            if rank / field > LOBBY_LAST:
                continue
            pair = (rank / field, value / (games * best))
            for key in (str((game, team)), str((game, "")), "*"):
                points.setdefault(key, []).append(pair)

    def buckets(pairs):
        rows = []
        for edge in LOBBY_BUCKETS:
            inside = [(q, y) for q, y in pairs if q <= edge and (q, y) not in used]
            if len(inside) < LOBBY_MIN:
                used.update(inside)
                continue
            used.update(inside)
            ys = [y for _, y in inside]
            med = statistics.median(ys)
            mad = statistics.median(abs(y - med) for y in ys)
            rel = 1.4826 * mad / med if med > 0 else 0.0
            rows.append([round(statistics.median(q for q, _ in inside), 4),
                         round(med, 4), round(max(rel, LOBBY_REL_FLOOR), 3), len(inside)])
        return rows

    out = {}
    for key, pairs in points.items():
        used: set = set()
        rows = buckets(pairs)
        if len(rows) >= 2:
            out[key] = rows
    return out


def lobby_read(rows: list, q: float) -> tuple[float, float, int]:
    """(share of max, rel, n) at share q, log-linear between the two buckets
    either side. The top bucket holds above its own q; below the deepest, the
    board is read as flat and the band widens, because nothing was measured
    there and the last places are where a lobby's ladder collapses."""
    if q <= rows[0][0]:
        return rows[0][1], rows[0][2], rows[0][3]
    if q >= rows[-1][0]:
        return rows[-1][1], rows[-1][2] * 1.5, rows[-1][3]
    for lo, hi in zip(rows, rows[1:]):
        if lo[0] <= q <= hi[0]:
            f = (math.log(q) - math.log(lo[0])) / (math.log(hi[0]) - math.log(lo[0]))
            share = math.exp(math.log(lo[1]) + f * (math.log(hi[1]) - math.log(lo[1])))
            return share, max(lo[2], hi[2]), min(lo[3], hi[3])
    return rows[-1][1], rows[-1][2], rows[-1][3]


def lobby_estimate(comp: dict, calib: dict, rank: int, field: int) -> dict | None:
    """A closed lobby the model has never seen, from the finals it has."""
    table = (calib.get("reference_pace") or {}).get("lobby") or {}
    games = float(comp.get("max_games") or 0)
    scoring = comp.get("scoring") or {}
    if games <= 0 or comp.get("scoring_known") is False or not scoring.get("placement"):
        return None
    best = predict.max_game_score(scoring, kills=scoring.get("kill_cap") or 8)
    if best <= 0:
        return None
    game = comp.get("game_mode") or ""
    team = comp.get("team_mode") or ""
    for key, widen in ((str((game, team)), LOBBY_WIDEN["team"]),
                       (str((game, "")), LOBBY_WIDEN["mode"]), ("*", LOBBY_WIDEN["any"])):
        rows = table.get(key) or []
        if len(rows) >= 2:
            break
    else:
        return None
    share, rel, n = lobby_read(rows, rank / field)
    if n < LOBBY_THIN:
        widen *= LOBBY_WIDEN["thin"]
    return {"value": share * best * games, "rel": rel * widen, "n": n}


def anchor_level(comp: dict, calib: dict) -> dict | None:
    """The tournament's level, estimated without having seen it."""
    games = float(comp.get("max_games") or 0)
    if games <= 0:
        return None

    pace = calib.get("reference_pace") or calib.get("winner_pace") or {}
    until = season_of(comp, calib.get("seasons"))

    def measured(key, name):
        """A single edition already counts — with a widened range."""
        entry = (pace.get(key) or {}).get(name)
        if not entry or entry.get("n", 0) < 1:
            return None
        # Widened when the cup has run once, not when the level is read off one
        # edition by choice: `seen` is how many there were, `n` how many the
        # reading used, and RECENT_EDITIONS = 1 would otherwise widen every cup.
        rel = ANCHOR_SPREAD[key] * (1.5 if entry.get("seen", entry["n"]) == 1 else 1.0)
        # The stored level is what these editions actually reached, over the
        # number of games they actually played. A target running the same number
        # gets it unchanged — the factor below is exactly 1 — and only a target
        # running a different number is scaled, at the exponent measured for the
        # per-game model rather than the 1.0 that dividing and multiplying
        # implies. Thresholds do not double when the games do.
        theirs = float(entry.get("games") or 0) or games
        value = entry["median"] * (games / theirs) ** GAMES_EXPONENT
        # A level read across a turn of season moves with the season, like a
        # previous edition's reading does - at the reference rank's band.
        shift, spread, _ = season_move(calib, entry.get("season"), until, REFERENCE_RANK)
        if spread > 0:
            value *= math.exp(shift)
            rel = math.sqrt(rel ** 2 + spread ** 2)
        return {"value": value, "rel": rel, "source": key, "n": entry["n"]}

    for key, name in (("category", category_name(comp)), ("family", family_name(comp))):
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
        signature = cold_signature(comp)
        for key, factor in ((str(signature + (comp.get("region") or "",)), 0.9),
                            (str(signature), 1.0),
                            (str((game, team)), 1.0), (str((game, "")), 1.2)):
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


# The re-scored table's own dispersion, when the table does not say: the 80th
# percentile of its error at the ranks it covers, measured in rescore.py.
REPLAY_REL = 0.12


def table_signature(scoring: dict | None, games) -> str:
    """A compact spelling of a scoring table and a game count. A re-scored
    table carries the signature of the table it was replayed under, so a form
    whose table has since been edited does not read a replay of another one."""
    rows = sorted((int(lo), int(hi), float(pts))
                  for lo, hi, pts in (scoring or {}).get("placement") or [])
    parts = [f"{lo}-{hi}:{pts:g}" for lo, hi, pts in rows]
    cap = (scoring or {}).get("kill_cap")
    kill = float((scoring or {}).get("kill") or 0)
    return ";".join(parts) + f"|k{kill:g}|c{int(cap) if cap else 0}|g{int(games or 0)}"


def replay_reading(comp: dict, calib: dict, rank: int, field: int) -> dict | None:
    """The re-scored boards' value at this rank, or None.

    `comp["cold"]` is what rescore.cold_table wrote beside the calendar row:
    recent boards of the same region, format and platform replayed under this
    cup's table, the median standing at each rank down to the depth the
    boards were loaded to. Read straight where the table reaches the rank -
    between two of its ranks, log-linear like the other tables - and past its
    depth continued from the deepest rank along the ladder (the curve where
    the ladder has no boards), the band widened by the ladder's own spread.
    A table replayed under another scoring than the form's, or another game
    count, is not read: the signature says which.
    """
    cold = comp.get("cold")
    if not isinstance(cold, dict) or not cold.get("ranks"):
        return None
    if cold.get("sig") and cold["sig"] != table_signature(comp.get("scoring"), comp.get("max_games")):
        return None
    table = {}
    for pair in cold["ranks"]:
        try:
            at, value = int(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if at >= 1 and value > 0:
            table[at] = value
    if not table:
        return None
    rel = float(cold.get("rel") or REPLAY_REL)
    donors = int(cold.get("donors") or 0)
    deepest = max(table)
    rank = int(rank)
    if rank in table:
        return {"value": table[rank], "rel": rel, "n": donors, "shape_source": "replay",
                "deep": deepest, "shape_rel": 0.0}
    if rank < deepest:
        found = bracket({str(k): v for k, v in table.items()}, rank, lambda v: bool(v))
        if found:
            lo, hi, f = found
            return {"value": interpolated(table[lo], table[hi], f), "rel": rel, "n": donors,
                    "shape_source": "replay", "deep": deepest, "shape_rel": 0.0}
        return None
    # Past the depth the boards were loaded to: continued along the ladder,
    # from the deepest rank read, both ratios off the same table.
    here = shape_from(comp, calib, rank, "scoring", field)
    there = shape_from(comp, calib, deepest, "scoring", field)
    if here and there:
        ratio, shape_rel, shape_source = here[0] / there[0], math.sqrt(here[1] ** 2 + there[1] ** 2), "ladder"
    else:
        curve = curve_for(calib, field)
        ratio = shape_ratio(rank, field, curve) / shape_ratio(deepest, field, curve)
        shape_rel, shape_source = SHAPE_FALLBACK_REL, "curve"
    return {"value": table[deepest] * ratio, "rel": math.sqrt(rel ** 2 + shape_rel ** 2),
            "n": donors, "shape_source": shape_source, "deep": deepest, "shape_rel": shape_rel}


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

    # Rung zero: this cup, this region, this rank, last time. Nothing is
    # modelled — see `direct_tables` for why that beats modelling it.
    direct = direct_from(comp, calib, rank, field)
    if direct:
        value, rel, n = direct["value"], direct["rel"], direct["n"]
        return {
            "ok": True, "shape_source": "direct",
            "value": round(value, 1),
            "low": round(max(0.0, value * (1 - rel)), 1),
            "high": round(value * (1 + rel), 1),
            "n": n, "source": "previous edition",
            "games": int(comp.get("max_games") or 0),
            "field": field, "share": round(100 * rank / field, 2),
            "guessed_field": guessed,
            # Here: how much the known field moved the edition's value, in
            # per cent - not, as on the other rungs, what a 30 % error would.
            "field_effect": round(direct["field_effect"], 1),
            "entry_note": direct["note"],
            # The edition the rank was read off, and what carrying it across
            # a turn of season did to it: per cent, and the pairs it rests on.
            "edition": direct["season"]["date"],
            "season_effect": round(direct["season"]["effect"], 1),
            "season_pairs": direct["season"]["pairs"],
            "season_from": direct["season"]["since"], "season_to": direct["season"]["until"],
            "level": round(value, 1), "ref_rank": rank,
            "sealed": predict.is_sealed(comp),
        }

    # The last places of a closed lobby are not a ladder. A team that left
    # after two games sits there with next to nothing, and no level, shape or
    # share of the lobby says how many left: rank 50 of 50 was forecast at
    # thirty points where the board said eight, twenty-one times out of
    # twenty-one. The previous edition's own reading, above, is the one
    # honest answer; short of it the model says so rather than guess.
    if single_lobby(comp, field) and rank / field > LOBBY_LAST:
        return {"ok": False, "reason": last_places_reason(int(rank), int(field))}

    top = anchor_level(comp, calib)

    # A closed lobby without editions of its own: read off the finals of its
    # mode, by share of the lobby, rather than off an anchor and a curve that
    # were both measured on open queues of thousands. Editions of the cup
    # itself, where there are any, still come first.
    if single_lobby(comp, field) and (not top or top["source"] not in ("category", "family")):
        lobby = lobby_estimate(comp, calib, rank, field)
        if lobby:
            value, rel = lobby["value"], lobby["rel"]
            return {
                "ok": True, "shape_source": "lobby",
                "value": round(value, 1),
                "low": round(max(0.0, value * (1 - rel)), 1),
                "high": round(value * (1 + rel), 1),
                "n": lobby["n"], "source": "closed lobby",
                "games": int(comp.get("max_games") or 0),
                "field": field, "share": round(100 * rank / field, 2),
                "guessed_field": guessed, "field_effect": 0.0,
                "level": round(value, 1), "ref_rank": rank,
                "sealed": predict.is_sealed(comp),
            }

    # The cup nobody has seen in this region: recent boards of its format
    # replayed under its own table, see rescore.py. Alone where the cup has
    # no editions anywhere; where it has run in other regions, half of that
    # reading and half of this one, in log terms - measured better than
    # either. Never for one lobby, whose ladder is another thing.
    replay = None if single_lobby(comp, field) else replay_reading(comp, calib, rank, field)
    if replay and (not top or top["source"] not in ("category", "family")):
        value, rel = replay["value"], replay["rel"]
        curve = curve_for(calib, field)
        slope = field_sensitivity(rank, field, curve)
        return {
            "ok": True, "shape_source": replay["shape_source"],
            "value": round(value, 1),
            "low": round(max(0.0, value * (1 - rel)), 1),
            "high": round(value * (1 + rel), 1),
            "n": replay["n"], "source": "re-scored boards",
            "games": int(comp.get("max_games") or 0),
            "field": field, "share": round(100 * rank / field, 2),
            "guessed_field": guessed,
            # The field reaches a replayed rank only past the boards' depth,
            # through the ladder: what a 30 % error would cost there.
            "field_effect": round(100 * slope * 0.30, 1) if rank > replay["deep"] else 0.0,
            "level": round(value, 1), "ref_rank": rank,
            "replay_deep": replay["deep"],
            "sealed": predict.is_sealed(comp),
        }

    if not top:
        return {"ok": False, "reason": why_no_anchor(comp, calib)}

    curve = curve_for(calib, field)
    slope = field_sensitivity(rank, field, curve)
    # Measured first, modelled second. Where comparable editions have actually
    # been to this rank, their median says what it was worth; the curve answers
    # only for ranks none of them reached.
    found = shape_from(comp, calib, rank, top["source"], field)
    if found:
        ratio, shape_rel, scope = found
        # The cup's own editions, or the ladder of every open queue of this
        # field size and game mode: said apart, so the page can say which.
        shape_source = "ladder" if scope == "ladder" else "measured"
        # No separate field term here. The dispersion above is what these very
        # editions did at this very rank, fields and all, so adding a field
        # correction on top would be counting the same variation twice.
        field_part = 0.0
    else:
        ratio, shape_rel = shape_ratio(rank, field, curve), SHAPE_FALLBACK_REL
        shape_source = "curve"
        # An error on the team count only reaches the threshold in proportion to
        # the slope, which is 0.05 near the top of the standings. So a guessed
        # field costs almost nothing.
        field_part = slope * (field_spread if guessed else 0.15)
    value = top["value"] * ratio
    rel = math.sqrt(top["rel"] ** 2 + shape_rel ** 2 + field_part ** 2)
    source, n, replay_deep = top["source"], top["n"], None
    if replay and source == "family":
        # Two readings of two populations - the cup elsewhere, this region's
        # boards under this table - averaged in log terms; the band of an
        # average of two independent readings.
        value = math.sqrt(value * replay["value"])
        rel = math.sqrt(rel ** 2 + replay["rel"] ** 2) / 2
        source, replay_deep = "family + re-scored boards", replay["deep"]
    return {
        "ok": True,
        "shape_source": shape_source,
        "value": round(value, 1),
        "low": round(max(0.0, value * (1 - rel)), 1),
        "high": round(value * (1 + rel), 1),
        "n": n,
        "source": source,
        "games": int(comp.get("max_games") or 0),
        "field": field,
        "share": round(100 * rank / field, 2),
        "guessed_field": guessed,
        # what a 30 % error on the team count would cost
        "field_effect": round(100 * slope * 0.30, 1),
        "level": round(top["value"], 1),
        "ref_rank": REFERENCE_RANK,
        "replay_deep": replay_deep,
        "sealed": predict.is_sealed(comp),
    }


def last_places_reason(rank, field) -> str:
    """Why the last places of a closed lobby get no number. `rank` and `field`
    are printed as given, so the export can hand the page a template."""
    return (f"Rank {rank} of {field} is among the last places of a closed lobby: "
            f"teams that left after a game or two, not a threshold anyone plays for. "
            f"The model does not forecast them; the previous edition's standings are "
            f"the only reading.")


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
    # The label is what the page shows; the key is what decides sameness. They
    # differ where the label cannot: two cups of the same name in different
    # modes read alike and are not comparable.
    key = db.category_key(comp)

    # Listed once, not once per circle: four circles are tried before the widest
    # one answers, and re-reading every competition each time is four full table
    # scans for a list that cannot have changed between them. `catalogue` rather
    # than `list_competitions` because every test below asks about region, stage
    # or mode and none of them opens a scoring table.
    everything = db.catalogue(conn)

    def collect(match):
        rows = [o for o in everything
                if not (exclude_id and o["id"] == exclude_id) and match(o)]
        rows.sort(key=lambda c: c["start_time"])
        out = []
        # Newest first, stopping at CIRCLE_CAP: the widest circle is
        # region + team size + mode, which the API harvest fills with thousands of
        # tournaments, and loading them all — with their snapshots — to take a
        # median is minutes of work for a number that stopped moving decades of
        # editions ago. The shrinkage weight n/(n+2) is within 5 % of 1 by n = 40,
        # and the level itself is read off the wide sample, not from here.
        for other in reversed(rows):
            full = db.get_competition_full(conn, other["id"])
            if full and full["snapshots"]:
                out.append(full)
            if len(out) >= CIRCLE_CAP:
                break
        out.reverse()
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
         lambda o: db.category_key(o) == key),
        (f"{kind}, all regions", True,
         lambda o: db.series_of(o) == key[0] and db.round_of(o) == key[1]
         and o["team_mode"] == comp["team_mode"] and o["game_mode"] == comp["game_mode"]),
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


# Reserved cache key: the wide-sample half of a calibration, which every scope
# shares. Not a scope name — no scope can produce a key starting with "*".
BROAD_KEY = "*broad*"


def _fingerprint(conn) -> int:
    """A cheap number that changes whenever the wide sample could have.

    Every write in the app calls `invalidate`, but the harvester writes straight
    through `db`, so the cache cannot rely on being told. Counting two tables is
    two index scans and catches anything that adds, removes or re-scores a
    tournament.
    """
    comps = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM competition").fetchone()
    finals = conn.execute("SELECT COUNT(*) FROM final_result").fetchone()[0]
    return (int(comps[0]) * 1_000_003 + int(comps[1]) * 1_009 + int(finals)) * 10 + TABLES_VERSION


# Bumped whenever the shape of `broad_stats` changes, so a payload cached by
# an older version of this file is recomputed rather than read with keys it
# does not have. 2: tables keyed on the format, curves by field band. 3: the
# seasons and the moves at each turn of one, the season of each edition read.
# 4: the ladder, the pooled shape by band of field size and game mode.
TABLES_VERSION = 4


def shared_broad(conn, db) -> dict:
    """`broad_stats` over the whole database, computed once and cached.

    Without this, every scope that misses the cache refits the curve over every
    threshold in the database — 2,000 scopes paying a minute each for the same
    three tables. The fit depends on the wide sample alone, so one row serves
    the lot.
    """
    mark = _fingerprint(conn)
    row = conn.execute("SELECT * FROM calibration_cache WHERE key = ?", (BROAD_KEY,)).fetchone()
    if row and row["n_comps"] == mark:
        try:
            return json.loads(row["payload"])
        except (ValueError, TypeError):
            pass
    broad = broad_stats(db.all_full(conn))
    conn.execute(
        "INSERT INTO calibration_cache (key, n_comps, payload, computed_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET n_comps = excluded.n_comps, "
        "payload = excluded.payload, computed_at = excluded.computed_at",
        (BROAD_KEY, mark, json.dumps(broad, ensure_ascii=False), db.norm_ts(None)),
    )
    return broad


def load_or_compute(conn, db, scope: dict, history: list[dict], ranks: list[int]) -> dict:
    """Return the calibration, recomputing only if the history has changed.

    `wide` is everything known, not just the comparable circle: the curve shape
    is a property of Fortnite scoring, not of one tournament family, so it is
    fitted on every result available. Passing it here matters — a cached
    calibration computed without it used to differ from a fresh one.
    """

    key = cache_key(scope)
    n_done = sum(1 for c in history if predict.is_complete(c))
    if key == BROAD_KEY:                       # not a scope; see shared_broad
        raise ValueError("BROAD_KEY is reserved")
    row = conn.execute("SELECT * FROM calibration_cache WHERE key = ?", (key,)).fetchone()
    if row and row["n_comps"] == n_done:
        try:
            payload = json.loads(row["payload"])
            # A payload written by an older version of this file carries
            # tables keyed another way; recomputed rather than read wrong.
            current = payload.get("tables_version") == TABLES_VERSION
            if current and (set(payload.get("ratio", {})) >= {str(r) for r in ranks if r} or not ranks):
                return payload
        except (ValueError, TypeError):
            pass
    calib = calibrate(history, ranks, broad=shared_broad(conn, db))
    conn.execute(
        "INSERT INTO calibration_cache (key, n_comps, payload, computed_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET n_comps = excluded.n_comps, "
        "payload = excluded.payload, computed_at = excluded.computed_at",
        (key, n_done, json.dumps(calib, ensure_ascii=False), db.norm_ts(None)),
    )
    return calib


def invalidate(conn) -> None:
    conn.execute("DELETE FROM calibration_cache")
