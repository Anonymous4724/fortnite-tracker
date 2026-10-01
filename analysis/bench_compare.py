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
Runs of the live bench are refused: their forecasts, one per cup, rank and
point of the cup, are kept under "forecasts" and are not paired yet.

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

How sure the change is comes from drawing the cups again with replacement
(DRAWS times, from a fixed seed, so the same two files always print the same
interval): a cup's forecasts are drawn together, since one cup's ranks err
together. The verdict follows the rule a change has to pass to count as a
gain, all of it:

- the mean and the median absolute error and the mean log error go down;
- the 90 % interval of the paired change in the mean absolute error lies
  entirely below zero;
- the same sign in both halves of the span: the mean and the median absolute
  error and the mean log error all go down in each half;
- when at least two seasons have SEASON_CUPS paired cups or more, the median
  absolute error and the mean log error both go down in more than half of
  those seasons. The mean absolute error is shown season by season but does
  not judge: over a long run, a few families more than 100 % off carry it;
- no region and neither rank band (to 250, past it) is worse by more than
  HARM points of mean absolute error;
- the change prices every forecast the base prices.

A change with not one forecast moved is "no change"; one whose interval lies
entirely above zero is a "loss"; any other that fails a condition is "no gain",
with the conditions it fails.

The strawman is the correction any idea has to beat: each forecast divided by
one plus the median signed error of its rung, rank band (to 250, past it) and
region, over the forecasts of the same run whose cups were over when the
forecast's model was built (`bench.held`, the end times read from the
database), the median shrunk by n / (n + STRAWMAN_PRIOR) for a group of n
forecasts. Learned walk-forward, it is what a correction by the groups the
bench prints is worth out of sample.

    python -m analysis.bench_compare base.json variant.json
    python -m analysis.bench --compare base.json variant.json   (any option below too)
    python -m analysis.bench_compare base.json variant.json --json out.json
    python -m analysis.bench_compare base.json variant.json --unchecked catalogue
    python -m analysis.bench_compare base.json variant.json --since 2026-09-09
    python -m analysis.bench_compare --strawman base.json --db tracker.db --json strawman.json
"""
from __future__ import annotations

import argparse
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
# Points of mean absolute error a region or a rank band may lose.
HARM = 0.5
# Results under this many points are shown apart.
LOW = 10
# The rank bands the rule reads: to this rank, and past it.
SPLIT = 250
# A season counts towards the season rule with this many paired cups.
SEASON_CUPS = 20
STRAWMAN_PRIOR = 50
# Two results this close are the same result.
SAME_RESULT = 1e-9

CHECKS = ("bench", "database", "catalogue", "pages", "span", "cutoff", "ranks", "bands", "arrival")
# Checks of a field only some benches write: missing from both runs, nothing to check.
OPTIONAL = ("arrival",)
# What a forecast aims at: both sides of a pair must agree on all of it.
AIM = ("result", "id", "day", "region", "at_cut")
STATS = ("signed_mean", "signed_median", "abs_mean", "abs_median", "log_mean", "log_median", "in_50", "in_90")
DRAWN = ("signed_mean", "abs_mean", "abs_median", "log_mean")
# The measures that must go down, overall and in each half.
DOWN = (("abs_mean", "mean absolute error"), ("abs_median", "median absolute error"), ("log_mean", "mean log error"))


class SnapshotMismatch(ValueError):
    """Two runs that were not made from the same snapshot."""


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #
def refuse_live(runs: dict) -> None:
    """ValueError when one of `runs` ({what it is: run}) comes from the live
    bench, whose forecasts are kept under "forecasts", not "pairs"."""
    live = [name for name, run in runs.items()
            if run.get("bench") == "live" or ("forecasts" in run and "pairs" not in run)]
    if live:
        raise ValueError(f"live runs are not paired yet: {' and '.join(live)} {'come' if len(live) > 1 else 'comes'}"
                         " from the live bench, which keeps a forecast per cup, rank and point of the cup;"
                         " only the cold bench's runs are compared")


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


def aimed_apart(a: dict, b: dict) -> tuple[int, dict, str]:
    """(how many of the keys both runs price do not aim at the same thing on
    both sides, how many differ on each field of AIM, the first one told)."""
    count, fields, first = 0, {}, ""
    for key in sorted(set(a) & set(b)):
        x, y = a[key], b[key]
        off = [name for name in AIM if (abs(x["result"] - y["result"]) > SAME_RESULT if name == "result"
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
def key_of(pair: dict) -> tuple[str, int]:
    return str(pair["window"]), int(pair["rank"])


def positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def indexed(pairs: list[dict], side: str) -> dict:
    """{(window, rank): forecast} of one run, each forecast checked for what
    the measures read."""
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
            raise ValueError(f"the {side} prices {key[0]} at rank {key[1]} twice")
        out[key] = p
    return out


def log_error(pair: dict) -> float:
    """|ln(forecast / result)|, in hundredths."""
    return 100.0 * abs(math.log(pair["forecast"] / pair["result"]))


def stats(pairs: list[dict]) -> dict:
    """The measures of one side over a group of forecasts."""
    if not pairs:
        return {name: None for name in STATS}
    errors = [coldbench.error(p) for p in pairs]
    logs = [log_error(p) for p in pairs]
    near = [x for x in (coldbench.inside(p, "50") for p in pairs) if x is not None]
    wide = [x for x in (coldbench.inside(p, "90") for p in pairs) if x is not None]
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


def interval(matched: list[tuple[dict, dict]], draws: int = DRAWS, seed: int = SEED,
             level: float = LEVEL) -> dict:
    """{measure: [low, high]}: the interval of the paired change in the signed
    and absolute mean, the absolute median and the mean log error, the cups
    drawn again with replacement, each with all its forecasts."""
    cups: dict = {}
    for x, y in matched:
        ex, ey = coldbench.error(x), coldbench.error(y)
        cell = cups.setdefault(x["window"], [0, 0.0, 0.0, 0.0, [], []])
        cell[0] += 1
        cell[1] += ey - ex
        cell[2] += abs(ey) - abs(ex)
        cell[3] += log_error(y) - log_error(x)
        cell[4].append(abs(ex))
        cell[5].append(abs(ey))
    units = [cups[k] for k in sorted(cups)]
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
            unchecked=(), since: str = "", until: str = "") -> dict:
    """The comparison of two runs, as a dict `print_report` prints and JSON
    can hold. Raises SnapshotMismatch when the runs were not made from the
    same snapshot, or when a pair does not aim at the same thing on both
    sides; `unchecked` names the checks a run may lack the data of. `since`
    and `until` judge the cups of a part of the runs' span only. Raises
    ValueError for a live run, and when not one forecast is paired."""
    refuse_live({"the base": base_payload, "the variant": variant_payload})
    since, until = checked_day(since, "since"), checked_day(until, "until")
    if since and until and since >= until:
        raise ValueError(f"since {since} is not before until {until}")
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
    keys = sorted(set(a) & set(b))
    if not keys:
        raise ValueError("not one forecast is in both runs" + (f" from {since or 'their start'} to "
                                                               f"{until or 'their end'}" if since or until else "")
                         + ": nothing to compare")
    matched = [(a[k], b[k]) for k in keys]
    only_base, only_variant = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    moved = sum(1 for x, y in matched if (x["forecast"], x.get("rel"), x.get("bands"))
                != (y["forecast"], y.get("rel"), y.get("bands")))
    days = sorted(x["day"] for x, _ in matched)
    since = since or base_payload.get("since") or (days[0] if days else "")
    until = until or base_payload.get("until") or ((date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat()
                                                   if days else "")
    middle = halves_of(since, until)[0] if since and until else ""
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
              "rung (the base's)": grouped(matched, lambda p: p.get("source") or "?")}
    drawn = interval(matched, draws, seed)
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
        "paired": len(matched), "moved": moved,
        "unpaired": {"base only": len(only_base), "variant only": len(only_variant),
                     "base only, first": [f"{w} @ {r}" for w, r in only_base[:8]],
                     "variant only, first": [f"{w} @ {r}" for w, r in only_variant[:8]]},
        "overall": overall,
        "interval": {"level": LEVEL, "draws": draws, "seed": seed, **drawn},
        "halves": [dict(row(first_half), label=f"{since} to {day_before(middle)}", since=since, until=middle),
                   dict(row(second_half), label=f"{middle} to {day_before(until)}", since=middle, until=until)],
        "seasons": seasons,
        "tables": tables,
    }
    result["conditions"], result["verdict"] = judge(result, only_base)
    result["failed"] = [c["rule"] for c in result["conditions"] if not c["holds"]]
    return result


def judge(result: dict, only_base: list) -> tuple[list[dict], str]:
    """The conditions of the gain rule, and the verdict."""
    change, drawn = result["overall"]["all"]["change"], result["interval"]
    conditions = []

    def need(rule: str, holds: bool, detail: str) -> None:
        conditions.append({"rule": rule, "holds": bool(holds), "detail": detail})

    for name, what in DOWN:
        need(f"{what} down", change[name] < 0, f"{change[name]:+.3f}")
    low, high = drawn["abs_mean"] or (0.0, 0.0)
    need(f"{100 * LEVEL:.0f} % interval of the paired mean absolute error below zero", high < 0,
         f"[{low:+.3f}, {high:+.3f}]")
    halves = result["halves"]
    need("mean, median |error| and log error down in both halves",
         all(h["pairs"] and all(h["change"][name] < 0 for name, _ in DOWN) for h in halves),
         "; ".join("empty" if not h["pairs"] else " ".join(f"{h['change'][name]:+.2f}" for name, _ in DOWN)
                   for h in halves))
    counted = [s for s in result["seasons"] if s["season"] is not None and s["cups"] >= SEASON_CUPS]
    if len(counted) >= 2:
        down = sum(1 for s in counted if s["change"]["abs_median"] < 0 and s["change"]["log_mean"] < 0)
        need("median |error| and log error down in most seasons", 2 * down > len(counted),
             f"{down} of {len(counted)} seasons with {SEASON_CUPS}+ cups (mean |error|, not judged: "
             + ", ".join(f"{s['season']} {s['change']['abs_mean']:+.2f}" for s in counted) + ")")
    for table, name in (("region", "region"), ("rank band", "rank band")):
        worse = {k: r["change"]["abs_mean"] for k, r in result["tables"][table].items()
                 if r["change"]["abs_mean"] is not None and r["change"]["abs_mean"] > HARM}
        need(f"no {name} worse by more than {HARM} point", not worse,
             ", ".join(f"{k} {v:+.2f}" for k, v in worse.items()) or "none")
    need("every forecast of the base priced", not only_base, f"{len(only_base)} missing from the variant")
    if not result["moved"] and not result["unpaired"]["base only"] and not result["unpaired"]["variant only"]:
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
    drawn = result["interval"]
    print(f"\n{100 * drawn['level']:.0f} % interval of the paired change, {drawn['draws']} draws of the cups"
          f" (seed {drawn['seed']}):")
    for name, what in (("signed_mean", "signed mean"), ("abs_mean", "mean |error|"),
                       ("abs_median", "median |error|"), ("log_mean", "mean log error")):
        found = drawn.get(name)
        change = result["overall"]["all"]["change"][name]
        print(f"  {what:<16}{fmt(change, '+.3f'):>9}   "
              + ("-" if not found else f"[{found[0]:+.3f}, {found[1]:+.3f}]"))
    print_changes("by half of the span", [(h["label"], h) for h in result["halves"] if h["pairs"]])
    if len([s for s in result["seasons"] if s["season"] is not None]) > 1:
        print_changes("by season", [(f"season {s['season']}" if s["season"] is not None else "no season", s)
                                    for s in result["seasons"]])
    for name, rows in result["tables"].items():
        print_changes(f"by {name}", list(rows.items()))
    print(f"\nthe gain rule:")
    for c in result["conditions"]:
        print(f"  {'ok  ' if c['holds'] else 'FAIL'}  {c['rule']:<62}{c['detail']}")
    print("\n" + verdict_line(result))


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


def strawman_group(pair: dict, regions: bool = True) -> tuple:
    group = (pair.get("source") or "?", rank_band(int(pair["rank"])))
    return group + ((pair.get("region") or "?",) if regions else ())


def strawman(payload: dict, times: dict[int, dict], regions: bool = True,
             prior: float = STRAWMAN_PRIOR) -> dict:
    """The run, each forecast corrected by the strawman (see the module's
    notes), as a payload `compare` takes against the run itself. `times` holds
    each cup's start and end, `cup_times` off the run's own database."""
    refuse_live({"the run": payload})
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
    corrected["all"] = coldbench.summary(out) if out else None
    corrected["at_cut"] = coldbench.summary([p for p in out if p["at_cut"]]) if out else None
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
    parser.add_argument("--draws", type=int, default=DRAWS, help=f"draws of the cups (default {DRAWS})")
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
            print(f"Strawman: {told['corrected']:,} of {len(variant['pairs']):,} forecasts have a group to learn"
                  f" from, {told['moved']:,} of them moved")
            if args.json:
                with open(args.json, "w", encoding="utf-8") as fh:
                    json.dump(variant, fh, ensure_ascii=False, indent=1)
                print(f"Wrote {args.json}")
            runs = (base, variant)
        else:
            runs = (load(args.runs[0]), load(args.runs[1]))
        result = compare(*runs, draws=args.draws, seed=args.seed, unchecked=unchecked,
                         since=args.since, until=args.until)
    except SnapshotMismatch as refused:
        print(f"Refused: {refused}")
        return 2
    except (OSError, ValueError, KeyError) as error:
        print(f"Cannot compare: {error}")
        return 2
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
