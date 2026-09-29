"""Offline checks for the Osirion reader, on payloads shaped like the real ones.

The harvester cannot be tested against the live API from every machine, and it
runs for hours before anyone would notice a misreading. So the parts that turn
Epic's JSON into numbers — the scoring rules above all — are exercised here
against fixtures whose right answers are known by construction.

    python src/test_osirion.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

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

    # Past the API's ceiling the page count says "ten thousand or more"; the
    # rosters' percentiles - Epic's tenth of the whole field, rounded down -
    # say how many more, and exactly where they step up.
    def board_page(work, page, field, total):
        harvest.save(harvest.page_path(work, page), {"success": True, "leaderboard": {
            "page": page, "totalPages": total,
            "entries": [{"rank": r, "pointsEarned": 9.0, "percentile": (10 * r // field) / 10}
                        for r in range(page * 100 + 1, min(field, page * 100 + 100) + 1)]}})
    capped = {"event_id": "e3", "window_id": "w3"}
    for page in (0, 1, 2, 79):
        board_page(capped, page, 57465, 100)
    check("at the ceiling, the cut's page alone: the least the field can be", 40000,
          harvest.field_of(capped, harvest.saved_entries(capped, 3)))
    board_page(capped, 57, 57465, 100)          # the page where 0 steps up to 0.1
    check("the page holding the step, read with the rest: the field to a few rosters", 57465,
          harvest.field_of(capped, harvest.saved_entries(capped, 3)))
    check("its page is counted on disk, so the window is derived again", 5,
          harvest.pages_on_disk(capped))
    below = {"event_id": "e4", "window_id": "w4"}
    for page in range(10):
        board_page(below, page, 4525, 46)
    check("below the ceiling the percentiles replace the page count's half page",
          True, abs(harvest.field_of(below, harvest.saved_entries(below, 10)) - 4525) <= 5)
    check("a board whose rosters carry no percentile says nothing", None,
          osirion.field_bounds([{"rank": 5, "pointsEarned": 3.0}]))

    # A hundred full pages are the first ten thousand of a bigger board, not
    # the whole of it: the percentiles in them still say how big.
    held = {"event_id": "e6", "window_id": "w6"}
    for page in range(100):
        board_page(held, page, 57465, 100)
    check("a capped board held to its hundredth page still reads its field off the percentiles",
          57465, harvest.field_of(held, harvest.saved_entries(held, 100)))
    # Pages of two moments can disagree: bounds that cross pin nothing.
    crossed = {"event_id": "e7", "window_id": "w7"}
    for page in range(3):
        board_page(crossed, page, 57465, 100)
    board_page(crossed, 29, 30000, 100)         # a board of 30,000 steps up at rank 3,000
    board_page(crossed, 30, 57465, 100)         # this one is still at nought past it
    check("bounds that cross give the least the pages allow, not their middle", 31000,
          harvest.field_of(crossed, harvest.saved_entries(crossed, 3)))

    # A page read while the board was filling holds the field of that minute.
    settled_work = {"event_id": "e8", "window_id": "w8",
                    "begin": "2026-01-10T17:00:00Z", "end": "2026-01-10T20:00:00Z"}
    for page in range(3):
        board_page(settled_work, page, 57465, 100)
    board_page(settled_work, 49, 50000, 100)   # the step of the 50,000 who had played by then
    before = harvest.settles_at(settled_work) - 3600
    os.utime(harvest.page_path(settled_work, 49), (before, before))
    check("a page read before its board settled is not counted", [],
          harvest.pages_beyond(settled_work, 3))
    check("so its step pins nothing: the page count's reading stands", 9950,
          harvest.field_of(settled_work, harvest.saved_entries(settled_work, 3)))
    # And no field page is asked for while the window runs.
    now = datetime.now(timezone.utc)
    running = {"event_id": "e9", "window_id": "w9",
               "begin": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "end": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for page in range(3):
        board_page(running, page, 30000, 100)
    asked = []
    real_page = osirion.leaderboard_page
    osirion.leaderboard_page = lambda event_id, window_id, page=0: asked.append(page) or {
        "success": True, "leaderboard": {"page": page, "totalPages": 100, "entries": []}}
    try:
        harvest.fetch_window(running, 3)
    finally:
        osirion.leaderboard_page = real_page
    check("no field page is read while the window runs", [], asked)

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

    print("\n10. A board read while its cup ran is read again, and derived again")
    # harvest.bat left running through a cup reads the board as it stands, as
    # deep as its passes go. Once the cup has settled, a shallow pass reads it
    # again from page zero - ten pages - and whatever the deeper passes then
    # find on disk past that is still the board of that minute.
    harvest.RAW = tempfile.mkdtemp(prefix="osirion-settle-")
    served, asked = {"pages": 30, "top": 5000}, []          # an hour in: 5000 - rank

    def moving_board(event_id, window_id, page=0):
        asked.append(page)
        ranks = range(page * 100 + 1, page * 100 + 101) if page < served["pages"] else ()
        return {"success": True, "leaderboard": {"page": page, "totalPages": served["pages"], "entries": [
            {"rank": r, "pointsEarned": float(served["top"] - r)} for r in ranks]}}

    closed = datetime.now(timezone.utc) - timedelta(hours=20)
    night = {"eventId": "epicgames_S42_NightCup_NAC", "regions": ["NAC"],
             "displayData": {"titleLine1": "Night Cup"}}
    ran = {"eventWindowId": "S42_NightCup_Event1Round1_NAC", "matchCap": 6,
           "beginTime": (closed - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "endTime": closed.strftime("%Y-%m-%dT%H:%M:%SZ")}
    cup = {"event_id": night["eventId"], "window_id": ran["eventWindowId"], "begin": ran["beginTime"],
           "end": ran["endTime"], "round": 1, "event": night, "window": ran}

    def aged(work, pages, when):
        for page in pages:
            os.utime(harvest.page_path(work, page), (when, when))

    def stored(rank):
        with db.session() as conn:
            comp = db.find_by_window(conn, cup["event_id"], cup["window_id"])
            return db.get_finals(conn, comp["id"]).get(rank) if comp else None

    settled_at = harvest.settles_at(cup)
    osirion.leaderboard_page = moving_board
    try:
        for depth in (10, 30, 101):                         # harvest.bat, during the cup
            harvest.fetch_window(cup, depth)
        aged(cup, range(30), closed.timestamp() - 3600)
        served.update(pages=40, top=10000)                  # settled: 10000 - rank
        harvest.fetch_window(cup, 10)                       # update.bat, after the close
        aged(cup, range(10), settled_at + 600)
        harvest.build([cup], pages=101)
        check("a build before the deeper passes leaves the mid-session pages out", (9000.0, None),
              (stored(1000), stored(2500)))
        index = harvest.raw_path("index", cup["event_id"], f"{harvest.slug(cup['window_id'])}.json.gz")
        os.utime(index, (time.time() - 3600,) * 2)          # derived an hour ago
        asked.clear()
        harvest.fetch_window(cup, 30)                       # harvest.bat's deeper passes
        check("a deeper pass reads again the pages left on disk from mid-session",
              list(range(10, 30)), asked)
        check("and the window is derived again, with no more pages than before", 1,
              harvest.build([cup], pages=101)["updated"])
        asked.clear()
        harvest.fetch_window(cup, 101)
        check("the deepest pass reaches the settled board's last page, not the running one's",
              list(range(30, 40)), asked)
        harvest.build([cup], pages=101)
        check("the deep ranks are the settled board's", (9000.0, 7500.0), (stored(1000), stored(2500)))
        check("run again and nothing is derived twice", 1, harvest.build([cup], pages=101)["already"])

        # A cut's page is read on the first pass, beside the first pages. One
        # left from mid-session - the run stopped before it, or its request
        # failed - is read again, though page zero was read after the close.
        paying = dict(ran, eventWindowId="S42_NightCup_Event2Round1_NAC", scoreLocations=[
            {"isMain": True, "payoutTables": [{"scoringType": "rank", "ranks": [
                {"threshold": 1500, "payouts": [{"rewardType": "token", "quantity": 1,
                                                 "value": "S42_NightCup_Event2Round2_NAC"}]}]}]}])
        cut = dict(cup, window_id=paying["eventWindowId"], window=paying)
        harvest.fetch_window(cut, 3)
        aged(cut, [14], closed.timestamp() - 3600)
        asked.clear()
        harvest.fetch_window(cut, 3)
        check("a cut's page read before the close is read again", [14], asked)

        # refresh.bat without --fetch derives what is on disk, and reads
        # nothing again: a board read while its cup ran is not its result.
        early = dict(cup, window_id="S42_NightCup_Event3Round1_NAC",
                     window=dict(ran, eventWindowId="S42_NightCup_Event3Round1_NAC"))
        served.update(pages=30, top=5000)
        harvest.fetch_window(early, 10)
        aged(early, range(10), closed.timestamp() - 3600)
        check("a board read while its cup ran, and not since, is not derived", 0,
              harvest.build([early], pages=101)["written"])
    finally:
        osirion.leaderboard_page = real_page

    print("\n11. A network failure is not an answer")
    # The API down for a while - a 503 that outlasts the retries, a connection
    # dropped before the answer came - says nothing about the window. Written
    # down as its answer, it told every later pass the window never ran.
    import http.client
    import urllib.error
    harvest.RAW = tempfile.mkdtemp(prefix="osirion-net-")
    outage = {"error": None}

    def network(request, timeout=None):
        if outage["error"] is not None:
            raise outage["error"]
        page = int(request.full_url.rsplit("page=", 1)[1])
        return io.BytesIO(json.dumps({"success": True, "leaderboard": {"page": page, "totalPages": 3, "entries": [
            {"rank": page * 100 + i + 1, "pointsEarned": 50.0} for i in range(100)]}}).encode())

    def window(name):
        return {"event_id": "epicgames_S42_NetCup_EU", "window_id": f"S42_NetCup_{name}_EU",
                "begin": "2026-09-01T17:00:00Z", "end": "2026-09-01T20:00:00Z"}

    real_urlopen, real_sleep = osirion.urllib.request.urlopen, time.sleep
    osirion.urllib.request.urlopen, time.sleep = network, (lambda seconds: None)
    try:
        down = window("Event1Round1")
        outage["error"] = urllib.error.HTTPError("https://api", 503, "Service Unavailable", {}, None)
        harvest.fetch_window(down, 3)
        check("a 503 that outlasts the retries leaves nothing on disk", None, harvest.read_page(down, 0))
        outage["error"] = None
        harvest.fetch_window(down, 3)
        check("and the next pass reads the window once the API is back", 300,
              len(harvest.saved_entries(down, 3)))
        outage["error"] = http.client.RemoteDisconnected("Remote end closed connection without response")
        try:
            harvest.fetch_window(window("Event2Round1"), 3)
            raised = None
        except Exception as exc:                                    # noqa: BLE001
            raised = type(exc).__name__
        check("a connection dropped before the answer is waited out, not a crash", None, raised)
        never = window("Event3Round1")
        outage["error"] = urllib.error.HTTPError("https://api", 404, "Not Found", {}, None)
        harvest.fetch_window(never, 3)
        check("a window that never ran, a 404, is still noted as empty", 0,
              osirion.total_pages(harvest.read_page(never, 0) or {}))
    finally:
        osirion.urllib.request.urlopen, time.sleep = real_urlopen, real_sleep

    print("\n12. The live feed's readings are filed once, however often the pull runs")
    # A rank read off a page the API stamped at another minute than the run's
    # first page carries that page's updatedAt, as worker.js keeps it: ISO,
    # with a T. The day's file is pulled on three runs in a row (DAYS).
    import pull_live
    with db.session() as conn:
        followed = db.create_competition(conn, "Feed Cup", region="NAC",
                                         start_time="2026-09-28 17:00", end_time="2026-09-28 20:00")
        db.update_competition(conn, followed, event_id="epicgames_S42_FeedCup_NAC",
                              window_id="S42_FeedCup_Event1Round1_NAC")
    day_file = {"windows": {"S42_FeedCup_Event1Round1_NAC": {
        "event": "epicgames_S42_FeedCup_NAC", "window": "S42_FeedCup_Event1Round1_NAC",
        "name": "Feed Cup", "region": "NAC", "begin": "2026-09-28T17:00:00.000Z", "end": "2026-09-28T20:00:00.000Z",
        "readings": [{"updated": "2026-09-28T18:40:07.512Z", "games": 3,
                      "readings": [[100, 90.0], [500, 60.0], [1000, 41.0, "2026-09-28T18:38:02.117Z"]]},
                     {"updated": "2026-09-28T18:50:05.004Z", "games": 4,
                      "readings": [[100, 101.0], [500, 66.0], [1000, 45.0]]}]}}}
    real_fetch, filed = pull_live.fetch, []
    pull_live.fetch = lambda url: day_file
    try:
        for _ in range(3):
            with db.session() as conn:
                for window in pull_live.gather("https://feed.invalid", 1).values():
                    pull_live.file_window(conn, window, False)
            with db.session() as conn:
                filed.append(len(db.get_snapshots(conn, followed)))
    finally:
        pull_live.fetch = real_fetch
    check("three pulls of the same day file each reading once", [3, 3, 3], filed)
    with db.session() as conn:
        check("a page's own stamp is filed at its own minute",
              ["2026-09-28 18:38", "2026-09-28 18:40", "2026-09-28 18:50"],
              [str(s["ts"])[:16] for s in db.get_snapshots(conn, followed)])

    print("\n13. What the publish pushes: nothing private, whoever made the commit")
    # A clone of this repository and a bare one playing GitHub. refresh.py
    # pushes every commit GitHub does not have yet - a run's, or one made by
    # hand - so the rule that keeps data, keys and local notes out of the
    # run's own commit has to hold for all of them.
    import shutil
    import subprocess
    from pathlib import Path
    import refresh
    if not shutil.which("git"):
        print("   (git is not installed here: skipped)")
    else:
        sandbox = Path(tempfile.mkdtemp(prefix="osirion-publish-"))
        (sandbox / "gitconfig").write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n"
                                           "[commit]\n\tgpgsign = false\n")
        saved_env = dict(os.environ)
        os.environ.update(GIT_CONFIG_GLOBAL=str(sandbox / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
                          GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Test", GIT_COMMITTER_NAME="Test",
                          GIT_AUTHOR_EMAIL="test@example.invalid", GIT_COMMITTER_EMAIL="test@example.invalid")

        def git(*args, cwd=sandbox / "tracker"):
            return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)

        def write(*names):
            for name in names:
                (sandbox / "tracker" / name).parent.mkdir(parents=True, exist_ok=True)
                (sandbox / "tracker" / name).write_text(name + "\n")

        def on_github():
            return git("--git-dir", str(sandbox / "github.git"), "ls-tree", "-r", "--name-only", "main").stdout.split()

        real_root = refresh.ROOT
        try:
            git("init", "-q", "--bare", str(sandbox / "github.git"), cwd=sandbox)
            (sandbox / "tracker").mkdir()
            git("init", "-q")
            if (real_root / ".gitignore").exists():
                shutil.copy(real_root / ".gitignore", sandbox / "tracker" / ".gitignore")
            write("README.md")
            git("add", "-A")
            git("commit", "-q", "-m", "seed")
            git("branch", "-M", "main")
            git("remote", "add", "origin", str(sandbox / "github.git"))
            git("push", "-q", "-u", "origin", "main")
            refresh.ROOT = sandbox / "tracker"
            # Committed by hand: two names .gitignore does not cover, three
            # forced past it.
            write("api_token.json", "src/tool.py", "cito_key.txt", "notes.local.md", "data/tracker.db")
            git("add", "-A")
            git("add", "-f", "cito_key.txt", "notes.local.md", "data/tracker.db")
            git("commit", "-q", "-m", "by hand")
            pushed = refresh.publish_tracker(lambda text: text)
            check("a commit made by hand that carries a key, a note or data is not pushed",
                  (False, []), (pushed, [name for name in on_github() if refresh.NEVER.search(name)]))
            git("reset", "-q", "--hard", "origin/main")
            write("src/other_tool.py")
            git("add", "-A")
            git("commit", "-q", "-m", "by hand, code only")
            check("one that carries code only is", (True, True),
                  (refresh.publish_tracker(lambda text: text), "src/other_tool.py" in on_github()))
            # A note committed before .gitignore covered it is taken out of
            # the repository, and that deletion is not held back.
            write("old.local.md", "héritage.local.md")
            git("add", "-f", "old.local.md", "héritage.local.md")
            git("commit", "-q", "-m", "notes, before the rule")
            git("push", "-q", "origin", "main")
            pushed = refresh.publish_tracker(lambda text: text)
            names = git("--git-dir", str(sandbox / "github.git"), "ls-tree", "-r", "-z", "--name-only", "main").stdout
            check("private notes git already follows are taken out, an accented name too", (True, []),
                  (pushed, [name for name in names.split("\0") if name.endswith(".local.md")]))
        finally:
            refresh.ROOT = real_root
            os.environ.clear()
            os.environ.update(saved_env)

    print("\n14. A build stopped midway is made again")
    # A chunk's derivations reach the database together, when it commits.
    # Window A, read again, is derived again; the build stops on window B,
    # before the commit - a Ctrl-C. The next build must not take A for done.
    harvest.RAW = tempfile.mkdtemp(prefix="osirion-chunk-")
    ended = datetime.now(timezone.utc) - timedelta(days=2)

    def chunk_work(name):
        event = {"eventId": "epicgames_S42_ChunkCup_EU", "regions": ["EU"],
                 "displayData": {"titleLine1": "Chunk Cup"}}
        window = {"eventWindowId": f"S42_ChunkCup_{name}_EU", "matchCap": 6,
                  "beginTime": (ended - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "endTime": ended.strftime("%Y-%m-%dT%H:%M:%SZ")}
        return {"event_id": event["eventId"], "window_id": window["eventWindowId"], "begin": window["beginTime"],
                "end": window["endTime"], "round": 1, "event": event, "window": window}

    def lay(work, top):
        for page in range(3):
            harvest.save(harvest.page_path(work, page), {"success": True, "leaderboard": {
                "page": page, "totalPages": 3, "entries": [
                    {"rank": r, "pointsEarned": float(top - r)} for r in range(page * 100 + 1, page * 100 + 101)]}})

    first, second = chunk_work("Event1Round1"), chunk_work("Event2Round1")
    lay(first, 1000)
    harvest.build([first], pages=3)
    index = harvest.raw_path("index", first["event_id"], f"{harvest.slug(first['window_id'])}.json.gz")
    os.utime(index, (time.time() - 3600,) * 2)          # derived an hour ago
    lay(first, 2000)                                    # read again since
    lay(second, 900)

    def stopped(*args, **kwargs):
        raise KeyboardInterrupt

    real_create, db.create_competition = db.create_competition, stopped
    try:
        harvest.build([first, second], pages=3)
    except KeyboardInterrupt:
        pass
    finally:
        db.create_competition = real_create
    counts = harvest.build([first, second], pages=3)
    with db.session() as conn:
        at_250 = db.get_finals(conn, db.find_by_window(conn, first["event_id"], first["window_id"])["id"]).get(250)
    check("the next build derives again what the stopped one lost", (1, 1750.0), (counts["updated"], at_250))

    print("\n15. model.json: the Python model's forecasts, and nothing else")
    # A database of its own: four weeks of cups, a final in one lobby, and
    # standings carrying names that must stay out of the file.
    import calibration
    import contextlib
    import copy
    import export_model
    from pathlib import Path
    shop = os.path.join(workdir, "model.db")
    db.init_db(shop)
    table = {"placement": [[1, 1, 60], [2, 5, 40], [6, 25, 20], [26, 100, 5]], "kill": 2.0}

    def cup(conn, family, region, day, field, level, team="Solo", stage="", games=10,
            ranks=(1, 5, 10, 20, 25, 50, 100, 250, 500, 1000)):
        ranks = [r for r in ranks if r <= field]
        cid = db.create_competition(conn, f"{family} {region} {day}", region=region, team_mode=team,
                                    start_time=f"{day} 18:00", end_time=f"{day} 21:00", ranks=ranks,
                                    max_games=games, scoring=table)
        db.update_competition(conn, cid, family=family, stage=stage, field_size=field,
                              finished_at=f"{day} 21:00", round_no=9 if stage else 0)
        db.set_finals(conn, cid, {r: round(level * (r / 20) ** -0.3, 1) for r in ranks})
        db.set_standings(conn, cid, [{"rank": r, "name": f"SecretPlayer{cid}x{r}",
                                      "members": [f"SecretMate{cid}x{r}"], "score": 99.0} for r in (1, 2)])
        return cid

    with db.session(shop) as conn:
        for week, day in enumerate(("2026-07-04", "2026-07-11", "2026-07-18", "2026-07-25")):
            cup(conn, "Solo Cup", "EU", day, 1200 + 50 * week, 200 + 5 * week)
            cup(conn, "Solo Cup", "NAC", day, 900, 180 + week)
            cup(conn, "Duo Cup", "EU", day, 800, 150 + week, team="Duo")
            cup(conn, "Duo Cup", "EU", day, 40, 250 + week, team="Duo", stage="Final", games=6,
                ranks=(1, 3, 5, 10, 20, 25))
        # A cup typed in by hand, its field left blank.
        for week, day in enumerate(("2026-07-05", "2026-07-12")):
            db.create_history_entry(conn, "Hand Cup", "EU", day, {1: 300 + week, 20: 200 + week, 100: 150 + week},
                                    max_games=10, scoring=table, team_mode="Solo")
            # A cup followed in the app, closed without copying its thresholds.
            tracked = db.create_competition(conn, "Tracked Cup", region="EU", team_mode="Solo",
                                            start_time=f"{day} 18:00", end_time=f"{day} 21:00",
                                            ranks=[20, 100], max_games=10, scoring=table)
            db.update_competition(conn, tracked, family="Tracked Cup", field_size=1000,
                                  finished_at=f"{day} 21:05")
            db.add_snapshot(conn, tracked, ts=f"{day} 19:30", points={20: 90, 100: 60})
            db.add_snapshot(conn, tracked, ts=f"{day} 21:05", points={20: 190 + week, 100: 140 + week})

    budget = export_model.SIZE_BUDGET

    def exported(conn, size=None):
        export_model.SIZE_BUDGET = budget if size is None else size
        try:
            return export_model.build_model(conn, comps, calib)
        finally:
            export_model.SIZE_BUDGET = budget

    def form_of(kind, rank, field):
        return {"category": kind, "name": kind, "region": "EU", "team_mode": "Solo",
                "game_mode": "Battle Royale", "max_games": 10, "field_size": field,
                "scoring": table, "scoring_known": True, "rank": rank}

    with db.session(shop) as conn:
        comps = export_model.load_competitions(conn)
        calib = export_model.calibration_of(comps)
        # Every model below stays alive to the end: the export's lookups are
        # cached by the identity of the lists they index.
        model = exported(conn)
        payload = export_model.encoded(model)
        check("no player, no team, no standings in the file", [],
              [word for word in ("SecretPlayer", "SecretMate") if word in payload])
        found = export_model.verify(model, comps, calib)
        check("the export reproduces the model it was built from, forecast for forecast",
              (True, 0), (len(found["diffs"]) > 0, len(found["mismatches"])))
        edges = [dict(c, **change) for c in comps[:6] for change in (
            {"field_size": None}, {"field_size": -5}, {"max_games": None}, {"region": None},
            {"team_mode": ""}, {"scoring": None}, {"ranks": list(c["ranks"]) + [5000]})]
        check("and agrees on missing fields, no field, no games and ranks past the field", 0,
              len(export_model.verify(model, edges, calib)["mismatches"]))

        moved = copy.deepcopy(model)
        for row in moved["categories"]:
            for entry in row.get("direct", {}).values():
                entry[0] *= 1.1
        check("an export whose numbers moved is refused", True,
              bool(export_model.verify(moved, comps[:8], calib)["mismatches"]))
        lost = copy.deepcopy(model)
        lost["families"] = []
        found = export_model.verify(lost, comps[:8], calib)
        check("a row missing though the budget cut nothing is a mismatch, not a trade",
              (0, True), (len(found["traded"]), bool(found["mismatches"])))

        full = len(payload.encode("utf-8"))
        check("the file's labels carry characters of more than one byte", True, full > len(payload))
        fitted = exported(conn, len(payload))
        check("trimmed to a budget in bytes, the file fits it", True,
              len(export_model.encoded(fitted).encode("utf-8")) <= len(payload))
        for share in (0.9, 0.5):
            trimmed = exported(conn, int(full * share))
            found = export_model.verify(trimmed, comps, calib)
            check(f"trimmed to {share:.0%}, the rows traded away are counted, not refused",
                  (True, 0), (len(found["traded"]) > 0, len(found["mismatches"])))

        hand = form_of("Hand Cup", 100, 1000)
        want = calibration.prior_prediction(dict(hand, kind="Hand Cup", family="Hand Cup"), calib, 100)
        got = export_model.predict_from_model(model, hand)
        check("a cup typed in without its field keeps its previous edition",
              ("previous edition", want.get("value")), (got.get("source"), got.get("value")))
        # The app reads every tournament whole: a rank with no final is read
        # off the readings, and the export has to read the same.
        app = calibration.broad_stats(db.keep_for_training(db.all_full(conn))[0])
        followed = form_of("Tracked Cup", 100, 1000)
        want = calibration.prior_prediction(dict(followed, kind="Tracked Cup", family="Tracked Cup"), app, 100)
        got = export_model.predict_from_model(model, followed)
        check("a cup closed without copying its thresholds reads as it does in the app",
              ("previous edition", want.get("value")), (got.get("source"), got.get("value")))

    # Nothing finished, nothing compared: no model is written over the last one.
    empty = os.path.join(workdir, "empty.db")
    db.init_db(empty)
    out = os.path.join(workdir, "empty-model.json")
    saved_path, saved_argv = export_model.DB_PATH, sys.argv
    export_model.DB_PATH, sys.argv = Path(empty), ["export_model.py", "--out", out]
    try:
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            code = export_model.main()
    finally:
        export_model.DB_PATH, sys.argv = saved_path, saved_argv
    check("an export that checked no forecast is not written", (1, False), (code, os.path.exists(out)))

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
