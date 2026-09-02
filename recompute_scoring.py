"""Re-infers the scoring table for already-imported tournaments — no requests spent.

The standings and per-game detail are already in the database: there's
nothing left to ask the API for. This script rereads that data, reruns the
scoring inference (including the elimination cap), and saves the result.

    python recompute_scoring.py
"""
from __future__ import annotations

import sys
from collections import defaultdict

import db
import scoring_infer


def rows_from_db(conn, comp_id: int) -> list[dict]:
    """Reconstructs what the API originally returned, from what we kept of it."""
    sessions = defaultdict(list)
    for r in conn.execute(
            "SELECT rank, match_number, placement, kills, points, started_at "
            "FROM team_match WHERE competition_id = ? ORDER BY rank, match_number",
            (comp_id,)):
        sessions[r["rank"]].append({
            "matchNumber": r["match_number"], "placement": r["placement"],
            "kills": r["kills"], "points": r["points"], "startTime": r["started_at"],
        })
    rows = []
    for r in conn.execute("SELECT rank, name, score, kills, games FROM standing "
                          "WHERE competition_id = ? ORDER BY rank", (comp_id,)):
        rows.append({"rank": r["rank"], "name": r["name"], "score": r["score"],
                     "kills": r["kills"], "games": r["games"],
                     "sessions": sessions.get(r["rank"], [])})
    return rows


def describe(scoring: dict) -> str:
    table = scoring.get("placement") or []
    head = " · ".join(f"top {a}{'' if a == b else '-' + str(b)} = {p:g}"
                      for a, b, p in table[:4])
    cap = scoring.get("kill_cap")
    return (f"{scoring.get('kill')} pt/elim"
            + (f" (capped {cap}/game)" if cap else " (no cap)")
            + f" · {head}" + (" · …" if len(table) > 4 else ""))


def main() -> int:
    db.init_db()
    changed = 0
    with db.session() as conn:
        comps = list(conn.execute("SELECT id, name, region FROM competition ORDER BY id"))
        if not comps:
            print("No competition in the database.")
            return 0
        for comp in comps:
            rows = rows_from_db(conn, comp["id"])
            label = f"[{comp['id']}] {comp['name']} · {comp['region']}"
            if not rows:
                print(f"{label}\n   no standings on file — skipped\n")
                continue
            before = db.get_competition(conn, comp["id"])
            guess = scoring_infer.infer(rows)
            print(label)
            print(f"   before: {describe(before['scoring'])}"
                  f"{'' if before.get('scoring_known') else '   (generic scoring)'}")
            if not guess.get("ok"):
                print(f"   after: unchanged — {guess.get('reason')}\n")
                continue
            scoring = {"kill": guess["kill"], "kill_cap": guess.get("kill_cap"),
                       "placement": guess["placement"]}
            db.update_competition(conn, comp["id"], scoring=scoring,
                                  scoring_accuracy=guess.get("accuracy"))
            changed += 1
            print(f"   after: {describe(scoring)}")
            print(f"   inferred from {guess.get('n_sample')} teams "
                  f"({guess['source']}) — reproduces {guess['accuracy']}% of scores\n")
    print(f"{changed} scoring table(s) updated. No API request spent.")

    # Older versions used to invent two thresholds named "Qualification" and
    # "Skin": that's no longer the case, but ones already saved stick around.
    with db.session() as conn:
        legacy = list(conn.execute(
            "SELECT kind, region, label, rank FROM objective "
            "WHERE label IN ('Qualification', 'Skin') ORDER BY kind, region, rank"))
    if legacy:
        print("\nThresholds auto-named by an older version — "
              "rename them in the app if the label is wrong:")
        for o in legacy:
            print(f"   {o['kind']} · {o['region']} : \"{o['label']}\" at rank {o['rank']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
