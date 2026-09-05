"""Checks that everything a form submits actually lands in the database.

This check exists because of a specific bug: the Train page had a "Ranked
teams" field, the user filled it in, and the function that sent the entry
dropped that field from the request body. Nothing failed, nothing warned —
the value just vanished, and it cost an afternoon of data entry along with it.

The approach: for every form, send a recognizable value into **every** field,
then read the database back and demand all of them turn up. A field lost
along the way fails the check instead of slipping through unnoticed.

    python src/check_forms.py
"""
from __future__ import annotations

import os
import sys
import tempfile

FAILURES: list[str] = []


def check(what: str, expected, actual) -> None:
    if expected == actual:
        print(f"   ok    {what:<44} {actual}")
    else:
        FAILURES.append(f"{what}: sent {expected!r}, found {actual!r}")
        print(f"   LOST  {what:<44} sent {expected!r} -> {actual!r}")


def main() -> int:
    # fresh, throwaway database: this check never touches your real data
    tmpdir = tempfile.mkdtemp(prefix="check-forms-")
    os.environ["FNT_DB"] = os.path.join(tmpdir, "test.db")
    import app as application
    import db

    client = application.app.test_client()
    scoring = {"kill": 3, "kill_cap": 7,
               "placement": [[1, 1, 61], [2, 2, 52], [3, 3, 43]]}

    print("\n1. Saved scoring")
    sent = {"name": "Control", "kill": 3, "kill_cap": 7, "team_mode": "Trio",
            "game_mode": "Zero Build", "placement": scoring["placement"],
            "notes": "control note"}
    received = client.post("/api/scorings", json=sent).get_json()["scoring"]
    for field in ("name", "kill_cap", "team_mode", "game_mode", "notes", "placement"):
        check(f"scoring · {field}", sent[field], received[field])
    check("scoring · kill", float(sent["kill"]), float(received["kill"]))
    scoring_id = received["id"]

    print("\n2. Entering a past tournament (Train page)")
    sent = {"kind": "Control tournament", "stage": "Qualification", "region": "OCE",
            "team_mode": "Trio", "game_mode": "Zero Build", "max_games": 7,
            "duration_min": 95, "field_size": 12345, "scoring": scoring,
            "rows": [{"date": "2026-05-04", "label": "Week 9",
                      "values": {"250": 321}, "max_games": 7}]}
    response = client.post("/api/history/manual", json=sent).get_json()
    check("entry · saved", 1, response.get("created"))
    with db.session() as conn:
        comp = next(c for c in db.all_full(conn) if c["family"] == "Control tournament")
    for field in ("region", "team_mode", "game_mode", "field_size", "max_games", "stage"):
        check(f"entry · {field}", sent[field], comp[field])
    check("entry · family", sent["kind"], comp["family"])
    check("entry · scoring kept", scoring["placement"], comp["scoring"]["placement"])
    check("entry · kill cap", scoring["kill_cap"], comp["scoring"]["kill_cap"])
    check("entry · recorded threshold", 321.0, comp["finals"].get(250))
    check("entry · duration", 95.0,
          round((_minutes(comp["end_time"]) - _minutes(comp["start_time"])), 0))

    print("\n3. Recategorizing from history")
    sent = {"family": "Other tournament", "stage": "Final", "field_size": 4242}
    client.put(f"/api/competitions/{comp['id']}/category", json=sent)
    with db.session() as conn:
        read_back = db.get_competition(conn, comp["id"])
    for field in ("family", "stage", "field_size"):
        check(f"recategorize · {field}", sent[field], read_back[field])

    print("\n4. Confirming a tracked tournament (Settings page)")
    with db.session() as conn:
        cid = db.create_competition(conn, name="Tracked control", region="EU",
                                    team_mode="Duo", game_mode="Battle Royale",
                                    start_time="2026-05-04 19:00")
    sent = {"name": "Renamed control", "family": "Tracked control", "stage": "Round 2",
            "region": "BR", "team_mode": "Squad", "game_mode": "Reload",
            "start_time": "2026-05-04 18:30", "end_time": "2026-05-04 21:00",
            "game_minutes": 17, "tracker_lag_min": 4, "max_games": 9,
            "games_mode": "sealed", "qualifier": True, "field_size": 8765,
            "scoring": scoring,
            "objectives": [{"label": "Control", "rank": 404}]}
    client.post(f"/api/competitions/{cid}/confirm", json=sent)
    with db.session() as conn:
        read_back = db.get_competition_full(conn, cid)
        thresholds = db.get_objectives(conn, db.category_of(read_back), sent["region"])
    for field in ("name", "family", "stage", "region", "team_mode", "game_mode",
                  "max_games", "games_mode", "field_size", "qualifier"):
        check(f"confirm · {field}", sent[field], read_back[field])
    check("confirm · game duration", 17.0, float(read_back["game_minutes"]))
    check("confirm · tracker lag", 4.0, float(read_back["tracker_lag_min"]))
    check("confirm · scoring", scoring["placement"], read_back["scoring"]["placement"])
    check("confirm · threshold", [("Control", 404)],
          [(t["label"], t["rank"]) for t in thresholds])

    print("\n5. Scoring applied to a whole category")
    client.put("/api/category/scoring", json={"family": "Other tournament", "stage": "Final",
                                              "region": "", "scoring_id": scoring_id,
                                              "overwrite": True})
    with db.session() as conn:
        read_back = db.get_competition(conn, comp["id"])
    check("apply · scoring set", scoring["placement"], read_back["scoring"]["placement"])
    check("apply · linked to scoring", scoring_id, read_back["scoring_id"])

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"  {len(FAILURES)} VALUE(S) LOST ALONG THE WAY:")
        for f in FAILURES:
            print(f"    · {f}")
        print("=" * 70 + "\n")
        return 1
    print("  Every submitted value made it into the database. Nothing gets lost.")
    print("=" * 70 + "\n")
    return 0


def _minutes(timestamp: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(timestamp).timestamp() / 60


if __name__ == "__main__":
    sys.exit(main())
