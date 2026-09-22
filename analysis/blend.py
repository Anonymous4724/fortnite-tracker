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

    python -m analysis.blend        rolling forecasts (a few minutes), then the scales

Writes analysis/blend.json; export_model.py carries it into model.json.
"""
from __future__ import annotations

import json
import math
import os
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
    with open(BLEND_PATH, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    print(f"\n{result['evenings']} evenings followed by the feed, {result['reading']['rows']} readings.")
    print(f"  readings: typical error {result['reading']['scale']:.3f} of the pace's width")
    for source, (scale, n, cups) in result["cold"].items():
        print(f"  {source:<26} typical error {scale:.3f} of its band ({n} forecasts, {cups} cups)"
              f" -> its width counts x{scale / result['reading']['scale']:.2f} in the blend")
    print(f"\nWrote {os.path.relpath(BLEND_PATH)} - the export carries it into model.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
