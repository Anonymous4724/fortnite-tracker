"""How much a cup's own readings should weigh against the forecast made before
it, measured on the evenings the live feed followed.

The page blends the two by precision: the forecast from history carries the
width of its band, the readings the width of the pace curve at the point of
the session they were taken, and each weighs the inverse of its width
squared. The two widths are not measured the same way. A band says how far
next week can land from last week in a bad week - the 80th percentile of the
moves - and the pace's width how far a board can be from its usual share of
the final at a given minute. Compared as they stand, the readings of a cup
with a steady history took half the answer by mid-session and dragged it up
and down with every early reading: on the evenings of 14 to 20 September,
between a third and two thirds of the session, the forecast of a cup with a
previous edition was 2.8 % off in median where the forecast made before the
cup, left alone, was 2.35 % off - and it travelled 17 % over its evening.

So each side's width is put in the units of its own typical error, measured
on the same evenings: for the forecast from history, the median of
|log(forecast / final)| over its band, by the rung it came from; for the
readings, the median of |log(extrapolation / final)| over the pace's width,
the extrapolation being the reading over the share of the final expected of
it at that minute. The page scales the history's width by the ratio of the
two before blending (`blend` in the model); the range drawn around the answer
keeps the widths as they stood, since the multipliers that turn a width into
a range were measured on those.

Replayed through the page on the evenings of 14 to 20 September, with the
model and the scales as of the 13th (34,892 answers, 408 forecast paths of
cups with a previous edition): the median error at a third of the session
went from 4.0 % to 3.6 %, at half from 3.4 % to 3.1 %, and over the whole
evening from 2.21 % to 2.13 %; the 90th percentile moved from 9.8 % to 9.9 %,
half a point worse in the last fifth of the session, where a forecast from
history that was badly off now holds on a little longer. The ranges held
what they claimed as before (46 % in the inner one, 88 % in the outer). The
forecast of a cup with a previous edition travelled 12 % over its evening in
median instead of 17 %, and its worst moment was 4.2 % off instead of 5.2 %.

The scales move with the evenings: measured again on the database of 22
September, which holds the 95 evenings since and a more complete harvest of
the ones before, a cup with a previous edition typically misses by 0.93 of
what the readings do rather than 0.67, and the page leans on its history
correspondingly less than it did in the test above. Hence the refresh
measures them again every three days, with the pace tables.

Tried and left out: trusting the readings more when they disagree with the
history by far more than both widths allow - the reading of a stale history.
On both weeks it made the tail worse: a large disagreement early in a
session is more often the readings' noise than the history's mistake.

Late in a session the scales still leave the history too much of the answer:
on the feed's evenings of 5 to 29 September the forecast of a cup that ended
more than a tenth away from its forecast before the cup had covered, in median, only
0.55 of the way there at four fifths of the session, and 0.65 at the close.
So from a share of SHARE_FROM of the session on, and past the close, the
history's weight is multiplied by k, one k per tranche of the session (the
share the readings stand at, or the minutes past the close): the k of
SHARE_GRID with the least mean |log(answer / final)| over the blended answers
of open queues on the cups that had ended, each answer as the page gave it,
reading by reading (the live bench's replay, analysis/bench_live.py, each cup
from the model of the last update before it). A tranche fewer than
SHARE_MIN_CUPS cups have reached keeps k = 1, and so does the blend below
SHARE_FROM. Learned that way cutoff by cutoff on the evenings of 4 September
to 11 October, k settled at 0.1 from 0.7 of the session to the close and at 0
to 0.05 past it. Replayed on the evenings of 1 to 11 October with the table
of each cutoff (1,401 forecasts at each point), the mean |error| went from
6.51 % to 5.56 % at four fifths of the session, from 4.36 % to 3.82 % at the
close and from 3.97 % to 3.46 % ten minutes past it; nothing moved before four
fifths. `by_share` in the model; the page reads a missing tranche as k = 1.
When the cups cannot be replayed or learned from, the table of the last
blend.json is kept as it was, and the run says so: a table lost is never k = 1.

    python -m analysis.blend        rolling forecasts (a few minutes), the scales, then the
                                    feed's cups replayed for the history's weight (minutes)

Writes analysis/blend.json; export_model.py carries it into model.json.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone

import db
from analysis import data, live, validate

BLEND_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blend.json")
# Readings taken before this share of the session, or after the close, are
# not what the scale is for: the first minutes hold a game or two and the
# page gives them no weight worth measuring; past the close the width comes
# from the feed's own tail, measured apart.
FROM_SHARE, TO_SHARE = 0.2, 1.0
# A rung needs this many cups on the evenings followed before its scale is
# believed, and this many forecasts among them; the page leaves an unmeasured
# rung's width as it is. Cups rather than rows: the ranks of one evening miss
# together, and on the database of 22 September the "thin sample" scoring rung
# had 82 forecasts from 8 cups - a scale of 0.5 that was eight evenings' luck.
MIN_CUPS = 20
MIN_ROWS = 30
# The history's weight late in a session (see above): from this share of the
# session on, and past the close, by tranche; k is one of SHARE_GRID, and a
# tranche needs SHARE_MIN_CUPS cups before its k is believed.
SHARE_FROM = 0.7
SHARE_TRANCHES = ("0.7-0.9", "0.9-1", "after 0-10", "after 10+")
SHARE_GRID = (0.0, 0.01, 0.02, 0.03, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0)
SHARE_MIN_CUPS = 30


def share_tranche(share, after):
    """The tranche of the session a live answer stands at (shareTranche on the
    page): past the close by its minutes, else by the share of the session the
    readings stand at; None below SHARE_FROM, where the blend is left as it is."""
    if after and after > 0:
        return "after 0-10" if after <= 10 else "after 10+"
    if share is None or share < SHARE_FROM:
        return None
    return "0.7-0.9" if share < 0.9 else "0.9-1"


def share_value(row: dict, k: float) -> float:
    """A blended live answer with the history's weight multiplied by k: the
    page's blend, (readings * wl + history * k * wc) / (wl + k * wc), written
    with w = wl / (wl + wc), and never under the board's floor."""
    w, fr, cold = row["readings_weight"], row["from_readings"], row["cold"]
    return max((fr * w + cold * (1 - w) * k) / (w + (1 - w) * k), row["floor"] or 0)


def learn_by_share(rows: list, ends: dict, cutoff: str, cups: dict | None = None) -> dict:
    """{tranche: k}, from the live bench's answers `rows` (each with its cup's
    `id`, `lobby`, `tranche`, `readings_weight`, `from_readings`, `cold`,
    `floor` and `result`): per tranche, over the blended answers of open
    queues on the cups that ended before `cutoff` (`ends`: cup id -> its end,
    "YYYY-MM-DD HH:MM:SS" UTC), the k of SHARE_GRID with the least mean
    |log(answer / final)|, a tie going to the k nearest 1. A tranche fewer than
    SHARE_MIN_CUPS cups have reached is left out: k = 1 there. `cups`, when
    given, gets the number of cups behind each tranche."""
    by_tranche: dict = {}
    for r in rows:
        if r.get("readings_weight") is None or r.get("from_readings") is None or r.get("lobby") \
                or r.get("tranche") not in SHARE_TRANCHES or not (ends.get(r["id"]) or "9999") < cutoff:
            continue
        by_tranche.setdefault(r["tranche"], []).append(r)
    found = {}
    for tranche in SHARE_TRANCHES:
        got = by_tranche.get(tranche) or []
        n = len({r["id"] for r in got})
        if cups is not None:
            cups[tranche] = n
        if n < SHARE_MIN_CUPS:
            continue
        found[tranche] = min(SHARE_GRID, key=lambda k: (
            sum(abs(math.log(share_value(r, k) / r["result"])) for r in got) / len(got), abs(math.log(k + 1e-9))))
    return found


def share_rows(path: str, until: str) -> tuple[list, dict]:
    """(the live answers, {cup id: its end}) of the cups the feed followed,
    each replayed by the live bench from the model of the last update before
    it, as the page answered them reading by reading. The models are built
    afresh, a few seconds each."""
    from analysis import bench, bench_live
    import export_model
    conn = bench.open_read_only(path, live=bench.same_file(path, str(db.DB_PATH)))
    conn.row_factory = sqlite3.Row
    try:
        comps = export_model.load_competitions(conn)
        found = bench_live.measure(conn, "", until, bench.load_catalogue(bench.CATALOGUE), None,
                                   bench.model_key(path), progress=False, learn=False)
    finally:
        conn.close()
    return found["rows"], {c["id"]: str(c.get("end_time") or "") for c in comps}


def last_blend(path: str = BLEND_PATH) -> dict:
    """The blend.json a run is about to replace, {} when there is none."""
    try:
        with open(path, encoding="utf-8") as fh:
            found = json.load(fh)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def add_by_share(result: dict, last: dict, path: str, now: str) -> bool:
    """Puts the history's weight by share (`by_share`, and `by_share_cups`)
    into `result`: learned at `now` on the feed's cups replayed from the
    database at `path`; when the replay or the learning fails, the table of
    the last blend.json (`last`) as it was. False when the table was not
    learned."""
    try:
        rows, ends = share_rows(path, "9999-12-31")
        cups: dict = {}
        table = learn_by_share(rows, ends, now, cups)
    except Exception as exc:  # the scales stand without it
        kept = [key for key in ("by_share", "by_share_cups") if key in last]
        for key in kept:
            result[key] = last[key]
        print(f"  the history's weight could not be learned ({type(exc).__name__}: {exc}): "
              + ("the last table is kept" if "by_share" in kept else "there is no last table: k = 1"))
        return False
    result["by_share"], result["by_share_cups"] = table, cups
    return True


def pooled_from(pace: dict) -> dict:
    """The pooled tables `live.expected_share` reads, from pace.json."""
    return {"curve": (pace.get("curve") or {}).get("open_by_time") or {},
            "dispersion": (pace.get("dispersion") or {}).get("open_by_time") or {},
            "tail": (pace.get("tail") or {}).get("open_by_time") or {},
            "tail_by_rank": (pace.get("tail") or {}).get("by_rank") or []}


def measure(cv, evenings: list, pace: dict) -> dict:
    """The two scales, from rolling forecasts `cv` and the feed's evenings."""
    pooled = pooled_from(pace)
    families, categories = pace.get("families") or [], pace.get("categories") or []
    depth = pace.get("depth") or []
    spread = pooled["dispersion"]
    cold = {}
    for row in cv.itertuples():
        if row.value and row.value > 0 and row.high and row.high > row.value:
            cold[(int(row.competition_id), int(row.rank))] = (float(row.value), (float(row.high) - float(row.value)) / float(row.value), str(row.anchor))
    reading_rows, cold_rows, used = [], {}, set()
    for ev in evenings:
        comp = ev["comp"]
        for share, after, points in ev["readings"]:
            if after > 0 or not (FROM_SHARE <= share < TO_SHARE):
                continue
            rel_l = live.interp(spread, share, below_linear=False)
            if not rel_l or rel_l <= 0:
                continue
            for rank, p in points.items():
                final = ev["finals"].get(rank)
                forecast = cold.get((int(comp["id"]), int(rank)))
                if not final or not forecast or rank == 1:
                    continue
                expected = live.expected_share(comp, rank, share, after, pooled, families, categories, depth)
                guess = max(p / expected, p)
                reading_rows.append(abs(math.log(guess / final)) / rel_l)
                value, rel_c, source = forecast
                if rel_c > 0:
                    cold_rows[(int(comp["id"]), int(rank))] = (abs(math.log(value / final)) / rel_c, source)
                used.add(int(comp["id"]))
    by_source: dict = {}
    cups: dict = {}
    for (comp_id, _), (ratio, source) in cold_rows.items():
        by_source.setdefault(source, []).append(ratio)
        cups.setdefault(source, set()).add(comp_id)
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "evenings": len(used),
        "note": "typical error over the width each side carries, on the evenings the feed "
                "followed: the page scales the history's width by cold / reading before blending",
        "reading": {"scale": round(statistics.median(reading_rows), 4), "rows": len(reading_rows)}
        if reading_rows else None,
        "cold": {source: [round(statistics.median(v), 4), len(v), len(cups[source])]
                 for source, v in sorted(by_source.items())
                 if len(v) >= MIN_ROWS and len(cups[source]) >= MIN_CUPS},
    }


def main() -> int:
    comps = data.load()
    print("Forecasting the newest tournaments from what came before each (a few minutes)...", flush=True)
    cv = validate.cross_validate(comps, rolling=True, progress=False)
    with db.session() as conn:
        evenings = live.tracked_evenings(conn)
    try:
        with open(live.PACE_PATH, encoding="utf-8") as fh:
            pace = json.load(fh)
    except (OSError, ValueError):
        print("No analysis/pace.json - run `python -m analysis.live` first.", file=sys.stderr)
        return 1
    result = measure(cv, evenings, pace)
    if not result["reading"] or not result["cold"]:
        print("Too few evenings followed to measure the scales; nothing written.")
        return 1
    print("Replaying the cups the feed followed, for the history's weight late in a session (minutes)...",
          flush=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    learned = add_by_share(result, last_blend(), str(db.DB_PATH), now)
    # Written aside and moved in place: a run stopped midway leaves the last
    # file whole.
    with open(BLEND_PATH + ".part", "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    os.replace(BLEND_PATH + ".part", BLEND_PATH)
    print(f"\n{result['evenings']} evenings followed by the feed, {result['reading']['rows']} readings.")
    print(f"  readings: typical error {result['reading']['scale']:.3f} of the pace's width")
    for source, (scale, n, cups) in result["cold"].items():
        print(f"  {source:<26} typical error {scale:.3f} of its band ({n} forecasts, {cups} cups)"
              f" -> its width counts x{scale / result['reading']['scale']:.2f} in the blend")
    for tranche in SHARE_TRANCHES if "by_share" in result else ():
        k = result["by_share"].get(tranche)
        print(f"  history's weight, {tranche:<11} x{1 if k is None else k:g}"
              f"   ({(result.get('by_share_cups') or {}).get(tranche, 0)} cups"
              f"{'' if k is not None else ', too few: 1'}{'' if learned else ', kept from the last run'})")
    print(f"\nWrote {os.path.relpath(BLEND_PATH)} - the export carries it into model.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
