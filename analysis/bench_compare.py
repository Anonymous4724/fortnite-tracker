"""Is a change to the forecast better, measured on the same cups priced twice?

Two runs of `bench` written with `--json`, the forecasts as they are and as a
change makes them, are set side by side, forecast against forecast: the same
cup (its window) at the same rank. Two runs only compare when they were made
from one snapshot: the same database (and -wal), the same catalogue, the same
raw leaderboard pages (as many events on disk, and the variant has read every
board the base read, with the same teams and content), the same span of days,
the same rule for the model a cup reads, the same ranks, the same multipliers
for the ranges and, for runs that say when a reading arrives (`arrival`), the
same rule for that. Anything else is refused, with what differs: a number
measured on another copy of the database is not a number to set against this
one. A run that does not say which catalogue it read is refused too, unless
told not to check it (`--unchecked catalogue`), and the verdict then says so;
so is a variant that did not read every board the base read (`--unchecked
pages`), the verdict then saying how many it did not read.
Runs of the live bench (`bench --live`) are compared with runs of the live
bench only, under the same checks and the same rule for when a reading
reaches the page (`arrival`, its cadence included) and for the cups the
feed's outages touched: their forecasts, one per cup, rank and point of the
cup, are kept under "forecasts" and paired by window, rank and point. The
gain rule is then judged whole at each point of the session (20, 40, 60, 80
and 100 % of it, then 10 and 20 minutes past the close), with a verdict per
point; a variant that loses forecasts fails at the points it loses them,
unless judged as a refusal (`--refusal`).

Each pair has to aim at the same thing on both sides: the same result (to
1e-9), cup id, day, region and cut. Runs with a pair that does not are refused
too: a change that moved the results would be judged against another truth.
Nor is there a verdict on nothing: runs with not one forecast in common over
the days judged, or a `--since` or `--until` outside their span, are not
compared.

The paired forecasts are measured on each side and as the change between them:
the signed error, the absolute error, the error in log |ln(forecast / result)|
(in hundredths, next to the percentages), each as a mean and a median, and how
often the result fell inside the inner and outer range. The results under ten
points are shown apart: a few points either way is a large share of them. The
change is then cut by region, by rank (to 250 and past it, and the bench's
finer bands for information) and by rung, by half of the span, and season by
season when the runs cover more than one.

How sure the change is comes from drawing again with replacement (DRAWS
times, from a fixed seed, so the same two files always print the same
interval) the forecasts of one family on one local day together (the day in
the cup's region): an event played in several regions the same evening errs
together, and is one draw, not one per region. That interval judges; the one
drawn by cups (each cup's ranks together) and the one drawn by the day of the
model are shown beside it, for information. The verdict follows the rule a
change has to pass to count as a gain, all of it:

- the mean absolute error and the mean log error go down, and the median
  absolute error does not go up (SAME_MEDIAN: equal passes); with a segment
  declared before the measure (`--segment`, on what the page knew before the
  cup), the segment's median absolute error goes down;
- the 90 % interval of the paired change in the mean absolute error, drawn by
  family x local day, lies entirely below zero;
- the same sign in both halves of the span: in each half the mean absolute
  error and the mean log error go down and the median does not go up;
- when at least two seasons have SEASON_CUPS paired cups or more, the median
  absolute error and the mean log error both go down in the same season, in
  more than half of those seasons. The mean absolute error is shown season by
  season but does not judge: over a long run, a few families more than 100 %
  off carry it;
- no region with REGION_PAIRS forecasts and REGION_CUPS cups or more, and
  neither rank band (to 250, past it), is worse by more than HARM points of
  mean absolute error (HARM exactly passes);
- the change prices every forecast the base prices.

A change with not one forecast moved is "no change"; one whose interval lies
entirely above zero is a "loss"; any other that fails a condition is "no gain",
with the conditions it fails.

A variant that prices fewer forecasts than the base, on what the page knew
before the cup, is judged as a refusal with `--refusal`, and by these
conditions only: the forecasts it keeps are unchanged and it prices none the
base does not; it refuses REFUSED_SHARE of the base's forecasts at most; and
the median absolute error of the base on the forecasts refused is
REFUSED_FACTOR times that of the others at least, in each half (a half with
none refused fails) and, when two seasons or more have SEASON_CUPS cups and a
refusal, in most of them. Without `--refusal`, a forecast lost fails the rule.

The strawman is the correction any idea has to beat: each forecast divided by
one plus the median signed error of its rung, rank band (to 250, past it) and
region, over the forecasts of the same run whose cups were over when the
forecast's model was built (`bench.held`, the end times read from the
database), the median shrunk by n / (n + STRAWMAN_PRIOR) for a group of n
forecasts. Learned walk-forward, it is what a correction by the groups the
bench prints is worth out of sample.

    python -m analysis.bench_compare data/base.json data/variant.json
    python -m analysis.bench --compare data/base.json data/variant.json   (any option below too)
    python -m analysis.bench_compare data/base.json data/variant.json --json data/compared.json
    python -m analysis.bench_compare data/base.json data/variant.json --unchecked catalogue
    python -m analysis.bench_compare data/base.json data/variant.json --since 2026-09-09
    python -m analysis.bench_compare data/base.json data/variant.json --segment "p['rank'] > 250"
    python -m analysis.bench_compare data/base.json data/variant.json --refusal
    python -m analysis.bench_compare --strawman data/base.json --json data/strawman.json
    python -m analysis.bench_compare data/live.json data/live.variant.json   two live runs, point by point
    python -m analysis.bench_compare --strawman data/live.json --db data/tracker.db   the live strawman

The live strawman is the cold one's, by point of the session, depth into the
field (the live bench's `depth`) and region: each forecast divided by one plus
the median signed error of its group over the forecasts of the same run whose
cups were over when its model was built, shrunk by n / (n + STRAWMAN_PRIOR);
its ranges move with it.
"""
from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import math
import os
import random
import statistics
import sys
from datetime import date, timedelta

# The app's modules, whichever way this file is run.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis import bench, coldbench  # noqa: E402

DRAWS = 1000
SEED = 2718
LEVEL = 0.90
# Points of mean absolute error a region or a rank band may lose (that much
# exactly passes).
HARM = 0.5
# A region is judged with this many paired forecasts and cups at least.
REGION_PAIRS = 30
REGION_CUPS = 20
# Two medians this close are the same median: a median "not up" may be this
# much higher, the noise of sums in floating point.
SAME_MEDIAN = 1e-9
# A refusal judged apart: the forecasts the variant no longer prices are this
# share of the base's at most, and their median absolute error this many
# times the others' at least.
REFUSED_SHARE = 0.05
REFUSED_FACTOR = 2.0
# Results under this many points are shown apart.
LOW = 10
# The rank bands the rule reads: to this rank, and past it.
SPLIT = 250
# A season counts towards the season rule with this many paired cups.
SEASON_CUPS = 20
STRAWMAN_PRIOR = 50
# Two results this close are the same result.
SAME_RESULT = 1e-9

CHECKS = ("bench", "database", "catalogue", "pages", "span", "cutoff", "ranks", "bands", "arrival", "outages")
# Checks of a field only some benches write: missing from both runs, nothing to check.
OPTIONAL = ("arrival", "outages")
# What a forecast aims at: both sides of a pair must agree on all of it.
AIM = ("result", "id", "day", "region", "at_cut")
STATS = ("signed_mean", "signed_median", "abs_mean", "abs_median", "log_mean", "log_median", "in_50", "in_90")
DRAWN = ("signed_mean", "abs_mean", "abs_median", "log_mean")
# The measures that must go down, overall and in each half: the median may
# also stay where it was.
DOWN = (("abs_mean", "mean absolute error"), ("abs_median", "median absolute error"), ("log_mean", "mean log error"))
# What a segment's expression may call, besides reading the pair `p`.
SEGMENT_BUILTINS = {name: getattr(builtins, name)
                    for name in ("abs", "all", "any", "bool", "float", "int", "len", "max", "min", "round", "str")}


class SnapshotMismatch(ValueError):
    """Two runs that were not made from the same snapshot."""


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #
def is_live(run: dict) -> bool:
    """Is the run the live bench's, whose forecasts are kept under "forecasts"?"""
    return run.get("bench") == "live" or ("forecasts" in run and "pairs" not in run)


def refuse_live(runs: dict) -> None:
    """ValueError when one of `runs` ({what it is: run}) comes from the live
    bench, where a run of the cold bench is wanted."""
    live = [name for name, run in runs.items() if is_live(run)]
    if live:
        raise ValueError(f"a live run where a cold one is wanted: {' and '.join(live)} "
                         f"{'come' if len(live) > 1 else 'comes'} from the live bench, which keeps a forecast per cup,"
                         " rank and point of the cup; two live runs are compared with each other only")


def boards_of(payload: dict) -> dict | None:
    """{"event|window": (teams, content hash)} of the boards the run's replays
    read, or None for a run that did not list them."""
    listed = (payload.get("replays") or {}).get("boards")
    if listed is None:
        return None
    out = {}
    for line in listed:
        board, teams, sha = str(line).rsplit("|", 2)
        out[board] = (teams, sha)
    return out


def boards_digest(payload: dict) -> str | None:
    """The run's digest of the boards it read, or the one its list makes."""
    listed = (payload.get("replays") or {}).get("boards")
    if listed is not None:
        return hashlib.sha1("\n".join(sorted(listed)).encode()).hexdigest()[:16]
    return (payload.get("replays") or {}).get("digest")


def events_of(payload: dict):
    """How many events of raw leaderboard pages the run found on disk, or None."""
    return (payload.get("leaderboards") or {}).get("events")


def pages_agree(base: dict, variant: dict) -> tuple[bool | None, str, int]:
    """(the same pages, what was compared, how many boards the base read that
    the variant did not). The same number of events of raw pages on disk; on
    the boards both listed, the same teams and the same content; a run that
    only kept the digest of its boards must have read exactly the same ones.
    None when it cannot be told: a run does not say, or the variant did not
    read every board the base read."""
    ea, eb = events_of(base), events_of(variant)
    if ea != eb:
        if ea is None or eb is None:
            return None, f"the raw pages on disk are not in {'the base' if ea is None else 'the variant'}", 0
        return False, f"raw pages of {ea} events on disk against {eb}", 0
    a, b = boards_of(base), boards_of(variant)
    if a is not None and b is not None:
        both = sorted(set(a) & set(b))
        differ = [board for board in both if a[board] != b[board]]
        missed = len(set(a) - set(b))
        if differ:
            return False, f"{len(differ)} of the {len(both)} boards both read differ, first {differ[0]} " \
                          f"({a[differ[0]]} against {b[differ[0]]})", missed
        if missed:
            return None, (f"the variant read none of the {len(a)} boards the base read" if missed == len(a)
                          else f"the variant did not read {missed} of the {len(a)} boards the base read"), missed
        return True, f"{len(both)} boards read by both, {len(set(b) - set(a))} by the variant only", missed
    da, db_ = boards_digest(base), boards_digest(variant)
    if da is None or db_ is None:
        return None, "the boards read are not in " + ("either run" if da is None and db_ is None else
                                                      "the base" if da is None else "the variant"), 0
    if da != db_:
        return False, f"boards digest {da} against {db_}, and no list to compare the boards both read", 0
    return True, f"the same boards (digest {da})", 0


def fingerprint(payload: dict) -> dict:
    """What has to agree between two runs, each None where the run does not say."""
    base = payload.get("database") or {}
    bands = payload.get("bands")
    if not bands:
        # Older runs: the multipliers the forecasts themselves carry.
        seen = sorted({json.dumps(p.get("bands"), sort_keys=True) for p in payload.get("pairs") or []
                       if p.get("bands")})
        bands = {"carried by the forecasts": seen} if seen else None
    return {
        "bench": payload.get("bench"),
        "database": [base.get("sha1"), base.get("wal_sha1")] if base.get("sha1") else None,
        "catalogue": (payload.get("catalogue") or {}).get("sha1"),
        "span": [payload.get("since"), payload.get("until")] if payload.get("since") and payload.get("until") else None,
        "cutoff": [payload.get("cutoff"), payload.get("update")] if payload.get("cutoff") else None,
        "ranks": sorted({int(rank) for rank in payload["ranks"]}) if payload.get("ranks") else None,
        "bands": bands,
        "arrival": payload.get("arrival"),
        # Whether a live run kept the cups an outage of the feed touched (a run
        # from before they were marked kept them, and does not say).
        "outages": None if payload.get("outages") is None else
        "left out" if payload["outages"].get("left_out") else "kept",
    }


def snapshot_problems(base: dict, variant: dict, unchecked=()) -> tuple[list[str], list[str], str, int]:
    """(what differs, what could not be checked and was allowed not to be,
    what the pages check compared, how many boards the base read that the
    variant did not). A value that differs is never allowed; a value missing
    from either run is, when named in `unchecked`."""
    unchecked = set(unchecked)
    unknown = unchecked - set(CHECKS)
    if unknown:
        raise ValueError(f"no such check: {', '.join(sorted(unknown))} (the checks: {', '.join(CHECKS)})")
    a, b = fingerprint(base), fingerprint(variant)
    problems, skipped, pages, missed = [], [], "", 0
    for name in CHECKS:
        if name == "pages":
            same, told, missed = pages_agree(base, variant)
            pages = told
        elif name in OPTIONAL and a[name] is None and b[name] is None:
            continue
        elif a[name] is None or b[name] is None:
            same = None
            told = "not in " + ("either run" if a[name] is None and b[name] is None
                                else "the base" if a[name] is None else "the variant")
        else:
            same = a[name] == b[name]
            told = "" if same else f"{json.dumps(a[name])[:120]} against {json.dumps(b[name])[:120]}"
        if same is False:
            problems.append(f"{name}: {told}")
        elif same is None:
            if name in unchecked:
                skipped.append(f"{name} ({told})")
            else:
                problems.append(f"{name}: {told}, so the runs cannot be shown to share it"
                                f" (--unchecked {name} to compare all the same)")
    return problems, skipped, pages, missed


def aimed_apart(a: dict, b: dict, aim: tuple = AIM) -> tuple[int, dict, str]:
    """(how many of the keys both runs price do not aim at the same thing on
    both sides, how many differ on each field of `aim`, the first one told)."""
    count, fields, first = 0, {}, ""
    for key in sorted(set(a) & set(b)):
        x, y = a[key], b[key]
        off = [name for name in aim if (abs(x["result"] - y["result"]) > SAME_RESULT if name == "result"
                                        else x.get(name) != y.get(name))]
        if not off:
            continue
        count += 1
        for name in off:
            fields[name] = fields.get(name, 0) + 1
        if not first:
            first = f"{key[0]} at rank {key[1]}: " + ", ".join(f"{name} {x.get(name)!r} against {y.get(name)!r}"
                                                             for name in off)
    return count, fields, first


# --------------------------------------------------------------------------- #
# The measures
# --------------------------------------------------------------------------- #
def key_of(pair: dict) -> tuple:
    """What pairs two forecasts: the cup's window and the rank, and for the
    live bench the point of the session."""
    if "point" in pair:
        return str(pair["window"]), int(pair["rank"]), str(pair["point"])
    return str(pair["window"]), int(pair["rank"])


def positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def indexed(pairs: list[dict], side: str) -> dict:
    """{(window, rank[, point]): forecast} of one run, each forecast checked
    for what the measures read."""
    out = {}
    for p in pairs:
        lacking = [name for name in ("window", "rank", "day", "forecast", "result") if p.get(name) is None]
        if lacking:
            raise ValueError(f"a forecast of the {side} has no {', '.join(lacking)}: {str(p)[:100]}")
        key = key_of(p)
        for name in ("forecast", "result"):
            if not positive(p[name]):
                raise ValueError(f"the {side}'s {name} for {key[0]} at rank {key[1]} is not a positive number:"
                                 f" {p[name]!r}")
        if key in out:
            raise ValueError(f"the {side} prices {key[0]} at rank {key[1]}"
                             + (f" at {key[2]}" if len(key) > 2 else "") + " twice")
        out[key] = p
    return out


def log_error(pair: dict) -> float:
    """|ln(forecast / result)|, in hundredths."""
    return 100.0 * abs(math.log(pair["forecast"] / pair["result"]))


def covered(pair: dict, key: str) -> bool | None:
    """Does the range `key` ("50", "90") hold the result, its ends as the page
    prints them (whole numbers, JavaScript's Math.round)? A cold forecast
    carries its multipliers and `rel`, a live one its ranges ("near", "wide")."""
    if pair.get("bands") is not None or "near" not in pair:
        return coldbench.inside(pair, key, shown=True)
    ends = pair.get("near" if key == "50" else "wide")
    if not ends:
        return None
    return math.floor(ends[0] + 0.5) <= pair["result"] <= math.floor(ends[1] + 0.5)


def stats(pairs: list[dict]) -> dict:
    """The measures of one side over a group of forecasts."""
    if not pairs:
        return {name: None for name in STATS}
    errors = [coldbench.error(p) for p in pairs]
    logs = [log_error(p) for p in pairs]
    near = [x for x in (covered(p, "50") for p in pairs) if x is not None]
    wide = [x for x in (covered(p, "90") for p in pairs) if x is not None]
    return {"signed_mean": statistics.mean(errors), "signed_median": statistics.median(errors),
            "abs_mean": statistics.mean(abs(e) for e in errors),
            "abs_median": statistics.median(abs(e) for e in errors),
            "log_mean": statistics.mean(logs), "log_median": statistics.median(logs),
            "in_50": sum(near) / len(near) if near else None, "in_90": sum(wide) / len(wide) if wide else None}


def row(matched: list[tuple[dict, dict]]) -> dict:
    """Both sides and the change, over a group of paired forecasts."""
    a, b = stats([x for x, _ in matched]), stats([y for _, y in matched])
    return {"pairs": len(matched), "cups": len({x["window"] for x, _ in matched}), "base": a, "variant": b,
            "change": {name: (b[name] - a[name]) if a[name] is not None and b[name] is not None else None
                       for name in STATS}}


def quantile(ranked: list[float], q: float) -> float:
    """The q-quantile of sorted values, by linear interpolation between them."""
    at = q * (len(ranked) - 1)
    low = math.floor(at)
    high = min(low + 1, len(ranked) - 1)
    return ranked[low] + (ranked[high] - ranked[low]) * (at - low)


def family_day(pair: dict) -> tuple[str, str]:
    """The draw a forecast belongs to: its family on its local day. An event
    played in several regions the same evening is one draw, not one per region."""
    return pair.get("family") or "", pair.get("local_day") or pair["day"]


def model_day(pair: dict) -> str:
    """The day of the model a forecast was priced from."""
    return str(pair.get("model") or pair["day"])[:10]


# The units the cups are drawn again by: the first judges, the others are
# shown for information.
UNITS = (("family x local day", family_day), ("cup", lambda pair: pair["window"]), ("model day", model_day))


def interval(matched: list[tuple[dict, dict]], draws: int = DRAWS, seed: int = SEED,
             level: float = LEVEL, unit=lambda pair: pair["window"]) -> dict:
    """{measure: [low, high]}: the interval of the paired change in the signed
    and absolute mean, the absolute median and the mean log error, the units
    of `unit` (the cups by default) drawn again with replacement, each with
    all its forecasts."""
    cells: dict = {}
    for x, y in matched:
        ex, ey = coldbench.error(x), coldbench.error(y)
        cell = cells.setdefault(unit(x), [0, 0.0, 0.0, 0.0, [], []])
        cell[0] += 1
        cell[1] += ey - ex
        cell[2] += abs(ey) - abs(ex)
        cell[3] += log_error(y) - log_error(x)
        cell[4].append(abs(ex))
        cell[5].append(abs(ey))
    units = [cells[k] for k in sorted(cells, key=str)]
    if not units or draws <= 0:
        return {name: None for name in DRAWN}
    rng = random.Random(seed)
    found = {name: [] for name in DRAWN}
    for _ in range(draws):
        picked = [units[int(rng.random() * len(units))] for _ in units]
        n = sum(u[0] for u in picked)
        found["signed_mean"].append(sum(u[1] for u in picked) / n)
        found["abs_mean"].append(sum(u[2] for u in picked) / n)
        found["log_mean"].append(sum(u[3] for u in picked) / n)
        found["abs_median"].append(statistics.median([e for u in picked for e in u[5]])
                                   - statistics.median([e for u in picked for e in u[4]]))
    tail = (1 - level) / 2
    return {name: [quantile(sorted(v), tail), quantile(sorted(v), 1 - tail)] for name, v in found.items()}


def rank_band(rank: int) -> str:
    return f"1-{SPLIT}" if rank <= SPLIT else f"{SPLIT + 1}+"


def halves_of(since: str, until: str) -> tuple[str, str]:
    """The day the second half of [since, until) begins on."""
    first, last = date.fromisoformat(since), date.fromisoformat(until)
    return (first + timedelta(days=(last - first).days // 2)).isoformat(), until


def day_before(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat() if day else ""


def span_of(payload: dict, forecasts: dict) -> tuple[str, str]:
    """(its first day, the day after its last) of a run: as the run says, or
    else as the days of its forecasts go."""
    days = sorted(p["day"] for p in forecasts.values())
    first = payload.get("since") or (days[0] if days else "")
    end = payload.get("until") or ((date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat() if days else "")
    return first, end


def checked_day(text: str, what: str) -> str:
    """`text` when it is a day written YYYY-MM-DD (or empty); ValueError otherwise."""
    if not text:
        return ""
    try:
        if date.fromisoformat(text).isoformat() == text:
            return text
    except ValueError:
        pass
    raise ValueError(f"{what}: not a day written YYYY-MM-DD: {text!r}")


def grouped(matched: list[tuple[dict, dict]], label, ordered: bool = False) -> dict:
    """{value: row} of a cut, the largest group first; with `ordered`, the
    label gives (its place, its value) and the groups go in that order."""
    out: dict = {}
    for x, y in matched:
        out.setdefault(label(x), []).append((x, y))
    if ordered:
        return {str(k[1]): row(v) for k, v in sorted(out.items())}
    return {str(k): row(v) for k, v in sorted(out.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))}


def fine_band(rank: int) -> tuple[int, str]:
    """(its place, the bench's rank band)."""
    return next((n, coldbench.band_label(rank, bench.RANK_BANDS)) for n, (lo, hi) in enumerate(bench.RANK_BANDS)
                if lo <= rank <= hi)


def compare(base_payload: dict, variant_payload: dict, draws: int = DRAWS, seed: int = SEED,
            unchecked=(), since: str = "", until: str = "", segment: str = "", refusal: bool = False) -> dict:
    """The comparison of two runs, as a dict `print_report` prints and JSON
    can hold. Raises SnapshotMismatch when the runs were not made from the
    same snapshot, or when a pair does not aim at the same thing on both
    sides; `unchecked` names the checks a run may lack the data of. `since`
    and `until` judge the cups of a part of the runs' span only. Two live
    runs are compared point by point (`compare_live`). `segment`,
    an expression on the base's forecast `p` declared before the measure,
    adds its own condition; with `refusal` the forecasts the variant no
    longer prices are judged as a refusal (see the module's notes). Raises
    ValueError for a live run against a cold one, and when not one forecast
    is paired."""
    if is_live(base_payload) and is_live(variant_payload):
        return compare_live(base_payload, variant_payload, draws, seed, unchecked, since, until, segment, refusal)
    refuse_live({"the base": base_payload, "the variant": variant_payload})
    since, until = checked_day(since, "since"), checked_day(until, "until")
    if since and until and since >= until:
        raise ValueError(f"since {since} is not before until {until}")
    in_segment = segment_test(segment)
    problems, skipped, pages, missed = snapshot_problems(base_payload, variant_payload, unchecked)
    if problems:
        raise SnapshotMismatch("the two runs were not made from the same snapshot:\n  - " + "\n  - ".join(problems))
    a = indexed(base_payload.get("pairs") or [], "base")
    b = indexed(variant_payload.get("pairs") or [], "variant")
    apart, fields, first = aimed_apart(a, b)
    if apart:
        raise SnapshotMismatch(f"{apart} of the {len(set(a) & set(b))} paired forecasts do not aim at the same thing"
                               f" on both sides ({', '.join(f'{k}: {n}' for k, n in fields.items())}), first {first}")
    if since or until:
        first, end = span_of(base_payload, a)
        if since and end and since >= end:
            raise ValueError(f"since {since} is not before the end of the runs' span ({end}, not included)")
        if until and first and until <= first:
            raise ValueError(f"until {until} is not after the start of the runs' span ({first})")
        a = {k: p for k, p in a.items() if (since or "") <= p["day"] < (until or "9999")}
        b = {k: p for k, p in b.items() if (since or "") <= p["day"] < (until or "9999")}
    if not set(a) & set(b):
        raise ValueError("not one forecast is in both runs" + (f" from {since or 'their start'} to "
                                                               f"{until or 'their end'}" if since or until else "")
                         + ": nothing to compare")
    days = sorted(p["day"] for k, p in a.items() if k in b)
    since = since or base_payload.get("since") or days[0]
    until = until or base_payload.get("until") or (date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat()
    result = {
        "base": {"generated": base_payload.get("generated"), "model_key": base_payload.get("model_key"),
                 "forecasts": len(a)},
        "variant": {"generated": variant_payload.get("generated"), "model_key": variant_payload.get("model_key"),
                    "forecasts": len(b), "variant": variant_payload.get("variant")},
        "snapshot": {"database": (base_payload.get("database") or {}).get("sha1"),
                     "catalogue": (base_payload.get("catalogue") or {}).get("sha1"),
                     "pages": pages, "boards of the base": len(boards_of(base_payload) or {}),
                     "boards of the base not read by the variant": missed,
                     "arrival": base_payload.get("arrival"),
                     "since": since, "until": until, "unchecked": skipped},
    }
    result.update(judged(a, b, since, until, draws, seed, segment, in_segment, refusal))
    return result


def compare_live(base_payload: dict, variant_payload: dict, draws: int = DRAWS, seed: int = SEED,
                 unchecked=(), since: str = "", until: str = "", segment: str = "", refusal: bool = False) -> dict:
    """Two runs of the live bench, forecast against forecast at the same cup,
    rank and point of the session, judged by the whole gain rule at each
    point: {"points": {point: the comparison at it}, "verdicts": {point:
    verdict}, ...}. The same checks of the snapshot as `compare`, `arrival`
    and the outages included, and the raw pages the live analysis read."""
    for name, run in (("the base", base_payload), ("the variant", variant_payload)):
        if not is_live(run):
            raise ValueError(f"{name} is not a run of the live bench: a live run and a cold one do not compare")
    since, until = checked_day(since, "since"), checked_day(until, "until")
    if since and until and since >= until:
        raise ValueError(f"since {since} is not before until {until}")
    in_segment = segment_test(segment)
    problems, skipped, pages, missed = snapshot_problems(base_payload, variant_payload, unchecked)
    read = [(run.get("live_pages") or {}).get("digest") for run in (base_payload, variant_payload)]
    if read[0] != read[1]:
        problems.append(f"live pages: the raw pages the live analysis read, digest {read[0]} against {read[1]}")
    if problems:
        raise SnapshotMismatch("the two runs were not made from the same snapshot:\n  - " + "\n  - ".join(problems))
    a = indexed(base_payload.get("forecasts") or [], "base")
    b = indexed(variant_payload.get("forecasts") or [], "variant")
    apart, fields, first = aimed_apart(a, b, AIM + ("minute",))
    if apart:
        raise SnapshotMismatch(f"{apart} of the {len(set(a) & set(b))} paired forecasts do not aim at the same thing"
                               f" on both sides ({', '.join(f'{k}: {n}' for k, n in fields.items())}), first {first}")
    if since or until:
        a = {k: p for k, p in a.items() if (since or "") <= p["day"] < (until or "9999")}
        b = {k: p for k, p in b.items() if (since or "") <= p["day"] < (until or "9999")}
    if not set(a) & set(b):
        raise ValueError("not one forecast is in both runs" + (f" from {since or 'their start'} to "
                                                               f"{until or 'their end'}" if since or until else "")
                         + ": nothing to compare")
    days = sorted(p["day"] for k, p in a.items() if k in b)
    since = since or base_payload.get("since") or days[0]
    until = until or base_payload.get("until") or (date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat()
    # The halves are cut on the days the runs priced: the feed began after
    # the start of a span asked for (5 September 2026), and halves cut on the
    # span asked would put few days, or none, in the first.
    first = max(since, days[0])
    end = min(until, (date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat())
    listed = [p["label"] for p in base_payload.get("points") or []]
    order = listed + sorted({k[2] for k in set(a) | set(b)} - set(listed))
    points = {}
    for label in order:
        mine = {k: p for k, p in a.items() if k[2] == label}
        theirs = {k: p for k, p in b.items() if k[2] == label}
        if set(mine) & set(theirs):
            points[label] = judged(mine, theirs, first, end, draws, seed, segment, in_segment, refusal)
        elif mine or theirs:
            points[label] = {"paired": 0, "moved": 0, "verdict": "no gain", "failed": ["nothing paired"],
                             "unpaired": {"base only": len(mine), "variant only": len(theirs)}, "conditions": []}
    return {
        "bench": "live",
        "base": {"generated": base_payload.get("generated"), "model_key": base_payload.get("model_key"),
                 "forecasts": len(a)},
        "variant": {"generated": variant_payload.get("generated"), "model_key": variant_payload.get("model_key"),
                    "forecasts": len(b), "variant": variant_payload.get("variant")},
        "snapshot": {"database": (base_payload.get("database") or {}).get("sha1"),
                     "catalogue": (base_payload.get("catalogue") or {}).get("sha1"),
                     "pages": pages, "live pages": read[0], "arrival": base_payload.get("arrival"),
                     "outages": (base_payload.get("outages") or {}).get("left_out", False),
                     "since": since, "until": until, "priced from": first, "priced until": end,
                     "unchecked": skipped},
        "paired": len(set(a) & set(b)), "moved": sum(p["moved"] for p in points.values()),
        "unpaired": {"base only": len(set(a) - set(b)), "variant only": len(set(b) - set(a))},
        "judged": "refusal" if refusal else "correction",
        "points": points,
        "verdicts": {label: p["verdict"] for label, p in points.items()},
    }


def segment_test(segment: str):
    """The test of a declared segment on a forecast `p`, or None for none.
    ValueError when the expression does not read."""
    if not segment:
        return None
    try:
        code = compile(segment, "--segment", "eval")
    except SyntaxError as exc:
        raise ValueError(f"--segment is not an expression: {exc.msg}: {segment!r}") from None

    def test(pair: dict) -> bool:
        try:
            return bool(eval(code, {"__builtins__": SEGMENT_BUILTINS}, {"p": pair}))  # noqa: S307 (the author's own)
        except Exception as exc:
            raise ValueError(f"--segment {segment!r} fails on {pair.get('window')} at rank {pair.get('rank')}:"
                             f" {type(exc).__name__}: {exc}") from None
    return test


def moved_pair(x: dict, y: dict) -> bool:
    """Do the two sides of a pair differ in what the forecast says?"""
    return any(x.get(name) != y.get(name) for name in ("forecast", "rel", "bands", "near", "wide"))


def judged(a: dict, b: dict, since: str, until: str, draws: int = DRAWS, seed: int = SEED,
           segment: str = "", in_segment=None, refusal: bool = False) -> dict:
    """The measures of two runs indexed alike ({key: forecast}, over the days
    judged only) and the verdict of the gain rule on them."""
    keys = sorted(set(a) & set(b))
    matched = [(a[k], b[k]) for k in keys]
    only_base, only_variant = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    moved = sum(1 for x, y in matched if moved_pair(x, y))
    middle = halves_of(since, until)[0]
    first_half = [m for m in matched if m[0]["day"] < middle]
    second_half = [m for m in matched if m[0]["day"] >= middle]
    by_season: dict = {}
    for m in matched:
        by_season.setdefault(m[0].get("season"), []).append(m)
    seasons = [dict(row(v), season=s) for s, v in sorted(by_season.items(), key=lambda kv: (kv[0] is None, str(kv[0])))]
    overall = {"all": row(matched),
               "at the cut": row([m for m in matched if m[0].get("at_cut")]),
               f"results of {LOW}+ points": row([m for m in matched if m[0]["result"] >= LOW]),
               f"results under {LOW} points": row([m for m in matched if m[0]["result"] < LOW])}
    tables = {"region": grouped(matched, lambda p: p.get("region") or "?"),
              "rank band": grouped(matched, lambda p: (int(p["rank"]) > SPLIT, rank_band(int(p["rank"]))),
                                   ordered=True),
              "rank band (the bench's, for information)": grouped(matched, lambda p: fine_band(int(p["rank"])),
                                                                  ordered=True),
              "rung (the base's)": grouped(matched, lambda p: p.get("source") or p.get("rung") or "?")}
    intervals = {name: {"level": LEVEL, "draws": draws, "seed": seed, "unit": name,
                        **interval(matched, draws, seed, unit=unit)} for name, unit in UNITS}
    result = {
        "paired": len(matched), "moved": moved,
        "unpaired": {"base only": len(only_base), "variant only": len(only_variant),
                     "base only, first": [" @ ".join(map(str, k)) for k in only_base[:8]],
                     "variant only, first": [" @ ".join(map(str, k)) for k in only_variant[:8]]},
        "overall": overall,
        # The interval that judges, by draws of family x local day; the others
        # are shown for information.
        "interval": intervals[UNITS[0][0]],
        "intervals": {name: found for name, found in intervals.items() if name != UNITS[0][0]},
        "halves": [dict(row(first_half), label=f"{since} to {day_before(middle)}", since=since, until=middle),
                   dict(row(second_half), label=f"{middle} to {day_before(until)}", since=middle, until=until)],
        "seasons": seasons,
        "tables": tables,
        "segment": None,
        "refusal": None,
    }
    if in_segment is not None:
        inside = [m for m in matched if in_segment(m[0])]
        result["segment"] = dict(row(inside), expression=segment)
    if refusal:
        result["refusal"] = refused_apart(a, [a[k] for k in only_base], matched, middle)
    result["judged"] = "refusal" if refusal else "correction"
    result["conditions"], result["verdict"] = judge(result, only_base)
    result["failed"] = [c["rule"] for c in result["conditions"] if not c["holds"]]
    return result


def refused_apart(a: dict, refused: list[dict], matched: list[tuple[dict, dict]], middle: str) -> dict:
    """What the refusal rule reads: how many forecasts of the base the variant
    refuses, and their median absolute error against the others' (the paired
    forecasts of the base), over the span, in each half and in each season."""
    def median_of(group: list[dict]) -> float | None:
        return statistics.median(abs(coldbench.error(p)) for p in group) if group else None

    def cell(label: str, picked) -> dict:
        mine = [p for p in refused if picked(p)]
        others = [x for x, _ in matched if picked(x)]
        theirs, ours = median_of(mine), median_of(others)
        return {"label": label, "refused": len(mine), "others": len(others), "refused_median": theirs,
                "others_median": ours, "cups": len({p["window"] for p in mine + others}),
                "holds": bool(mine and others and theirs >= REFUSED_FACTOR * ours)}

    seasons = sorted({p.get("season") for p in refused} - {None}, key=str)
    return {"refused": len(refused), "of": len(a), "share": len(refused) / len(a) if a else 0.0,
            "all": cell("all", lambda p: True),
            "halves": [cell("first half", lambda p: p["day"] < middle),
                       cell("second half", lambda p: p["day"] >= middle)],
            "seasons": [dict(cell(f"season {s}", lambda p, s=s: p.get("season") == s), season=s) for s in seasons]}


def judge(result: dict, only_base: list) -> tuple[list[dict], str]:
    """The conditions of the gain rule, and the verdict: those of a
    correction, or those of a refusal judged apart."""
    change, drawn = result["overall"]["all"]["change"], result["interval"]
    unpaired = result["unpaired"]
    conditions = []

    def need(rule: str, holds: bool, detail: str) -> None:
        conditions.append({"rule": rule, "holds": bool(holds), "detail": detail})

    # The conditions that could not be judged for want of data, said as such.
    unjudged = result.setdefault("not judged", [])
    if result.get("refusal"):
        told = result["refusal"]
        need("the variant prices no forecast the base does not", not unpaired["variant only"],
             f"{unpaired['variant only']} in the variant only")
        need("the forecasts it does not refuse are unchanged", not result["moved"], f"{result['moved']} moved")
        need(f"at most {100 * REFUSED_SHARE:g} % of the base's forecasts refused",
             0 < told["refused"] <= REFUSED_SHARE * told["of"],
             f"{told['refused']} of {told['of']} ({100 * told['share']:.2f} %)")
        need(f"median |error| of the refused {REFUSED_FACTOR:g} times the others' at least, in both halves",
             all(h["holds"] for h in told["halves"]),
             "; ".join(f"{h['refused']} refused {fmt(h['refused_median'], '.2f')} against "
                       f"{fmt(h['others_median'], '.2f')}" for h in told["halves"]))
        counted = [s for s in told["seasons"] if s["cups"] >= SEASON_CUPS]
        if len(counted) >= 2:
            held = sum(1 for s in counted if s["holds"])
            need(f"the same in most seasons with {SEASON_CUPS}+ cups and a refusal", 2 * held > len(counted),
                 f"{held} of {len(counted)} seasons")
        else:
            unjudged.append(f"the refusal by season: {len(counted)} season(s) with {SEASON_CUPS}+ cups and a refusal,"
                            " two needed")
        if not told["refused"] and not result["moved"] and not unpaired["variant only"]:
            return conditions, "no change"
        return conditions, "gain" if all(c["holds"] for c in conditions) else "no gain"

    for name, what in DOWN:
        if name == "abs_median":
            need(f"{what} not up", change[name] <= SAME_MEDIAN, f"{change[name]:+.3f}")
        else:
            need(f"{what} down", change[name] < 0, f"{change[name]:+.3f}")
    if result.get("segment") is not None:
        seg = result["segment"]
        need(f"median |error| of the declared segment down ({seg['pairs']} forecasts)",
             bool(seg["pairs"]) and seg["change"]["abs_median"] < 0,
             "empty segment" if not seg["pairs"] else f"{seg['change']['abs_median']:+.3f}")
    low, high = drawn["abs_mean"] or (0.0, 0.0)
    by_cup = (result.get("intervals") or {}).get("cup", {}).get("abs_mean")
    need(f"{100 * LEVEL:.0f} % interval of the paired mean |error| below zero (draws of {drawn['unit']})", high < 0,
         f"[{low:+.3f}, {high:+.3f}]" + (f" (by cup [{by_cup[0]:+.3f}, {by_cup[1]:+.3f}])" if by_cup else ""))
    halves = result["halves"]
    need("in both halves: mean |error| and log error down, median not up",
         all(h["pairs"] and h["change"]["abs_mean"] < 0 and h["change"]["log_mean"] < 0
             and h["change"]["abs_median"] <= SAME_MEDIAN for h in halves),
         "; ".join("empty" if not h["pairs"] else " ".join(f"{h['change'][name]:+.2f}" for name, _ in DOWN)
                   for h in halves))
    counted = [s for s in result["seasons"] if s["season"] is not None and s["cups"] >= SEASON_CUPS]
    if len(counted) >= 2:
        down = sum(1 for s in counted if s["change"]["abs_median"] < 0 and s["change"]["log_mean"] < 0)
        need("median |error| and log error down in most seasons", 2 * down > len(counted),
             f"{down} of {len(counted)} seasons with {SEASON_CUPS}+ cups (mean |error|, not judged: "
             + ", ".join(f"{s['season']} {s['change']['abs_mean']:+.2f}" for s in counted) + ")")
    else:
        unjudged.append("the seasons: " + (
            "the forecasts carry no season" if all(s["season"] is None for s in result["seasons"]) else
            f"{len(counted)} season(s) with {SEASON_CUPS}+ cups, two needed"))
    if not any(r["pairs"] >= REGION_PAIRS and r["cups"] >= REGION_CUPS for r in result["tables"]["region"].values()):
        unjudged.append(f"the regions: none has {REGION_PAIRS} forecasts and {REGION_CUPS} cups")
    worse = {k: r["change"]["abs_mean"] for k, r in result["tables"]["region"].items()
             if r["pairs"] >= REGION_PAIRS and r["cups"] >= REGION_CUPS and r["change"]["abs_mean"] is not None
             and r["change"]["abs_mean"] > HARM + SAME_MEDIAN}
    need(f"no region ({REGION_PAIRS}+ forecasts, {REGION_CUPS}+ cups) worse by more than {HARM} point", not worse,
         ", ".join(f"{k} {v:+.2f}" for k, v in worse.items()) or "none")
    worse = {k: r["change"]["abs_mean"] for k, r in result["tables"]["rank band"].items()
             if r["change"]["abs_mean"] is not None and r["change"]["abs_mean"] > HARM + SAME_MEDIAN}
    need(f"no rank band worse by more than {HARM} point", not worse,
         ", ".join(f"{k} {v:+.2f}" for k, v in worse.items()) or "none")
    need("every forecast of the base priced", not only_base, f"{len(only_base)} missing from the variant")
    if not result["moved"] and not unpaired["base only"] and not unpaired["variant only"]:
        return conditions, "no change"
    if all(c["holds"] for c in conditions):
        return conditions, "gain"
    return conditions, "loss" if low > 0 else "no gain"


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def fmt(value, spec: str, scale: float = 1.0) -> str:
    return "-" if value is None else format(scale * value, spec)


def print_overall(result: dict) -> None:
    print(f"\n{'':<34}{'pairs':>6}{'cups':>6}   {'':<9}{'signed':>8}{'median':>8}{'|err|':>8}{'median':>8}"
          f"{'log':>8}{'median':>8}{'in 50%':>8}{'in 90%':>8}")
    for label, r in result["overall"].items():
        for n, side in enumerate(("base", "variant", "change")):
            s = r[side]
            sign = "+" if side == "change" else ""
            print(f"  {(label if n == 0 else ''):<32}{(r['pairs'] if n == 0 else ''):>6}"
                  f"{(r['cups'] if n == 0 else ''):>6}"
                  f"   {side:<9}{fmt(s['signed_mean'], '+.2f'):>8}{fmt(s['signed_median'], '+.2f'):>8}"
                  f"{fmt(s['abs_mean'], sign + '.2f'):>8}{fmt(s['abs_median'], sign + '.2f'):>8}"
                  f"{fmt(s['log_mean'], sign + '.2f'):>8}{fmt(s['log_median'], sign + '.2f'):>8}"
                  f"{fmt(s['in_50'], sign + '.1f', 100):>8}{fmt(s['in_90'], sign + '.1f', 100):>8}")


def print_changes(title: str, rows: list[tuple[str, dict]]) -> None:
    print(f"\n{title}")
    print(f"  {'':<34}{'pairs':>6}{'cups':>6}{'|err|':>8}{'then':>8}{'change':>8}{'median':>8}{'then':>8}"
          f"{'change':>8}{'log':>8}{'in 50%':>8}{'in 90%':>8}")
    for label, r in rows:
        a, b, c = r["base"], r["variant"], r["change"]
        print(f"  {str(label)[:33]:<34}{r['pairs']:>6}{r['cups']:>6}{fmt(a['abs_mean'], '.2f'):>8}"
              f"{fmt(b['abs_mean'], '.2f'):>8}{fmt(c['abs_mean'], '+.2f'):>8}{fmt(a['abs_median'], '.2f'):>8}"
              f"{fmt(b['abs_median'], '.2f'):>8}{fmt(c['abs_median'], '+.2f'):>8}{fmt(c['log_mean'], '+.2f'):>8}"
              f"{fmt(c['in_50'], '+.1f', 100):>8}{fmt(c['in_90'], '+.1f', 100):>8}")


def print_report(result: dict) -> None:
    """The comparison, for a reader."""
    snap = result["snapshot"]
    print("\n" + "=" * 112)
    print(f"  Paired comparison, cups from {snap['since']} to {snap['until']} (not included)")
    print("=" * 112)
    print(f"  database {str(snap['database'])[:12]}, catalogue {str(snap['catalogue'] or 'unknown')[:12]};"
          f" pages: {snap['pages']}")
    if snap["unchecked"]:
        print(f"  NOT CHECKED: {'; '.join(snap['unchecked'])}")
    un = result["unpaired"]
    print(f"  {result['paired']} forecasts paired by cup and rank, {result['moved']} of them moved;"
          f" {un['base only']} in the base only, {un['variant only']} in the variant only")
    for side in ("base only", "variant only"):
        if un[f"{side}, first"]:
            more = " ..." if un[side] > len(un[f"{side}, first"]) else ""
            print(f"    {side}: {', '.join(un[f'{side}, first'])}{more}")
    print("  error in % of the result (+ when the forecast said more); log = 100 |ln(forecast / result)|;"
          " the change is the variant less the base")
    print_overall(result)
    print_judged(result)
    print("\n" + verdict_line(result))


def print_judged(result: dict) -> None:
    """The intervals, the cuts, the declared segment or the refusal, and the
    conditions of the gain rule, as `print_report` prints them."""
    drawn = result["interval"]
    print(f"\n{100 * drawn['level']:.0f} % interval of the paired change, {drawn['draws']} draws (seed"
          f" {drawn['seed']}) of {drawn['unit']}, which judges; of the cups and of the model's day, for information:")
    shown = [drawn] + list((result.get("intervals") or {}).values())
    for name, what in (("signed_mean", "signed mean"), ("abs_mean", "mean |error|"),
                       ("abs_median", "median |error|"), ("log_mean", "mean log error")):
        change = result["overall"]["all"]["change"][name]
        print(f"  {what:<16}{fmt(change, '+.3f'):>9}   " + "   ".join(
            "-" if not found.get(name) else f"[{found[name][0]:+.3f}, {found[name][1]:+.3f}]" for found in shown))
    print_changes("by half of the span", [(h["label"], h) for h in result["halves"] if h["pairs"]])
    if len([s for s in result["seasons"] if s["season"] is not None]) > 1:
        print_changes("by season", [(f"season {s['season']}" if s["season"] is not None else "no season", s)
                                    for s in result["seasons"]])
    for name, rows in result["tables"].items():
        print_changes(f"by {name}", list(rows.items()))
    if result.get("segment") is not None:
        print_changes(f"the declared segment: {result['segment']['expression']}", [("segment", result["segment"])])
    told = result.get("refusal")
    if told:
        print(f"\nthe refusal: {told['refused']} of the base's {told['of']} forecasts"
              f" ({100 * told['share']:.2f} %); median |error| of the refused against the others':")
        for c in [told["all"]] + told["halves"] + told["seasons"]:
            print(f"  {c['label']:<16}{c['refused']:>7} refused {fmt(c['refused_median'], '.2f'):>8}"
                  f"   {c['others']:>7} others {fmt(c['others_median'], '.2f'):>8}   {c['cups']:>5} cups")
    print(f"\nthe gain rule{' (a refusal, judged apart)' if told else ''}:")
    for c in result["conditions"]:
        print(f"  {'ok  ' if c['holds'] else 'FAIL'}  {c['rule']:<72} {c['detail']}")
    for told in result.get("not judged") or []:
        print(f"  --    not judged, for want of data: {told}")


def print_live_report(result: dict) -> None:
    """Two live runs compared, for a reader: a line per point of the session,
    then the conditions at each point where anything moved."""
    snap = result["snapshot"]
    print("\n" + "=" * 112)
    print(f"  Paired comparison of two live runs, cups from {snap['since']} to {snap['until']} (not included),"
          f" arrival {snap['arrival']}")
    print("=" * 112)
    print(f"  database {str(snap['database'])[:12]}, catalogue {str(snap['catalogue'] or 'unknown')[:12]};"
          f" pages: {snap['pages']}; live pages {snap['live pages']}")
    if snap["unchecked"]:
        print(f"  NOT CHECKED: {'; '.join(snap['unchecked'])}")
    un = result["unpaired"]
    print(f"  {result['paired']} forecasts paired by cup, rank and point, {result['moved']} of them moved;"
          f" {un['base only']} in the base only, {un['variant only']} in the variant only")
    print(f"\n  {'point':<9}{'pairs':>7}{'moved':>7}{'|err|':>8}{'then':>8}{'change':>8}{'median':>8}{'change':>8}"
          f"{'log':>8}   {'interval of the mean (family x local day)':<44}verdict")
    for label, r in result["points"].items():
        if not r["paired"]:
            print(f"  {label:<9}{0:>7}   nothing paired: {r['unpaired']}")
            continue
        a, c = r["overall"]["all"]["base"], r["overall"]["all"]["change"]
        drawn = r["interval"]["abs_mean"]
        b = r["overall"]["all"]["variant"]
        print(f"  {label:<9}{r['paired']:>7}{r['moved']:>7}{fmt(a['abs_mean'], '.2f'):>8}{fmt(b['abs_mean'], '.2f'):>8}"
              f"{fmt(c['abs_mean'], '+.3f'):>8}{fmt(a['abs_median'], '.2f'):>8}{fmt(c['abs_median'], '+.3f'):>8}"
              f"{fmt(c['log_mean'], '+.3f'):>8}   "
              f"{'-' if not drawn else f'[{drawn[0]:+.3f}, {drawn[1]:+.3f}]':<44}{r['verdict'].upper()}")
    for label, r in result["points"].items():
        if r["paired"] and r["verdict"] != "no change":
            print(f"\nat {label}:")
            for c in r["conditions"]:
                print(f"  {'ok  ' if c['holds'] else 'FAIL'}  {c['rule']:<72} {c['detail']}")
            for told in r.get("not judged") or []:
                print(f"  --    not judged, for want of data: {told}")
    everywhere = set.intersection(*[set(r.get("not judged") or []) for r in result["points"].values()]) \
        if result["points"] else set()
    if everywhere:
        print("\nnot judged at any point, for want of data: " + "; ".join(sorted(everywhere)))
    print(f"halves cut on the days priced, {snap['priced from']} to {snap['priced until']} (not included)")
    print("\nverdicts: " + ", ".join(f"{label} {verdict.upper()}" for label, verdict in result["verdicts"].items())
          + (f" (not checked: {'; '.join(snap['unchecked'])})" if snap["unchecked"] else ""))


def verdict_line(result: dict) -> str:
    """The verdict, what it fails, and what it could not check."""
    snap = result["snapshot"]
    return (f"verdict: {result['verdict'].upper()}"
            + (f" - fails: {'; '.join(result['failed'])}" if result["failed"] and result["verdict"] != "no change"
               else "")
            + (f" (not checked: {'; '.join(snap['unchecked'])})" if snap["unchecked"] else ""))


# --------------------------------------------------------------------------- #
# The strawman
# --------------------------------------------------------------------------- #
def cup_times(db_path: str) -> dict[int, dict]:
    """{competition id: {"start_time", "end_time"}} off the database, read-only."""
    conn = bench.open_read_only(db_path, live=bench.same_file(db_path, str(bench.db.DB_PATH)))
    try:
        return {cid: {"start_time": start, "end_time": end}
                for cid, start, end in conn.execute("SELECT id, start_time, end_time FROM competition")}
    finally:
        conn.close()


def over_before(times: dict, cutoff: str) -> bool:
    """Was the cup over when the model of `cutoff` was built? As the bench
    decides which tournaments a model holds."""
    return bench.held(times, cutoff)


def strawman_live(payload: dict, times: dict[int, dict], prior: float = STRAWMAN_PRIOR) -> dict:
    """The live run, each forecast corrected by the live strawman (see the
    module's notes): the median signed error of its point, depth and region
    over the forecasts of cups over when its model was built, shrunk by
    n / (n + prior); its ranges move with it. A payload `compare_live` takes
    against the run itself."""
    rows = payload.get("forecasts") or []
    missing = sorted({r["id"] for r in rows if r.get("id") not in times})
    if missing:
        raise ValueError(f"{len(missing)} cups of the run are not in the database given, first id {missing[0]}")

    def group(row: dict) -> tuple:
        return str(row.get("point")), str(row.get("depth") or "?"), str(row.get("region") or "?")

    learned: dict = {}
    for cutoff in sorted({r["model"] for r in rows}):
        groups: dict = {}
        for q in rows:
            if over_before(times[q["id"]], cutoff):
                groups.setdefault(group(q), []).append(coldbench.error(q))
        learned[cutoff] = groups
    out = []
    for r in rows:
        errors = learned[r["model"]].get(group(r), [])
        n = len(errors)
        middle = statistics.median(errors) if n else 0.0
        shrunk = middle * n / (n + prior)
        factor = 1 / (1 + shrunk / 100)
        near = [x * factor for x in r["near"]] if r.get("near") else r.get("near")
        wide = [x * factor for x in r["wide"]] if r.get("wide") else r.get("wide")
        moved = dict(r, forecast=r["forecast"] * factor, near=near, wide=wide,
                     error=100 * (r["forecast"] * factor / r["result"] - 1),
                     strawman={"n": n, "median": middle, "shrunk": shrunk, "before": r["forecast"]})
        moved["in_50"], moved["in_90"] = covered(moved, "50"), covered(moved, "90")
        out.append(moved)
    corrected = {k: v for k, v in payload.items() if k not in ("forecasts", "tables", "lines")}
    corrected["variant"] = {"name": "strawman", "prior": prior, "group": "point x depth x region",
                            "corrected": sum(1 for r in out if r["strawman"]["n"]),
                            "moved": sum(1 for r in out if r["forecast"] != r["strawman"]["before"])}
    corrected["forecasts"] = out
    return corrected


def strawman_group(pair: dict, regions: bool = True) -> tuple:
    group = (pair.get("source") or "?", rank_band(int(pair["rank"])))
    return group + ((pair.get("region") or "?",) if regions else ())


def strawman(payload: dict, times: dict[int, dict], regions: bool = True,
             prior: float = STRAWMAN_PRIOR) -> dict:
    """The run, each forecast corrected by the strawman (see the module's
    notes), as a payload `compare` takes against the run itself. `times` holds
    each cup's start and end, `cup_times` off the run's own database. A live
    run takes the live strawman (`strawman_live`)."""
    if is_live(payload):
        return strawman_live(payload, times, prior)
    pairs = payload.get("pairs") or []
    missing = sorted({p["id"] for p in pairs if p.get("id") not in times})
    if missing:
        raise ValueError(f"{len(missing)} cups of the run are not in the database given, first id {missing[0]}")
    learned: dict = {}
    for cutoff in sorted({p["model"] for p in pairs}):
        groups: dict = {}
        for q in pairs:
            if over_before(times[q["id"]], cutoff):
                cell = groups.setdefault(strawman_group(q, regions), [[], ""])
                cell[0].append(coldbench.error(q))
                cell[1] = max(cell[1], bench.over_by(times[q["id"]]))
        learned[cutoff] = groups
    out = []
    for p in pairs:
        errors, latest = learned[p["model"]].get(strawman_group(p, regions), [[], ""])
        n = len(errors)
        middle = statistics.median(errors) if n else 0.0
        shrunk = middle * n / (n + prior)
        out.append(dict(p, forecast=p["forecast"] / (1 + shrunk / 100),
                        strawman={"n": n, "median": middle, "shrunk": shrunk, "latest": latest,
                                  "before": p["forecast"]}))
    corrected = dict(payload, pairs=out,
                     variant={"name": "strawman", "regions": regions, "prior": prior,
                              "group": "rung x rank band" + (" x region" if regions else ""),
                              "corrected": sum(1 for p in out if p["strawman"]["n"]),
                              "moved": sum(1 for p in out if p["forecast"] != p["strawman"]["before"])})
    corrected["all"] = coldbench.summary(out, shown=True) if out else None
    corrected["at_cut"] = coldbench.summary([p for p in out if p["at_cut"]], shown=True) if out else None
    corrected.pop("tables", None)
    corrected.pop("biases", None)
    return corrected


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        run = json.load(fh)
    if not isinstance(run, dict):
        raise ValueError(f"{path} is not a run of the bench written with --json")
    return run


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="*", metavar="RUN.json", help="the base, then the variant")
    parser.add_argument("--unchecked", default="",
                        help=f"checks a run may lack the data of, comma-separated ({', '.join(CHECKS)})")
    parser.add_argument("--since", default="", help="judge the cups from this day only (YYYY-MM-DD)")
    parser.add_argument("--until", default="", help="judge the cups before this day only (YYYY-MM-DD)")
    parser.add_argument("--segment", default="", metavar="EXPRESSION",
                        help="a segment declared before the measure, on what the page knew before the cup: a "
                             "Python expression on the base's forecast p, e.g. \"p['rank'] > 250\"; its median "
                             "|error| must go down too")
    parser.add_argument("--refusal", action="store_true",
                        help="judge the forecasts the variant no longer prices as a refusal (see the notes); "
                             "without it, a forecast lost fails the gain rule")
    parser.add_argument("--draws", type=int, default=DRAWS, help=f"draws of each interval (default {DRAWS})")
    parser.add_argument("--seed", type=int, default=SEED, help=f"the draws' seed (default {SEED})")
    parser.add_argument("--json", default="", help="write the comparison here (with --strawman: the corrected run)")
    parser.add_argument("--strawman", default="", metavar="BASE.json",
                        help="correct the run by the strawman, then compare it with the run")
    parser.add_argument("--db", default="", help="with --strawman: the run's database, for the cups' end times")
    parser.add_argument("--no-regions", action="store_true", help="with --strawman: groups without the region")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = argument_parser().parse_args(argv)
    unchecked = [x.strip() for x in args.unchecked.split(",") if x.strip()]
    if not args.strawman and len(args.runs) != 2:
        print("Two runs of the bench written with --json, the base then the variant:\n"
              "  python -m analysis.bench_compare base.json variant.json   (see --help)")
        return 0 if not args.runs else 2
    try:
        checked_day(args.since, "--since")
        checked_day(args.until, "--until")
        if args.strawman:
            base = load(args.strawman)
            path = args.db or str(bench.db.DB_PATH)
            known = (base.get("database") or {}).get("sha1")
            if not os.path.exists(path):
                print(f"No database at {path}.")
                return 2
            if known and bench.file_sha1(path) != known:
                print(f"The database {path} is not the one the run was made from ({known[:12]}).")
                return 2
            variant = strawman(base, cup_times(path), regions=not args.no_regions)
            told = variant["variant"]
            print(f"Strawman: {told['corrected']:,} of {len(variant.get('pairs') or variant.get('forecasts')):,}"
                  f" forecasts have a group to learn from, {told['moved']:,} of them moved")
            if args.json:
                with open(args.json, "w", encoding="utf-8") as fh:
                    json.dump(variant, fh, ensure_ascii=False, indent=1)
                print(f"Wrote {args.json}")
            runs = (base, variant)
        else:
            runs = (load(args.runs[0]), load(args.runs[1]))
        result = compare(*runs, draws=args.draws, seed=args.seed, unchecked=unchecked,
                         since=args.since, until=args.until, segment=args.segment, refusal=args.refusal)
    except SnapshotMismatch as refused:
        print(f"Refused: {refused}")
        return 2
    except (OSError, ValueError, KeyError) as error:
        print(f"Cannot compare: {error}")
        return 2
    if result.get("bench") == "live":
        print_live_report(result)
    else:
        print_report(result)
    if args.json and not args.strawman:
        try:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1)
        except OSError as error:
            print(f"\nCannot write {args.json}: {error}")
            return 2
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
