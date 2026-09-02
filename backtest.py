"""Measure how reliable the predictions are on your own finished competitions.

For each finished competition we pretend the session stopped at 15 %, 25 %,
50 %, 75 % and finally 90 %, ask for a prediction of the final threshold using
only the snapshots available at that point, and compare it against the
threshold the tournament actually ended on.

    python backtest.py
    python backtest.py --region EU --team-mode Solo
"""
from __future__ import annotations

import argparse
from datetime import datetime

import db
import predict

CUTS = [0.15, 0.25, 0.5, 0.75, 0.9]


def truncate(comp: dict, cut: float) -> dict:
    tl = predict.timeline(comp)
    start, total = tl["start"], tl["total_min"] * 60
    keep = [s for s in comp["snapshots"]
            if (datetime.fromisoformat(s["ts"]) - start).total_seconds() / total <= cut]
    clone = dict(comp)
    clone["snapshots"] = keep
    clone["finals"] = {}      # hide the real result from the model
    return clone


def run(region=None, team_mode=None, game_mode=None) -> None:
    db.init_db()
    with db.session() as conn:
        comps = []
        for row in db.list_competitions(conn, region, team_mode, game_mode):
            full = db.get_competition_full(conn, row["id"])
            if full and predict.is_complete(full) and len(full["snapshots"]) >= 4:
                comps.append(full)

    if not comps:
        print("No finished competition with at least 4 snapshots: nothing to test.")
        return

    print(f"{len(comps)} finished competition(s) tested\n")
    errors: dict[tuple[float, int], list[float]] = {}
    covered: dict[float, list[int]] = {c: [] for c in CUTS}

    for comp in comps:
        history = [c for c in comps if c["id"] != comp["id"]
                   and (c["region"], c["team_mode"], c["game_mode"])
                   == (comp["region"], comp["team_mode"], comp["game_mode"])]
        for cut in CUTS:
            partial = truncate(comp, cut)
            if not partial["snapshots"]:
                continue
            res = predict.predict_live(partial, history)
            if not res.get("ok"):
                continue
            for rank in comp["ranks"]:
                truth = predict.final_value(comp, rank)
                if truth is None:
                    continue
                pred = res["ranks"].get(rank)
                if not pred or not pred.get("ok") or truth <= 0:
                    continue
                errors.setdefault((cut, rank), []).append(abs(pred["value"] - truth) / truth * 100)
                covered[cut].append(1 if pred["low"] <= truth <= pred["high"] else 0)

    ranks = sorted({r for (_, r) in errors})
    header = " ".join(f"{'Top ' + str(r):>9}" for r in ranks)
    print(f"Average error of the prediction (%)\n{'elapsed':>8} | {header}")
    print("-" * (11 + 10 * len(ranks)))
    for cut in CUTS:
        cells = []
        for rank in ranks:
            vals = errors.get((cut, rank))
            cells.append(f"{sum(vals) / len(vals):8.1f} " if vals else "       — ")
        print(f"{int(cut * 100):>6} % | " + " ".join(cells))

    print("\nHow often the low-high band contains the truth")
    for cut in CUTS:
        vals = covered[cut]
        if vals:
            print(f"  at {int(cut * 100):>3} % of the session: "
                  f"{100 * sum(vals) / len(vals):.0f} % of the real thresholds "
                  f"land inside the band")


# --------------------------------------------------------------------------- #
# Learning curve: does it get better as the history grows?
# --------------------------------------------------------------------------- #
def learning_curve(region=None, team_mode=None, game_mode=None, stage=None) -> None:
    """Replay the history in order and measure the error against its size."""
    import calibration

    db.init_db()
    with db.session() as conn:
        comps = []
        for row in db.list_competitions(conn, region, team_mode, game_mode):
            if stage and row["stage"] != stage:
                continue
            full = db.get_competition_full(conn, row["id"])
            if full and predict.is_complete(full) and full["snapshots"]:
                comps.append(full)
    comps.sort(key=lambda c: c["start_time"])
    if len(comps) < 2:
        print("A learning curve needs at least 2 finished competitions.")
        return

    print(f"{len(comps)} finished competition(s), replayed in chronological order.\n")
    print(f"{'history':>12} | {'no snapshot':>12} | {'1 snapshot':>11} | "
          f"{'3 snapshots':>12} | {'5 snapshots':>12}")
    print("-" * 70)
    for k in range(1, len(comps)):
        target, history = comps[k], comps[:k]
        calib = calibration.calibrate(history, target["ranks"])
        cells = []
        for n in (0, 1, 3, 5):
            partial = dict(target)
            partial["snapshots"] = target["snapshots"][:n]
            partial["finals"] = {}
            res = predict.predict_live(partial, history, calib)
            errs = []
            for rank in target["ranks"]:
                truth = predict.final_value(target, rank)
                p = (res.get("ranks") or {}).get(rank)
                if p and p.get("ok") and truth:
                    errs.append(abs(p["value"] - truth) / truth * 100)
            cells.append(f"{sum(errs) / len(errs):.1f} %" if errs else "—")
        print(f"{k:>5} comp. | {cells[0]:>12} | {cells[1]:>11} | "
              f"{cells[2]:>12} | {cells[3]:>12}")
    print("\n'no snapshot' = the estimate made from the scoring table alone, "
          "before the tournament starts.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Backtest the predictions")
    p.add_argument("--region")
    p.add_argument("--team-mode", dest="team_mode")
    p.add_argument("--game-mode", dest="game_mode")
    p.add_argument("--stage", help="keep a single stage only (e.g. Open, Final)")
    p.add_argument("--learning-curve", dest="curve", action="store_true",
                   help="error against the size of the history")
    a = p.parse_args()
    if a.curve:
        learning_curve(a.region, a.team_mode, a.game_mode, a.stage)
    else:
        run(a.region, a.team_mode, a.game_mode)
