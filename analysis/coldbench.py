"""How far off was the week's list, before each cup started?

The list of the week (`calendar.js`) carries, for every cup the page can open
by itself, the forecast the page gives before the cup starts (`fc`): the cut
the cup pays out on and ranks 100 and 1,000 of an open queue, each with its
half-width. The predictor's repository keeps every version of that file and
of `model.json`, so both halves of the question are in its history: what was
listed, and what the cup then scored. Nothing else is read - no database, no
board - so anyone with a full clone can run it.

    python -m analysis.coldbench                     the predictor beside this folder
    python -m analysis.coldbench PATH                a clone of it, full history
                                                     (git fetch --unshallow)
    python -m analysis.coldbench --replay            the same windows priced again by
                                                     today's export_model, with the model
                                                     and the row each was listed with
    python -m analysis.coldbench --calibrate WHEN    the later-day move measured on the
                                                     windows over before WHEN (a day, or
                                                     2026-09-29T18:00Z), and the ones that
                                                     open from WHEN on priced with and
                                                     without it
    python -m analysis.coldbench --since DAY         windows from that day on (--until too)
    python -m analysis.coldbench --json out.json     every pair, for another tool

The forecast is the last version of `calendar.js` generated before the window
opened: `fc` is frozen once a cup starts, so that is what the list showed. The
replay prices that version's row again with `export_model.calendar_forecast` on
the `model.json` of the same commit - the page's own port, so a change to it
can be measured before it ships; the rows the list itself priced say whether
the replay is faithful.

The result is read off the first version of `model.json` whose category row
for the cup - its label, region, team size and game mode, the key
`export_model.category_row` looks up - holds an edition dated the window's
day (`latest`), among the versions committed once the window's board had
settled (its end plus SETTLE_MINUTES): an update that ran while the cup was
being played exported the board of that minute, which the next one replaced.
Two windows of one cup on one day cannot be told apart there and are left
out. Since 22 September `direct` is not the edition's own value:
it is the latest edition averaged with the ones before it played the same
way (`calibration.smoothed_run`, weight DIRECT_ALPHA on the newest). So the
value is taken back out of it, against the version before, whose cell was the
average of the same earlier run:

    value = exp((ln direct_after - (1 - ALPHA) ln direct_before) / ALPHA)

- unless the run broke at this edition (another entry bar, season or format)
and the cell is the edition read straight. Two witnesses say which: the row's
`level`, which is the latest edition's rank 20 read straight, and the spread
(`rel`) of a rank held by two editions only, which is then the one move
between them. Each has to agree with exactly one of the two readings, and all
the witnesses of the same edition before with the same one; a rank that none
of them speaks for is left out, as is one where more than one edition arrived
between the two versions. A run capped at DIRECT_RUN editions drops its oldest
one at the same time, which moves the value taken back by less than 0.05 %.
On the history of 30 September the 100 rank-20 readings and the 157 spreads
that spoke agree with the reading they chose to 0.007 point and 0.0001, and
the 90 results taken back out of an average all fall within 0.007 of a whole
point - which a threshold is. The replay gives back all 139 forecasts the list
carried from 22 September on.

Everything is a ratio to the result: the signed error is 100 (forecast /
result - 1), positive when the list said more than the cup took; the ranges
are the page's (`quality.bands` of the same model, in units of the
half-width), the inner one claiming half of the results and the outer nine in
ten.

A bias is a group of forecasts - one value of one of the causes below - that
leans one way in three cups out of four at least. Its cost is the absolute
error it accounts for: the sum over the group of |error| less the sum of
|error - its median|, what a correction by its median would take away. The
same cups can lean under two names - the second day of the FNCS Solo
qualifiers is also a field that shrank - so each bias says which costlier one
holds most of its forecasts.

What the list of 22 to 30 September said, 152 forecasts of 68 cups: 8.1 % off
in median, 4.2 % low in
median, 36 % of the results inside the inner range and 77 % inside the outer
one where they claim 50 and 90. The biases, costliest first: the Mobile
Reload Victory Cup of 26 September, whose field grew by 60 to 120 % from one
week to the next with nothing in its row to say so (-11 %, 7 cups); the cups
new to their region, priced off the family and replayed boards (-8.6 %, 22
cups), each event its own way; and the second day of the FNCS Solo qualifiers
(+4.1 %, 7 cups out of 7), priced off the first day, which more players had
played - the one with a rule behind it, now `export_model.later_day_shifts`.
Measured on the second days over before 29 September 16:00 UTC (Oceania,
Asia) and priced on the four after it, the move takes their error from 3.8 %
to 1.7 % on average and their lean from +3.9 % to +0.2 %; the ranks of the
top hundred, which the second day barely moves, gain nothing until more
second days have been measured.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone

# The app's modules, whichever way this file is run: `-m analysis.coldbench`
# puts them on the path through the package, a direct run does not.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CODE = os.path.join(_ROOT, "src") if os.path.exists(os.path.join(_ROOT, "src", "db.py")) else _ROOT
if _CODE not in sys.path:
    sys.path.insert(0, _CODE)

import calendar_snapshot  # noqa: E402
import calibration  # noqa: E402
import export_model  # noqa: E402

ALPHA = calibration.DIRECT_ALPHA
REFERENCE = str(calibration.REFERENCE_RANK)

# A group needs this many cups and forecasts before its lean is called a bias,
# and this share of its cups leaning its way: a lean that three cups in four
# do not share is a few cups far off, not a bias.
MIN_WINDOWS = 3
MIN_PAIRS = 5
CONSISTENT = 0.75
# The cuts a bias is looked for in: what the model reads or assumes. The cup's
# family and its mode are in the tables but not here - over the weeks the
# history spans, a family is an event or two, and its lean is that event's
# surprise (a cup twice as popular as its first edition) rather than a rule
# the model gets wrong.
CAUSES = ("rank band", "region", "entry bar", "field", "field against the edition read", "season",
          "closed-lobby final", "day of the round", "rung")
# The value of a cut that says "nothing special". A lean there is the other
# causes it holds, which the other cuts name; it is not a cause of its own.
PLAIN = {("day of the round", "one session"), ("closed-lobby final", "open queue"),
         ("season", "same season"), ("entry bar", "none"),
         ("field against the edition read", "within 10 %"),
         ("field against the edition read", "not counted")}
TOP = 8

RANK_BANDS = ((1, 10), (11, 100), (101, 1000), (1001, 10 ** 9))
FIELD_BANDS = ((1, 100), (101, 1000), (1001, 3000), (3001, calibration.FIELD_CAP - 1))

# A board goes on moving for a while after its window closes, as the games
# under way at the buzzer come in: the harvest reads none before this many
# minutes (harvest_osirion.SETTLE_MINUTES). A model committed before then
# cannot hold the cup's final standings, only the board as it stood.
SETTLE_MINUTES = 30


# --------------------------------------------------------------------------- #
# The predictor's history
# --------------------------------------------------------------------------- #
def git(repo: str, *args: str) -> str:
    done = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


class Blobs:
    """Many files out of one repository through one `git cat-file --batch`."""

    def __init__(self, repo: str):
        self.proc = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, blob: str) -> str:
        self.proc.stdin.write(blob.encode() + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split()
        if len(header) < 3 or header[1] != b"blob":
            raise RuntimeError(f"no blob {blob}")
        data = self.proc.stdout.read(int(header[2]))
        self.proc.stdout.read(1)
        return data.decode("utf-8")

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait()


def versions(repo: str, path: str) -> list[tuple[str, str]]:
    """(commit, blob of `path` there), oldest first, for every commit that changed it."""
    return [(commit, blob_at(repo, commit, path))
            for commit in git(repo, "log", "--reverse", "--format=%H", "--", path).split()]


def commit_times(repo: str, path: str) -> dict[str, str]:
    """commit -> when it was made, in UTC ("YYYY-MM-DDTHH:MM"), for every
    commit that changed `path`."""
    out = {}
    for line in git(repo, "log", "--format=%H %ct", "--", path).splitlines():
        commit, _, stamp = line.partition(" ")
        if stamp.strip().isdigit():
            out[commit] = datetime.fromtimestamp(int(stamp), timezone.utc).strftime("%Y-%m-%dT%H:%M")
    return out


def settled_by(row: dict) -> str:
    """When the window's board stopped moving, in UTC ("YYYY-MM-DDTHH:MM"):
    its end plus SETTLE_MINUTES, or "" for a row that states no readable end."""
    end = str(row.get("end") or "").replace("Z", "")[:16]
    try:
        closed = datetime.strptime(end, "%Y-%m-%dT%H:%M")
    except ValueError:
        return ""
    return (closed + timedelta(minutes=SETTLE_MINUTES)).strftime("%Y-%m-%dT%H:%M")


def blob_at(repo: str, commit: str, path: str) -> str:
    try:
        return git(repo, "rev-parse", f"{commit}:{path}").strip()
    except RuntimeError:
        return ""


def calendar_payload(text: str) -> dict | None:
    start, stop = text.find("{"), text.rfind("}")
    if start < 0 or stop < 0:
        return None
    try:
        return json.loads(text[start:stop + 1])
    except ValueError:
        return None


def key_of(row: dict) -> tuple:
    """The category row a calendar row reads: the key calendar_tournament looks up."""
    return (str(row.get("kind") or "") or str(row.get("name") or "").strip(), row.get("region"),
            str(row.get("team") or ""), str(row.get("mode") or ""))


def slim(model: dict, keys: set) -> dict:
    """What the results are read from, for the cups the list carried."""
    rows = {}
    for row in model.get("categories") or []:
        key = (row.get("category"), row.get("region"), row.get("team_mode") or "", row.get("game_mode") or "")
        if key in keys:
            rows[key] = {k: row.get(k) for k in ("latest", "level", "season", "direct")}
    return {"rows": rows, "bands": (model.get("quality") or {}).get("bands") or None,
            # Rows keyed on the format too, which the export's port reads:
            # the models of early September are not, and are not replayed.
            "keyed": any("team_mode" in r for r in (model.get("categories") or [])[:50])}


# --------------------------------------------------------------------------- #
# What each cup scored
# --------------------------------------------------------------------------- #
def cell_date(row: dict, cell: list) -> str:
    return str(cell[7]) if len(cell) > 7 and cell[7] else str(row.get("latest") or "")


def cell_season(row: dict, cell: list):
    return cell[6] if len(cell) > 6 and isinstance(cell[6], int) and cell[6] >= 0 else row.get("season")


def cell_field(cell: list) -> int:
    return int(cell[3]) if len(cell) > 3 and cell[3] else 0


def taken_back(after: float, before: float) -> float:
    return math.exp((math.log(after) - (1 - ALPHA) * math.log(before)) / ALPHA)


def edition_of(models: list[dict], key: tuple, day: str, settled: str = "") -> dict:
    """The versions an edition of the cup dated `day` is read between, and how.

    {"after": index, "later": [later indexes still ending on that edition],
     "before": index or None,
     "runs": {day of the edition before: "straight" | "averaged"},
     "checks": {witness: [(what the reading gives, what it has to give)]}}
    or {"skip": reason}.

    Whether the edition was averaged with the one before depends on that one -
    a run goes on only through editions played the same way - so it is decided
    per edition before: rank 20's and a deep rank's can be two different ones,
    when the last edition did not reach the deep rank.

    `settled` is when the window's board stopped moving (`settled_by`). A
    version committed before then (its "at") holds the board an update read
    while the cup ran - on 24 September the 18:00 update caught the Middle
    East's Reload Icon Cup an hour from its close, 213 points at rank 50 for
    a cup that finished on 520 - so the edition is read from the first
    version committed after it, against the version before the first to hold
    the day at all.
    """
    holding = [i for i, m in enumerate(models) if (m["rows"].get(key) or {}).get("latest") == day]
    if not holding:
        newest = max((str(r.get("latest") or "") for r in models[-1]["rows"].values()), default="") if models else ""
        return {"skip": "played after the newest model" if day > newest else "no edition of that day in any model"}
    before = next((i for i in range(holding[0] - 1, -1, -1) if key in models[i]["rows"]), None)
    if settled:
        holding = [i for i in holding if str(models[i].get("at") or "9999") >= settled]
        if not holding:
            return {"skip": "every model holding that day was made while the cup ran"}
    after = holding[0]
    ra = models[after]["rows"][key]
    rb = models[before]["rows"][key] if before is not None else None
    if rb and str(rb.get("latest") or "") >= day:
        return {"skip": "the version before already holds that day"}
    runs, checks = {}, {"rank 20": [], "spread": []}
    level = ra.get("level")

    def vote(previous, run, witness, got, want):
        if runs.setdefault(previous, run) != run:
            raise ValueError
        checks[witness].append((got, want))

    try:
        for rank, cell in (ra.get("direct") or {}).items():
            old = ((rb or {}).get("direct") or {}).get(rank)
            if not cell or not cell[0] or cell_date(ra, cell) != day or not old or not old[0] \
                    or int(cell[1]) != int(old[1]) + 1:
                continue
            averaged = taken_back(float(cell[0]), float(old[0]))
            previous = cell_date(rb, old)
            if rank == REFERENCE and level:
                # To the rounding of the file, and a run capped at DIRECT_RUN.
                straight = abs(cell[0] - level) <= 0.0101
                mean = abs(averaged - level) <= 0.02 + 0.0005 * level
                if straight != mean:
                    vote(previous, "straight" if straight else "averaged", "rank 20",
                         cell[0] if straight else averaged, level)
                elif not straight:
                    return {"skip": "neither reading gives back the edition's rank 20"}
            if int(old[1]) == 1 and cell[2] is not None and float(cell[2]) > calibration.SHAPE_FLOOR + 0.001:
                spread = float(cell[2])
                straight = abs(abs(cell[0] / old[0] - 1) - spread) <= 0.0005
                mean = abs(abs(averaged / old[0] - 1) - spread) <= 0.0005
                if straight != mean:
                    vote(previous, "straight" if straight else "averaged", "spread",
                         abs((cell[0] if straight else averaged) / old[0] - 1), spread)
    except ValueError:
        return {"skip": "the witnesses disagree"}
    return {"after": after, "later": holding[1:], "before": before, "runs": runs, "checks": checks}


def held(models: list[dict], edition: dict, key: tuple, day: str, rank: int) -> tuple:
    """(row, cell) of the first version that holds `rank` from the edition -
    a board's first harvest reaches its cuts, a deeper one comes later - or
    the first version's when none does."""
    first = None
    for index in [edition["after"]] + edition["later"]:
        row = models[index]["rows"][key]
        cell = (row.get("direct") or {}).get(str(rank))
        first = first or (row, cell)
        if cell and cell[0] and cell_date(row, cell) == day:
            return row, cell
    return first


def outcome(models: list[dict], edition: dict, key: tuple, day: str, rank: int) -> tuple:
    """(value, None) or (None, why): what the edition scored at `rank`."""
    ra, cell = held(models, edition, key, day, rank)
    if not cell or not cell[0]:
        return None, "rank not in the model"
    if cell_date(ra, cell) != day:
        return None, "rank not published by that edition"
    rb = models[edition["before"]]["rows"].get(key) if edition["before"] is not None else None
    before = ((rb or {}).get("direct") or {}).get(str(rank))
    if int(cell[1]) != (int(before[1]) if before else 0) + 1:
        return None, "more than one edition between the two versions"
    if not before:
        # The first edition to hold the rank: nothing to be averaged with.
        return float(cell[0]), None
    run = edition["runs"].get(cell_date(rb, before))
    if run is None:
        return None, "nothing says whether the edition was averaged"
    if run == "straight":
        return float(cell[0]), None
    return taken_back(float(cell[0]), float(before[0])), None


# --------------------------------------------------------------------------- #
# The cuts
# --------------------------------------------------------------------------- #
def band_label(value: int, bands) -> str:
    for lo, hi in bands:
        if lo <= value <= hi:
            return f"{lo:,}-{hi:,}" if hi < 10 ** 8 else f"{lo:,}+"
    return "?"


def day_of_round(row: dict) -> str:
    """"one session", "day 1 of several" or "day 2 or later": a round played
    over several days - the FNCS Solo qualifiers' first round, one window a
    day, its cut ranked on the total - as the window's id numbers it."""
    if export_model.later_day(row.get("window")):
        return "day 2 or later"
    return "day 1 of several" if export_model.DAY_OF_ROUND.search(str(row.get("window") or "")) else "one session"


def season_label(pair: dict) -> str:
    if pair["new_cup"]:
        return "new cup here"
    if pair["season"] is not None and pair["previous_season"] is not None \
            and int(pair["previous_season"]) < int(pair["season"]):
        return "first of its season"
    return "same season"


def field_label(pair: dict) -> str:
    field = pair["result_field"]
    if field <= 0:
        return f"{calibration.FIELD_CAP:,}+ (not counted)"
    return band_label(field, FIELD_BANDS)


def moved_label(pair: dict) -> str:
    now, then = pair["result_field"], pair["previous_field"]
    if now <= 0 or then <= 0:
        return "not counted"
    move = now / then - 1
    if move < -0.1:
        return "shrank > 10 %"
    if move > 0.1:
        return "grew > 10 %"
    return "within 10 %"


CUTS = OrderedDict([
    ("rank band", lambda p: band_label(p["rank"], RANK_BANDS)),
    ("region", lambda p: p["region"]),
    ("cup family", lambda p: p["family"]),
    ("mode", lambda p: f"{p['game_mode']} · {p['team_mode']}"),
    ("entry bar", lambda p: p["entry"] or "none"),
    ("field", field_label),
    ("field against the edition read", moved_label),
    ("season", season_label),
    ("closed-lobby final", lambda p: "closed lobby" if p["lobby"] else "open queue"),
    ("day of the round", lambda p: p["day"]),
    ("rung", lambda p: p["source"] or "?"),
])


# --------------------------------------------------------------------------- #
# The measure
# --------------------------------------------------------------------------- #
def error(pair: dict) -> float:
    return 100.0 * (pair["forecast"] / pair["result"] - 1)


def inside(pair: dict, key: str) -> bool | None:
    bands = pair.get("bands")
    if not bands or not bands.get(key):
        return None
    lo, hi = bands[key]
    value, rel = pair["forecast"], pair["rel"]
    return value * math.exp(lo * rel) <= pair["result"] <= value * math.exp(hi * rel)


def summary(pairs: list[dict]) -> dict:
    errors = [error(p) for p in pairs]
    near = [x for x in (inside(p, "50") for p in pairs) if x is not None]
    wide = [x for x in (inside(p, "90") for p in pairs) if x is not None]
    return {"pairs": len(pairs), "windows": len({p["window"] for p in pairs}),
            "signed_mean": statistics.mean(errors) if errors else None,
            "signed_median": statistics.median(errors) if errors else None,
            "abs_mean": statistics.mean(abs(e) for e in errors) if errors else None,
            "abs_median": statistics.median(abs(e) for e in errors) if errors else None,
            "in_50": sum(near) / len(near) if near else None,
            "in_90": sum(wide) / len(wide) if wide else None}


def cost(errors: list[float]) -> tuple[float, float]:
    """(median, the absolute error a correction by it would take away)."""
    middle = statistics.median(errors)
    return middle, sum(abs(e) for e in errors) - sum(abs(e - middle) for e in errors)


def biases(pairs: list[dict], top: int = TOP) -> list[dict]:
    """The groups that lean one way, costliest first, each with the costlier
    one that holds most of its forecasts (`within`)."""
    groups: dict = {}
    for p in pairs:
        for name in CAUSES:
            groups.setdefault((name, CUTS[name](p)), []).append(p)
    found = []
    for (name, value), members in groups.items():
        windows: dict = {}
        for p in members:
            windows.setdefault(p["window"], []).append(error(p))
        if (name, value) in PLAIN or len(members) < MIN_PAIRS or len(windows) < MIN_WINDOWS:
            continue
        middle, gain = cost([error(p) for p in members])
        leans = [statistics.median(v) for v in windows.values()]
        if gain <= 0 or sum(1 for x in leans if x * middle > 0) < CONSISTENT * len(leans):
            continue
        found.append({"cut": name, "value": value, "pairs": len(members), "windows": len(windows),
                      "lean": middle, "cost": gain, "members": {id(p) for p in members}})
    found.sort(key=lambda b: -b["cost"])
    found = found[:top]
    for n, b in enumerate(found):
        share, k = max(((len(b["members"] & a["members"]) / len(b["members"]), j + 1)
                        for j, a in enumerate(found[:n])), default=(0.0, None))
        b["within"] = k if share >= 0.5 else None
    for b in found:
        del b["members"]
    return found


def fmt(value, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def print_table(title: str, rows: list[tuple[str, dict]]) -> None:
    print(f"\n{title}")
    print(f"  {'':<34}{'pairs':>6}{'cups':>6}{'signed':>9}{'median':>9}{'|err|':>8}{'median':>9}"
          f"{'in 50%':>8}{'in 90%':>8}")
    for label, s in rows:
        print(f"  {str(label)[:33]:<34}{s['pairs']:>6}{s['windows']:>6}"
              f"{fmt(s['signed_mean'], '+.1f'):>9}{fmt(s['signed_median'], '+.1f'):>9}"
              f"{fmt(s['abs_mean'], '.1f'):>8}{fmt(s['abs_median'], '.1f'):>9}"
              f"{fmt(s['in_50'] and 100 * s['in_50'] if s['in_50'] is not None else None, '.0f'):>8}"
              f"{fmt(s['in_90'] and 100 * s['in_90'] if s['in_90'] is not None else None, '.0f'):>8}")


def report(pairs: list[dict], heading: str) -> list[dict]:
    print("\n" + "=" * 96)
    print(f"  {heading}")
    print("=" * 96)
    if not pairs:
        print("  nothing to measure")
        return []
    print("  error in % of the result, + when the list said more than the cup took;"
          " the ranges are the page's")
    print_table("all", [("all", summary(pairs))])
    for name, cut in CUTS.items():
        grouped: dict = {}
        for p in pairs:
            grouped.setdefault(cut(p), []).append(p)
        rows = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
        print_table(f"by {name}", [(label, summary(members)) for label, members in rows])
    found = biases(pairs)
    total = sum(abs(error(p)) for p in pairs)
    print(f"\nbiases, costliest first (a group of {MIN_WINDOWS}+ cups and {MIN_PAIRS}+ forecasts,"
          f" {100 * CONSISTENT:.0f} % of its cups leaning its way;\n  lean = its median error; cost ="
          f" the |error| a correction by it takes away, of {total:,.0f} in all;\n  within = the costlier"
          f" group that holds most of its forecasts)")
    print(f"  {'':<4}{'cut':<32}{'value':<30}{'pairs':>6}{'cups':>6}{'lean':>8}{'cost':>8}{'share':>7}"
          f"{'within':>8}")
    for n, b in enumerate(found, 1):
        print(f"  {n:<4}{b['cut'][:31]:<32}{str(b['value'])[:29]:<30}{b['pairs']:>6}{b['windows']:>6}"
              f"{b['lean']:>+8.1f}{b['cost']:>8.0f}{100 * b['cost'] / total:>6.0f}%"
              f"{('#' + str(b['within'])) if b['within'] else '':>8}")
    return found


# --------------------------------------------------------------------------- #
# Forecasts and results, paired
# --------------------------------------------------------------------------- #
class History:
    """The predictor's history, read once: every window with the last row the
    list carried before it opened, and the results the models took in after."""

    def __init__(self, repo: str):
        self.blobs = Blobs(repo)
        self.skipped: Counter = Counter()
        self.checks = {"rank 20": [], "spread": []}
        calendars = []
        for commit, blob in versions(repo, "calendar.js"):
            payload = calendar_payload(self.blobs.read(blob))
            if payload and payload.get("generated"):
                calendars.append({"commit": commit, "payload": payload,
                                  "model": blob_at(repo, commit, "model.json")})
        listed: dict = {}
        for version in calendars:
            generated = str(version["payload"]["generated"])
            for row in version["payload"].get("events") or []:
                if not row.get("event") or not row.get("window") or not row.get("begin"):
                    continue
                if generated < str(row["begin"]):
                    ident = (row["event"], row["window"])
                    if ident not in listed or generated >= str(listed[ident][0]["payload"]["generated"]):
                        listed[ident] = (version, row)
        keys = {key_of(row) for _, row in listed.values()}
        self.models, self.by_blob = [], {}
        made = commit_times(repo, "model.json")
        for commit, blob in versions(repo, "model.json"):
            if blob in self.by_blob:
                continue
            self.by_blob[blob] = len(self.models)
            self.models.append(dict(slim(json.loads(self.blobs.read(blob)), keys), commit=commit, blob=blob,
                                    at=made.get(commit, "")))
        same_day = Counter((key_of(row), str(row["begin"])[:10]) for _, row in listed.values())
        self.work = []
        for _, (version, row) in sorted(listed.items(), key=lambda kv: kv[1][1]["begin"]):
            key, day = key_of(row), str(row["begin"])[:10]
            if same_day[(key, day)] > 1:
                self.skipped["two windows of the cup that day"] += 1
                continue
            edition = edition_of(self.models, key, day, settled_by(row))
            if "skip" in edition:
                self.skipped[edition["skip"]] += 1
                continue
            for witness, found in edition["checks"].items():
                self.checks[witness] += found
            self.work.append((version, row, key, day, edition))

    def close(self) -> None:
        self.blobs.close()

    def pairs(self, version, row, key, day, edition, fc, skipped: Counter) -> list[dict]:
        """One pair per rank the forecast priced and the edition published."""
        out = []
        used = self.by_blob.get(version["model"])
        models = self.models
        rb = models[edition["before"]]["rows"].get(key) if edition["before"] is not None else None
        # What the forecast could read: the row of the model it was made with.
        seen = models[used]["rows"].get(key) if used is not None else None
        table = (seen or {}).get("direct") or {}
        for rank, value, rel, source in fc.get("ranks") or []:
            result, why = outcome(models, edition, key, day, int(rank))
            if result is None:
                skipped[why] += 1
                continue
            cell = held(models, edition, key, day, int(rank))[1]
            before = ((rb or {}).get("direct") or {}).get(str(int(rank)))
            # The edition read at this rank, or the latest one where the rank
            # was read between two others.
            read = table.get(str(int(rank))) or next(
                (c for c in table.values() if c and cell_date(seen, c) == str(seen.get("latest") or "")), None)
            out.append({
                "window": f"{row['event']}|{row['window']}", "begin": row["begin"], "day": day_of_round(row),
                "name": row.get("name"), "family": str(row.get("kind") or row.get("name") or "").split(" · ")[0],
                "region": row.get("region"), "team_mode": row.get("team"), "game_mode": row.get("mode"),
                "entry": str(row.get("entry") or ""), "lobby": bool(fc.get("lobby")),
                "rank": int(rank), "cut": int(fc.get("cut") or 0), "forecast": float(value),
                "rel": float(rel or 0), "source": source, "result": result,
                "bands": models[used]["bands"] if used is not None else None,
                "listed_field": int(fc.get("field") or 0), "result_field": cell_field(cell),
                "previous_field": cell_field(read) if read else 0,
                "season": export_model.season_of_event(row.get("event")),
                "previous_season": cell_season(seen, read) if read else None,
                "new_cup": not table,
                "taken_back": bool(before) and edition["runs"].get(cell_date(rb, before)) == "averaged",
                "calendar": version["commit"][:7], "model": (version["model"] or "")[:7]})
        return out

    def as_listed(self) -> tuple[list[dict], Counter]:
        out, skipped = [], Counter()
        for version, row, key, day, edition in self.work:
            if isinstance(row.get("fc"), dict):
                out += self.pairs(version, row, key, day, edition, row["fc"], skipped)
        return out, skipped

    def replayed(self, since: str = "", later: dict | None = None) -> tuple[list[dict], Counter]:
        """Every window from `since` on, priced again by today's export_model
        with the model and the row it was listed with - and `later`, a
        later-day table, in the models that carry none."""
        by_model: dict = {}
        for item in self.work:
            if str(item[1]["begin"]) >= since:
                by_model.setdefault(item[0]["model"], []).append(item)
        out, skipped = [], Counter()
        for blob, items in by_model.items():
            if not blob or blob not in self.by_blob or not self.models[self.by_blob[blob]]["keyed"]:
                skipped["a model without the format in its rows"] += len(items)
                continue
            model = json.loads(self.blobs.read(blob))
            if later and not model.get("later_day"):
                model["later_day"] = later
            # The export's lookups are cached by the identity of the lists
            # they index; a model read after another may reuse an identity.
            export_model._INDEXES.clear()
            export_model._KIN.clear()
            for version, row, key, day, edition in items:
                try:
                    fc = export_model.calendar_forecast(model, dict(row), version["payload"].get("scorings") or [])
                except (KeyError, TypeError, ValueError) as exc:
                    skipped[f"{type(exc).__name__} on an older model"] += 1
                    continue
                if not fc:
                    skipped["no forecast"] += 1
                    continue
                found = self.pairs(version, row, key, day, edition, fc, skipped)
                listed = row.get("fc") if isinstance(row.get("fc"), dict) else None
                for p in found:
                    p["as_listed"] = None if listed is None else listed == fc
                out += found
            del model
        return out, skipped

    def later_day(self, until: str) -> dict | None:
        """The later-day move export_model.later_day_shifts measures, from the
        editions this history holds of the windows over before `until`: what
        a model exported then could have carried. The windows priced against
        it are the ones that open from `until` on, so none is on both sides."""
        editions = []
        for version, row, key, day, edition in self.work:
            if str(row.get("end") or row["begin"]) > until:
                continue
            finals = {}
            ranks = {int(r) for i in [edition["after"]] + edition["later"]
                     for r in (self.models[i]["rows"][key].get("direct") or {})}
            for rank in sorted(ranks):
                value, _ = outcome(self.models, edition, key, day, rank)
                if value:
                    finals[int(rank)] = value
            editions.append({"event_id": row["event"], "window_id": row["window"],
                             "ranks": sorted(finals), "finals": finals})
        return export_model.later_day_shifts(editions)


def predictor_path(given: str | None) -> str | None:
    return given or calendar_snapshot.predictor_dir()


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("predictor", nargs="?", help="a clone of the predictor's repository, full history")
    parser.add_argument("--replay", action="store_true",
                        help="also price every window again with today's export_model")
    parser.add_argument("--calibrate", default="", metavar="WHEN",
                        help="measure the later-day move on the windows over before WHEN, and price the "
                             "ones that open from WHEN on with and without it")
    parser.add_argument("--since", default="", help="windows opening on or after this day")
    parser.add_argument("--until", default="", help="windows opening before this day")
    parser.add_argument("--json", default="", help="write every pair to this file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = argument_parser().parse_args(argv)
    repo = predictor_path(args.predictor)
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        print("No clone of the predictor's repository beside this folder: pass its path. Nothing measured.")
        return 0
    if git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        print(f"{repo} is a shallow clone: only the history it holds is read "
              f"(git -C {repo} fetch --unshallow reads it all).")
    print(f"Reading the history of calendar.js and model.json in {repo}...", flush=True)
    history = History(repo)

    def kept(pairs):
        return [p for p in pairs if (not args.since or p["begin"][:len(args.since)] >= args.since)
                and (not args.until or p["begin"][:len(args.until)] < args.until)]

    try:
        as_listed, listed_out = history.as_listed()
        replayed, replay_out = history.replayed() if args.replay or args.calibrate else ([], Counter())
        table, calibrated = None, []
        if args.calibrate:
            table = history.later_day(args.calibrate)
            calibrated = history.replayed(args.calibrate, table)[0] if table else []
    finally:
        history.close()
    as_listed, replayed, calibrated = kept(as_listed), kept(replayed), kept(calibrated)

    print("\nWhat the witnesses said, for the editions averaged with the one before or not:")
    for witness, found in history.checks.items():
        if found:
            print(f"  {witness:<8} {len(found):>4} readings agree with it, worst gap "
                  f"{max(abs(a - b) for a, b in found):.4f}")
    back = [p["result"] for p in as_listed + replayed if p["taken_back"]]
    if back:
        print(f"  {len(back)} results taken back out of an average; the furthest from a whole "
              f"point is {max(abs(v - round(v)) for v in back):.3f} away")
    print("\nWindows left out:")
    for why, n in history.skipped.most_common():
        print(f"  {n:>5}  {why}")
    for title, out in (("Forecasts left out, as listed:", listed_out), ("Forecasts left out, replayed:", replay_out)):
        if out:
            print(f"\n{title}")
            for why, n in out.most_common():
                print(f"  {n:>5}  {why}")
    span = f" ({args.since or '...'} to {args.until or '...'})" if args.since or args.until else ""
    report(as_listed, "as listed: the forecast calendar.js carried before each window opened" + span)
    if replayed:
        agree = [p["as_listed"] for p in replayed if p["as_listed"] is not None]
        print(f"\nReplayed by today's export_model: {len(replayed)} forecasts; "
              f"{sum(agree)} of the {len(agree)} the list itself priced come out the same.")
        report(replayed, "replayed: today's code, the model and the row each window was listed with" + span)
    if args.calibrate:
        print("\n" + "=" * 96)
        if not table:
            print(f"  No round played over several days was harvested day by day before {args.calibrate}: "
                  f"the later-day move has nothing to be measured on, and moves nothing.")
        else:
            print(f"  The later-day move measured before {args.calibrate}, on {table['days']} later day(s):")
            for band in ("100", "500", "0"):
                if band not in table["bands"]:
                    continue
                shift, spread, n = table["bands"][band]
                print(f"    ranks {'1-100' if band == '100' else ('101-500' if band == '500' else '501+'):<8}"
                      f" {100 * (math.exp(shift) - 1):+.1f} %  spread {100 * spread:.1f} %  ({n} readings)")
            before = [p for p in replayed if p["begin"] >= args.calibrate]
            then = {(p["window"], p["rank"]): p["forecast"] for p in before}
            moved = [p for p in calibrated if then.get((p["window"], p["rank"])) != p["forecast"]]
            print(f"  From {args.calibrate} on, {len(moved)} of {len(calibrated)} forecasts move, "
                  f"{sum(1 for p in moved if p['day'] == 'day 2 or later')} of them on later days.")
            report(before, f"replayed from {args.calibrate} on, as the models stood")
            report(calibrated, f"replayed from {args.calibrate} on, with the later-day move measured before it")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"as_listed": as_listed, "replayed": replayed, "calibrated": calibrated,
                       "later_day": table}, fh, ensure_ascii=False, indent=1)
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
