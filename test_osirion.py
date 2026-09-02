"""Offline checks for the Osirion reader, on payloads shaped like the real ones.

The harvester cannot be tested against the live API from every machine, and it
runs for hours before anyone would notice a misreading. So the parts that turn
Epic's JSON into numbers — the scoring rules above all — are exercised here
against fixtures whose right answers are known by construction.

    python test_osirion.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

FAILURES: list[str] = []


def check(name, expected, got):
    if expected == got:
        print(f"   ok   {name}")
    else:
        FAILURES.append(f"{name}: expected {expected!r}, got {got!r}")
        print(f"   FAIL {name}: expected {expected!r}, got {got!r}")


# A cup paying 60/54/48 down the top three and 2 a kill, written the way Epic
# writes it: one rule per cut, each carrying the increment over the cut below.
CUMULATIVE_WINDOW = {
    "eventWindowId": "S41_Test_Event1Round1_EU",
    "beginTime": "2026-05-04T19:00:00Z",
    "endTime": "2026-05-04T22:00:00Z",
    "round": 1,
    "scoringRules": [
        {"trackedStat": "PLACEMENT_STAT_INDEX", "matchRule": "lte",
         "rewardTier": 1, "pointsEarned": 6, "multiplicative": False},
        {"trackedStat": "PLACEMENT_STAT_INDEX", "matchRule": "lte",
         "rewardTier": 2, "pointsEarned": 6, "multiplicative": False},
        {"trackedStat": "PLACEMENT_STAT_INDEX", "matchRule": "lte",
         "rewardTier": 3, "pointsEarned": 48, "multiplicative": False},
        {"trackedStat": "TEAM_ELIMS_STAT_INDEX", "matchRule": "gte",
         "rewardTier": 1, "pointsEarned": 2, "multiplicative": True},
    ],
    "payoutTable": [
        {"scoreTier": 1, "payouts": [{"rewardType": "USD", "value": 100}]},
        {"scoreTier": 100, "payouts": [{"rewardType": "TOKEN", "value": 1}]},
    ],
}

EVENT = {
    "eventId": "epicgames_S41_ReloadCup_Duos_EU",
    "regions": ["EU"],
    "displayData": {"titleLine1": "Reload Victory Cup"},
    "eventWindows": [CUMULATIVE_WINDOW],
}


def team(rank, games, players=2):
    """A standings row whose total is exactly what the scoring says it is."""
    sessions, total = [], 0.0
    for index in range(games):
        placement = min(rank + index, 60)
        elims = max(0, 8 - index)
        sessions.append({"PLACEMENT_STAT_INDEX": placement,
                         "TEAM_ELIMS_STAT_INDEX": elims,
                         "MATCH_PLAYED_STAT": 1})
        total += (60 if placement == 1 else 54 if placement == 2
                  else 48 if placement == 3 else 0) + 2 * elims
    return {"rank": rank, "pointsEarned": total, "sessionHistory": sessions,
            "players": [{"accountId": f"p{rank}_{n}"} for n in range(players)]}


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import osirion

    print("\n1. Reading Epic's scoring rules")
    cumulative = osirion.scoring_from_window(CUMULATIVE_WINDOW, cumulative=True)
    check("kill value", 2.0, cumulative["kill"])
    check("placement table", [[1, 1, 60], [2, 2, 54], [3, 3, 48]],
          [[a, b, round(c)] for a, b, c in cumulative["placement"]])

    flat = osirion.scoring_from_window(CUMULATIVE_WINDOW, cumulative=False)
    check("the flat reading is different", True,
          flat["placement"] != cumulative["placement"])

    print("\n2. One game, scored")
    check("first place with 5 kills", 70.0, osirion.points_for(cumulative, 1, 5))
    check("thirtieth with 3 kills", 6.0, osirion.points_for(cumulative, 30, 3))
    capped = dict(cumulative, kill_cap=4)
    check("kills above the cap don't count", 68.0, osirion.points_for(capped, 1, 9))

    print("\n3. Choosing the reading the standings agree with")
    entries = [team(rank, games=6) for rank in range(1, 61)]
    scoring, agreement, kind = osirion.best_scoring(CUMULATIVE_WINDOW, entries)
    check("picks the cumulative reading", "cumulative", kind)
    check("reproduces every published total", 1.0, round(agreement, 3))
    check("and the table it picked is the right one",
          [[1, 1, 60.0], [2, 2, 54.0], [3, 3, 48.0]], scoring["placement"])

    print("\n4. A window whose rules do not match its standings")
    wrong = json.loads(json.dumps(CUMULATIVE_WINDOW))
    wrong["scoringRules"][3]["pointsEarned"] = 7          # kills are not worth 7
    _, agreement, _ = osirion.best_scoring(wrong, entries)
    check("agreement collapses", True, agreement < 0.5)

    print("\n5. Reading the rest of the window")
    check("team mode from the rosters", "Duo", osirion.team_mode(EVENT, entries))
    check("game mode from the title", "Reload", osirion.game_mode(EVENT))
    check("payout tiers", [1, 100], [t["rank"] for t in osirion.payout_tiers(CUMULATIVE_WINDOW)])

    print("\n6. Standings to thresholds, and into the database")
    workdir = tempfile.mkdtemp(prefix="osirion-test-")
    os.environ["FNT_DB"] = os.path.join(workdir, "test.db")
    import harvest_osirion as harvest
    harvest.RAW = os.path.join(workdir, "raw")
    import db
    db.init_db()

    finals = harvest.thresholds_of(entries)
    check("thresholds at the ranks we keep", [1, 3, 5, 10, 20, 25, 50],
          sorted(finals))
    check("rank 20 threshold", entries[19]["pointsEarned"], finals[20])

    work = {"event_id": EVENT["eventId"], "window_id": CUMULATIVE_WINDOW["eventWindowId"],
            "begin": CUMULATIVE_WINDOW["beginTime"], "end": CUMULATIVE_WINDOW["endTime"],
            "round": 1, "event": EVENT, "window": CUMULATIVE_WINDOW}
    harvest.save(harvest.page_path(work, 0), {"leaderboardData": entries})
    counts = harvest.build([work], pages=1)
    check("one competition written", 1, counts["written"])
    check("its scoring was verified, not guessed", 1, counts["scoring_ok"])

    with db.session() as conn:
        comp = db.list_competitions(conn)[0]
        stored = db.final_points(conn, comp["id"])
    check("region", "EU", comp["region"])
    check("team mode", "Duo", comp["team_mode"])
    check("game mode", "Reload", comp["game_mode"])
    check("games counted", 6, comp["max_games"])
    check("field size", 60, comp["field_size"])
    check("scoring stored as measured", True, comp["scoring_known"])
    check("thresholds stored", finals[20], stored[20])
    check("run again and nothing is duplicated", 1, harvest.build([work], pages=1)["skipped"])

    print("\n" + "=" * 68)
    if FAILURES:
        print(f"  {len(FAILURES)} failure(s):")
        for line in FAILURES:
            print(f"    - {line}")
        print("=" * 68 + "\n")
        return 1
    print("  The Osirion reader agrees with payloads whose answers are known.")
    print("=" * 68 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
