"""Offline checks for the Osirion reader, on payloads shaped like the real ones.

The harvester cannot be tested against the live API from every machine, and it
runs for hours before anyone would notice a misreading. So the parts that turn
Epic's JSON into numbers — the scoring rules above all — are exercised here
against fixtures whose right answers are known by construction.

    python src/test_osirion.py
"""
from __future__ import annotations

import atexit
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

FAILURES: list[str] = []
# Every folder the checks make, removed when they are over: a run leaves
# nothing behind in the temporary folder.
SCRATCH: list[str] = []


def scratch(prefix: str, **kwargs) -> str:
    folder = tempfile.mkdtemp(prefix=prefix, **kwargs)
    SCRATCH.append(folder)
    return folder


@atexit.register
def discard_scratch() -> None:
    # git marks its objects read-only, which Windows will not delete as they are.
    for folder in reversed(SCRATCH):
        for where, _, names in os.walk(folder):
            for name in names:
                try:
                    os.chmod(os.path.join(where, name), stat.S_IREAD | stat.S_IWRITE)
                except OSError:
                    pass
        shutil.rmtree(folder, ignore_errors=True)


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
    workdir = scratch("osirion-test-")
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
    deep = scratch("osirion-field-")
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
    harvest.RAW = scratch("osirion-settle-")
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
    harvest.RAW = scratch("osirion-net-")
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
        sandbox = Path(scratch("osirion-publish-"))
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
    harvest.RAW = scratch("osirion-chunk-")
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

    print("\n16. A later day of a round: the day before, moved as the later days moved")
    # A qualifier whose first round is played over two days, in two regions:
    # its first round measured whole, its second with the first day played and
    # the second on the calendar. On the second day the top of the board keeps
    # its level, ranks 101 to 500 lose 2 % and the deep end 5 %.
    import math
    two_days = os.path.join(workdir, "days.db")
    db.init_db(two_days)
    plan = {"placement": [[1, 1, 60], [2, 5, 40], [6, 25, 20], [26, 100, 5]], "kill": 2.0}
    first_day = {1: 400.0, 5: 380.0, 10: 360.0, 20: 340.0, 25: 320.0, 50: 300.0, 100: 280.0,
                 250: 260.0, 500: 240.0, 1000: 200.0, 2000: 180.0}
    second_day = {r: v * (1.0 if r <= 100 else 0.98 if r <= 500 else 0.95) for r, v in first_day.items()}
    with db.session(two_days) as conn:
        for region in ("EU", "NAC"):
            for qual, days in (("Qual1", ("2026-07-06", "2026-07-07")), ("Qual2", ("2026-07-13",))):
                for n, date in enumerate(days, 1):
                    cid = db.create_competition(conn, f"Test Qualifiers {region} {qual} {n}", region=region,
                                                team_mode="Solo", game_mode="Battle Royale",
                                                start_time=f"{date} 18:00", end_time=f"{date} 21:00",
                                                ranks=sorted(first_day), max_games=10, scoring=plan)
                    db.update_competition(conn, cid, family="Test Qualifiers", field_size=5000,
                                          finished_at=f"{date} 21:00", event_id=f"epicgames_S42_TestQuals_{region}",
                                          window_id=f"S42_TestQuals_{qual}Round1Day{n}_{region}")
                    db.set_finals(conn, cid, second_day if n == 2 else first_day)
        comps = export_model.load_competitions(conn)
        calib = export_model.calibration_of(comps)
        model = export_model.build_model(conn, comps, calib)
        check("the export still reproduces the model it was built from", 0,
              len(export_model.verify(model, comps, calib)["mismatches"]))
    check("the export measures the later days per band of rank",
          {"days": 2, "bands": {"100": [0.0, 0.02, 14], "500": [-0.0202, 0.02, 4], "0": [-0.0513, 0.02, 4]}},
          model.get("later_day"))
    check("a window is a later day from its id's Day 2 on",
          [True, True, False, False, False],
          [export_model.later_day(w) for w in ("S42_TestQuals_Qual2Round1Day2_EU", "S42_X_Round1Day3_NAC",
                                               "S42_TestQuals_Qual2Round1Day1_EU", "S42_HolidayCup_Event1_EU", None)])
    row = {"kind": "Test Qualifiers", "name": "Test Qualifiers", "event": "epicgames_S42_TestQuals_EU",
           "window": "S42_TestQuals_Qual2Round1Day2_EU", "stage": 0, "region": "EU", "team": "Solo",
           "mode": "Battle Royale", "games": 10, "begin": "2026-07-14T18:00Z", "end": "2026-07-14T21:00Z",
           "tiers": [["q", 1000, "Test Qual 2 Round 2", 2]], "entry": "", "field": 0, "scoring": 0}
    base = export_model.category_row(model, {"category": "Test Qualifiers", "region": "EU", "team_mode": "Solo",
                                             "game_mode": "Battle Royale"})["direct"]

    def listed(model_, row_):
        return [[r, v] for r, v, _, _ in export_model.calendar_forecast(model_, row_, [plan])["ranks"]]

    check("the second day is the day before, moved by its band's measured move",
          [[100, round(base["100"][0], 1)], [1000, round(base["1000"][0] * math.exp(-0.0513), 1)]],
          listed(model, row))
    t = export_model.calendar_tournament(model, row, [plan])
    moved = export_model.predict_from_model(model, dict(t, rank=1000))
    plain = export_model.predict_from_model(dict(model, later_day=None), dict(t, rank=1000))
    check("its band widened by the move's spread, and the readings behind it said",
          (True, 4, "previous edition"),
          (moved["high"] / moved["value"] > plain["high"] / plain["value"], moved["later_day"], moved["source"]))
    check("the first day of a round is not moved",
          [[100, round(base["100"][0], 1)], [1000, round(base["1000"][0], 1)]],
          listed(model, dict(row, window="S42_TestQuals_Qual2Round1Day1_EU")))
    check("nor is a later day in a model that measured none",
          [[100, round(base["100"][0], 1)], [1000, round(base["1000"][0], 1)]],
          listed(dict(model, later_day=None), row))
    typed = export_model.predict_from_model(model, dict(t, rank=1000, field_size=4000))
    check("a field typed in moves the day before along the curve instead", (0, True),
          (typed["later_day"], typed["field_effect"] < 0))

    print("\n17. The cold bench: every cup priced from the model online before it")
    import importlib.util
    if any(importlib.util.find_spec(p) is None for p in ("numpy", "pandas", "scipy")):
        print("   (skipped: analysis/ needs its own requirements, see analysis/requirements.txt)")
    else:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import gzip
        import calendar_snapshot
        import rescore
        from analysis import bench
        bench_db = os.path.join(workdir, "bench.db")
        db.init_db(bench_db)
        # A duo cup the list carries, its row rebuilt from the catalogue: its
        # window pays out to the top 100, who go through to a second round.
        listed = calendar_snapshot.entry(EVENT, CUMULATIVE_WINDOW, "EU")
        duo = {"placement": [[1, 1, 60], [2, 2, 54], [3, 3, 48]], "kill": 2.0}
        base = {r: round(300 * (r / 20) ** -0.3, 1) for r in (1, 10, 20, 25, 50, 100, 250, 500, 1000, 2500)}

        def edition(conn, family, region, day, scale, team="Solo", mode="Battle Royale", games=10,
                    scoring=table, event="", window="", hour=18):
            cid = db.create_competition(conn, f"{family} {region} {day}", region=region, team_mode=team,
                                        game_mode=mode, start_time=f"{day} {hour:02d}:00",
                                        end_time=f"{day} {hour + 3:02d}:00",
                                        ranks=sorted(base), max_games=games, scoring=scoring)
            db.update_competition(conn, cid, family=family, field_size=3000, finished_at=f"{day} {hour + 3:02d}:00",
                                  event_id=event, window_id=window)
            db.set_finals(conn, cid, {r: round(v * scale, 1) for r, v in base.items()})

        with db.session(bench_db) as conn:
            # Its third edition doubles: a forecast that read it would say so.
            for day, scale in (("2026-07-06", 1.0), ("2026-07-13", 1.05), ("2026-07-20", 2.0)):
                edition(conn, "Bench Cup", "EU", day, scale)
            for day in ("2026-07-20", "2026-07-27"):
                edition(conn, "Bench Cup", "NAC", day, 0.8)
            # A cup played first in Asia in the morning, then in Europe that evening.
            edition(conn, "Morning Cup", "ASIA", "2026-07-20", 0.9, hour=8)
            edition(conn, "Morning Cup", "EU", "2026-07-20", 1.0)
            # Its scoring only assumed: the page could not open it.
            edition(conn, "Blind Cup", "EU", "2026-07-13", 1.0, scoring=None)
            listed_window = CUMULATIVE_WINDOW["eventWindowId"]
            for day, scale, window in (("2026-07-06", 1.0, ""), ("2026-07-13", 1.1, listed_window)):
                edition(conn, listed["kind"], "EU", day, scale, team=listed["team"], mode=listed["mode"],
                        games=listed["games"], scoring=duo, event=EVENT["eventId"] if window else "", window=window)
        folder = os.path.join(workdir, "catalogue")
        os.makedirs(folder, exist_ok=True)
        with gzip.open(os.path.join(folder, "EU.json.gz"), "wt", encoding="utf-8") as fh:
            json.dump([EVENT], fh)
        catalogue = bench.load_catalogue(folder)
        check("the catalogue gives the list's windows back, each with its region",
              [(EVENT["eventId"], CUMULATIVE_WINDOW["eventWindowId"], "EU")],
              [(e, w, found[2]) for (e, w), found in catalogue.items()])

        key = bench.model_key(bench_db)
        with db.session(bench_db) as conn:
            ours = bench.Models(conn, export_model.load_competitions(conn), key, None).get("2026-07-20")
        out = os.path.join(workdir, "before.json")
        saved_path, saved_argv = export_model.DB_PATH, sys.argv
        export_model.DB_PATH, sys.argv = Path(bench_db), ["export_model.py", "--before", "2026-07-20", "--out", out]
        try:
            quiet = io.StringIO()
            with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                code = export_model.main()
        finally:
            export_model.DB_PATH, sys.argv = saved_path, saved_argv
        with open(out, encoding="utf-8") as fh:
            check("the bench's model of a day is the one export_model.py --before writes",
                  (0, True), (code, json.load(fh) == ours))

        with db.session(bench_db) as conn:
            asked = ("Bench Cup", "NAC", "Solo", "Battle Royale")
            check("a cup is new in its region until its first edition is over, whatever the database holds since",
                  (True, False, True),
                  (rescore.has_edition(conn, *asked), bench.has_edition_before(conn, *asked, "2026-07-20"),
                   bench.has_edition_before(conn, *asked, "2026-07-21")))
            run = bench.cold(conn, "2026-07-13", "2026-07-21", catalogue, None, key, progress=False)
        pairs = run["pairs"]

        def at(region, day, rank, family="Bench Cup"):
            return next((p for p in pairs if (p["region"], p["day"], p["rank"], p["family"])
                         == (region, day, rank, family)), {})

        first, doubled, new = at("EU", "2026-07-13", 100), at("EU", "2026-07-20", 100), at("NAC", "2026-07-20", 100)
        check("each cup is priced from the editions before its day: last week's, straight",
              ("previous edition", round(base[100], 1), round(base[100] * 1.05, 1)),
              (first.get("source"), first.get("forecast"), first.get("result")))
        check("and never from its own result, twice last week's", (True, True),
              (doubled.get("forecast", 0) < 1.1 * base[100], doubled.get("result", 0) > 1.9 * base[100]))
        check("a cup new in its region reads another rung than editions of its own", (True, "family"),
              (bool(new), new.get("source")))
        cut = at("EU", "2026-07-13", 100, family=listed["kind"])
        check("a cup the list carried is priced off its row, the cut it pays out on among its ranks",
              ("calendar", 100, True), (cut.get("input"), cut.get("cut"), cut.get("at_cut")))
        check("the ranks priced: the six, the cut and every deeper rank the standings hold",
              [10, 25, 100, 250, 500, 1000, 2500],
              sorted(p["rank"] for p in pairs
                     if (p["region"], p["day"], p["family"]) == ("EU", "2026-07-13", "Bench Cup")))
        check("a cup whose scoring the database only assumes is left out, and counted", 1,
              run["skipped"]["a row the page does not open by itself"])

        # The model a cup reads is the last daily update's: collected at 16:00
        # UTC, online half an hour later, holding what was over by then.
        check("a cup reads the model of the last update online before it starts",
              ["2026-07-20 16:00:00", "2026-07-19 16:00:00", "2026-07-19 16:00:00", "2026-07-20"],
              [bench.cutoff_of("2026-07-20 18:00:00"), bench.cutoff_of("2026-07-20 08:00:00"),
               bench.cutoff_of("2026-07-20 16:15:00"), bench.cutoff_of("2026-07-20 18:00:00", "day")])
        morning = {"start_time": "2026-07-20 08:00:00", "end_time": "2026-07-20 11:00:00"}
        late = {"start_time": "2026-07-20 15:00:00", "end_time": "2026-07-20 18:00:00"}
        check("which holds the tournaments over by its collection, and no other",
              [True, False, True, False],
              [bench.held(morning, "2026-07-20 16:00:00"), bench.held(late, "2026-07-20 16:00:00"),
               bench.held({"start_time": "2026-07-19 18:00:00", "end_time": "2026-07-19 21:00:00"}, "2026-07-20"),
               bench.held(morning, "2026-07-20")])
        with db.session(bench_db) as conn:
            evening = {by: next((p["source"] for p in bench.cold(conn, "2026-07-20", "2026-07-21", {}, None, key,
                                                                  by, progress=False)["pairs"]
                                 if (p["family"], p["region"], p["rank"]) == ("Morning Cup", "EU", 100)), None)
                       for by in ("update", "day")}
        check("an evening cup reads the morning's edition in another region; the day before's model has none",
              ("family", True), (evening["update"], evening["day"] != "family"))

        # The update keeps Paris time: 18:00 is 16:00 UTC until the last
        # Sunday of October, 17:00 from that day to the last Sunday of March.
        check("the update's hour is Paris time, through both changes of the clocks",
              ["2026-10-24 16:00:00", "2026-10-25 17:00:00", "2026-10-25 17:00:00", "2026-03-28 17:00:00",
               "2026-03-29 16:00:00"],
              [bench.cutoff_of("2026-10-24 20:00:00"), bench.cutoff_of("2026-10-25 17:30:00"),
               bench.cutoff_of("2026-10-26 17:29:00"), bench.cutoff_of("2026-03-29 16:29:00"),
               bench.cutoff_of("2026-03-29 16:30:00")])
        check("another hour, or a second update a day, is an option away",
              ["2026-07-20 14:00:00", "2026-07-20 16:00:00", "2026-07-20 20:15:00"],
              [bench.cutoff_of("2026-07-20 15:00:00", "update", ("16:00",)),
               bench.cutoff_of("2026-07-20 20:30:00", "update", bench.clocks("22:15,18:00")),
               bench.cutoff_of("2026-07-20 20:45:00", "update", bench.clocks("22:15,18:00"))])
        # The replayed tables read the raw pages the harvest keeps: a run
        # pointed at a folder without them says so, in print and in its file.
        out, printed, kept = os.path.join(workdir, "bench.json"), io.StringIO(), rescore.RAW
        with contextlib.redirect_stdout(printed):
            code = bench.main(["--cold", "--since", "2026-07-13", "--until", "2026-07-21", "--db", bench_db,
                               "--catalogue", folder, "--cache", "none", "--json", out,
                               "--leaderboards", os.path.join(workdir, "no-pages")])
        with open(out, encoding="utf-8") as fh:
            written = json.load(fh)
        check("a run without the raw pages warns, notes it, and leaves the reader as it found it",
              (0, True, 0, ["18:00"], len(pairs), True),
              (code, "WARNING: no raw leaderboard pages" in printed.getvalue(), written["leaderboards"]["events"],
               written["update"]["paris"], len(written["pairs"]), rescore.RAW == kept))
        # Two boards of the day in the Middle East, one over by the update and
        # one still running then: only the first had final standings to replay.
        with db.session(bench_db) as conn:
            for name, hour in (("Donor Morning", 8), ("Donor Running", 15)):
                cid = db.create_competition(conn, f"{name} ME", region="ME", team_mode="Solo",
                                            game_mode="Battle Royale", start_time=f"2026-07-27 {hour:02d}:00",
                                            end_time=f"2026-07-27 {hour + 3:02d}:00", ranks=sorted(base),
                                            max_games=10, scoring=table)
                db.update_competition(conn, cid, family=name, source="osirion", event_id=f"donor_{hour}",
                                      window_id="window")
                db.set_finals(conn, cid, dict(base))
            cutoff = bench.cutoff_of("2026-07-27 18:00:00")
            asked = (conn, "ME", "Solo", "Battle Royale", rescore.platform_of("Donor Morning ME"), cutoff)
            listed_donors = [c["family"] for c in rescore.candidates(*asked)]
            bench_donors = [c["family"] for c in bench.donors_over_by(rescore.candidates, cutoff)(*asked)]
            original = rescore.candidates, rescore.has_edition
            bench.cold_cell(conn, {"kind": "Donor Test", "name": "Donor Test ME", "region": "ME", "team": "Solo",
                                   "mode": "Battle Royale", "games": 10, "scoring": table, "tiers": []}, cutoff)
        check("a board still running at the update is no donor to the replay, though it started before it",
              (["Donor Running", "Donor Morning"], ["Donor Morning"], True),
              (listed_donors, bench_donors, (rescore.candidates, rescore.has_edition) == original))

        # The replay end to end, from pages on disk: a cup new in the Middle
        # East, whose own result the database already holds; two boards of the
        # region over before the update, and a third still running then.
        replay_db, pages = os.path.join(workdir, "replay.db"), os.path.join(workdir, "pages")
        db.init_db(replay_db)
        with db.session(replay_db) as conn:
            edition(conn, "Replay Cup", "EU", "2026-07-13", 1.0)
            edition(conn, "Replay Cup", "ME", "2026-07-20", 1.0)
            for name, day, hour in (("Board A", "2026-07-18", 10), ("Board B", "2026-07-19", 10),
                                    ("Board Late", "2026-07-20", 15)):
                cid = db.create_competition(conn, f"{name} ME", region="ME", team_mode="Solo",
                                            game_mode="Battle Royale", start_time=f"{day} {hour:02d}:00",
                                            end_time=f"{day} {hour + 3:02d}:00", ranks=sorted(base),
                                            max_games=10, scoring=table)
                event = "epicgames_" + name.replace(" ", "")
                db.update_competition(conn, cid, family=name, source="osirion", event_id=event,
                                      window_id=event + "_ME")
                db.set_finals(conn, cid, dict(base))
                entries = [{"teamId": f"t{n}", "rank": n, "pointsEarned": 0, "sessionHistory": [
                    {"trackedStats": {osirion.PLACEMENT_STAT: (n * 7 + g * 13) % 100 + 1, osirion.ELIMS_STAT: g % 4},
                     "endTime": f"{day}T{hour + 1:02d}:{g * 5:02d}:00Z"} for g in range(10)]} for n in range(1, 151)]
                folder = os.path.join(pages, rescore.slug(event))
                os.makedirs(folder, exist_ok=True)
                with gzip.open(os.path.join(folder, rescore.slug(event + "_ME") + "_p000.json.gz"), "wt",
                               encoding="utf-8") as fh:
                    json.dump({"success": True, "leaderboard": {"page": 0, "totalPages": 1, "entries": entries}}, fh)
            target = next(c for c in export_model.load_competitions(conn)
                          if (c.get("family"), c.get("region")) == ("Replay Cup", "ME"))
            kept, rescore.RAW = rescore.RAW, pages
            try:
                cell = bench.cold_cell(conn, bench.database_row(target), bench.cutoff_of("2026-07-20 18:00:00"))
                listed_cell = rescore.cold_table(conn, bench.database_row(target), before="2026-07-20 16:00:00")
            finally:
                rescore.RAW = kept
        check("the replay reads the two boards over before the update, though the cup's own result is in",
              (2, None), ((cell or {}).get("donors"), listed_cell))
        runs = {}
        for label, folder in (("pages", pages), ("none", os.path.join(workdir, "no-pages"))):
            out, printed = os.path.join(workdir, f"replay-{label}.json"), io.StringIO()
            with contextlib.redirect_stdout(printed):
                bench.main(["--cold", "--since", "2026-07-20", "--until", "2026-07-21", "--db", replay_db,
                            "--catalogue", os.path.join(workdir, "no-catalogue"), "--cache", "none",
                            "--json", out, "--leaderboards", folder])
            with open(out, encoding="utf-8") as fh:
                written = json.load(fh)
            runs[label] = (written, "WARNING" in printed.getvalue(),
                           sorted({p["source"] for p in written["pairs"] if p["family"] == "Replay Cup"}))
        # Two tables with the pages: the cup's, and the late board's own, new
        # in the region too (its forecasts refused: no field for a first board).
        check("pointed at the pages, a run replays the cup and says nothing; without them it warns and falls back",
              ((3, False, 2, 2, ["family + re-scored boards"]), (0, True, 0, 0, ["family"])),
              tuple((w["leaderboards"]["events"], warned, w["replays"]["cups"], w["replays"]["boards_with_pages"],
                     rungs) for w, warned, rungs in (runs["pages"], runs["none"])))

        # What a model is keyed on: a change to the lines that choose its
        # tournaments, or to the tournaments the app loads, is a new model.
        chooser = bench.held
        try:
            bench.held = lambda comp, cutoff: True
            moved = bench.model_key(replay_db)
        finally:
            bench.held = chooser
        with db.session(replay_db) as conn:
            comps = export_model.load_competitions(conn)
            keys = {bench.Models(conn, comps, "k", None).key, bench.Models(conn, comps[1:], "k", None).key}
        check("a model's key moves with the bench's own choice of tournaments, and with the list it is handed",
              (True, 2), (moved != bench.model_key(replay_db), len(keys)))
        # The bias list, on forecasts made up so the answer is known: a family
        # leaning 10 % low, played in seven regions an evening, the last one
        # after midnight UTC, with a field only counted once it was over.
        def made(family, region, start, error, drawn=3000):
            return {"rank": 100, "at_cut": False, "region": region, "game_mode": "Battle Royale",
                    "team_mode": "Solo", "field": 3000, "guessed_field": "", "result_field": drawn,
                    "previous_field": 0, "source": "previous edition", "entry": "", "round_day": "one session",
                    "first_week": False, "lobby": False, "input": "calendar", "family": family,
                    "day": start[:10], "local_day": bench.local_day(start, region),
                    "window": f"{family}|{region}|{start}", "forecast": 100.0 + error, "result": 100.0}

        clock = (("OCE", "07"), ("ASIA", "09"), ("ME", "15"), ("EU", "17"), ("BR", "21"), ("NAC", "23"))
        leaning = (-20, -10, -10, -10, 0, -10, -5)

        def evenings(days):
            out = []
            for day in days:
                night = (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%d")
                starts = [(r, f"{day} {h}:00:00") for r, h in clock] + [("NAW", f"{night} 01:00:00")]
                out += [made("Lean Cup", r, at, e, drawn=0) for (r, at), e in zip(starts, leaning)]
            return out

        plain = [made(f"Plain Cup {n}", r, f"2026-07-{10 + n:02d} {h}:00:00", e)
                 for n in range(6) for (r, h), e in zip(clock, (-3, 3, -1, 1, -2, 2))]
        found = {n: {(b["cut"], b["value"]): b for b in bench.biases(evenings(days) + plain, top=50)}
                 for n, days in ((2, ("2026-07-20", "2026-07-27")), (3, ("2026-07-20", "2026-07-27", "2026-08-03")))}
        lean = found[3].get(("family", "Lean Cup"), {})
        errors = [float(e) for e in leaning * 3]
        after = bench.AFTER
        try:
            bench.AFTER = set()
            unguarded = {(b["cut"], b["value"]) for b in bench.biases(evenings(("2026-07-20", "2026-07-27",
                                                                             "2026-08-03")) + plain, top=50)}
        finally:
            bench.AFTER = after
        drawn = ("field drawn", f"{bench.calibration.FIELD_CAP:,}+ (not counted)")
        check("a bias needs three evenings: an event in seven regions is one draw, and the list never offers "
              "the field a cup drew",
              (False, True, 3, False, True),
              (("family", "Lean Cup") in found[2], bool(lean), lean.get("family_days"), drawn in found[3],
               drawn in unguarded))
        # Skewed errors: the factor that helps most is not one plus the median.
        check("and its cost is the most one factor on its forecasts takes away, not a shift of every error",
              (round(bench.gain_of(errors)[1], 6), True, (-5, 18.57)),
              (round(lean.get("cost", 0), 6), bench.gain_of(errors)[1] < sum(abs(e) for e in errors)
               - sum(abs(e + 10) for e in errors),
               tuple(round(v, 2) for v in bench.gain_of([-30, -10, -5, 40, 80]))))

    print("\n18. The live bench: each cup the feed followed, replayed reading by reading")
    import importlib.util
    if any(importlib.util.find_spec(p) is None for p in ("numpy", "pandas", "scipy")):
        print("   (skipped: analysis/ needs its own requirements, see analysis/requirements.txt)")
    else:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from analysis import bench, bench_live
        live_db = os.path.join(workdir, "live.db")
        db.init_db(live_db)
        worth = {r: round(300 * (r / 20) ** -0.3, 1) for r in (1, 10, 25, 100, 250, 500, 1000, 2500, 7500, 10000)}

        def held_cup(conn, family, day, field, ranks, scale=1.0, hour=18):
            cid = db.create_competition(conn, f"{family} EU {day}", region="EU", team_mode="Solo",
                                        game_mode="Battle Royale", start_time=f"{day} {hour:02d}:00",
                                        end_time=f"{day} {hour + 3:02d}:00", ranks=sorted(ranks), max_games=10,
                                        scoring=table)
            db.update_competition(conn, cid, family=family, field_size=field, finished_at=f"{day} {hour + 3:02d}:00")
            db.set_finals(conn, cid, {r: round(worth[r] * scale, 1) for r in ranks})
            return cid

        def followed(conn, cid, read, pages=None, ranked=None):
            # A reading every twelve minutes, the board short of its final by
            # the share of the window still to play, and final past the close.
            for minute in range(12, 216, 12):
                at = datetime(2026, 7, 20, 18, 0) + timedelta(minutes=minute)
                db.add_snapshot(conn, cid, ts=at.strftime("%Y-%m-%d %H:%M"),
                                points={r: round(worth[r] * min(1.0, minute / 190), 1) for r in read},
                                note="live feed" + (" · final" if minute >= 180 else ""),
                                games=min(10, 1 + minute // 18), pages=pages, ranked=ranked)

        small = (1, 10, 25, 100, 250, 500, 1000)
        deep = (10, 25, 100, 250, 500, 1000, 2500, 7500, 10000)
        with db.session(live_db) as conn:
            for day in ("2026-07-06", "2026-07-13"):
                held_cup(conn, "Live Cup", day, 3000, small)
                held_cup(conn, "Big Cup", day, 15000, deep)
            # An edition over after the update that prices the evening: no
            # forecast made before the evening could read it.
            held_cup(conn, "Live Cup", "2026-07-20", 3000, small, scale=2.0, hour=14)
            target = held_cup(conn, "Live Cup", "2026-07-20", 3000, small)
            followed(conn, target, (1, 10, 100, 1000))
            # A field of fifteen thousand counted exactly past the API's last
            # page: the page reads the 7,500th as the casual half, live.py no
            # deeper than q = 0.49.
            big = held_cup(conn, "Big Cup", "2026-07-20", 15000, deep)
            followed(conn, big, (100, 7500), pages=100, ranked=15000)
        key = bench.model_key(live_db)

        def replayed(arrival=None):
            # Left out, the arrival is the bench's own default.
            with db.session(live_db) as conn:
                return bench_live.measure(conn, "2026-07-20", "2026-07-21", {}, None, key, progress=False,
                                          **({"arrival": arrival} if arrival else {}))

        def at(rows, point):
            return {(r["id"], r["rank"]): r["forecast"] for r in rows if r["point"] == point}

        first, second = replayed(), replayed()
        rows = first["rows"]
        check("the feed's cups are replayed at the seven points of their session, twice the same",
              (2, True, ["20 %", "40 %", "60 %", "80 %", "100 %", "+10 min", "+20 min"]),
              (first["cups"], json.dumps(rows, sort_keys=True) == json.dumps(second["rows"], sort_keys=True),
               sorted({r["point"] for r in rows}, key=[p for p, _, _ in bench_live.POINTS].index)))
        with db.session(live_db) as conn:
            cold = {(p["id"], p["rank"]): p["forecast"]
                    for p in bench.cold(conn, "2026-07-20", "2026-07-21", {}, None, key, progress=False)["pairs"]}
        mine = [r for r in rows if r["id"] == target]
        check("each cup starts from the model online before it, and its cold forecast is the cold bench's",
              ({bench.cutoff_of("2026-07-20 18:00:00")}, True, True),
              ({r["model"] for r in rows},
               all(r["cold"] == cold[(r["id"], r["rank"])] for r in mine if (r["id"], r["rank"]) in cold),
               all(r["cold"] < 1.5 * worth[r["rank"]] for r in mine if r["cold"])))
        # One every twelve minutes. Known at its stamp, the one stamped at 36
        # minutes is in at a fifth of the session; the one at 180, called
        # final, only past the close. On the feed's passes - the board is read
        # deeper than its first page, so on the ten-minute marks - each lands
        # two minutes after the next mark: the one at 36 at 42, the one at 72
        # at 82, the one at 192 at 202.
        stamped = [r for r in replayed("stamp")["rows"] if r["id"] == target]
        check("a point reads the readings that had reached the page by then and no other: at their stamp, "
              "or after the feed's pass, the default",
              ([3, 6, 9, 12, 14, 15, 16], [2, 5, 8, 11, 14, 15, 15], "feed"),
              tuple([next(r["snapshots"] for r in got if r["point"] == p) for p, _, _ in bench_live.POINTS]
                    for got in (stamped, mine)) + (bench.argument_parser().parse_args(["--live"]).arrival,))
        opens, closes = datetime(2026, 7, 20, 18, 0), datetime(2026, 7, 20, 21, 0)
        check("a reading lands two minutes after the first pass that could read it: a ten-minute mark, or a "
              "five-minute one for the first page from twenty minutes before the close",
              [22, 42, 72, 152, 167, 172, 182, 34, 39],
              [bench_live.landed(opens, closes, minute, ranks, arrival) for minute, ranks, arrival in (
                  (12, (1, 1000), "feed"), (34, (10,), "feed"), (70, (10,), "feed"), (143, (10,), "feed"),
                  (163, (10,), "feed"), (163, (10, 1000), "feed"), (176, (1, 100), "feed"),
                  (34, (10,), "stamp"), (34, (10,), 5))])

        # A reading planted at 170 minutes and called final - the feed only
        # says so once the window is over - must move nothing before the close.
        with db.session(live_db) as conn:
            db.add_snapshot(conn, target, ts="2026-07-20 20:50", points={10: 5 * worth[10]},
                            note="live feed · final", games=10)
        planted = replayed()["rows"]
        early = all(at(rows, p) == at(planted, p) for p in ("20 %", "40 %", "60 %", "80 %", "100 %"))
        check("a reading moves no answer before it was taken, a final one none before the close",
              (True, True), (early, at(rows, "+10 min") != at(planted, "+10 min")))
        kept = bench_live.available
        try:
            bench_live.available = lambda snapshots, minute, close, strict=True: list(snapshots)
            reads_all = replayed()["rows"]
            bench_live.available = lambda snapshots, minute, close, strict=True: kept(snapshots, minute, close, False)
            reads_final = replayed()["rows"]
        finally:
            bench_live.available = kept
        check("a replay that read later readings, or a final one before the close, is caught",
              (False, False), (at(reads_all, "20 %") == at(planted, "20 %"),
                               at(reads_final, "100 %") == at(planted, "100 %")))

        # The checks below price the one cup at a time, off one model.
        import export_model
        from collections import Counter
        cutoff = bench.cutoff_of("2026-07-20 18:00:00")
        with db.session(live_db) as conn:
            model = bench.Models(conn, export_model.load_competitions(conn), key, None).get(cutoff)

        def priced(arrival="feed", early=None):
            with db.session(live_db) as conn:
                comp = next(c for c in export_model.load_competitions(conn) if c["id"] == target)
                return bench_live.price_cup(conn, model, comp, {}, cutoff, Counter(), Counter(), arrival=arrival,
                                            early=early)[0]

        def plant(minute, points):
            stamp = opens + timedelta(minutes=minute)
            with db.session(live_db) as conn:
                return db.add_snapshot(conn, target, ts=stamp.strftime("%Y-%m-%d %H:%M"), points=points,
                                       note="live feed", games=min(10, 1 + max(0, minute) // 18))

        def unplant(sid):
            with db.session(live_db) as conn:
                db.delete_snapshot(conn, sid)

        # A reading stamped a minute after each point, not final, moves
        # nothing at that point, whether it lands at its stamp or after the
        # feed's pass. Taken a minute early, it moves every one of them.
        unmoved, moved = [], []
        for label, share, past in bench_live.POINTS:
            minute = 180 * share if share is not None else 180 + past
            before = {a: at(priced(a), label) for a in ("feed", "stamp")}
            sid = plant(round(minute) + 1, {10: round(8 * worth[10], 1)})
            try:
                unmoved.append(all(at(priced(a), label) == before[a] for a in before))
                bench_live.available = lambda snapshots, minute, close, strict=True: \
                    kept(snapshots, minute + 1, close, strict)
                moved.append(at(priced("stamp"), label) != before["stamp"])
            finally:
                bench_live.available = kept
                unplant(sid)
        check("a reading stamped a minute after a point moves nothing there, and would move it if read early",
              ([True] * 7, [True] * 7), (unmoved, moved))

        # Two minutes before a point outside the endgame, a board stamped then
        # has not been read yet: the feed's next pass is on the ten-minute mark.
        late = []
        for label, minute in (("20 %", 34), ("60 %", 106), ("80 %", 142)):
            before = {a: at(priced(a), label) for a in ("feed", "stamp")}
            sid = plant(minute, {10: round(8 * worth[10], 1)})
            try:
                late.append((at(priced("feed"), label) == before["feed"],
                             at(priced("stamp"), label) != before["stamp"]))
            finally:
                unplant(sid)
        check("a reading stamped two minutes before a point outside the endgame has not reached the page on the "
              "feed's passes, and has at its stamp", [(True, True)] * 3, late)

        # Outside the endgame the page waits for the ten-minute pass, whatever
        # the ranks: a board stamped a minute after a mark is in two minutes
        # after the next one, not sooner. Twenty-five minutes before the close
        # the quick pass is not on yet, so a first-page board stamped then
        # waits for the ten-minute mark as well, not the five-minute one.
        sids = [plant(61, {10: round(8 * worth[10], 1)}), plant(155, {10: round(8 * worth[10], 1)})]
        try:
            with db.session(live_db) as conn:
                held = {a: bench_live.snapshots_of(conn, target, opens, closes, a) for a in ("feed", "stamp")}
        finally:
            for sid in sids:
                unplant(sid)
        first_in = tuple(tuple(next((m for m in range(241) if any(s["id"] == sid for s in
                                                                  bench_live.available(held[a], m, 180))), None)
                               for sid in sids) for a in ("feed", "stamp"))
        check("a board stamped a minute after a ten-minute mark is first held two minutes after the next one, and a "
              "first-page board twenty-five minutes before the close on the ten-minute mark",
              ((72, 162), (61, 155)), first_in)

        # A reading stamped two days before the window opened belongs to
        # another window: left out, and counted.
        before, early = priced(), Counter()
        sid = plant(-2880, {10: round(8 * worth[10], 1)})
        try:
            after = priced(early=early)
        finally:
            unplant(sid)
        check("a reading stamped long before the window opened is left out and counted",
              (True, 1), (after == before, sum(early.values())))

        # The tournament's row is given the feed's last count after the cup:
        # the field the page read is never that one.
        before = {a: priced(a) for a in ("feed", "stamp")}
        with db.session(live_db) as conn:
            db.update_competition(conn, target, field_size=9000)
        try:
            after = {a: priced(a) for a in ("feed", "stamp")}
        finally:
            with db.session(live_db) as conn:
                db.update_competition(conn, target, field_size=3000)
        check("a field written to the tournament after the cup moves nothing and is never read as typed",
              (True, False), (after == before, any(r["field_from"] == "typed" or r["field"] == 9000
                                                   for got in after.values() for r in got)))

        # A final deeper than any reading, the 2,500th, is known only once the
        # cup is over: no other rank's forecast moves, and before the board
        # gives a count the field is the one the model guesses - the 3,000
        # its two editions drew - never the deepest rank of the finals.
        before = {a: priced(a) for a in ("feed", "stamp")}
        with db.session(live_db) as conn:
            db.set_finals(conn, target, {2500: round(worth[2500], 1)})
        try:
            after = {a: [r for r in priced(a) if r["rank"] != 2500] for a in ("feed", "stamp")}
        finally:
            with db.session(live_db) as conn:
                db.set_finals(conn, target, {2500: None})
        check("a final deeper than any reading moves no other rank's forecast, and the field guessed is the model's",
              (True, {3000}), (after == before, {r["field"] for got in after.values() for r in got
                                                 if r["field_from"] == "guessed"}))

        # The board's count grows through the evening: at 40 % the field
        # counted is the last count that had reached the page.
        with db.session(live_db) as conn:
            for sid, ts in conn.execute("SELECT id, ts FROM snapshot WHERE competition_id = ?", (target,)).fetchall():
                minute = (datetime.fromisoformat(str(ts)) - opens).total_seconds() / 60
                conn.execute("UPDATE snapshot SET ranked = ? WHERE id = ?", (int(2000 + 10 * minute), sid))
        counted = {a: next(r for r in priced(a) if r["point"] == "40 %") for a in ("stamp", "feed")}
        check("a count that grows through the evening is read as it stood when the point's last reading landed",
              (("counted", 2720), ("counted", 2600)),
              tuple((counted[a]["field_from"], counted[a]["field"]) for a in ("stamp", "feed")))

        # With no count, only the pages the feed walked - one more every
        # twelve minutes - the field is a hundred a page, the last half full,
        # off the last reading that had landed: seven pages in stamp, six in
        # the feed, never the eighteen of the evening's last.
        with db.session(live_db) as conn:
            for sid, ts in conn.execute("SELECT id, ts FROM snapshot WHERE competition_id = ?", (target,)).fetchall():
                minute = (datetime.fromisoformat(str(ts)) - opens).total_seconds() / 60
                conn.execute("UPDATE snapshot SET pages = ?, ranked = 0 WHERE id = ?", (1 + int(minute) // 12, sid))
        counted = {a: next(r for r in priced(a) if r["point"] == "40 %") for a in ("stamp", "feed")}
        check("pages that grow through the evening with no count are read as they stood when the last reading landed",
              (("counted", 650), ("counted", 550)),
              tuple((counted[a]["field_from"], counted[a]["field"]) for a in ("stamp", "feed")))

        # The cup is marked whatever its ranks; a forecast where the two
        # depths move it - the 7,500th, read past q = 0.49, and the ranks
        # priced off it, but not the 100th, read itself.
        flagged = [r for r in rows if r["id"] == big and r["rank"] == 7500 and r["point"] in ("60 %", "80 %")]
        plain = [r for r in rows if r["id"] == target]
        check("where live.py reads a counted field of 15,000 at its ceiling and the page does not, the forecast "
              "is flagged with the page's own beside it",
              (2, True, True, False, (["ceiling"], []), 0),
              (len(flagged), all("ceiling" in r["flags"] for r in flagged),
               all(r["page_forecast"] and r["page_forecast"] != r["forecast"] for r in flagged),
               any(r["flags"] for r in plain),
               tuple(next(line["reasons"] for line in first["lines"] if line["id"] == cid) for cid in (big, target)),
               sum(1 for r in rows if "ceiling" in r["flags"] and r["rank"] == 100)))

    print("\n19. The cold bench in several processes, and a variant measured beside today's code")
    if any(importlib.util.find_spec(p) is None for p in ("numpy", "pandas", "scipy")):
        print("   (skipped: analysis/ needs its own requirements, see analysis/requirements.txt)")
    else:
        import hashlib
        import types
        import analysis
        from analysis import bench
        variants = os.path.join(workdir, "variants")
        os.makedirs(variants, exist_ok=True)

        def variant_file(name, body):
            path = os.path.join(variants, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            return path

        identity = variant_file("identity.py", "def apply():\n    return None\n")
        planted_body = (
            "import export_model\n\n"
            "def apply():\n"
            "    plain = export_model.predict_from_model\n\n"
            "    def planted(model, t):\n"
            "        got = plain(model, t)\n"
            "        if got and got.get('value'):\n"
            "            got = dict(got, **{k: got[k] * 1.01 for k in ('value', 'low', 'high') if got.get(k)})\n"
            "        return got\n\n"
            "    export_model.predict_from_model = planted\n"
            "    return lambda: setattr(export_model, 'predict_from_model', plain)\n")
        planted = variant_file("planted.py", planted_body)
        pricing_only = variant_file("pricing_only.py", "CHANGES_MODEL = False\n" + planted_body)
        # Says it leaves the models alone, and marks every model it builds.
        liar = variant_file("liar.py", (
            "import export_model\n\nCHANGES_MODEL = False\n\n"
            "def apply():\n"
            "    plain = export_model.build_model\n"
            "    export_model.build_model = lambda *a, **k: dict(plain(*a, **k), marked=True)\n"
            "    return lambda: setattr(export_model, 'build_model', plain)\n"))
        # Makes its replacement as it is imported, with nothing left for apply().
        on_import = variant_file("on_import.py", (
            "import export_model\n\n"
            "plain = export_model.predict_from_model\n\n\n"
            "def planted(model, t):\n"
            "    got = plain(model, t)\n"
            "    if got and got.get('value'):\n"
            "        got = dict(got, **{k: got[k] * 1.01 for k in ('value', 'low', 'high') if got.get(k)})\n"
            "    return got\n\n\n"
            "export_model.predict_from_model = planted\n\n\n"
            "def apply():\n"
            "    return None\n"))
        failing = variant_file("failing.py", (
            "import export_model\n\n"
            "def apply():\n"
            "    plain = export_model.predict_from_model\n\n"
            "    def failing(model, t):\n"
            "        raise RuntimeError('this variant fails')\n\n"
            "    export_model.predict_from_model = failing\n"
            "    return lambda: setattr(export_model, 'predict_from_model', plain)\n"))
        # Leave the process instead of failing, as they are run or applied.
        exits_on_import = variant_file("exits_on_import.py", (
            "import sys\n\nsys.exit(0)\n\n\ndef apply():\n    return None\n"))
        exits_in_apply = variant_file("exits_in_apply.py", "import sys\n\n\ndef apply():\n    sys.exit(0)\n")
        # Makes its replacement in apply(), with nothing to put it back.
        twice = variant_file("twice.py", "CHANGES_MODEL = False\n" + planted_body.replace(
            "    return lambda: setattr(export_model, 'predict_from_model', plain)\n", "    return None\n"))

        # bench_compare is measured on its own; here a stand-in records what
        # the bench hands it.
        handed = []
        stand_in = types.ModuleType("analysis.bench_compare")
        stand_in.compare = lambda a, b: handed.append((a, b)) or {"pairs": len(a["pairs"])}
        stand_in.print_report = lambda result: print("compared", result["pairs"])
        saved_module = sys.modules.get("analysis.bench_compare")
        saved_attr = getattr(analysis, "bench_compare", None)
        sys.modules["analysis.bench_compare"], analysis.bench_compare = stand_in, stand_in
        cache = os.path.join(workdir, "bench-cache")

        left_behind = []

        def bench_run(name, *extra, models=None, stale=False):
            out, printed = os.path.join(workdir, f"{name}.json"), io.StringIO()
            if stale:
                # What a variant's run of an earlier day left beside this one.
                with open(bench.variant_json(out), "w", encoding="utf-8") as fh:
                    json.dump({"stale": True}, fh)
            predict = export_model.predict_from_model
            try:
                with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
                    try:
                        code = bench.main(["--cold", "--since", "2026-07-13", "--until", "2026-07-21", "--db",
                                           replay_db, "--catalogue", os.path.join(workdir, "no-catalogue"), "--cache",
                                           models or cache, "--leaderboards", pages, "--json", out, *extra])
                    except SystemExit as exc:
                        # Let through, it would end this suite with its code.
                        code = f"SystemExit({exc.code}) out of main"
            finally:
                # A replacement the run left in this process would reach the next.
                if export_model.predict_from_model is not predict:
                    left_behind.append(name)
                export_model.predict_from_model = predict
            found = []
            for path in (out, bench.variant_json(out)):
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as fh:
                        found.append(json.load(fh))
            return code, found, printed.getvalue()

        def plain(payload):
            return {k: v for k, v in payload.items() if k != "generated"}

        def ratios_of(run):
            before, after = run[1]
            return {round(a["forecast"] / b["forecast"], 9) for a, b in zip(after["pairs"], before["pairs"])}

        def aged(folder, names):
            month_old = time.time() - 30 * 86400
            for name in names:
                os.utime(os.path.join(folder, name), (month_old, month_old))

        def still_aged(folder):
            """The models neither dropped nor built again since `aged`."""
            return {name for name in os.listdir(folder)
                    if os.path.getmtime(os.path.join(folder, name)) < time.time() - 20 * 86400}

        def edited_run(name, jobs):
            """A variant one percent high, its file edited to five once today's
            run is over, as one would while waiting on a long run; and the
            SHA-1 of the file as it is after."""
            path = variant_file(f"{name}.py", planted_body)
            plain_cold = bench.cold

            def cold(*args, **kwargs):
                got = plain_cold(*args, **kwargs)
                if kwargs.get("variant") is None:
                    variant_file(f"{name}.py", planted_body.replace("1.01", "1.05"))
                return got

            bench.cold = cold
            try:
                return bench_run(name, "--variant", path, "--jobs", str(jobs)), bench.Variant(path).sha1
            finally:
                bench.cold = plain_cold

        try:
            one = bench_run("jobs-1")
            two = bench_run("jobs-2", "--jobs", "2")
            same = bench_run("identity", "--variant", identity)
            moved = bench_run("planted-2", "--variant", planted, "--jobs", "2")
            moved_one = bench_run("planted-1", "--variant", planted)
            refused = bench_run("liar", "--variant", liar)
            compared = len(handed)
            early = {jobs: bench_run(f"on-import-{jobs}", "--variant", on_import, "--jobs", str(jobs))
                     for jobs in (1, 2)}
            failed = {jobs: bench_run(f"failing-{jobs}", "--variant", failing, "--jobs", str(jobs))
                      for jobs in (1, 2)}
            exited = {jobs: bench_run(f"exits-{jobs}", "--variant", found, "--jobs", str(jobs), stale=True)
                      for jobs, found in ((1, exits_on_import), (2, exits_in_apply))}
            doubled = {jobs: bench_run(f"twice-{jobs}", "--variant", twice, "--jobs", str(jobs)) for jobs in (1, 2)}
            edited = {jobs: edited_run(f"edited-{jobs}", jobs) for jobs in (1, 2)}
            # A model is dropped by its file's date; reading it leaves the date.
            pruned = os.path.join(workdir, "pruned-cache")
            bench_run("pruned-today", models=pruned)
            todays = set(os.listdir(pruned))
            bench_run("pruned-variant", "--variant", planted, models=pruned)
            theirs = set(os.listdir(pruned)) - todays
            aged(pruned, todays | theirs)
            bench_run("pruned-variant-again", "--variant", planted, models=pruned)
            kept_both = still_aged(pruned)
            bench_run("pruned-plain", models=pruned)
            kept_plain, left = still_aged(pruned), set(os.listdir(pruned))
            processors, caps = os.cpu_count, {}
            try:
                for cpus in (64, 4):
                    os.cpu_count = lambda cpus=cpus: cpus
                    caps[cpus] = (bench.jobs_allowed(62), bench.jobs_allowed(2),
                                  bench_run(f"capped-{cpus}", "--jobs", "62", "--cutoff", "day", "--since",
                                            "2026-07-20", "--until", "2026-07-21"))
            finally:
                os.cpu_count = processors
            spread = bench_run("spread", "--jobs", "3", "--since", "2026-07-20", "--until", "2026-07-21")
            agrees = stand_in.compare

            def refuses(a, b):
                raise ValueError("pages: the variant did not read 2 of the 3 boards the base read "
                                 "(--unchecked pages to compare all the same)")

            stand_in.compare = refuses
            try:
                not_compared = bench_run("not-compared", "--variant", identity)
            finally:
                stand_in.compare = agrees
        finally:
            if saved_module is None:
                sys.modules.pop("analysis.bench_compare", None)
            else:
                sys.modules["analysis.bench_compare"] = saved_module
            if saved_attr is None:
                if hasattr(analysis, "bench_compare"):
                    del analysis.bench_compare
            else:
                analysis.bench_compare = saved_attr
        today = one[1][0]
        check("two processes give the file one gives: the same forecasts in the same order, the same counts",
              (0, 0, True, True, True),
              (one[0], two[0], len(today["pairs"]) > 5, today["replays"]["cups"] > 0, plain(two[1][0]) == plain(today)))
        with db.session(bench_db) as conn:
            # A week on, a cut of more cups than any cut before it; in it, the
            # cup entered first starts last.
            edition(conn, "Bench Cup", "EU", "2026-07-27", 1.0, hour=20)
            edition(conn, "Morning Cup", "EU", "2026-07-27", 1.0)
            edition(conn, "Morning Cup", "NAC", "2026-07-27", 1.0)
        with db.session(bench_db) as conn:
            alone_run, shared_run = (bench.cold(conn, "2026-07-13", "2026-07-28", catalogue, None, "k", progress=False,
                                                jobs=jobs, database=(bench_db, False)) for jobs in (1, 3))
            started = {row[0]: row[1] for row in conn.execute("SELECT id, start_time FROM competition")}
        cups_of = {}
        for p in alone_run["pairs"]:
            cups_of.setdefault(p["model"], set()).add(p["id"])
        sizes = [alone_run["models"][cut]["cups"] for cut in sorted(cups_of)]
        check("the cuts priced hold one of more cups than a cut priced before it, and one whose cups did not "
              "start in the order of their ids",
              (True, True),
              (any(size > min(sizes[:n]) for n, size in enumerate(sizes) if n),
               any(sorted(ids, key=lambda i: (started[i], i)) != sorted(ids) for ids in cups_of.values())))
        check("and so do three on the cups of the list, cut by cut",
              (True, True),
              (len(alone_run["pairs"]) > 20 and len(alone_run["models"]) > 3,
               all(alone_run[k] == shared_run[k] for k in ("pairs", "skipped", "refused", "models", "cups",
                                                           "replays", "bands", "model_key"))))
        order = [(p["model"], started[p["id"]], p["id"], p["rank"]) for p in alone_run["pairs"]]
        check("a cut prices its cups in the order they started, then by id, each rank in turn, and three "
              "processes keep that order",
              (True, True, True),
              (any(len(ids) > 1 for ids in cups_of.values()), order == sorted(order),
               [(p["model"], p["id"], p["rank"]) for p in shared_run["pairs"]]
               == [(p["model"], p["id"], p["rank"]) for p in alone_run["pairs"]]))
        check("a variant that changes nothing: today's run as a plain run writes it, the variant's the same "
              "forecasts and the same fields, named and dated by its file, its models under its own key",
              (0, 2, True, True, {"path": os.path.basename(identity), "sha1": bench.Variant(identity).sha1},
               ["model_key", "variant"]),
              (same[0], len(same[1]), plain(same[1][0]) == plain(today),
               same[1][1]["pairs"] == today["pairs"], same[1][1].get("variant"),
               sorted(k for k in set(same[1][0]) | set(same[1][1])
                      if k != "generated" and same[1][0].get(k) != same[1][1].get(k))))
        before, after = moved[1]
        ratios = {round(a["forecast"] / b["forecast"], 9) for a, b in zip(after["pairs"], before["pairs"])}
        check("a variant planted one percent high is one percent high on every forecast, its ranges as wide",
              (True, {1.01}, True),
              ([(p["window"], p["rank"]) for p in after["pairs"]]
               == [(p["window"], p["rank"]) for p in before["pairs"]],
               ratios, all(abs(a["rel"] - b["rel"]) < 1e-9 for a, b in zip(after["pairs"], before["pairs"]))))
        check("and two processes give the variant's run one gives",
              (True, True), (plain(moved_one[1][1]) == plain(after), plain(moved_one[1][0]) == plain(today)))
        check("the two runs go to the comparison, today's first",
              (True, None, os.path.basename(planted), "compared"),
              (compared == 3, handed[1][0].get("variant"), handed[1][1]["variant"]["path"],
               moved[2].strip().splitlines()[-1].split()[0]))
        check("a variant that says it leaves the models alone and does not is refused, today's run alone "
              "written",
              (2, 1, True), (refused[0], len(refused[1]), plain(refused[1][0]) == plain(today)))
        check("a variant that makes its replacement as it is imported leaves today's run as a plain run writes "
              "it, in one process or two, and is one percent high in its own",
              (0, 0, True, True, {1.01}, {1.01}),
              (early[1][0], early[2][0], plain(early[1][1][0]) == plain(today), plain(early[2][1][0]) == plain(today),
               ratios_of(early[1]), ratios_of(early[2])))
        check("a variant that fails leaves today's run written as a plain run writes it, says why, and ends "
              "with an error, in one process or two",
              (1, 1, 1, 1, True, True, True),
              (failed[1][0], failed[2][0], len(failed[1][1]), len(failed[2][1]),
               plain(failed[1][1][0]) == plain(today), plain(failed[2][1][0]) == plain(today),
               all("The variant's run failed (RuntimeError: this variant fails)" in run[2]
                   for run in failed.values())))
        check("a variant that leaves the process, as it is run or in apply(), is caught the same: today's run "
              "written, no file of the variant's left from an earlier run, an error",
              (1, 1, [1, 1], True, True, True),
              (exited[1][0], exited[2][0], [len(run[1]) for run in exited.values()],
               plain(exited[1][1][0]) == plain(today), plain(exited[2][1][0]) == plain(today),
               all("The variant's run failed (SystemExit: 0)" in run[2] for run in exited.values())))
        check("a variant whose apply() puts nothing back is applied once, in one process or two: one percent "
              "high, and no replacement left in this process after any run",
              (0, 0, {1.01}, {1.01}, []),
              (doubled[1][0], doubled[2][0], ratios_of(doubled[1]), ratios_of(doubled[2]), left_behind))
        first_sha1 = hashlib.sha1(planted_body.encode()).hexdigest()
        check("a variant's file edited during the run: the text read as it began runs, in one process or two, "
              "under the SHA-1 written",
              ([0, 0], [True, True], [first_sha1, first_sha1], [{1.01}, {1.01}]),
              ([run[0] for run, _ in edited.values()], [after != first_sha1 for _, after in edited.values()],
               [run[1][1]["variant"]["sha1"] for run, _ in edited.values()],
               [ratios_of(run) for run, _ in edited.values()]))
        check("a run with a variant drops no model, today's or its own; a plain run drops the old ones of "
              "other keys",
              (True, True, True, True),
              (bool(todays) and bool(theirs), todays | theirs <= kept_both, todays <= kept_plain,
               not theirs & left))
        windows = sys.platform == "win32"
        pool = (61, "the most this system runs in one pool") if windows else (62, "")
        check("more processes than the system's pool takes, or than it has processors, are cut down to what "
              "it takes, and the run says so",
              (pool, (2, ""), 0, windows, (4, "the processors this system has"), 0, True),
              (caps[64][0], caps[64][1], caps[64][2][0],
               "--jobs 62: 61 processes at most, the most this system runs in one pool" in caps[64][2][2],
               caps[4][0], caps[4][2][0],
               "--jobs 62: 4 processes at most, the processors this system has" in caps[4][2][2]))
        cutoffs = len(spread[1][0]["models"])
        check("a run starts no more processes than it has cutoffs, and says how many",
              (0, 2, True),
              (spread[0], cutoffs, f"{min(3, cutoffs, os.cpu_count() or 1)} processes for {cutoffs} cutoffs, "
               "each with its own read-only connection" in spread[2]))

        # The variant's models: its own, keyed on its source, unless it leaves
        # them alone; then today's are read, none built.
        keys = [bench.Variant(planted).key("k")]
        variant_file("planted.py", planted_body + "# another line\n")
        keys.append(bench.Variant(planted).key("k"))
        check("the variant's models are kept under a key that moves with its source, but for one that leaves "
              "them alone", (3, "k"), (len(set(keys) | {"k"}), bench.Variant(pricing_only).key("k")))
        kept = rescore.RAW
        rescore.RAW = pages
        try:
            with db.session(replay_db) as conn:
                runs = {name: bench.cold(conn, "2026-07-13", "2026-07-21", {}, cache, "k", progress=False,
                                         jobs=jobs, database=(replay_db, False),
                                         variant=bench.Variant(found) if found else None)
                        for name, found, jobs in (("plain", None, 1), ("pricing", pricing_only, 1),
                                                  ("model", planted, 2))}
        finally:
            rescore.RAW = kept
        check("a pricing-only variant reads today's models; one that may change them builds its own",
              (True, 0, True, True),
              (runs["plain"]["built"] + runs["plain"]["read"] > 0, runs["pricing"]["built"],
               runs["pricing"]["model_key"] == runs["plain"]["model_key"],
               runs["model"]["built"] > 0 and runs["model"]["model_key"] != runs["plain"]["model_key"]))
        missing = dict(module=sys.modules.get("analysis.bench_compare"), attr=getattr(analysis, "bench_compare", None))
        sys.modules["analysis.bench_compare"] = None
        if missing["attr"] is not None:
            del analysis.bench_compare
        try:
            alone = bench_run("alone", "--variant", identity)
        finally:
            if missing["module"] is None:
                sys.modules.pop("analysis.bench_compare", None)
            else:
                sys.modules["analysis.bench_compare"] = missing["module"]
            if missing["attr"] is not None:
                analysis.bench_compare = missing["attr"]
        check("without bench_compare the two runs are still written, and the run says why nothing was compared",
              (0, 2, True), (alone[0], len(alone[1]), "bench_compare.py is not here" in alone[2]))

        # The model the page served: the rows the database held when its
        # model.json was committed. The European edition of the 13th was only
        # caught up with on the 25th.
        served = ["2026-07-10 12:00:00", "2026-07-20 17:30:00"]
        with db.session(bench_db) as conn:
            conn.execute("UPDATE competition SET created_at = datetime(COALESCE(end_time, start_time), '+2 hours')")
            conn.execute("UPDATE competition SET created_at = '2026-07-25 10:00:00' "
                          "WHERE name = 'Bench Cup EU 2026-07-13'")
        check("a cup reads the model.json committed last before it starts; the database dates rows in Paris time",
              ("2026-07-20 17:30:00", "2026-07-20 19:30:00", "2026-12-01 18:30:00"),
              (bench.cutoff_of("2026-07-20 18:00:00", "published", served=served),
               bench.paris_of("2026-07-20 17:30:00"), bench.paris_of("2026-12-01 17:30:00")))
        dated = bench.CREATED_SINCE
        try:
            bench.CREATED_SINCE = "2026-07-12 00:00:00"
            with db.session(bench_db) as conn:
                shown = {jobs: bench.cold(conn, "2026-07-06", "2026-07-21", catalogue, None, "k", "published",
                                          progress=False, jobs=jobs, database=(bench_db, False), served=served)
                         for jobs in (1, 2)}
                updated = bench.cold(conn, "2026-07-20", "2026-07-21", catalogue, None, "k", progress=False)
                # The models it reads are kept under keys of their own, one
                # for each set of rows: a month old, they stay all the same.
                served_models = os.path.join(workdir, "served-cache")
                kept_served = []
                for again in (False, True):
                    if again:
                        aged(served_models, os.listdir(served_models))
                    kept_served.append(bench.cold(conn, "2026-07-06", "2026-07-21", catalogue, served_models, "k",
                                                  "published", progress=False, served=served))
                    kept_served[-1]["files"] = set(os.listdir(served_models))
        finally:
            bench.CREATED_SINCE = dated
        page = next((p for p in shown[1]["pairs"] if (p["family"], p["region"], p["day"], p["rank"])
                     == ("Bench Cup", "EU", "2026-07-20", 100)), {})
        daily = next((p for p in updated["pairs"] if (p["family"], p["region"], p["day"], p["rank"])
                      == ("Bench Cup", "EU", "2026-07-20", 100)), {})
        check("the page's model leaves out a row the database only had later, which the daily rule reads",
              ("2026-07-20 17:30:00", round(base[100], 1), True),
              (page.get("model"), page.get("forecast"), daily.get("forecast") != page.get("forecast")))
        skipped = shown[1]["skipped"]
        check("a cup before the first model served, or served one built before rows were dated, is counted out",
              (True, True, True),
              (skipped["cups started before the first model the page served"] > 0,
               skipped["cups the page priced from a model built before the database dated its rows"] > 0,
               all(shown[1][k] == shown[2][k] for k in ("pairs", "skipped", "refused", "models", "replays"))))
        check("a published run keeps the models it read, though a month old and of keys not today's",
              (True, 0, True, True, True),
              (kept_served[0]["built"] > 0, kept_served[1]["built"], kept_served[1]["read"] == kept_served[0]["built"],
               kept_served[1]["files"] == kept_served[0]["files"] and bool(kept_served[1]["files"]),
               still_aged(served_models) == kept_served[1]["files"]))
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = bench.main(["--cold", "--cutoff", "published", "--ladder", os.path.join(workdir, "no-ladder"),
                               "--db", bench_db, "--catalogue", os.path.join(workdir, "no-catalogue"),
                               "--cache", "none", "--since", "2026-07-06", "--until", "2026-07-21"])
        check("without the page's history, a published run measures nothing and says why",
              (2, True), (code, "No history of model.json" in printed.getvalue()))
        check("two runs of one launch the comparison refuses are both written, and the run says why and what to add",
              (2, 2, True, True),
              (not_compared[0], len(not_compared[1]), "not set against each other" in not_compared[2],
               "--unchecked pages compares them" in not_compared[2]))
        refusals = []
        for extra in (["--live", "--cutoff", "published"], ["--live", "--jobs", "2"], ["--cold", "--arrival", "stamp"],
                      ["--cold", "--old-cadence"], ["--cold", "--skip-outages"]):
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = bench.main([*extra, "--db", bench_db, "--catalogue", os.path.join(workdir, "no-catalogue"),
                                   "--cache", "none", "--since", "2026-07-06", "--until", "2026-07-21"])
            refusals.append((code, "Nothing measured" in printed.getvalue()))
        check("the live bench refuses the cold bench's --cutoff published and --jobs, and the cold bench the live "
              "one's --arrival, --old-cadence and --skip-outages, rather than drop them", [(2, True)] * 5, refusals)

    print("\n20. Two runs of the bench, forecast against forecast")
    import importlib.util
    if any(importlib.util.find_spec(p) is None for p in ("numpy", "pandas", "scipy")):
        print("   (skipped: analysis/ needs its own requirements, see analysis/requirements.txt)")
    else:
        import copy
        import random
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from analysis import bench_compare

        def bench_pair(window, rank, day, region, error, result=None, **more):
            result = result or round(400 * rank ** -0.25, 1)
            return dict({"window": window, "rank": rank, "day": day, "region": region, "season": 42,
                         "forecast": result * (1 + error / 100), "result": result, "rel": 0.2,
                         "bands": {"50": [-0.5, 0.5], "90": [-1.5, 1.5]}, "source": "previous edition",
                         "at_cut": rank == 100, "id": 0, "model": f"{day} 16:00:00"}, **more)

        def bench_run(pairs, **more):
            run = {"bench": "cold", "since": "2026-07-01", "until": "2026-07-29", "cutoff": "update",
                   "update": {"paris": ["18:00"], "online_after_minutes": 30},
                   "database": {"sha1": "a" * 40}, "catalogue": {"sha1": "b" * 40},
                   "leaderboards": {"events": 2}, "replays": {"boards": ["E1|W1|20|0badf00d", "E2|W2|35|feedface"]},
                   "bands": {"generated": "2026-06-30", "from": "2026-06-01"},
                   "ranks": [10, 25, 100, 250, 500, 1000], "pairs": pairs}
            run.update(more)
            return run

        # Four weeks of cups in three regions, every forecast a little high.
        draw = random.Random(20)
        cups = [bench_pair(f"Cup{d}|{region}", rank, f"2026-07-{1 + d:02d}", region, 0.5 + abs(draw.gauss(4, 6)))
                for d in range(28) for region in ("EU", "NAC", "OCE") for rank in (10, 100, 500, 1000)]
        base_run = bench_run(cups)

        def moved(run, how):
            return dict(run, pairs=[dict(p, forecast=how(p)) for p in run["pairs"]])

        def worse_by_a_point(p):
            return p["forecast"] + 0.01 * p["result"] * (1 if p["forecast"] >= p["result"] else -1)

        itself = bench_compare.compare(base_run, copy.deepcopy(base_run))
        check("a run against itself: every change 0, its interval [0, 0], and no change",
              ("no change", 0, 0.0, [0.0, 0.0], True),
              (itself["verdict"], itself["moved"], itself["overall"]["all"]["change"]["abs_mean"],
               itself["interval"]["abs_mean"],
               json.dumps(itself) == json.dumps(bench_compare.compare(base_run, copy.deepcopy(base_run)))))

        def judged(base, variant, **more):
            """The verdict of two runs, or what refused them."""
            try:
                return bench_compare.compare(base, variant, draws=0, **more)["verdict"]
            except bench_compare.SnapshotMismatch as refusal:
                return f"refused: {refusal}"

        refused = {}
        for name, other in (("database", {"database": {"sha1": "c" * 40}}),
                            ("-wal", {"database": {"sha1": "a" * 40, "wal_sha1": "c" * 40}}),
                            ("bench", {"bench": "other"}), ("catalogue", {"catalogue": {"sha1": "d" * 40}}),
                            ("span", {"since": "2026-07-02"}), ("ranks", {"ranks": [10, 25, 100, 250, 500]}),
                            ("pages", {"replays": {"boards": ["E1|W1|20|0badf00d", "E2|W2|35|00000000"]}}),
                            ("pages on disk", {"leaderboards": {"events": 5}}),
                            ("cutoff", {"cutoff": "day"}),
                            ("update", {"update": {"paris": ["16:00"], "online_after_minutes": 30}}),
                            ("bands", {"bands": {"generated": "2026-06-23", "from": "2026-06-01"}}),
                            ("no catalogue", {"catalogue": {"windows": 10}}), ("arrival", {"arrival": "feed"})):
            refused[name] = judged(base_run, bench_run(cups, **other)).startswith("refused")
        more_boards = bench_run(cups, replays={"boards": base_run["replays"]["boards"] + ["E3|W3|12|12345678"]})
        unchecked = bench_compare.compare(base_run, bench_run(cups, catalogue={"windows": 10}),
                                          unchecked=("catalogue",))
        check("two snapshots are refused: database, -wal, bench, catalogue, span, ranks, a page read, the pages on "
              "disk, the rule, its hour, the ranges, a run that does not name its catalogue or its arrival",
              {name: True for name in refused}, refused)
        check("a variant may read more boards, list its ranks in another order, and a catalogue left unchecked "
              "is said in the verdict",
              ("no change", "no change", ["catalogue (not in the variant)"]),
              (judged(base_run, more_boards), judged(base_run, bench_run(cups, ranks=[1000, 500, 250, 100, 25, 10])),
               unchecked["snapshot"]["unchecked"]))
        check("a run's rule of arrival: the same on both sides compares, two rules are refused",
              ("no change", True),
              (judged(bench_run(cups, arrival="feed"), bench_run(cups, arrival="feed")),
               judged(bench_run(cups, arrival="stamp"), bench_run(cups, arrival="feed")).startswith("refused")))

        # The live bench keeps its forecasts under "forecasts", one per cup,
        # rank and point: two live runs are not paired, they are refused.
        def live_run(run):
            live = {k: v for k, v in run.items() if k != "pairs"}
            return dict(live, bench="live", arrival="feed",
                        forecasts=[dict(p, point=point) for p in run["pairs"] for point in ("20 %", "50 %")])

        live = {}
        for name, runs in (("the variant live", (base_run, live_run(base_run))),
                           ("the base live", (live_run(base_run), base_run)),
                           ("forecasts and no pairs", (base_run, {k: v for k, v in live_run(base_run).items()
                                                                  if k != "bench"}))):
            try:
                live[name] = judged(*runs)
            except ValueError as error:
                live[name] = str(error)
        both = bench_compare.compare(live_run(base_run), live_run(base_run), draws=0)
        check("two live runs are compared point by point; a live run against a cold one is refused, either side",
              ({name: True for name in live}, {"20 %": "no change", "50 %": "no change"}),
              ({name: "comes from the live bench" in told for name, told in live.items()}, both["verdicts"]))

        # Each pair aims at one result: a variant that moved a result, or a
        # cup's id, day, region or cut, would be judged against another truth.
        aimed = {}
        for name, how in (("result", lambda p: dict(p, result=p["result"] * 1.1)), ("id", lambda p: dict(p, id=7)),
                          ("day", lambda p: dict(p, day="2026-07-02")), ("region", lambda p: dict(p, region="ASIA")),
                          ("at_cut", lambda p: dict(p, at_cut=not p["at_cut"]))):
            told = judged(base_run, bench_run([how(p) if n in (3, 5) else p for n, p in enumerate(cups)]))
            aimed[name] = (told.startswith("refused") and "2 of the 336 paired forecasts" in told
                           and f"({name}: 2)" in told and "first Cup0|EU at rank 1000" in told)
        aimed["a result 1e-12 off"] = judged(base_run, bench_run([dict(p, result=p["result"] + 1e-12)
                                                                  for p in cups])) == "no change"
        aimed["a result 1e-6 off"] = judged(base_run, bench_run([dict(p, result=p["result"] + 1e-6)
                                                                 if n == 7 else p for n, p in enumerate(cups)])) \
            .startswith("refused: 1 of the 336 paired forecasts")
        check("a pair must aim at the same thing on both sides: another result, id, day, region or cut is refused, "
              "with how many pairs and the first", {name: True for name in aimed}, aimed)

        # Pages: a variant that did not read every board the base read is
        # refused, unless told not to check them, and the verdict then says so.
        one_read = bench_run(cups, replays={"boards": base_run["replays"]["boards"][:1]})
        none_read = bench_run(cups, replays={"boards": []})
        one_and_another = bench_run(cups, replays={"boards": base_run["replays"]["boards"][:1] + ["E9|W9|20|0badf00d"]})
        subset = bench_compare.compare(base_run, one_read, draws=0, unchecked=("pages",))
        check("a variant that did not read every board of the base is refused, unless the pages are left unchecked, "
              "and the verdict then says how many it did not read",
              (True, True, True, "no change", "no change", 1,
               "verdict: NO CHANGE (not checked: pages (the variant did not read 1 of the 2 boards the base read))"),
              (judged(base_run, none_read).startswith("refused"), judged(base_run, one_read).startswith("refused"),
               judged(base_run, one_and_another).startswith("refused"),
               judged(base_run, none_read, unchecked=("pages",)), subset["verdict"],
               subset["snapshot"]["boards of the base not read by the variant"], bench_compare.verdict_line(subset)))

        planted = bench_compare.compare(base_run, moved(base_run, lambda p: p["forecast"] * 1.01))
        shift = sum(p["forecast"] / p["result"] for p in cups) / len(cups)
        low, high = planted["interval"]["signed_mean"]
        check("a variant planted 1 % higher everywhere is found: its shift, an interval clear of 0, and a loss",
              (round(shift, 9), True, True, "loss"),
              (round(planted["overall"]["all"]["change"]["signed_mean"], 9), low > 0,
               planted["interval"]["abs_mean"][0] > 0, planted["verdict"]))
        apart = bench_compare.compare(base_run, moved(base_run, worse_by_a_point))
        check("and one a point worse on every forecast is a point worse, to the draw",
              (1.0, [1.0, 1.0], "loss"),
              (round(apart["overall"]["all"]["change"]["abs_mean"], 9),
               [round(x, 9) for x in apart["interval"]["abs_mean"]], apart["verdict"]))
        replanted = bench_compare.compare(base_run, moved(base_run, lambda p: p["forecast"] * 1.01))["interval"]
        reseeded = bench_compare.compare(base_run, moved(base_run, lambda p: p["forecast"] * 1.01), seed=1)["interval"]
        check("the draws come from a fixed seed: the same interval every time, another one with another seed",
              (True, True), (replanted == planted["interval"], reseeded["abs_mean"] != planted["interval"]["abs_mean"]))
        only_fewer = bench_compare.compare(base_run, bench_run(cups[1:]), draws=0)
        check("a variant that only leaves forecasts out is never 'no change'",
              ("no gain", 0, 1), (only_fewer["verdict"], only_fewer["moved"], only_fewer["unpaired"]["base only"]))
        ranges = {"ranges": [dict(p, bands={"50": [-0.25, 0.25], "90": [-1.5, 1.5]}) for p in cups],
                  "spread": [dict(p, rel=0.1) for p in cups]}
        ranged = {name: bench_compare.compare(base_run, bench_run(pairs), draws=0) for name, pairs in ranges.items()}
        check("nor is a variant that only changes the ranges: its forecasts count as moved",
              {name: ("no gain", 336) for name in ranges},
              {name: (found["verdict"], found["moved"]) for name, found in ranged.items()})
        # One cup of twenty a point worse, the others as they were: the cups
        # drawn hold it k times, k binomial (20, 1/20), so the change drawn is
        # k / 20 point. P(k <= 2) = 0.925 and P(k <= 3) = 0.984: the 95th
        # percentile is 3 cups, the 90th 2.
        twenty = bench_run([bench_pair(f"Twenty|{n:02d}", rank, f"2026-07-{n + 1:02d}", "EU", 5.0)
                            for n in range(20) for rank in (10, 100)])
        one_worse = bench_compare.compare(twenty, moved(twenty, lambda p: worse_by_a_point(p)
                                                        if p["window"] == "Twenty|00" else p["forecast"]))
        check("the interval is the 90 % one: one cup of twenty a point worse puts it at [0, 3/20]",
              (0.9, [0.0, 0.15], 0.05), (one_worse["interval"]["level"],
                                         [round(x, 9) for x in one_worse["interval"]["abs_mean"]],
                                         round(one_worse["overall"]["all"]["change"]["abs_mean"], 9)))
        # 30 % over turned into 27 % under: closer, yet further in log.
        over = bench_run([dict(p, forecast=p["result"] * 1.3) for p in cups])
        under = bench_compare.compare(over, moved(over, lambda p: p["result"] * 0.73))
        check("an error that shrinks but grows in log is no gain, failed on the log and on the halves",
              ("no gain", ["mean log error down", "in both halves: mean |error| and log error down, median not up"],
               True),
              (under["verdict"], under["failed"], under["interval"]["abs_mean"][1] < 0))
        # Better in the first half of the span, worse in the second.
        halves = bench_compare.compare(base_run, moved(base_run, lambda p: (p["result"] + p["forecast"]) / 2
                                                       if p["day"] < "2026-07-15" else worse_by_a_point(p)))
        check("the halves split the span in its middle; a change better in one and worse in the other fails them",
              ([("2026-07-01", "2026-07-15", 168), ("2026-07-15", "2026-07-29", 168)], True, True, False),
              ([(h["since"], h["until"], h["pairs"]) for h in halves["halves"]],
               halves["halves"][0]["change"]["abs_mean"] < 0, halves["halves"][1]["change"]["abs_mean"] > 0,
               next(c["holds"] for c in halves["conditions"] if "halves" in c["rule"])))
        deep = bench_compare.compare(base_run, moved(base_run, lambda p: p["result"] + (p["forecast"] - p["result"]) / 4
                                                     if p["rank"] <= 250 else worse_by_a_point(p)))
        check("a gain everywhere but past rank 250, a point worse there, fails on that rank band alone",
              (["no rank band worse by more than 0.5 point"], "251+ +1.00"),
              (deep["failed"], next((c["detail"] for c in deep["conditions"] if c["rule"].startswith("no rank band")),
                                    None)))

        # A cup's forecasts err together, so the cups are drawn, not the
        # forecasts: two cups of forty, one a point worse and one a point
        # better, leave the change anywhere from -1 to +1.
        two = bench_run([bench_pair(f"Pair|{n}", rank, "2026-07-0" + str(n + 1), "EU", 5.0)
                         for n in range(2) for rank in range(10, 410, 10)])
        drawn = bench_compare.compare(two, moved(two, lambda p: p["forecast"] + 0.01 * p["result"] * (
            1 if p["window"] == "Pair|0" else -1)))
        check("the draws are of cups: two cups, a point worse and a point better, give an interval of [-1, +1], "
              "no gain and no loss",
              (0.0, [-1.0, 1.0], "no gain"), (round(drawn["overall"]["all"]["change"]["abs_mean"], 9),
                                              [round(x, 9) for x in drawn["interval"]["abs_mean"]], drawn["verdict"]))

        def better_but_oceania(p):
            if p["region"] == "OCE":
                return worse_by_a_point(p)
            return p["result"] + (p["forecast"] - p["result"]) / 2

        better = bench_compare.compare(base_run, moved(base_run, lambda p: better_but_oceania(dict(p, region="EU"))))
        one_region = bench_compare.compare(base_run, moved(base_run, better_but_oceania))
        check("a gain passes the rule; the same gain with one region a point worse fails on that region alone",
              ("gain", "no gain", ["no region (30+ forecasts, 20+ cups) worse by more than 0.5 point"], "OCE +1.00"),
              (better["verdict"], one_region["verdict"], one_region["failed"],
               next(c["detail"] for c in one_region["conditions"] if c["rule"].startswith("no region"))))
        # Forecasts 25 % over results of 64 points, then 26 % over on half of
        # Oceania's and 12.5 % elsewhere: Oceania exactly half a point worse.
        sixty_four = bench_run([dict(p, result=64.0, forecast=80.0) for p in cups])
        edge = bench_compare.compare(sixty_four, moved(sixty_four, lambda p: p["result"] * (
            (1.26 if p["rank"] in (10, 1000) else 1.25) if p["region"] == "OCE" else 1.125)), draws=0)
        check("a region exactly 0.5 point worse is not worse by more than 0.5 point",
              (0.5, True, "none"), (edge["tables"]["region"]["OCE"]["change"]["abs_mean"],
                                    *next((c["holds"], c["detail"]) for c in edge["conditions"]
                                          if c["rule"].startswith("no region"))))
        # A long run: three seasons, the change judged by the most of them.
        seasons = bench_run([dict(p, season=40 if p["day"] < "2026-07-11" else 41 if p["day"] < "2026-07-21" else 42)
                             for p in cups])

        def by_seasons(run, gaining, losing=worse_by_a_point):
            found = bench_compare.compare(run, moved(run, lambda p: p["result"] + (p["forecast"] - p["result"]) / 2
                                                     if p["season"] in gaining else losing(p)), draws=0)
            return next((c["holds"], c["detail"].split(" (")[0]) for c in found["conditions"] if "seasons" in c["rule"])

        # Four weeks, four seasons: two of four is not most of them.
        weekly = bench_run([dict(p, season=40 + (int(p["day"][8:]) - 1) // 7) for p in cups])
        # Errors of 2, 4, 6 and 40 %: the big one halved, the others a point worse. The mean falls, the
        # median rises, and a season that only lowers its mean is not a season the change helps.
        lumpy = bench_run([dict(p, forecast=p["result"] * (1 + {10: 2, 100: 4, 500: 6, 1000: 40}[p["rank"]] / 100))
                           for p in seasons["pairs"]])
        check("over several seasons, the median and the log error have to fall in most of them, the mean alone "
              "does not count",
              ((True, "2 of 3 seasons with 20+ cups"), (False, "1 of 3 seasons with 20+ cups"),
               (False, "2 of 4 seasons with 20+ cups"), (False, "1 of 3 seasons with 20+ cups")),
              (by_seasons(seasons, {40, 41}), by_seasons(seasons, {42}), by_seasons(weekly, {40, 41}),
               by_seasons(lumpy, {42}, lambda p: p["result"] * (1 + {10: 3, 100: 5, 500: 7, 1000: 20}[p["rank"]]
                                                                / 100))))
        dropped = bench_compare.compare(base_run, moved(bench_run(cups[1:]),
                                                        lambda p: better_but_oceania(dict(p, region="EU"))))
        check("a variant that leaves a forecast out is not a gain",
              ("no gain", 1, ["every forecast of the base priced"]),
              (dropped["verdict"], dropped["unpaired"]["base only"], dropped["failed"]))

        # The strawman: a cup of the same group still running at the update
        # (started before the cutoff, over after it) leans +50 %; the cup over
        # before it, 0 %. Read walk-forward, the next cup is left as it was.
        times = {1: {"start_time": "2026-07-01 18:00:00", "end_time": "2026-07-01 21:00:00"},
                 2: {"start_time": "2026-07-02 15:00:00", "end_time": "2026-07-02 18:00:00"},
                 3: {"start_time": "2026-07-02 19:00:00", "end_time": "2026-07-02 22:00:00"},
                 4: {"start_time": "2026-07-04 19:00:00", "end_time": "2026-07-04 22:00:00"}}
        straw_pairs = ([bench_pair("Old|EU", r, "2026-07-01", "EU", 0.0, id=1, model="2026-06-30 16:00:00")
                        for r in (10, 100)]
                       + [bench_pair("Late|EU", r, "2026-07-02", "EU", 50.0, id=2, model="2026-07-01 16:00:00")
                          for r in (10, 25, 100, 250)]
                       + [bench_pair("Next|EU", 10, "2026-07-02", "EU", 20.0, id=3, model="2026-07-02 16:00:00"),
                          bench_pair("Then|EU", 10, "2026-07-04", "EU", 20.0, id=4, model="2026-07-04 16:00:00")])

        def walk_forward(found):
            """(the next cup left as it was, no forecast corrected by a cup over after its model)."""
            after = {(p["window"], p["rank"]): p for p in found["pairs"]}
            return (after[("Next|EU", 10)]["forecast"] == after[("Next|EU", 10)]["strawman"]["before"],
                    all(p["strawman"]["latest"] <= p["model"] for p in found["pairs"] if p["strawman"]["n"]))

        straw = bench_compare.strawman(bench_run(straw_pairs), times)
        then = next(p for p in straw["pairs"] if p["window"] == "Then|EU")
        # Two days on, the seven forecasts of the three cups over are in: median +50, shrunk by 7 / 57.
        check("the strawman reads only the cups over before the model, and shrinks the median by n / (n + 50)",
              ((True, True), 7, round(then["strawman"]["before"] / (1 + 50 * 7 / 57 / 100), 9)),
              (walk_forward(straw), then["strawman"]["n"], round(then["forecast"], 9)))
        kept = bench_compare.over_before
        try:
            bench_compare.over_before = lambda cup, cutoff: str(cup["start_time"])[:19] <= cutoff
            leaky = bench_compare.strawman(bench_run(straw_pairs), times)
        finally:
            bench_compare.over_before = kept
        check("and a strawman that reads a cup started before the cutoff but over after it is caught",
              (False, False), walk_forward(leaky))
        # Its groups: EU leans +10 % to rank 250 and +30 % past it, NAC the opposite way.
        lean = {("EU", 10): 10, ("EU", 25): 10, ("EU", 500): 30, ("EU", 1000): 30}
        lean.update({("NAC", rank): -value for (_, rank), value in lean.items()})
        learned = [bench_pair(f"Learn|{region}", rank, "2026-07-01", region, value, id=1 if region == "EU" else 2,
                              model="2026-06-30 16:00:00") for (region, rank), value in lean.items()]
        target = [bench_pair("Target|EU", rank, "2026-07-03", "EU", 0.0, id=3, model="2026-07-02 16:00:00")
                  for rank in (10, 500)]
        over_by = {1: {"start_time": "2026-07-01 18:00:00", "end_time": "2026-07-01 21:00:00"},
                   2: {"start_time": "2026-07-01 23:00:00", "end_time": "2026-07-02 02:00:00"},
                   3: {"start_time": "2026-07-03 18:00:00", "end_time": "2026-07-03 21:00:00"}}
        grouped = {}
        for regions in (True, False):
            found = bench_compare.strawman(bench_run(learned + target), over_by, regions=regions)
            grouped[regions] = {p["rank"]: p["strawman"] for p in found["pairs"] if p["window"] == "Target|EU"}
        check("the strawman's groups are the rung, the rank band (to 250, past it) and, unless left out, the region",
              ((10.0, 30.0, 2), (0.0, 0.0, 4)),
              tuple((round(grouped[r][10]["median"], 9) + 0.0, round(grouped[r][500]["median"], 9) + 0.0,
                     grouped[r][10]["n"]) for r in (True, False)))

        # The command: what it cannot read is said, with a code of 2.
        import contextlib
        folder = scratch("bench-compare-", dir=workdir)

        def written(name, run):
            path = os.path.join(folder, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(run if isinstance(run, str) else json.dumps(run))
            return path

        def command(argv, entry=bench_compare.main):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    code = entry(argv)
                except (Exception, SystemExit) as error:  # what escapes the command fails the check, said as such
                    code = f"{type(error).__name__}: {error}"
            return code, out.getvalue()

        base_file = written("base.json", base_run)
        bad = {"no file": [base_file, os.path.join(folder, "missing.json")],
               "not JSON": [base_file, written("broken.json", "{not json")],
               "not a run": [base_file, written("list.json", [1, 2])],
               "a day badly written": [base_file, base_file, "--since", "2026-7-9"],
               "a day in another ISO form": [base_file, base_file, "--since", "20260709"],
               "since after until": [base_file, base_file, "--since", "2026-07-20", "--until", "2026-07-10"],
               "a forecast twice": [base_file, written("twice.json", bench_run(cups + cups[:1]))],
               "a forecast of 0": [base_file, written("zero.json", bench_run([dict(cups[0], forecast=0)] + cups[1:]))],
               "a forecast not a number": [base_file, written("nan.json", bench_run(
                   [dict(cups[0], forecast=float("nan"))] + cups[1:]))]}
        codes = {name: command(argv) for name, argv in bad.items()}
        check("a file, a day or a forecast that cannot be read is said plainly, with a code of 2",
              {name: (2, True) for name in bad},
              {name: (code, out.startswith("Cannot compare:")) for name, (code, out) in codes.items()})
        gap = written("gap.json", bench_run([p for p in cups if p["day"] != "2026-07-10"]))
        empty = {"since past the span": [base_file, base_file, "--since", "2026-07-29"],
                 "until before it": [base_file, base_file, "--until", "2026-07-01"],
                 "no cup in the days asked": [gap, base_file, "--since", "2026-07-10", "--until", "2026-07-11"],
                 "not one forecast in common": [base_file, written("elsewhere.json", bench_run(
                     [dict(p, window="Elsewhere" + p["window"]) for p in cups]))],
                 "a live run and a cold one": [written("live.json", live_run(base_run)), base_file]}
        empties = {name: command(argv) for name, argv in empty.items()}
        last_day = command([base_file, base_file, "--since", "2026-07-28"])
        check("nothing to pair is not compared: said plainly, a code of 2 and no verdict; an empty half is not printed",
              ({name: (2, True, False) for name in empty}, 0, False, True),
              ({name: (code, out.startswith("Cannot compare:"), "verdict" in out)
                for name, (code, out) in empties.items()},
               last_day[0], "2026-07-28 to 2026-07-27" in last_day[1], "2026-07-28 to 2026-07-28" in last_day[1]))
        two_live = command([os.path.join(folder, "live.json")] * 2)
        check("and live runs are said to be live; two of them are compared, point by point", (True, 0, True),
              ("comes from the live bench" in empties["a live run and a cold one"][1], two_live[0],
               "verdicts: 20 % NO CHANGE, 50 % NO CHANGE" in two_live[1]))
        said = {"a day badly written": (codes["a day badly written"][1], "not a day written YYYY-MM-DD: '2026-7-9'"),
                "a day in another ISO form": (codes["a day in another ISO form"][1],
                                              "not a day written YYYY-MM-DD: '20260709'"),
                "since after until": (codes["since after until"][1], "since 2026-07-20 is not before until 2026-07-10"),
                "since past the span": (empties["since past the span"][1],
                                        "since 2026-07-29 is not before the end of the runs' span (2026-07-29,"),
                "until before it": (empties["until before it"][1],
                                    "until 2026-07-01 is not after the start of the runs' span (2026-07-01)")}
        check("days that cannot hold a cup of the runs are said as such", {name: True for name in said},
              {name: told in out for name, (out, told) in said.items()})
        no_catalogue = written("no-catalogue.json", bench_run(cups, catalogue={"windows": 10}))
        forwarded = command(["--compare", base_file, no_catalogue, "--unchecked", "catalogue", "--since", "2026-07-10"],
                            bench_compare.bench.main)
        at_the_end = command([base_file, base_file, "--compare"], bench_compare.bench.main)
        check("bench --compare hands every other option to the comparison, wherever it stands on the line",
              (0, True, True, 2, 0, True),
              (forwarded[0], "NOT CHECKED: catalogue" in forwarded[1], "cups from 2026-07-10" in forwarded[1],
               command(["--compare", base_file, no_catalogue], bench_compare.bench.main)[0],
               at_the_end[0], "verdict: NO CHANGE" in at_the_end[1]))

        # The strawman, from the command: the run's database is read for the
        # cups' end times, and only the database the run was made from.
        import sqlite3
        cups_db = os.path.join(folder, "cups.db")
        conn = sqlite3.connect(cups_db)
        conn.execute("CREATE TABLE competition (id INTEGER PRIMARY KEY, start_time TEXT, end_time TEXT)")
        conn.executemany("INSERT INTO competition VALUES (?, ?, ?)",
                         [(cid, cup["start_time"], cup["end_time"]) for cid, cup in times.items()])
        conn.commit()
        conn.close()
        its_db = {"sha1": bench_compare.bench.file_sha1(cups_db)}
        straws = {"its database": bench_run(straw_pairs, database=its_db),
                  "another database": bench_run(straw_pairs),
                  "a forecast without its model": bench_run([{k: v for k, v in p.items() if k != "model"}
                                                             for p in straw_pairs], database=its_db)}
        told = {name: command(["--strawman", written(f"straw-{n}.json", run), "--db", cups_db])
                for n, (name, run) in enumerate(straws.items())}
        check("--strawman reads the run's own database only, and a run it cannot read is said, with a code of 2",
              {"its database": (0, True), "another database": (2, True), "a forecast without its model": (2, True)},
              {name: (code, (("verdict:" in out) if name == "its database" else
                             "is not the one the run was made from" in out if name == "another database" else
                             out.startswith("Cannot compare:")))
               for name, (code, out) in told.items()})

    print("\n21. The list's history: a cup's result is read from a model made once it was over")
    import importlib.util
    if any(importlib.util.find_spec(p) is None for p in ("numpy", "pandas", "scipy")):
        print("   (skipped: analysis/ needs its own requirements, see analysis/requirements.txt)")
    else:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from analysis import coldbench
        # A cup listed in the morning, 15:00 to 17:30 UTC. The 18:00 update in
        # Paris (16:19 UTC) exports while it runs and holds the board of that
        # minute; the next one (18:10 UTC) holds the final standings.
        cup = {"kind": "History Cup", "name": "History Cup", "event": "epicgames_S42_HistoryCup_ME",
               "window": "S42_HistoryCup_ME", "stage": 0, "region": "ME", "team": "Duo", "mode": "Reload",
               "games": 12, "begin": "2026-09-24T15:00Z", "end": "2026-09-24T17:30Z", "tiers": [["i", 50, ""]],
               "entry": "", "field": 0, "scoring": 0,
               "fc": {"cut": 50, "field": 9950, "lobby": False,
                      "ranks": [[50, 500.0, 0.28, "previous edition"], [100, 480.0, 0.28, "previous edition"]]}}
        bands = {"quality": {"bands": {"50": [-0.5, 0.5], "90": [-1.5, 1.5]}}}

        def history_model(direct):
            row = {"category": "History Cup", "region": "ME", "team_mode": "Duo", "game_mode": "Reload",
                   "latest": "2026-09-24", "level": direct["20"][0], "season": 42, "direct": direct}
            return json.dumps(dict(bands, categories=[row] if direct else []))

        blobs = {"cal": "window.CALENDAR = " + json.dumps({"generated": "2026-09-24T12:00Z", "days": 7,
                                                           "scorings": [], "events": [cup]}) + ";",
                 "m0": json.dumps(dict(bands, categories=[])),
                 "m1": history_model({"20": [222.0, 1, None], "50": [213.0, 1, None], "100": [204.0, 1, None]}),
                 "m2": history_model({"20": [537.0, 1, None], "50": [520.0, 1, None], "100": [508.0, 1, None]})}
        # (commit, when, {path: blob}), oldest first.
        commits = [("c1", "2026-09-24T12:00:00+00:00", {"calendar.js": "cal", "model.json": "m0"}),
                   ("c2", "2026-09-24T16:19:00+00:00", {"model.json": "m1"}),
                   ("c3", "2026-09-24T18:10:00+00:00", {"model.json": "m2"})]

        def fake_git(repo, *args):
            if args[0] == "rev-parse":
                commit, path = args[1].split(":", 1)
                found = ""
                for name, _, files in commits:
                    found = files.get(path, found)
                    if name == commit:
                        return found
                raise RuntimeError("no such commit")
            if args[0] == "log":
                path = args[-1]
                stamp = "%ct" in args[1]
                lines = [f"{name} {int(datetime.fromisoformat(at).timestamp())}" if stamp else name
                         for name, at, files in commits if path in files]
                return "\n".join(lines if "--reverse" in args else lines[::-1])
            raise RuntimeError(f"git {args[0]} not faked")

        class FakeBlobs:
            def __init__(self, repo):
                pass

            def read(self, blob):
                return blobs[blob]

            def close(self):
                pass

        real_git, real_blobs = coldbench.git, coldbench.Blobs
        coldbench.git, coldbench.Blobs = fake_git, FakeBlobs
        history = early = None
        listed = []
        try:
            history = coldbench.History("predictor")
            listed, _ = history.as_listed()
            commits.pop()
            early = coldbench.History("predictor")
        except (TypeError, KeyError) as exc:
            print(f"   ({type(exc).__name__}: {exc})")
        finally:
            coldbench.git, coldbench.Blobs = real_git, real_blobs
        check("a model exported while the cup ran is not its result: the first one made after it settled is",
              [(50, 520.0), (100, 508.0)], [(p["rank"], p["result"]) for p in listed])
        check("a cup only ever held by a model made while it ran is left out, not read off that board",
              (0, 1), (len(early.work) if early else -1,
                       early.skipped.get("every model holding that day was made while the cup ran", 0)
                       if early else -1))
        check("the board settles half an hour past the window's end; a row without an end is not held back",
              ("2026-09-24T18:00", ""), tuple(getattr(coldbench, "settled_by", lambda row: None)(row)
                                              for row in (cup, dict(cup, end=""))))
        check("the commit times are read in UTC, whatever the committer's clock",
              ["2026-09-24T12:00", "2026-09-24T16:19", "2026-09-24T18:10"],
              [m.get("at") for m in history.models] if history else None)

    print("\n22. The measuring tools: exact numbers, the gain rule, the live bench compared")
    if any(importlib.util.find_spec(p) is None for p in ("numpy", "pandas", "scipy")):
        print("   (skipped: analysis/ needs its own requirements, see analysis/requirements.txt)")
    else:
        measuring_tools(workdir, replay_db, pages, live_db)

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


def measuring_tools(workdir: str, replay_db: str, pages: str, live_db: str) -> None:
    """The bench's own tools: a variant run the way the bench is run, the gain
    rule, the numbers the page shows, and the live bench's comparison."""
    import contextlib
    import sqlite3
    import subprocess
    from analysis import bench
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    here = os.path.join(workdir, "tools")
    os.makedirs(here, exist_ok=True)
    no_catalogue = os.path.join(workdir, "no-catalogue")

    def write(name, body):
        path = os.path.join(here, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def loaded(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    # A variant that replaces one of the bench's own functions marks every
    # forecast it prices: run as `python -m analysis.bench`, in one process
    # and in two, its run is marked and today's is not.
    hook = write("hook.py", (
        "CHANGES_MODEL = False\n\n\n"
        "def apply():\n"
        "    from analysis import bench\n"
        "    plain = bench.price\n\n"
        "    def marked(*args, **kwargs):\n"
        "        return [dict(p, hooked=True) for p in plain(*args, **kwargs)]\n\n"
        "    bench.price = marked\n"
        "    return None\n"))
    marks = {}
    for jobs in (1, 2):
        out = os.path.join(here, f"hooked-{jobs}.json")
        done = subprocess.run([sys.executable, "-m", "analysis.bench", "--cold", "--since", "2026-07-13", "--until",
                               "2026-07-21", "--db", replay_db, "--catalogue", no_catalogue,
                               "--cache", os.path.join(here, "cache"), "--leaderboards", pages, "--json", out,
                               "--variant", hook, "--jobs", str(jobs)],
                              cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
        found = [loaded(p) for p in (out, bench.variant_json(out)) if os.path.exists(p)]
        marks[jobs] = (done.returncode, len(found),
                       tuple(sorted({bool(p.get("hooked")) for p in run["pairs"]}) for run in found))
    check("under python -m, a variant replacing a function of analysis.bench reaches the run, in 1 and 2 processes",
          {1: (0, 2, ([False], [True])), 2: (0, 2, ([False], [True]))}, marks)
    # In this process, the bench's functions are put back after the variant's
    # run, though the variant left nothing to put them back with.
    plain_price = bench.price
    with contextlib.redirect_stdout(io.StringIO()):
        bench.main(["--cold", "--since", "2026-07-13", "--until", "2026-07-21", "--db", replay_db, "--catalogue",
                    no_catalogue, "--cache", os.path.join(here, "cache"), "--leaderboards", pages,
                    "--json", os.path.join(here, "in-process.json"), "--variant", hook])
    check("the bench's own functions a variant replaced are put back after its run", True,
          bench.price is plain_price)
    # A model built again on another day is the same model.
    dated = bench.Variant(write("dated.py", (
        "import export_model\n\nCHANGES_MODEL = False\n\n\n"
        "def apply():\n"
        "    plain = export_model.build_model\n"
        "    export_model.build_model = lambda *a, **k: dict(plain(*a, **k), generated='2000-01-01')\n"
        "    return lambda: setattr(export_model, 'build_model', plain)\n")))
    marked = bench.Variant(write("marked.py", (
        "import export_model\n\nCHANGES_MODEL = False\n\n\n"
        "def apply():\n"
        "    plain = export_model.build_model\n"
        "    export_model.build_model = lambda *a, **k: dict(plain(*a, **k), marked=True)\n"
        "    return lambda: setattr(export_model, 'build_model', plain)\n")))
    # A field the model already has, moved: its measured quality, its curve.
    requality = bench.Variant(write("requality.py", (
        "import export_model\n\nCHANGES_MODEL = False\n\n\n"
        "def apply():\n"
        "    plain = export_model.build_model\n"
        "    moved = lambda m: dict(m, quality=dict(m['quality'], coverage=0.5))\n"
        "    export_model.build_model = lambda *a, **k: moved(plain(*a, **k))\n"
        "    return lambda: setattr(export_model, 'build_model', plain)\n")))
    recurve = bench.Variant(write("recurve.py", (
        "import export_model\n\nCHANGES_MODEL = False\n\n\n"
        "def apply():\n"
        "    plain = export_model.build_model\n"
        "    moved = lambda m: dict(m, curve=dict(m['curve'], b=m['curve']['b'] + 0.01))\n"
        "    export_model.build_model = lambda *a, **k: moved(plain(*a, **k))\n"
        "    return lambda: setattr(export_model, 'build_model', plain)\n")))
    key = bench.model_key(replay_db)
    conn = bench.open_read_only(replay_db, live=False)
    conn.row_factory = sqlite3.Row
    try:
        cutoff = bench.cutoff_of("2026-07-20 18:00:00")
        same = [bench.same_model(conn, cutoff, None, key, v) for v in (dated, marked, requality, recurve)]
    finally:
        conn.close()
    check("a model built on another day is the same model; one with anything else changed is not: a field added,"
          " its quality or its curve moved", [True, False, False, False], same)

    # The benches measure the page's numbers, not the export's tenth.
    import export_model
    import rescore
    from collections import Counter
    from analysis import bench_compare, bench_live, coldbench
    conn = bench.open_read_only(replay_db, live=False)
    conn.row_factory = sqlite3.Row
    kept_raw, rescore.RAW = rescore.RAW, pages
    try:
        comps = export_model.load_competitions(conn)
        model = bench.Models(conn, comps, key, None).get(cutoff)
        target = next(c for c in comps if (c.get("family"), c.get("region")) == ("Replay Cup", "ME"))
        row = bench.database_row(target)
        row["cold"] = bench.cold_cell(conn, row, cutoff)
        t = bench.tournament_of(model, row)[0]
        priced = {p["rank"]: p for p in bench.price(conn, model, target, {}, cutoff, bench.season_starts(conn),
                                                     Counter(), Counter())}
        forms = {}
        for name in (("Replay Cup", "EU"), ("Board Late", "ME")):
            other = bench.database_row(next(c for c in comps if (c.get("family"), c.get("region")) == name))
            other["cold"] = bench.cold_cell(conn, other, cutoff)
            forms[name] = bench.tournament_of(model, other)[0]
    finally:
        rescore.RAW = kept_raw
        conn.close()
    ranks = sorted(priced)
    plain = [export_model.predict_from_model(model, dict(t, rank=r)) for r in ranks]
    exact = [export_model.predict_from_model(model, dict(t, rank=r, unrounded=True)) for r in ranks]
    check("predict_from_model rounds to the tenth unless asked; unrounded, it carries the model's rel",
          (True, True, True, True),
          (all(p["value"] == round(e["value"], 1) and p["high"] == round(e["high"], 1) for p, e in zip(plain, exact)),
           all("rel" not in p for p in plain),
           all(abs(e["high"] - e["value"] * (1 + e["rel"])) < 1e-9 * e["high"] for e in exact),
           any(e["value"] != round(e["value"], 1) for e in exact)))
    # The same on each of the four ways to a forecast: the last edition, a
    # single lobby read off the finals of its mode (a table of them given to
    # the model), recent boards replayed under the cup's table, the cascade.
    board = forms[("Board Late", "ME")]
    lobby_model = dict(model, lobby=[{"game_mode": "*", "team_mode": "",
                                      "rows": [[0.05, 0.6, 0.2, 9], [0.5, 0.2, 0.25, 9]]}])
    ways = []
    for m, form in ((model, dict(forms[("Replay Cup", "EU")], rank=10)),
                    (lobby_model, dict(board, field_size=50, rank=5)),
                    (model, dict(board, field_size=2000, rank=10,
                                 cold={"ranks": [[1, 600.0], [100, 300.0]], "rel": 0.12, "donors": 4})),
                    (model, dict(t, rank=10))):
        p, e = export_model.predict_from_model(m, form), export_model.predict_from_model(m, dict(form, unrounded=True))
        ways.append((p["source"], all(p[k] == round(e[k], 1) for k in ("value", "low", "high")) and "rel" not in p,
                     abs(e["high"] - e["value"] * (1 + e["rel"])) < 1e-9 * e["high"]
                     and abs(e["low"] - e["value"] * (1 - e["rel"])) < 1e-9 * e["high"]
                     and e["high"] != round(e["high"], 1)))
    check("unrounded on each way to a forecast: the last edition, a single lobby, boards replayed, the cascade",
          [("previous edition", True, True), ("closed lobby", True, True), ("re-scored boards", True, True),
           ("family + re-scored boards", True, True)], ways)
    evening = bench_live.Evening(model, t, datetime(2026, 7, 20, 18), datetime(2026, 7, 20, 21), False)
    check("the cold bench and the live bench's cold answer price the page's numbers, with the model's rel",
          (True, True, True),
          (bool(ranks), all(priced[r]["forecast"] == e["value"] and priced[r]["rel"] == e["rel"]
                            for r, e in zip(ranks, exact)),
           all(evening.cold(r)["value"] == e["value"] and evening.cold(r)["rel"] == e["rel"]
               for r, e in zip(ranks, exact))))
    # A range of 10.4 x e^(+-0.05) is 9.89 to 10.93 as computed, 10 to 11 as
    # the page prints it; an end of 10.5 is printed 11 (Math.round), not 10.
    pair = {"forecast": 10.4, "rel": 0.1, "result": 11.0, "bands": {"50": [-0.5, 0.5]}}
    check("a range holds a result on its ends as the page prints them, rounded half up",
          (False, True, True, True, True, False),
          (coldbench.inside(pair, "50"), coldbench.inside(pair, "50", shown=True),
           bench_compare.covered(pair, "50"), bench_live.holds([9.893, 10.5], 11.0),
           bench_compare.covered({"result": 11.0, "near": [9.893, 10.5], "wide": [9.0, 12.0]}, "50"),
           bench_live.holds([9.893, 10.49], 11.0)))

    # The gain rule, on runs whose answers are known by construction.
    import copy
    from analysis import bench_compare
    rule = bench_compare

    def forecast(n, window, family, day, region, rank, result, forecast, season=None):
        return {"window": window, "id": n, "family": family, "day": day, "local_day": day,
                "model": f"{day} 16:00:00", "region": region, "rank": rank, "at_cut": False, "result": result,
                "forecast": forecast, "rel": 0.2, "bands": {"50": [-0.5, 0.5], "90": [-1.5, 1.5]},
                "source": "family", "season": season}

    def run_of(pairs, since="2026-07-01", until="2026-07-29"):
        return {"bench": "cold", "since": since, "until": until, "cutoff": "update", "update": None,
                "database": {"sha1": "d" * 40}, "catalogue": {"sha1": "c" * 40}, "leaderboards": {"events": 0},
                "replays": {"boards": []}, "ranks": [10, 25, 100, 250, 500, 1000], "bands": {"generated": "x"},
                "pairs": pairs}

    def scaled(pairs, by):
        """The pairs, each forecast times by(pair) (None: left out)."""
        out = []
        for p in pairs:
            factor = by(p)
            if factor is not None:
                out.append(dict(p, forecast=p["forecast"] * factor))
        return out

    # One event played in six regions on one evening and again a week later,
    # both times better, beside two cups of another family left alone: by the
    # cups the change is sure, by family x local day (four draws) it is not.
    pairs, n = [], 0
    for day, other in (("2026-07-05", "2026-07-06"), ("2026-07-19", "2026-07-20")):
        for region in ("EU", "NAC", "NAW", "BR", "ME", "OCE"):
            n += 1
            pairs += [forecast(n, f"F|{day}|{region}", "F", day, region, rank, 100.0, 110.0)
                      for rank in (10, 100, 1000)]
        n += 1
        pairs += [forecast(n, f"G|{other}", "G", other, "ASIA", rank, 100.0, 110.0) for rank in (10, 100, 1000)]
    better = scaled(pairs, lambda p: 105 / 110 if p["family"] == "F" else 1.0)
    found = rule.compare(run_of(pairs), run_of(better))
    by_cup = found["intervals"].get("cup", {}).get("abs_mean") or [0, 0]
    check("the interval that judges draws family x local day: one event in six regions is one draw",
          ("family x local day", True, False, "no gain"),
          (found["interval"]["unit"], by_cup[1] < 0, found["interval"]["abs_mean"][1] < 0, found["verdict"]))

    def next_day(rows, region, field):
        """The rows, those of the event in `region` with `field` moved to the next day."""
        return [dict(p, **{field: (datetime.fromisoformat(p[field]) + timedelta(days=1)).date().isoformat()})
                if (p["family"], p["region"]) == ("F", region) else p for p in rows]

    # The draws follow the local day, not the UTC one: the event's NAW cup
    # played past midnight UTC is still its evening's draw; an OCE cup whose
    # local day is the next one is a draw of its own.
    late_naw = rule.compare(run_of(next_day(pairs, "NAW", "day")), run_of(next_day(better, "NAW", "day")))
    oce_rows = next_day(pairs, "OCE", "local_day")
    next_oce = rule.compare(run_of(oce_rows), run_of(next_day(better, "OCE", "local_day")))
    check("the draws are family x local day: a NAW cup past midnight UTC stays in its evening's draw, an OCE cup"
          " on the next local day is another", (True, False, ("F", "2026-07-06")),
          (late_naw["interval"]["abs_mean"] == found["interval"]["abs_mean"],
           next_oce["interval"]["abs_mean"] == found["interval"]["abs_mean"],
           rule.family_day(next(p for p in oce_rows if p["region"] == "OCE"))))

    # Forty cups whose largest error alone is halved: the median stays where
    # it was (to 1e-9: the forecasts at the middle moved by 1e-13), the rest
    # goes down.
    pairs, n = [], 0
    for k in range(40):
        day = f"2026-07-{1 + k % 28:02d}"
        region = ("EU", "NAC", "BR", "ASIA")[k % 4]
        n += 1
        for rank, off in ((10, 2), (25, 3), (100, 4), (250, 5), (500, 6), (1000, 40)):
            pairs.append(forecast(n, f"T{k}|{day}", f"T{k}", day, region, rank, 200.0, 200.0 * (1 + off / 100)))
    tail = scaled(pairs, lambda p: (120 / 140) if p["rank"] == 1000 else (1 + 1e-13) if p["rank"] in (100, 250)
                  else 1.0)
    found = rule.compare(run_of(pairs), run_of(tail))
    median_change = found["overall"]["all"]["change"]["abs_median"]
    check("a change that leaves the median where it was, to 1e-9, and lowers the rest is a gain",
          (True, True, "gain"), (0 < median_change < 1e-9, found["interval"]["abs_mean"][1] < 0, found["verdict"]))
    segments = [rule.compare(run_of(pairs), run_of(tail), segment=s)["verdict"]
                for s in ("p['rank'] == 1000", "p['rank'] <= 250")]
    check("a declared segment must see its own median go down", ["gain", "no gain"], segments)
    try:
        rule.compare(run_of(pairs), run_of(tail), segment="p['nothing']")
        told = "no error"
    except ValueError as exc:
        told = "fails on" in str(exc)
    check("a segment that does not read on the forecasts is refused, not judged", True, told)
    # The regions and the halves, read off the same result.
    judged = copy.deepcopy(found)
    region = judged["tables"]["region"]["EU"]
    regions = []
    for pairs_, cups_, change in ((29, 25, 2.0), (30, 19, 2.0), (30, 20, 0.5), (30, 20, 0.51)):
        region.update(pairs=pairs_, cups=cups_)
        region["change"]["abs_mean"] = change
        regions.append(rule.judge(judged, [])[1])
    check("a region is judged with 30 forecasts and 20 cups, and may lose 0.5 point exactly",
          ["gain", "gain", "gain", "no gain"], regions)
    judged = copy.deepcopy(found)
    halves = []
    for median in (0.0, 1e-12, 0.01):
        judged["halves"][0]["change"]["abs_median"] = median
        halves.append(rule.judge(judged, [])[1])
    check("in each half the median may stay where it was, to 1e-9, but not go up", ["gain", "gain", "no gain"],
          halves)

    # A refusal judged apart: the variant drops the largest errors of the
    # base (under 5 % of its forecasts) and leaves the rest alone.
    pairs, n = [], 0
    for k in range(40):
        day = f"2026-07-{1 + k % 28:02d}"
        n += 1
        for rank, off in ((10, 2), (25, 3), (100, 4), (250, 5), (500, 6), (1000, 7)):
            off = 60 if (k % 5 == 0 and rank >= 500) else off
            pairs.append(forecast(n, f"R{k}|{day}", f"R{k}", day, "EU", rank, 200.0, 200.0 * (1 + off / 100),
                                  season=41 + k % 2))
    # The largest errors at the deepest rank (3.3 %), or at the two deepest (6.7 %: too many).
    dropped = [p for p in pairs if not (p["rank"] == 1000 and p["forecast"] > 300)]
    wide = [p for p in pairs if not p["forecast"] > 300]
    plain = [p for p in pairs if not (p["rank"] == 10 and int(p["window"][1:].split("|")[0]) % 10 == 0)]
    refusals = (rule.compare(run_of(pairs), run_of(dropped))["verdict"],
                rule.compare(run_of(pairs), run_of(dropped), refusal=True)["verdict"],
                rule.compare(run_of(pairs), run_of(wide), refusal=True)["verdict"],
                rule.compare(run_of(pairs), run_of(plain), refusal=True)["verdict"],
                rule.compare(run_of(pairs), run_of(scaled(dropped, lambda p: 1.01)), refusal=True)["verdict"])
    check("a refusal is judged apart: under 5 %, twice the others' median, the rest unchanged",
          ("no gain", "gain", "no gain", "no gain", "no gain"), refusals)
    # Twice the others' median in one half only is not enough: the variant
    # refuses the largest errors of the first half and, in the second, the
    # same cups' smallest.
    first_half = "2026-07-15"
    one_half = [p for p in pairs if not (int(p["window"][1:].split("|")[0]) % 5 == 0 and (
        p["rank"] == 1000 if p["day"] < first_half else p["rank"] == 10))]
    one_found = rule.compare(run_of(pairs), run_of(one_half), refusal=True)
    check("a refusal that holds twice the others' median in one half only is no gain",
          ("no gain", [True, False], 1),
          (one_found["verdict"], [h["holds"] for h in one_found["refusal"]["halves"]], len(one_found["failed"])))

    # The live bench writes, for each answer, the local day, rel and the floor.
    conn = bench.open_read_only(live_db, live=False)
    conn.row_factory = sqlite3.Row
    live_key = bench.model_key(live_db)
    try:
        measured = bench_live.measure(conn, "2026-07-20", "2026-07-21", {}, None, live_key, progress=False)
        comps = export_model.load_competitions(conn)
        cup = next(c for c in comps if c.get("family") == "Live Cup" and str(c.get("start_time")).startswith(
            "2026-07-20 18"))
        live_cutoff = bench.cutoff_of(str(cup["start_time"]))
        live_model = bench.Models(conn, comps, live_key, None).get(live_cutoff)
        cup_t = bench.tournament_of(live_model, bench.database_row(cup))[0]
        cup_snaps = bench_live.snapshots_of(conn, cup["id"], datetime(2026, 7, 20, 18), datetime(2026, 7, 20, 21))
        far_rows = bench_live.price_cup(conn, live_model, dict(cup, region="OCE"), {}, live_cutoff, Counter(),
                                        Counter())[0]
        named_rows = bench_live.price_cup(conn, live_model, dict(cup, event_id="epicgames_S42_LiveCup_EU",
                                                                 window_id="S42_LiveCup_Event1_EU"), {},
                                          live_cutoff, Counter(), Counter())[0]
    finally:
        conn.close()
    rows = measured["rows"]
    check("each live answer carries its local day, its rel and its floor, and never says less than the floor",
          (True, True, True, True),
          (bool(rows) and all(r["local_day"] == bench.local_day(f"{r['day']} 18:00:00", r["region"]) for r in rows)
           and bool(far_rows) and {r["local_day"] for r in far_rows} == {"2026-07-21"},
           all(isinstance(r["rel"], float) and r["rel"] >= 0 for r in rows),
           all(isinstance(r["floor"], (int, float)) for r in rows),
           all(r["forecast"] >= r["floor"] - 1e-9 for r in rows) and any(r["floor"] > 0 for r in rows)))
    check("each live answer carries its season, as the cold bench reads it (the database's, else the event's)",
          (True, {42}), (all("season" in r for r in rows), {r.get("season") for r in named_rows}))
    # An empty curve or tail of the cup's family reads the share as it
    # is, as the page does, not the pooled curve.
    empty_model = copy.deepcopy(live_model)
    empty_model["pace"]["families"] = [["Battle Royale", "Solo", 180, 10, {}, {}, None, 30,
                                        *bench_live.page_signature(cup_t)]] + list(
        empty_model["pace"].get("families") or [])
    empty_model["pace"]["categories"] = []
    found_pace = bench_live.family_pace(empty_model["pace"], cup_t, 180, datetime(2026, 7, 20, 18))
    evening = bench_live.Evening(empty_model, cup_t, datetime(2026, 7, 20, 18), datetime(2026, 7, 20, 21), False)
    evening.replay(bench_live.records_of(bench_live.available(cup_snaps, 120, 180)))
    page_side = evening.refine(mirror=False)
    by_name = bench_live.family_pace({"categories": [[bench_live.live.category_key(cup_t.get("name")), {}, {}, 30, "",
                                                      "feed"]], "families": []}, cup_t, 180, datetime(2026, 7, 20, 18))
    check("an empty family curve or tail is the page's raw share, not the pooled curve; flagged 'empty'",
          ({}, {}, {}, True, True),
          ((found_pace or {}).get("curve"), (found_pace or {}).get("tail"), (by_name or {}).get("curve"),
           bool(page_side) and page_side["expected_for"](10) == page_side["share"],
           "empty" in bench_live.cup_flags(evening, evening.refine(mirror=True))))

    # The feed's two regimes: until 4 October 2026, 16:45 UTC, the full pass
    # every ten minutes and the first page every five in the last twenty;
    # since, the full pass every five minutes and, twenty minutes either side
    # of the close, looks at the first page and the cuts' pages one, two and
    # three minutes past each mark.
    opens, closes = datetime(2026, 10, 5, 18), datetime(2026, 10, 5, 21)

    def lands(minute, ranks, cuts=(), old=False, begin=opens, end=closes):
        return round(bench_live.landed(begin, end, minute, ranks, "feed", cuts, old), 6)

    check("since the change: a full pass every 5 min, looks at minutes 1-3 near the close, first and cuts' pages",
          [37.0, 169.0, 172.0, 169.0, 197.0, 198.0, 202.0],
          [lands(31, (10, 100)), lands(167, (1, 10, 100)), lands(167, (10, 1000), (100,)),
           lands(167, (10, 1000), (1000,)), lands(195, (10,)), lands(196, (10,)), lands(199, (10,))])
    check("near the close, a reading stamped between two and three minutes past a mark lands on the look at three",
          [200.0, 180.0], [lands(197.5, (10,)), lands(177.4, (10, 1000), (1000,))])
    check("before it, and with --old-cadence: the full pass every 10 min, the first page every 5 near the close",
          [42.0, 172.0, 172.0, 42.0, 52.0, 47.0],
          [lands(31, (10, 100), old=True), lands(167, (1, 10, 100), old=True), lands(167, (10, 1000), (1000,), True),
           lands(31, (10, 100), begin=datetime(2026, 9, 20, 18), end=datetime(2026, 9, 20, 21)),
           lands(43, (10,), begin=datetime(2026, 10, 4, 16), end=datetime(2026, 10, 4, 19)),
           lands(45, (10,), begin=datetime(2026, 10, 4, 16), end=datetime(2026, 10, 4, 19))])
    check("the regime is the one the feed ran at the reading's stamp",
          ["before", "since", "before"],
          [bench_live.regime_of(datetime(2026, 10, 4, 16), 44), bench_live.regime_of(datetime(2026, 10, 4, 16), 45),
           bench_live.regime_of(datetime(2026, 10, 4, 16), 45, old=True)])
    # Brought back to the old cadence: of the readings one old pass would have
    # taken, the latest only; the readings before the change all stay.
    snaps = [{"minute": m, "known": lands(m, ranks, old=True), "points": {r: 1.0 for r in ranks}}
             for m, ranks in ((31, (10, 1000)), (33, (10, 1000)), (36, (10,)), (161, (10,)), (163, (10,)),
                              (166, (10,)))]
    check("--old-cadence keeps, per pass of the old feed, the latest reading it could take",
          [33, 36, 163, 166], [s["minute"] for s in bench_live.old_cadence(snaps, opens)])
    # The same through the database: a cup read at minutes 12 and 13, after
    # the change (moved back to July for the check), is read once at 13 then.
    import db
    import shutil
    later_db = os.path.join(here, "later.db")
    shutil.copy(live_db, later_db)
    with db.session(later_db) as conn:
        db.add_snapshot(conn, cup["id"], ts="2026-07-20 18:13", points={1: 30.0, 10: 20.0, 100: 10.0, 1000: 5.0},
                        note="live feed", games=1)
    kept_change, bench_live.FEED_CHANGE = bench_live.FEED_CHANGE, datetime(2026, 7, 1)
    try:
        conn = bench.open_read_only(later_db, live=False)
        try:
            read_as = {old: [(round(s["minute"]), round(s["known"])) for s in bench_live.snapshots_of(
                conn, cup["id"], datetime(2026, 7, 20, 18), datetime(2026, 7, 20, 21), "feed", (), old)][:3]
                for old in (False, True)}
        finally:
            conn.close()
    finally:
        bench_live.FEED_CHANGE = kept_change
    check("read from the database, the readings since the change land on its passes, or on the old ones thinned",
          {False: [(12, 17), (13, 17), (24, 27)], True: [(13, 22), (24, 32), (36, 42)]}, read_as)
    check("the evenings the feed was down are told by the window and the twenty minutes past it (Paris time)",
          [True, False, True, False],
          [bench_live.outage_of(datetime(2026, 9, 19, 18), datetime(2026, 9, 19, 21)),
           bench_live.outage_of(datetime(2026, 9, 19, 15), datetime(2026, 9, 19, 18, 9)),
           bench_live.outage_of(datetime(2026, 10, 4, 12), datetime(2026, 10, 4, 13)),
           bench_live.outage_of(datetime(2026, 10, 4, 14, 1), datetime(2026, 10, 4, 15))])
    check("a window closed fifteen minutes before the feed went down is marked: its twenty minutes past ran in it",
          True, bench_live.outage_of(datetime(2026, 9, 19, 15), datetime(2026, 9, 19, 18, 15)))
    kept_outages = bench_live.FEED_OUTAGES
    bench_live.FEED_OUTAGES = (("2026-07-20 19:00", "2026-07-20 21:00"),)
    try:
        conn = bench.open_read_only(live_db, live=False)
        conn.row_factory = sqlite3.Row
        try:
            marked = bench_live.measure(conn, "2026-07-20", "2026-07-21", {}, None, live_key, progress=False)
            left = bench_live.measure(conn, "2026-07-20", "2026-07-21", {}, None, live_key, progress=False,
                                      outages=False)
        finally:
            conn.close()
    finally:
        bench_live.FEED_OUTAGES = kept_outages
    check("a cup an outage touched is marked, and left out with --skip-outages",
          (True, False, 0, 2),
          (bool(marked["rows"]) and all(r["outage"] for r in marked["rows"]), any(r["outage"] for r in rows),
           len(left["rows"]), left["skipped"].get("cups an outage of the feed touched (--skip-outages)")))

    # Two live runs, point by point: the reference against itself, a variant
    # two percent high from 80 % of the session on, a variant that loses
    # forecasts, and runs of two arrivals.
    def live_row(n, family, day, region, rank, point, result, forecast):
        return {"window": f"{family}|{day}|{region}", "id": n, "family": family, "day": day, "local_day": day,
                "model": f"{day} 16:00:00", "region": region, "rank": rank, "point": point, "minute": 1.0,
                "at_cut": False, "result": result, "forecast": forecast, "near": [forecast * 0.9, forecast * 1.1],
                "wide": [forecast * 0.8, forecast * 1.2], "rel": 0.1, "floor": 0, "depth": "0.02-0.05"}

    labels = [p for p, _, _ in bench_live.POINTS]
    live_rows, n = [], 0
    for k in range(40):
        day = f"2026-07-{1 + k % 28:02d}"
        for region in ("EU", "NAC"):
            n += 1
            for point in labels:
                for rank, off in ((10, 3), (100, 4), (1000, 6)):
                    live_rows.append(live_row(n, f"L{k}", day, region, rank, point, 300.0, 300.0 * (1 + off / 100)))
    reference = dict(run_of([]), bench="live", arrival="feed", forecasts=live_rows,
                     points=[{"label": p} for p in labels], live_pages={"digest": "p"})
    reference.pop("pairs")
    late = {"80 %", "100 %", "+10 min", "+20 min"}
    planted_rows = [dict(r, forecast=r["forecast"] * 1.02) if r["point"] in late else r for r in live_rows]
    planted_run = dict(reference, forecasts=planted_rows)
    itself = bench_compare.compare(reference, reference)
    planted_found = bench_compare.compare(reference, planted_run)
    lossy = bench_compare.compare(reference, dict(reference, forecasts=[r for r in live_rows if not (
        r["point"] == "40 %" and r["rank"] == 1000 and r["id"] % 7 == 0)]))
    other_arrival = []
    for name, other in (("arrival", dict(reference, arrival="stamp")),
                        ("live pages", dict(reference, live_pages={"digest": "q"})),
                        ("outages", dict(reference, outages={"left_out": True}))):
        try:
            # Kept on one side and left out on the other is a value apart,
            # never allowed unchecked.
            bench_compare.compare(dict(reference, outages={"left_out": False}) if name == "outages" else reference,
                                  other, unchecked=("outages",) if name == "outages" else ())
            other_arrival.append("compared")
        except bench_compare.SnapshotMismatch as exc:
            other_arrival.append(f"{name}:" in str(exc))
    check("live runs: the reference against itself changes nothing; +2 % from 80 % on is a loss there only",
          ({p: "no change" for p in labels}, 0,
           {p: ("loss" if p in late else "no change") for p in labels}),
          (itself["verdicts"], itself["moved"], planted_found["verdicts"]))
    # A forecast read at another minute of the session on the variant's side
    # is not the same forecast: one at +10 min and one at +20 min moved.
    moved_at = {next(i for i, r in enumerate(live_rows) if r["point"] == label): by
                for label, by in (("+10 min", 10), ("+20 min", 20))}
    try:
        bench_compare.compare(reference, dict(reference, forecasts=[
            dict(r, minute=r["minute"] + moved_at[i]) if i in moved_at else r for i, r in enumerate(live_rows)]))
        aimed = "compared"
    except bench_compare.SnapshotMismatch as exc:
        aimed = str(exc)
    check("live runs: two forecasts aimed at other minutes on one side are refused, the minute named",
          True, f"2 of the {len(live_rows)} paired forecasts do not aim at the same thing on both sides (minute: 2)"
          in aimed)
    check("live runs: forecasts lost fail where they are lost; another arrival, other live pages or the outages"
          " left out on one side only are another snapshot",
          ("no gain", True, "no change", [True, True, True]),
          (lossy["verdicts"]["40 %"], "every forecast of the base priced" in lossy["points"]["40 %"]["failed"],
           lossy["verdicts"]["60 %"], other_arrival))
    # The seasons judge at each point once the answers carry them; without,
    # the report says they were not judged. A span asked from a month before
    # the first cup priced has its halves cut on the days priced.
    seasoned = dict(reference, forecasts=[dict(r, season=41 + int(r["family"][1:]) % 2) for r in live_rows])
    seasoned_planted = dict(reference, forecasts=[dict(r, season=41 + int(r["family"][1:]) % 2)
                                                   for r in planted_rows])
    with_seasons = bench_compare.compare(seasoned, seasoned_planted)["points"]["80 %"]
    early_span = bench_compare.compare(dict(reference, since="2026-06-01"), dict(planted_run, since="2026-06-01"))
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        bench_compare.print_live_report(planted_found)
    check("live seasons are judged when the answers carry them, and said not judged when they do not",
          (True, True, True),
          (any("most seasons" in c["rule"] for c in with_seasons["conditions"]),
           "the seasons: the forecasts carry no season" in planted_found["points"]["80 %"]["not judged"],
           "not judged at any point, for want of data: " in printed.getvalue()))
    check("the live halves are cut on the days priced, not on a span asked from before the feed began",
          ("2026-07-01", True, "2026-07-15"),
          (early_span["points"]["80 %"]["halves"][0]["since"], early_span["points"]["80 %"]["halves"][0]["pairs"] > 0,
           early_span["points"]["80 %"]["halves"][1]["since"]))
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        bench_compare.print_report(found)
    check("the printed rule keeps its detail apart, and says what it could not judge",
          (True, True), ("(draws of family x local day) [" in printed.getvalue(),
                         "not judged, for want of data: the regions" in printed.getvalue()))
    # The live strawman, learned walk-forward by point x depth x region.
    times = {r["id"]: {"start_time": f"{r['day']} 18:00:00", "end_time": f"{r['day']} 21:00:00"} for r in live_rows}
    straw = bench_compare.strawman_live(reference, times)
    first = {(r["id"], r["rank"], r["point"]): r for r in straw["forecasts"]}
    day_two = [r for r in live_rows if r["day"] == "2026-07-02" and r["region"] == "EU" and r["point"] == "80 %"]
    before = [r for r in live_rows if r["day"] < "2026-07-02" and r["region"] == "EU" and r["point"] == "80 %"]
    errors = sorted(100 * (r["forecast"] / r["result"] - 1) for r in before)
    middle = (errors[len(errors) // 2] if len(errors) % 2 else
              (errors[len(errors) // 2 - 1] + errors[len(errors) // 2]) / 2)
    expected = [r["forecast"] / (1 + middle * len(errors) / (len(errors) + 50) / 100) for r in day_two]
    check("the live strawman divides by 1 + its group's past median, shrunk by n / (n + 50); the first day stays",
          (True, True),
          (all(abs(first[(r["id"], r["rank"], r["point"])]["forecast"] - e) < 1e-9 for r, e in zip(day_two, expected)),
           all(first[(r["id"], r["rank"], r["point"])]["forecast"] == r["forecast"] for r in live_rows
               if r["day"] == "2026-07-01")))

    # bench --live --variant: the same cups replayed with a variant applied,
    # written beside today's run and set against it point by point.
    planted_live = write("planted_live.py", (
        "CHANGES_MODEL = False\n\n\n"
        "def apply():\n"
        "    from analysis import bench_live\n"
        "    plain = bench_live.price_cup\n\n"
        "    def planted(*args, **kwargs):\n"
        "        rows, line = plain(*args, **kwargs)\n"
        "        late = [r for r in rows if r['after_close'] is not None or (r['share'] or 0) >= 0.8]\n"
        "        for r in late:\n"
        "            r.update(forecast=r['forecast'] * 1.02, near=[x * 1.02 for x in r['near'] or []] or None,\n"
        "                     wide=[x * 1.02 for x in r['wide']])\n"
        "        return rows, line\n\n"
        "    bench_live.price_cup = planted\n"
        "    return None\n"))
    out, printed = os.path.join(here, "live.json"), io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = bench.main(["--live", "--since", "2026-07-20", "--until", "2026-07-21", "--db", live_db, "--catalogue",
                           no_catalogue, "--cache", "none", "--json", out, "--variant", planted_live])
    pair = [loaded(p) for p in (out, bench.variant_json(out)) if os.path.exists(p)]
    ratios = {(r["point"], round(v["forecast"] / r["forecast"], 9)) for r, v in zip(*[p["forecasts"] for p in pair])} \
        if len(pair) == 2 else set()
    check("bench --live --variant replays the cups with the variant, writes both runs and compares them per point",
          (0, 2, {(p, 1.02 if p in late else 1.0) for p in labels}, True, bench_live.price_cup.__name__),
          (code, len(pair), ratios, "verdicts: 20 % NO CHANGE" in printed.getvalue(), "price_cup"))


if __name__ == "__main__":
    sys.exit(main())
