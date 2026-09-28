"""Offline checks for the Osirion reader, on payloads shaped like the real ones.

The harvester cannot be tested against the live API from every machine, and it
runs for hours before anyone would notice a misreading. So the parts that turn
Epic's JSON into numbers — the scoring rules above all — are exercised here
against fixtures whose right answers are known by construction.

    python src/test_osirion.py
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
    "matchCap": 6,
    "playlistId": "Playlist_ShowdownTournament_NPM_Duos",
    # Nested the way Epic really nests it: scoreLocations -> scoringRules ->
    # rewardTiers, with the cut called keyValue. The published schema shows a
    # flat rule instead, and testing against the schema is what let a harvest
    # store ten thousand tournaments with no scoring at all.
    "scoreLocations": [{
        "isMain": True,
        "scoringRules": [
            {"trackedStat": "PLACEMENT_STAT_INDEX", "matchRule": "lte", "rewardTiers": [
                {"keyValue": 1, "pointsEarned": 6, "multiplicative": False},
                {"keyValue": 2, "pointsEarned": 6, "multiplicative": False},
                {"keyValue": 3, "pointsEarned": 48, "multiplicative": False},
            ]},
            {"trackedStat": "TEAM_ELIMS_STAT_INDEX", "matchRule": "gte", "rewardTiers": [
                {"keyValue": 1, "pointsEarned": 2, "multiplicative": True},
            ]},
        ],
        # Shaped like the real thing (catalogue, September 2026): a rank table
        # counts from 1, a token names the window it opens, a value table is a
        # score and not a position at all.
        "payoutTables": [
            {"scoringType": "rank", "ranks": [
                {"threshold": 1, "payouts": [{"rewardType": "ecomm", "rewardMode": "standard",
                                              "value": "USD", "quantity": 600}]},
                {"threshold": 8, "payouts": [{"rewardType": "ecomm", "rewardMode": "standard",
                                              "value": "USD", "quantity": 100}]},
                {"threshold": 100, "payouts": [
                    {"rewardType": "token", "rewardMode": "standard",
                     "value": "S41_ReloadCup_Duos_Event1Round2_EU", "quantity": 1},
                    {"rewardType": "ecomm", "rewardMode": "standard", "value": "USD", "quantity": 0}]},
                {"threshold": 105, "payouts": [{"rewardType": "score", "rewardMode": "standard",
                                                "value": "S41_SeriesLeaderboard_EU", "quantity": 25}]},
                {"threshold": 500, "payouts": [{"rewardType": "game", "rewardMode": "standard",
                                                "value": "AthenaSpray:spray_reload", "quantity": 1}]},
            ]},
            {"scoringType": "value", "ranks": [
                {"threshold": 60, "payouts": [{"rewardType": "game", "rewardMode": "standard",
                                               "value": "AthenaGlider:glider_x", "quantity": 1}]},
            ]},
        ],
    }],
}

PERCENTILE_WINDOW = {
    "eventWindowId": "S37_EarlyAccess_Round1_EU", "round": 1,
    "beginTime": "2026-05-01T17:00:00Z", "endTime": "2026-05-01T20:00:00Z",
    "scoreLocations": [{"isMain": True, "scoringRules": [], "payoutTables": [
        {"scoringType": "percentile", "ranks": [
            {"threshold": 0.25, "payouts": [{"rewardType": "token", "rewardMode": "standard",
                                             "value": "S37_EarlyAccess_Final", "quantity": 1}]}]}]}],
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
    # Before importing anything that reaches for a database. `db` resolves FNT_DB
    # once, at import time, so setting it later means the checks below run
    # against whatever real tracker is sitting in data/ — and write to it.
    workdir = tempfile.mkdtemp(prefix="osirion-test-")
    os.environ["FNT_DB"] = os.path.join(workdir, "test.db")
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
    wrong["scoreLocations"][0]["scoringRules"][1]["rewardTiers"][0]["pointsEarned"] = 7
    _, agreement, _ = osirion.best_scoring(wrong, entries)
    check("agreement collapses", True, agreement < 0.5)

    print("\n5. Reading the rest of the window")
    check("team mode from the playlist", "Duo",
          osirion.team_mode(EVENT, entries, CUMULATIVE_WINDOW))
    check("team mode from the rosters when there is no playlist", "Duo",
          osirion.team_mode(EVENT, entries))
    check("games from the rules", 6, osirion.match_cap(CUMULATIVE_WINDOW))
    check("game mode from the title", "Reload", osirion.game_mode(EVENT))
    tiers = osirion.payout_tiers(CUMULATIVE_WINDOW, EVENT["eventId"])
    check("payout tiers count from 1 and skip score-valued tables",
          [1, 8, 100, 105, 500], [t["rank"] for t in tiers])
    check("first place is cash", ("cash", "$600", 600.0),
          (tiers[0]["kind"], tiers[0]["label"], tiers[0]["amount"]))
    check("a token beside a $0 line is the qualification, named after its window",
          ("qualify", "Event 1 Round 2"), (tiers[2]["kind"], tiers[2]["label"]))
    check("series points and cosmetics are read as such", ["points", "item"],
          [tiers[3]["kind"], tiers[4]["kind"]])
    pct = osirion.payout_tiers(PERCENTILE_WINDOW, "epicgames_S37_EarlyAccessCup_EU")
    check("a percentile cut has a share and no rank", [(None, 0.25, "qualify", "Early Access Final")],
          [(t["rank"], t["share"], t["kind"], t["label"]) for t in pct])
    # The FNCS Solo qualifiers' Round 1: two days, each window's own board
    # paying nothing by rank, the cut ranked on the two days' total - a
    # "Deny" token taken back from the top 8,000 on a board both days post to.
    def day(n):
        return {"eventWindowId": f"S42_FNCSSoloQualifiers_Qual1Round1Day{n}_EU", "scoreLocations": [
            {"leaderboardEventWindowId": f"S42_FNCSSoloQualifiers_Qual1Round1Day{n}_EU", "isMain": True,
             "payoutTables": [{"scoringType": "value", "ranks": [{"threshold": 0, "payouts": [
                 {"rewardType": "token", "value": "S42_FNCSSolo_DenyQual1Round2_EU", "quantity": 1}]}]}]},
            {"leaderboardEventWindowId": "epicgames_S42_FNCSSoloQualifiers_EU_1_risk", "isMain": False,
             "payoutTables": [{"scoringType": "rank", "ranks": [{"threshold": 8000, "payouts": [
                 {"rewardType": "token", "value": "S42_FNCSSolo_DenyQual1Round2_EU", "quantity": -1}]}]}]}]}
    solo = {"eventId": "epicgames_S42_FNCSSoloQualifiers_EU", "eventWindows": [day(1), day(2)]}
    total = osirion.payout_tiers(day(1), solo["eventId"], solo)
    check("a cut on the round's total is read off the other board, with its sessions",
          [(8000, "qualify", "FNCSSolo Qual 1 Round 2", True, 2)],
          [(t["rank"], t["kind"], t["label"], t.get("total"), t.get("sessions")) for t in total])
    check("a token given to everyone on the session's board is no cut",
          [], [t for t in osirion.payout_tiers(day(1), solo["eventId"]) if not t.get("total")])
    check("token names read as words", "Qualifier 2 Round 4",
          osirion.humanise_token("S29_FNCS_Major2_Qualifier2Round4_EU",
                                 "epicgames_S29_FNCS_Major2_Qualifier2_EU"))

    print("\n6. Reading a response the way the API really shapes it")
    real = {"success": True, "leaderboard": {"page": 0, "totalPages": 4,
                                             "entries": [{"rank": 1}, {"rank": 2}]}}
    check("entries found under leaderboard.entries", 2, len(osirion.entries_of(real)))
    check("page count read", 4, osirion.total_pages(real))
    documented = {"success": True, "leaderboardData": [{"rank": 1}], "totalPages": 9}
    check("the documented shape still works", 1, len(osirion.entries_of(documented)))
    check("neither shape present gives nothing, not a crash", 0,
          len(osirion.entries_of({"success": True})))

    print("\n7. The field is the board's size, not the harvest's depth")
    # The bug this guards against cost a full measurement: storing the deepest
    # rank downloaded made every tournament exactly as wide as the harvest was
    # deep, and the model knows a rank only as q = rank / field.
    import harvest_osirion as harvest
    deep = tempfile.mkdtemp(prefix="osirion-field-")
    harvest.RAW = deep
    partial = {"event_id": "e", "window_id": "w"}
    for page in range(3):                       # 3 pages fetched of 240 that exist
        harvest.save(harvest.page_path(partial, page), {"success": True, "leaderboard": {
            "page": page, "totalPages": 240,
            "entries": [{"rank": page * 100 + i + 1, "pointsEarned": 9.0} for i in range(100)]}})
    rows = harvest.saved_entries(partial, 3)
    check("deepest rank downloaded is only 300", 300, max(e["rank"] for e in rows))
    check("field read from the page count instead", 23950, harvest.field_of(partial, rows))

    whole = {"event_id": "e2", "window_id": "w2"}
    harvest.save(harvest.page_path(whole, 0), {"success": True, "leaderboard": {
        "page": 0, "totalPages": 2,
        "entries": [{"rank": i + 1, "pointsEarned": 9.0} for i in range(100)]}})
    harvest.save(harvest.page_path(whole, 1), {"success": True, "leaderboard": {
        "page": 1, "totalPages": 2,
        "entries": [{"rank": 100 + i + 1, "pointsEarned": 9.0} for i in range(37)]}})
    check("a board held whole is not rounded up", 137,
          harvest.field_of(whole, harvest.saved_entries(whole, 2)))

    check("a zero-point rank is not stored as a threshold", [1, 3],
          sorted(harvest.thresholds_of([{"rank": 1, "pointsEarned": 40.0},
                                        {"rank": 3, "pointsEarned": 12.0},
                                        {"rank": 5, "pointsEarned": 0.0}])))

    print("\n8. Standings to thresholds, and into the database")
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
    # Shaped like a real response, not like the published schema: the two
    # disagree, and testing against the schema is what let a whole night of
    # downloading store pages the reader could not read.
    harvest.save(harvest.page_path(work, 0),
                 {"success": True,
                  "leaderboard": {"leaderboardEventId": EVENT["eventId"],
                                  "leaderboardEventWindowId": CUMULATIVE_WINDOW["eventWindowId"],
                                  "page": 0, "totalPages": 1, "entries": entries}})
    counts = harvest.build([work], pages=1)
    check("one competition written", 1, counts["written"])
    check("a window with no pages on disk is reported, not silently dropped", 1,
          harvest.build([dict(work, window_id="never-fetched")], pages=1)["no_pages"])
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
    check("run again and nothing is duplicated", 1,
          harvest.build([work], pages=1)["already"])
    check("the name carries no stage when the id names no round",
          "Reload Victory Cup", comp["name"])
    check("its category is the family alone", "Reload Victory Cup", db.category_of(comp))
    with db.session() as conn:
        objectives = db.get_objectives(conn, db.category_of(comp), "EU")
    check("objectives stored from the payout table, ranks counted from 1",
          [1, 8, 100, 105, 500], [o["rank"] for o in objectives])

    print("\n9. Naming: what a cup is called and which session this is")
    def named(title1, title2, event_id="epicgames_S42_Something_EU"):
        return harvest.family_of({"eventId": event_id,
                                  "displayData": {"titleLine1": title1, "titleLine2": title2}})
    check("both title lines make the name", "Solo Victory Cup", named("Solo", "Victory Cup"))
    check("a division is part of the name", "FNCS Division 2", named("FNCS", "Division 2"))
    check("a weekly cup titled after the game keeps its own name",
          "Fortnite Performance Evaluation", named("Fortnite", "Performance Evaluation"))
    check("one line is enough when that is all there is", "Solo Victory Cup",
          named("Solo Victory Cup", None))
    check("the Zero Build twin of a same-titled cup is told apart",
          "Override Series (Zero Build)",
          named("Override Series", None, "epicgames_S42_OverrideSeries_ZB_sep8_EU"))
    check("a title that already says ZB is left alone", "ZB Solo Victory Cup",
          named("ZB Solo", "Victory Cup", "epicgames_S42_ZBSoloVictoryCup_EU"))

    def staged(window_id, round_field):
        return harvest.stage_of({"window_id": window_id,
                                 "window": {"eventWindowId": window_id, "round": round_field}})
    check("week 2 day 1 is not a second round", "",
          staged("S33_FNCSDivisionalCup_Division1_Week2Day1_EU", 2))
    check("a final is a final whatever the round number says", "Final",
          staged("S33_FNCSDivisionalCup_Division1_Week2Final_EU", 2))
    check("round 2 comes from the id", "Round 2",
          staged("S42_PerformanceEvaluation_Event3Round2_EU", 4))
    check("round 1 beside a week number is the only session there is", "",
          staged("S42_PerformanceEvaluation_Event3Round1_EU", 4))
    check("no round in the id and a number beside it: still no stage", "",
          staged("S37_DuosDivisionalCup_Division2_Event3_EU", 2))
    check("a harvested row with round_no 0 is filed without a stage, whatever its label says",
          "Cup", db.category_of({"family": "Cup", "stage": "Round 2", "round_no": 0,
                                 "source": "osirion"}))
    check("a hand-entered row still reads its label", "Cup · Round 2",
          db.category_of({"family": "Cup", "stage": "Round 2", "round_no": 0,
                          "source": "history"}))

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
