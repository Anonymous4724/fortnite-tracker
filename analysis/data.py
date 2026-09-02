"""Load the tracker database into tidy frames. The only module here that runs SQL.

Everything downstream works on DataFrames, so a change to the schema costs one
edit rather than five. The connection is opened read-only: an analysis script
that silently rewrites the owner's tournament history would be worse than no
analysis at all, and `calibration.load_or_compute` does write a cache when you
let it.

    python -m analysis.data
"""
from __future__ import annotations

import os
import sqlite3

import pandas as pd

import calibration
import db

DB_PATH = os.environ.get("FNT_DB", db.DB_PATH)

# Taken from the app rather than re-typed: if the anchor rank ever moves, every
# frame here moves with it instead of quietly disagreeing.
REFERENCE_RANK = calibration.REFERENCE_RANK


def connect(path: str | None = None) -> sqlite3.Connection:
    """Read-only connection to the tracker database."""
    uri = f"file:{path or DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load(path: str | None = None) -> list[dict]:
    """Every competition with its snapshots and hand-entered final thresholds.

    Same shape the app itself works on, so anything measured here is measured on
    exactly the objects `predict` and `calibration` see.
    """
    with connect(path) as conn:
        return db.all_full(conn)


# --------------------------------------------------------------------------- #
# Tidy frames
# --------------------------------------------------------------------------- #
def competitions(comps: list[dict] | None = None) -> pd.DataFrame:
    """One row per tournament."""
    comps = comps if comps is not None else load()
    rows = []
    for c in comps:
        finals = c.get("finals") or {}
        rows.append({
            "competition_id": c["id"],
            "name": c["name"],
            "category": c["kind"],
            "family": c.get("family") or "",
            "region": c["region"],
            "team_mode": c["team_mode"],
            "game_mode": c["game_mode"],
            "stage": c.get("stage") or "",
            "date": pd.Timestamp(c["start_time"]),
            "field_size": c.get("field_size"),
            "max_games": c.get("max_games"),
            "scoring_known": bool(c.get("scoring_known")),
            "source": c.get("source") or "",
            "n_finals": len(finals),
            "n_snapshots": len(c.get("snapshots") or []),
        })
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def thresholds(comps: list[dict] | None = None) -> pd.DataFrame:
    """One row per (competition, rank, final threshold) — the analysis unit.

    `q` is the rank as a share of the field, which is what the model is a
    function of; `q_ref` is the same thing at the reference rank. Both are NaN
    without a field size, and a rank with no field size means nothing at all, so
    callers filter on it rather than getting a quiet zero.
    """
    comps = comps if comps is not None else load()
    meta = competitions(comps).set_index("competition_id")
    rows = []
    for c in comps:
        for rank, value in (c.get("finals") or {}).items():
            rows.append({"competition_id": c["id"], "rank": int(rank),
                         "threshold": float(value)})
    df = pd.DataFrame(rows)
    df = df.join(meta, on="competition_id")
    df["q"] = df["rank"] / df["field_size"]
    df["q_ref"] = REFERENCE_RANK / df["field_size"]
    return df.sort_values(["date", "competition_id", "rank"]).reset_index(drop=True)


def snapshots(comps: list[dict] | None = None) -> pd.DataFrame:
    """One row per (competition, reading, rank).

    `progress` is fraction of the *effective* session elapsed — official end plus
    one game plus the tracker lag — because that is the clock the models
    extrapolate against, not the closing time printed on the page.
    """
    import predict

    comps = comps if comps is not None else load()
    meta = competitions(comps).set_index("competition_id")
    rows = []
    for c in comps:
        tl = predict.timeline(c)
        total = tl["total_min"]
        for snap in c.get("snapshots", []):
            ts = pd.Timestamp(snap["ts"])
            progress = ((ts - pd.Timestamp(tl["start"])).total_seconds() / 60 / total
                        if total else float("nan"))
            for rank, value in snap["points"].items():
                rows.append({"competition_id": c["id"], "snapshot_id": snap["id"],
                             "ts": ts, "progress": progress,
                             "games": snap.get("games"), "rank": int(rank),
                             "points": float(value)})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).join(meta, on="competition_id")


def curve_sample(comps: list[dict] | None = None, drop_rank_one: bool = True,
                 min_ranks: int = 2) -> pd.DataFrame:
    """The rows a curve fit can actually use.

    A tournament needs at least two ranks before it says anything about the
    *shape*: with one it only tells you a level, and the level is a free
    parameter per tournament. Rank 1 is dropped by default for the same reason
    `calibration.fit_curve` drops it — a winner often owes their score to one
    exceptional game, and that noise lands squarely on the steepest part of the
    curve.
    """
    df = thresholds(comps)
    df = df[df["field_size"].notna() & (df["field_size"] > 0) & (df["threshold"] > 0)]
    if drop_rank_one:
        df = df[df["rank"] != 1]
    keep = df.groupby("competition_id")["rank"].nunique()
    df = df[df["competition_id"].isin(keep[keep >= min_ranks].index)]
    return df.reset_index(drop=True)


def summarise() -> None:
    comps = load()
    c, t = competitions(comps), thresholds(comps)
    print(f"database : {DB_PATH}")
    print(f"{len(c)} competitions, {int(c['n_snapshots'].sum())} snapshots, "
          f"{len(t)} final thresholds\n")

    print("thresholds per competition")
    print(c["n_finals"].value_counts().sort_index().to_string(), "\n")

    print("snapshots per competition")
    print(c["n_snapshots"].value_counts().sort_index().to_string(), "\n")

    fit = curve_sample(comps)
    print(f"usable for a curve fit: {len(fit)} observations over "
          f"{fit['competition_id'].nunique()} tournaments "
          f"(q from {fit['q'].min():.4f} to {fit['q'].max():.3f})\n")

    print("by game mode / team mode")
    print(c.groupby(["game_mode", "team_mode"]).size().to_string(), "\n")
    print("by region")
    print(c["region"].value_counts().to_string(), "\n")
    print("by source")
    print(c["source"].value_counts().to_string())


if __name__ == "__main__":
    summarise()
