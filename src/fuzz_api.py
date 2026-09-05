"""Throw malformed input at every write endpoint and make sure nothing 500s.

Anything a form can send, a stranger can send too. A 400 with a readable
message is a correct answer; a 500 is a crash the user cannot act on.
"""
import collections
import os
import shutil
import tempfile

# Set up a scratch copy BEFORE importing app: app.py calls db.init_db() at
# import time, so by the time this module has its imports it is too late to
# choose a database. The fuzzer writes — pointing it at real data loses data.
if "FNT_DB_FUZZ" not in os.environ:
    _scratch = tempfile.mkdtemp(prefix="fuzz-")
    _here = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(_here) if os.path.basename(_here) == "src" else _here
    _live = os.environ.get("FNT_DB", os.path.join(_root, "data", "tracker.db"))
    _copy = os.path.join(_scratch, "tracker.db")
    if os.path.exists(_live):
        shutil.copy(_live, _copy)
    os.environ["FNT_DB"] = _copy
    os.environ["FNT_DB_FUZZ"] = "1"

import app
import db

client = app.app.test_client()
app.app.config["PROPAGATE_EXCEPTIONS"] = False
with db.session() as conn:
    cid = db.list_competitions(conn)[0]["id"]
    # A reading and a saved scoring to aim the update endpoints at: without a
    # real id those return 404 before reaching anything worth breaking.
    snap_id = db.add_snapshot(conn, cid, ts="2026-05-04 20:00", points={100: 300})
    scoring_id = db.create_scoring(conn, "Fuzz", kill=2, placement=[[1, 1, 60]])
    series_id = db.create_series(conn, "Fuzz series")

# "1e400" parses as a float and comes out infinite, which is the way an overflow
# usually arrives: as text, not as float("inf").
JUNK = ["", "abc", None, -1, float("inf"), "2026-13-45", [], {}, " ",
        "'; DROP TABLE competition;--", 10 ** 18, -99999, "NaN", True,
        float("nan"), "1e400", "x" * 100_000, [[1, "x", 3]], {"1": {"2": 3}},
        [None], -0.5]

TARGETS = [
    ("POST", f"/api/competitions/{cid}/confirm",
     {"name": "x", "start_time": "2026-05-04 19:00", "end_time": "2026-05-04 21:00",
      "game_minutes": 20, "tracker_lag_min": 5, "max_games": 10, "field_size": 1000,
      "region": "EU", "team_mode": "Duo", "game_mode": "Reload", "stage": "Final",
      "family": "F", "games_mode": "max", "qualifier": True,
      "scoring": {"kill": 2, "placement": [[1, 1, 60]]},
      "objectives": [{"label": "Top 100", "rank": 100}]}),
    ("POST", "/api/history/manual",
     {"kind": "K", "region": "EU", "team_mode": "Duo", "game_mode": "Reload",
      "max_games": 7, "duration_min": 90, "field_size": 500, "stage": "Final",
      "scoring": {"kill": 2, "placement": [[1, 1, 60]]},
      "rows": [{"date": "2026-05-04", "values": {"100": 300}}]}),
    ("POST", "/api/scorings",
     {"name": "S", "kill": 2, "kill_cap": 8, "placement": [[1, 1, 60]],
      "team_mode": "Duo", "game_mode": "Reload", "notes": "n"}),
    ("PUT", f"/api/competitions/{cid}/category",
     {"family": "F", "stage": "S", "field_size": 100}),
    ("PUT", "/api/category/scoring",
     {"family": "F", "stage": "S", "region": "", "scoring_id": 1, "overwrite": True}),
    ("PUT", f"/api/history/entries/{cid}",
     {"date": "2026-05-04", "edition": "W1", "field_size": 500, "max_games": 7,
      "thresholds": {"100": 300}}),
    ("POST", f"/api/competitions/{cid}/snapshots",
     {"ts": "2026-05-04 20:00", "points": {"100": 300}, "note": "n",
      "games": 4, "my_points": 120}),
    ("PUT", f"/api/competitions/{cid}",
     {"name": "x", "region": "EU", "team_mode": "Duo", "game_mode": "Reload",
      "start_time": "2026-05-04 19:00", "end_time": "2026-05-04 21:00",
      "ranks": "1, 100, 500", "game_minutes": 20, "tracker_lag_min": 5,
      "max_games": 10, "games_mode": "max", "slot_minutes": 0, "notes": "n",
      "field_size": 1000, "family": "F", "stage": "S", "qualifier": True,
      "scoring": {"kill": 2, "placement": [[1, 1, 60]]}}),
    ("POST", "/api/competitions",
     {"name": "x", "region": "EU", "team_mode": "Duo", "game_mode": "Reload",
      "start_time": "2026-05-04 19:00", "end_time": "2026-05-04 21:00",
      "ranks": [1, 100], "notes": "n", "game_minutes": 20, "tracker_lag_min": 5,
      "max_games": 10, "scoring_id": None, "games_mode": "max", "slot_minutes": 0,
      "scoring": {"kill": 2, "placement": [[1, 1, 60]]}}),
    ("PUT", f"/api/snapshots/{snap_id}",
     {"ts": "2026-05-04 20:00", "note": "n", "points": {"100": 300},
      "games": 4, "my_points": 120}),
    ("PUT", f"/api/competitions/{cid}/finals", {"points": {"100": 300}}),
    ("PUT", "/api/objectives",
     {"kind": "K · Final", "region": "EU", "items": [{"label": "Top 100", "rank": 100}]}),
    ("PUT", f"/api/scorings/{scoring_id}",
     {"name": "S2", "kill": 2, "kill_cap": 8, "placement": [[1, 1, 60]],
      "team_mode": "Duo", "game_mode": "Reload", "notes": "n"}),
    ("POST", "/api/series",
     {"name": "Serie", "region": "EU", "team_mode": "Duo", "game_mode": "Reload",
      "scoring_id": None, "notes": "n",
      "stages": [{"name": "Open", "day_offset": 0, "start": "19:00",
                  "duration_min": 180, "ranks": [100], "max_games": 10,
                  "game_minutes": 30, "tracker_lag_min": 5, "games_mode": "max",
                  "slot_minutes": 0}]}),
    ("PUT", f"/api/series/{series_id}",
     {"name": "Serie", "region": "EU", "team_mode": "Duo", "game_mode": "Reload",
      "scoring_id": None, "notes": "n",
      "stages": [{"name": "Final", "day_offset": 1, "start": "19:00",
                  "duration_min": 120, "ranks": [1, 10], "max_games": 6,
                  "game_minutes": 30, "tracker_lag_min": 5, "games_mode": "sealed",
                  "slot_minutes": 35}]}),
    ("POST", f"/api/series/{series_id}/editions", {"date": "2026-05-04", "label": "W1"}),
    ("POST", "/api/favourites", {"name": "Player", "note": "n"}),
    ("PUT", "/api/category/field",
     {"family": "F", "stage": "S", "region": "", "field_size": 500, "overwrite": True}),
    ("POST", f"/api/competitions/{cid}/duplicate",
     {"start_time": "2026-05-11 19:00", "name": "copy"}),
    ("POST", "/api/estimate",
     {"kind": "K", "stage": "Final", "region": "EU", "team_mode": "Duo",
      "game_mode": "Reload", "max_games": 7, "duration_min": 90, "field_size": 500,
      "scoring": {"kill": 2, "placement": [[1, 1, 60]]}, "scoring_id": None,
      "rank": 100}),
]


def run():
    errors, calls = [], 0
    for method, url, base in TARGETS:
        for field in base:
            for junk in JUNK:
                payload = dict(base)
                payload[field] = junk
                calls += 1
                try:
                    response = client.open(url, method=method, json=payload)
                except Exception as exc:
                    errors.append((url, field, repr(junk)[:18],
                                   f"{type(exc).__name__}: {exc}"))
                    continue
                if response.status_code >= 500:
                    errors.append((url, field, repr(junk)[:18], "HTTP 500"))
    return calls, errors


if __name__ == "__main__":
    calls, errors = run()
    print(f"\n{calls} malformed requests across {len(TARGETS)} endpoints")
    print(f"server errors: {len(errors)}\n")
    by_field = collections.Counter((u.rsplit('/', 1)[-1], f) for u, f, _, _ in errors)
    for (endpoint, field), n in by_field.most_common():
        print(f"   {endpoint:<12} {field:<16} {n:>3}")
