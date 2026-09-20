"""Is a cup nobody has seen better priced by replaying recent boards under its
table than by the scoring rung? Out of sample, on the cups that were new.

    python -m analysis.rescore                  # first-of-their-name cups since SINCE
    python -m analysis.rescore --since 2026-08-01

The targets are the open-queue cups the rolling validation had to price off
the scoring table or off the family (`validate.cross_validate`, so each was
forecast from what had finished before its day). For each, `rescore.reading`
replays the boards of the same region, format and platform played before it
under its own table, and the replay is compared with the truth at every rank
the table covers - and past its depth continued along the ladder, through
`calibration.replay_reading` with the tables of the pool before `--since`.
Writes nothing the model carries; the numbers go in the note.
"""
from __future__ import annotations

import argparse
import collections
import sys

import numpy as np
import pandas as pd

import calibration
import rescore
from analysis import data, validate

SINCE = "2026-08-15"
BANDS = [(1, 5), (6, 25), (26, 100), (101, 250), (251, 1000), (1001, 10 ** 6)]


def band_of(rank: int) -> str:
    for lo, hi in BANDS:
        if lo <= rank <= hi:
            return f"{lo}-{hi}" if hi < 10 ** 6 else f"{lo}+"
    return "?"


def run(since: str = SINCE, cv: pd.DataFrame | None = None) -> pd.DataFrame:
    comps = data.load()
    if cv is None:
        print("Replaying the rolling validation (a few minutes)...", flush=True)
        cv = validate.cross_validate(comps, rolling=True, progress=False)
    model_at = {(int(r.competition_id), int(r.rank)): (r.value, r.anchor) for r in cv.itertuples()}
    pool = [c for c in comps if str(c.get("start_time") or "")[:10] < since]
    print(f"Tables of the {len(pool)} tournaments before {since} for the ladder past the boards' depth...", flush=True)
    calib = calibration.calibrate([], [calibration.REFERENCE_RANK], broad=calibration.broad_stats(pool))
    cold = collections.Counter(anchor for (_, rank), (_, anchor) in model_at.items() if rank == 20)
    print("  anchors the validation used at rank 20:", dict(cold))
    targets = [c for c in comps if str(c.get("start_time") or "")[:10] >= since
               and c.get("source") == "osirion" and not (c.get("stage") or "").strip()
               and not rescore.ODD.search(c.get("family") or c.get("name") or "")
               and str(model_at.get((c["id"], 20), (None, ""))[1] or "").split(" ")[0] in ("family", "scoring")]
    print(f"{len(targets)} open-queue cups since {since} the validation priced off the family or the scoring table")
    rows = []
    with data.connect() as conn:
        for comp in targets:
            scoring = comp.get("scoring") if isinstance(comp.get("scoring"), dict) else None
            if not scoring or not scoring.get("placement"):
                continue
            got = rescore.reading(conn, comp["region"], comp["team_mode"], comp["game_mode"],
                                  rescore.platform_of(comp.get("family") or comp["name"]), scoring,
                                  int(comp.get("max_games") or 0), str(comp["start_time"]), exclude_id=comp["id"])
            if not got:
                continue
            table = {"ranks": [[r, v] for r, v in got["ranks"].items()], "donors": got["donors"], "deep": got["deep"],
                     "rel": rescore.REL, "sig": calibration.table_signature(scoring, comp.get("max_games"))}
            replayed = dict(comp, cold=table)
            finals = {int(r): float(v) for r, v in (comp.get("finals") or {}).items() if float(v) > 0}
            for rank, truth in sorted(finals.items()):
                pred = calibration.prior_prediction(replayed, calib, rank)
                if not pred or not pred.get("ok") or "re-scored" not in str(pred.get("source")):
                    continue
                model = model_at.get((comp["id"], rank))
                rows.append({"competition_id": comp["id"], "name": comp["name"], "region": comp["region"],
                             "mode": comp["game_mode"], "platform": rescore.platform_of(comp.get("family") or comp["name"]),
                             "day": str(comp["start_time"])[:10], "rank": rank, "band": band_of(rank), "truth": truth,
                             "replay": pred["value"], "covered": rank <= got["deep"], "donors": got["donors"],
                             "model": model[0] if model else np.nan, "anchor": model[1] if model else None})
    return pd.DataFrame(rows)


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    def part(g):
        er = 100 * (g["replay"] / g["truth"] - 1)
        em = 100 * (g["model"] / g["truth"] - 1)
        return pd.Series({"n": len(g), "cups": g["competition_id"].nunique(),
                          "replay |e|": er.abs().median(), "replay bias": er.median(),
                          "replay p90": er.abs().quantile(0.9),
                          "model |e|": em.abs().median(), "model bias": em.median(),
                          "model p90": em.abs().quantile(0.9)})
    order = [f"{lo}-{hi}" if hi < 10 ** 6 else f"{lo}+" for lo, hi in BANDS]
    out = df.groupby("band").apply(part)
    return out.reindex([b for b in order if b in out.index]).round(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default=SINCE, help=f"first day of the targets (default {SINCE})")
    args = parser.parse_args()
    df = run(args.since)
    if df.empty:
        print("Nothing to measure: no cold cup with donor boards on disk.")
        return 1
    pd.set_option("display.width", 200)
    print(f"\n{df['competition_id'].nunique()} cups, {len(df)} thresholds. The replay against the "
          f"validation's own cold forecast, by rank band:")
    print(summarise(df).to_string())
    print("\nAt the ranks the boards cover (the replay read straight):")
    print(summarise(df[df["covered"]]).to_string())
    print("\nPast the boards' depth (continued along the ladder):")
    deep = df[~df["covered"]]
    if len(deep):
        print(summarise(deep).to_string())
    print("\nBy game mode and platform, ranks 1-100:")
    top = df[df["rank"] <= 100]
    print(top.groupby(["mode", "platform"]).apply(lambda g: pd.Series({
        "n": len(g), "cups": g["competition_id"].nunique(),
        "replay |e|": (100 * (g["replay"] / g["truth"] - 1)).abs().median(),
        "replay bias": (100 * (g["replay"] / g["truth"] - 1)).median(),
        "model |e|": (100 * (g["model"] / g["truth"] - 1)).abs().median(),
        "model bias": (100 * (g["model"] / g["truth"] - 1)).median()})).round(1).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
