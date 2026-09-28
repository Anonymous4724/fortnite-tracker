"""Client for the free Osirion public Fortnite API.

Two different products share the Osirion name. The **Match Data API** is paid:
it charges credits per second of compute and per uploaded replay, and it exists
to serve replay-derived events — eliminations, revives, positions. The **public
API** used here is a separate, free beta: no account, no key, no credits. It
publishes the tournament calendar, each event's scoring rules and payout table,
and the leaderboards. That is everything a threshold model needs, so the paid
tier never comes into it.

Published rate limits: 60 requests a minute on the leaderboard endpoint, 100 a
minute on the others. The throttle below stays under both, because a beta API
that starts refusing us is worse than a harvest that takes an extra hour.

Being a beta, it can change or disappear. Nothing in the app depends on it — it
is used once, offline, to build the training set.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://fnapi.osirion.gg/v1"
USER_AGENT = "fortnite-comp-tracker/8.0 (+https://github.com/Anonymous4724)"

# Published ceilings: 60 a minute on the leaderboard endpoint, 100 on the rest,
# 50 on the account endpoints this project never calls. The defaults sit under
# them because a beta API that starts refusing us costs more than the minutes
# saved; `set_rate` raises them for anyone who would rather live closer to the
# edge, and the retry on 429 is what makes that survivable.
LEADERBOARD_LIMIT = 60
LEADERBOARD_PER_MIN = 50
DEFAULT_PER_MIN = 80

# Osirion's own region codes. NAE and NAW predate the North American merge and
# still appear on 2025 events; ONSITE is what LAN finals are filed under.
REGIONS = ["EU", "NAC", "NAE", "NAW", "BR", "ASIA", "OCE", "ME", "ONSITE"]


class OsirionError(RuntimeError):
    """The API answered, but not with something usable."""


class _Throttle:
    """Space calls out so we never reach the published ceiling."""

    def __init__(self, per_minute: int):
        self.gap = 60.0 / per_minute
        self.last = 0.0

    def wait(self) -> None:
        pause = self.gap - (time.monotonic() - self.last)
        if pause > 0:
            time.sleep(pause)
        self.last = time.monotonic()


_throttles = {"leaderboard": _Throttle(LEADERBOARD_PER_MIN),
              "other": _Throttle(DEFAULT_PER_MIN)}


def set_rate(per_minute: int) -> int:
    """Change the leaderboard pace, never above what the API publishes."""
    global LEADERBOARD_PER_MIN
    LEADERBOARD_PER_MIN = max(1, min(int(per_minute), LEADERBOARD_LIMIT))
    _throttles["leaderboard"] = _Throttle(LEADERBOARD_PER_MIN)
    return LEADERBOARD_PER_MIN


def get(path: str, tries: int = 4, **params) -> dict:
    """One GET, throttled, retried on the failures that are worth retrying."""
    _throttles["leaderboard" if "leaderboard" in path else "other"].wait()
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    url = f"{BASE}/{path}"
    if clean:
        url += "?" + urllib.parse.urlencode(clean)

    for attempt in range(tries):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not payload.get("success", True):
                raise OsirionError(f"{path}: the API reported failure")
            return payload
        except urllib.error.HTTPError as exc:
            # 429 means we misjudged the limit; 5xx means their side. Both are
            # worth waiting out. A 4xx that is not 429 will never get better.
            if exc.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise OsirionError(f"{path}: HTTP {exc.code}") from exc
            time.sleep(5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == tries - 1:
                raise OsirionError(f"{path}: {exc}") from exc
            time.sleep(3 * (attempt + 1))
    raise OsirionError(f"{path}: gave up after {tries} attempts")


def tournaments(region: str | None = None, historic: bool = True) -> list[dict]:
    """The calendar for one region. `historic` reaches past the last 3 months."""
    payload = get("tournaments", region=region,
                  includeHistoricData="true" if historic else "false")
    return payload.get("tournaments") or []


def leaderboard_page(event_id: str, window_id: str, page: int = 0) -> dict:
    """One page of standings. The API caps `page` at 100."""
    return get("tournaments/leaderboard", leaderboardEventId=event_id,
               leaderboardEventWindowId=window_id, page=page)


def board(payload: dict) -> dict:
    """The leaderboard object inside a response envelope.

    The published schema calls the array `leaderboardData` at the top level.
    What the API actually returns is `leaderboard.entries`, with the page number
    and the page count beside it. Reading the documented spelling found nothing,
    silently, for hours - so both are accepted and the live check below refuses
    to pass unless one of them yields rows.
    """
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("leaderboard")
    if isinstance(inner, dict):
        return inner
    rows = payload.get("leaderboardData")
    if isinstance(rows, list):
        return {"entries": rows, "totalPages": payload.get("totalPages"),
                "page": payload.get("page")}
    return {}


def entries_of(payload: dict) -> list[dict]:
    rows = board(payload).get("entries")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def total_pages(payload: dict):
    """How many pages this window has, when the API says. None when it does not.

    Knowing it turns a blind probe into an exact request count: a window with
    three pages costs three calls instead of three plus an empty fourth, and a
    later, deeper pass can skip it without asking.
    """
    pages = board(payload).get("totalPages")
    return int(pages) if isinstance(pages, int) and pages >= 0 else None


# --------------------------------------------------------------------------- #
# Reading Epic's scoring rules
# --------------------------------------------------------------------------- #
PLACEMENT_STAT = "PLACEMENT_STAT_INDEX"
ELIMS_STAT = "TEAM_ELIMS_STAT_INDEX"
VICTORY_STAT = "VICTORY_ROYALE_STAT"


def score_location(window: dict) -> dict:
    """The scoreboard a window is played on.

    A window can post to several leaderboards - a main one and, on FNCS days, a
    per-week or per-division mirror. The main one is the one whose thresholds
    people quote.
    """
    locations = [l for l in (window.get("scoreLocations") or []) if isinstance(l, dict)]
    if not locations:
        return {}
    return next((l for l in locations if l.get("isMain")), locations[0])


def _tiers(window: dict) -> list[tuple[str, str, int, float, bool]]:
    """Every scoring tier, flattened to (stat, matchRule, cut, points, multiplicative).

    Epic nests these two deep - `scoreLocations[].scoringRules[].rewardTiers[]`
    - and calls the cut `keyValue`. The published schema shows a flat
    `rewardTier`/`pointsEarned` pair on the rule itself, which does exist in
    older payloads, so both shapes are read.
    """
    out = []
    rules = score_location(window).get("scoringRules") or window.get("scoringRules") or []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        stat = rule.get("trackedStat") or ""
        match = (rule.get("matchRule") or "").lower()
        nested = rule.get("rewardTiers")
        if isinstance(nested, list) and nested:
            for tier in nested:
                if not isinstance(tier, dict):
                    continue
                cut, points = tier.get("keyValue"), tier.get("pointsEarned")
                if cut is None or points is None:
                    continue
                out.append((stat, match, int(cut), float(points),
                            bool(tier.get("multiplicative"))))
        elif rule.get("rewardTier") is not None and rule.get("pointsEarned") is not None:
            out.append((stat, match, int(rule["rewardTier"]), float(rule["pointsEarned"]),
                        bool(rule.get("multiplicative"))))
    return out


def scoring_from_window(window: dict, cumulative: bool = True) -> dict | None:
    """Turn Epic's rule list into the app's {kill, kill_cap, placement} shape.

    Epic writes placement scoring as "finish at rank N or better, take these
    points", one rule per cut. Whether those points stack is the whole question:
    under the cumulative reading a first place collects every rule it satisfies,
    under the flat reading only the tightest one. Both spellings exist in the
    wild, and reading it wrong shifts every threshold by tens of points — so
    `check_scoring` below settles it against the real standings rather than
    trusting either.
    """
    placement_rules, kill_points, kill_cap = [], 0.0, None
    for stat, match, cut, points, multiplicative in _tiers(window):
        if stat == PLACEMENT_STAT and match == "lte":
            placement_rules.append((cut, points))
        elif stat == ELIMS_STAT:
            if multiplicative:
                kill_points = points / max(cut, 1)
            else:
                # A flat tier on eliminations is a cap dressed as a bonus:
                # "reach N elims, take these points, nothing beyond".
                kill_cap = cut
                kill_points = kill_points or points / max(cut, 1)

    if not placement_rules:
        return None
    placement_rules.sort()                       # tightest cut first

    table, previous_cut = [], 0
    for cut, points in placement_rules:
        # A team finishing at `cut` clears every looser cut as well, so under
        # the cumulative reading its placement points are the sum of the rules
        # at or above it — not the ones below.
        total = sum(p for c, p in placement_rules if c >= cut) if cumulative else points
        low = previous_cut + 1
        if cut >= low:
            table.append([low, cut, round(total, 3)])
        previous_cut = cut
    return {"kill": round(kill_points, 3), "kill_cap": kill_cap, "placement": table}


def points_for(scoring: dict, placement: int, elims: int) -> float:
    """What a single game is worth under this scoring table."""
    total = 0.0
    for low, high, points in scoring.get("placement") or []:
        if low <= placement <= high:
            total += points
            break
    cap = scoring.get("kill_cap")
    counted = min(elims, cap) if cap else elims
    return total + counted * (scoring.get("kill") or 0)


def _sessions(entry: dict) -> list[dict]:
    return [s for s in (entry.get("sessionHistory") or []) if isinstance(s, dict)]


def _stat(session: dict, name: str, default: int = 0) -> int:
    """Pull one tracked stat out of a session, whatever shape it arrives in."""
    holder = session.get("trackedStats") if isinstance(session.get("trackedStats"), dict) else session
    value = holder.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def check_scoring(scoring: dict, entries: list[dict], sample: int = 60) -> float:
    """Share of teams whose published total we can reproduce, to the point.

    Every leaderboard entry carries both its total and the per-game placement
    and elimination counts that produced it, so a scoring table can be marked
    right or wrong instead of assumed. Below about 0.9 the table is wrong and
    the tournament is better left to `scoring_infer`.
    """
    if not scoring:
        return 0.0
    checked = matched = 0
    for entry in entries[:sample]:
        sessions = _sessions(entry)
        if not sessions:
            continue
        total = sum(points_for(scoring, _stat(s, PLACEMENT_STAT, 999),
                               _stat(s, ELIMS_STAT)) for s in sessions)
        published = entry.get("pointsEarned")
        if published is None:
            continue
        checked += 1
        matched += abs(total - float(published)) < 0.5
    return matched / checked if checked else 0.0


def best_scoring(window: dict, entries: list[dict]) -> tuple[dict | None, float, str]:
    """The reading of the rules that the standings actually agree with."""
    best, best_score, best_kind = None, 0.0, "none"
    for cumulative in (True, False):
        table = scoring_from_window(window, cumulative=cumulative)
        if not table:
            continue
        agreement = check_scoring(table, entries)
        if agreement > best_score:
            best, best_score, best_kind = table, agreement, (
                "cumulative" if cumulative else "flat")
    return best, best_score, best_kind


# --------------------------------------------------------------------------- #
# Reading the rest of a window
# --------------------------------------------------------------------------- #
def _payout_rows(window: dict):
    """Every (threshold, payouts) pair the window pays out on."""
    for table in score_location(window).get("payoutTables") or []:
        if not isinstance(table, dict):
            continue
        for row in table.get("ranks") or []:
            if isinstance(row, dict):
                yield row


# Epic pays out on three kinds of threshold and the number means something
# different in each. Read off the harvest, September 2026:
#   rank        {"threshold": 1} is first place; {"threshold": 25} is everyone
#               up to 25th not already covered by a tighter row — "top 25".
#   percentile  {"threshold": 0.25} is the top quarter of the field.
#   value       the threshold is a score: 60 points earns the spray whatever the
#               rank. Not a position, nothing to forecast, and the reason an
#               earlier reading of this table thought thresholds counted from 0.
PAYOUT_KINDS = {"token": "qualify", "ecomm": "cash", "game": "item", "score": "points"}
# When one row pays several things, the one a player cares about first.
KIND_ORDER = ("qualify", "cash", "item", "points")


def humanise_token(value: str, event_id: str = "") -> str:
    """A qualification token names the window it opens; say that in words.

    "S29_FNCS_Major2_Qualifier2Round4_EU" for the event
    "epicgames_S29_FNCS_Major2_Qualifier2_EU" becomes "Qualifier 2 Round 4":
    the season, the region and whatever the event id already says are
    dropped, the rest is split where the capitals and digits are.
    """
    parts = [p for p in str(value or "").split("_") if p]
    if parts and parts[-1].upper() in REGIONS:
        parts.pop()
    if parts and re.fullmatch(r"S\d{1,3}", parts[0], re.I):
        parts.pop(0)
    own = {p.lower() for p in str(event_id or "").split("_") if p}
    kept = [p for p in parts if p.lower() not in own] or parts[-1:]
    words = " ".join(kept)
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", words)
    words = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", words)
    words = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", words)
    return re.sub(r"\s+", " ", words).strip()[:40]


def _payout_reading(payout: dict, event_id: str) -> dict | None:
    """One payout line as {kind, label, amount, currency}, or None if it pays nothing."""
    kind = PAYOUT_KINDS.get(str(payout.get("rewardType") or "").lower())
    if not kind:
        return None                             # copy_score and the like: bookkeeping
    value = payout.get("value")
    quantity = payout.get("quantity")
    amount = float(quantity) if isinstance(quantity, (int, float)) else 0.0
    if kind == "qualify":
        return {"kind": kind, "label": humanise_token(value, event_id) or "Qualification",
                "amount": None, "currency": None}
    if kind == "cash":
        if amount <= 0:
            return None                         # "$0" beside a token: the token is the prize
        currency = str(value or "USD").upper()
        shown = f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"
        label = f"${shown}" if currency == "USD" else f"{shown} {currency}"
        return {"kind": kind, "label": label, "amount": amount, "currency": currency}
    if kind == "item":
        return {"kind": kind, "label": "Cosmetic", "amount": None, "currency": None}
    if amount <= 0:
        return None
    shown = f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.1f}"
    return {"kind": kind, "label": f"{shown} series points", "amount": amount, "currency": None}


def _opens_round(payout: dict) -> bool:
    """A token that sends a player on: given, or - Epic's other spelling of the
    same thing - a "Deny" token taken back from everyone above the cut."""
    if str(payout.get("rewardType") or "").lower() != "token":
        return False
    value = str(payout.get("value") or "")
    quantity = payout.get("quantity")
    if not isinstance(quantity, (int, float)) or isinstance(quantity, bool) or not quantity:
        return False
    deny = "deny" in value.lower()
    return (quantity < 0) == deny


def payout_tiers(window: dict, event_id: str = "", event: dict | None = None) -> list[dict]:
    """The cuts a player aims at: qualification, prize money, cosmetics.

    One tier per threshold, best kind first when a row pays several things.
    `rank` is the position ("top 25"); a percentile tier has no rank, only a
    `share` of the field, because nobody knows the field before the cup is
    played. Score-valued tables are left out: they are not positions.

    A qualification can be ranked on another board than the session's own.
    The FNCS Solo qualifiers' Round 1 is played on two days and its cut - the
    top 8,000 in Europe, 4,000 in NA Central, 2,000 elsewhere - is ranked on
    the total of the two, a board each day's window posts to beside its own,
    where the cut is a token given or a "Deny" token taken back. Read off the
    session's board alone, the round had no cut at all, and the page asked
    the rank it asks by default. Such a cut comes back with `total` set and
    `sessions`: how many of the event's windows post to that board - the
    round's days - when the event is given, else 0.
    """
    tiers: dict = {}
    main = score_location(window)
    for table in main.get("payoutTables") or []:
        if not isinstance(table, dict):
            continue
        scoring = str(table.get("scoringType") or "rank").lower()
        if scoring == "value":
            continue
        for row in table.get("ranks") or []:
            if not isinstance(row, dict):
                continue
            threshold = row.get("threshold")
            if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) \
                    or threshold <= 0:
                continue
            best = None
            for payout in row.get("payouts") or []:
                if not isinstance(payout, dict):
                    continue
                reading = _payout_reading(payout, event_id)
                if reading and (best is None or KIND_ORDER.index(reading["kind"])
                                < KIND_ORDER.index(best["kind"])):
                    best = reading
            if not best:
                continue
            if scoring == "percentile":
                share = float(threshold)
                share = share / 100 if share > 1 else share      # 25 and 0.25 both seen as "a quarter"
                key, tier = ("share", round(share, 4)), dict(best, rank=None, share=round(share, 4))
            else:
                key, tier = ("rank", int(threshold)), dict(best, rank=int(threshold), share=None)
            if key not in tiers or KIND_ORDER.index(tier["kind"]) < KIND_ORDER.index(tiers[key]["kind"]):
                tiers[key] = tier
    for location in window.get("scoreLocations") or []:
        if not isinstance(location, dict) or location is main:
            continue
        board = location.get("leaderboardEventWindowId") or ""
        sessions = sum(1 for w in (event or {}).get("eventWindows") or []
                       if any(isinstance(l, dict) and l.get("leaderboardEventWindowId") == board
                              for l in w.get("scoreLocations") or [])) if board else 0
        for table in location.get("payoutTables") or []:
            if not isinstance(table, dict) or str(table.get("scoringType") or "rank").lower() != "rank":
                continue
            for row in table.get("ranks") or []:
                if not isinstance(row, dict):
                    continue
                threshold = row.get("threshold")
                if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) \
                        or threshold <= 0:
                    continue
                token = next((p for p in row.get("payouts") or []
                              if isinstance(p, dict) and _opens_round(p)), None)
                if token is None:
                    continue
                label = humanise_token(re.sub(r"(?i)deny", "", str(token.get("value") or "")),
                                       event_id) or "Qualification"
                # The session's own board, where it names a cut at this rank, has
                # the say.
                tiers.setdefault(("rank", int(threshold)), {"kind": "qualify", "label": label, "amount": None,
                                                   "currency": None, "rank": int(threshold), "share": None,
                                                   "total": True, "sessions": sessions})
    return sorted(tiers.values(),
                  key=lambda t: (t["rank"] is None, t["rank"] or 0, t["share"] or 0))


def _payout_kinds(window: dict) -> set[str]:
    kinds = set()
    for row in _payout_rows(window):
        for payout in row.get("payouts") or []:
            if isinstance(payout, dict) and payout.get("rewardType"):
                kinds.add(str(payout["rewardType"]).lower())
    return kinds


PLAYLIST_TEAMS = [("solo", "Solo"), ("duo", "Duo"), ("trio", "Trio"), ("squad", "Squad")]


def playlist_team_mode(window: dict) -> str:
    """Solo/Duo/Trio/Squad, read off the playlist the window is played on."""
    playlist = (window.get("playlistId") or "").lower()
    for needle, mode in PLAYLIST_TEAMS:
        if needle in playlist:
            return mode
    return ""


def match_cap(window: dict) -> int:
    """Games the rules allow. Epic states it; nothing needs to count sessions."""
    cap = window.get("matchCap")
    return int(cap) if isinstance(cap, int) and cap > 0 else 0


TEAM_SIZES = {1: "Solo", 2: "Duo", 3: "Trio", 4: "Squad"}


def team_mode(event: dict, entries: list[dict] | None = None,
              window: dict | None = None) -> str:
    """Solo/Duo/Trio/Squad — from the playlist if it says, else from the rosters."""
    if window:
        found = playlist_team_mode(window)
        if found:
            return found
    for key in ("teamSize", "maxTeamSize", "playersPerTeam"):
        size = event.get(key)
        if isinstance(size, int) and size in TEAM_SIZES:
            return TEAM_SIZES[size]
    if entries:
        sizes = [len(e.get("players") or []) for e in entries[:40]]
        sizes = [s for s in sizes if s]
        if sizes:
            return TEAM_SIZES.get(max(set(sizes), key=sizes.count), "Solo")
    return "Solo"


GAME_MODE_HINTS = [
    ("reload zero build", "Reload Zero Build"), ("reloadzb", "Reload Zero Build"),
    ("reload", "Reload"), ("blitz", "Blitz Royale"),
    ("zero build", "Zero Build"), ("zerobuild", "Zero Build"), ("zb", "Zero Build"),
    ("build", "Battle Royale"),
]


def game_mode(event: dict) -> str:
    """Epic never labels the mode; the event id and title are what give it away."""
    haystack = " ".join(str(x).lower() for x in (
        event.get("eventId") or "",
        (event.get("displayData") or {}).get("titleLine1") or "",
        (event.get("displayData") or {}).get("titleLine2") or "",
    ))
    for needle, mode in GAME_MODE_HINTS:
        if needle in haystack:
            return mode
    return "Battle Royale"


def event_regions(event: dict) -> list[str]:
    regions = event.get("regions") or []
    return [r for r in regions if isinstance(r, str)] or ["EU"]


# --------------------------------------------------------------------------- #
# Reading identity out of Epic's ids
#
# The display title is written for players and changes wording between seasons;
# the ids do not. "epicgames_S41_ConsoleVCC_SolosZB_EU" says season, series,
# mode and region in fixed positions, and the window "S41_ConsoleVCC_SolosZB_
# Event1Round1_EU" adds which occurrence and which round. That is the whole
# taxonomy, machine-readable, and it survives Epic renaming the cup.
# --------------------------------------------------------------------------- #
import re

SEASON = re.compile(r"(?:^|_)(S\d{1,3}|Ch\d+S\d+)(?:_|$)", re.I)
# Epic names a window in more than one way. "Event1Round1" and "Week3Day1" say
# the same two things - which running, and which session inside it - and both
# appear in the same calendar.
OCCURRENCE = re.compile(r"(?:Event|Week|Session)(\d+)", re.I)
ROUND = re.compile(r"(?:Round|Day)(\d+)", re.I)
FINAL = re.compile(r"(?:Grand)?Final", re.I)
SEMI = re.compile(r"Semi", re.I)
REGION_SUFFIX = re.compile(r"_(?:" + "|".join(REGIONS) + r")$", re.I)


def season_of(event_id: str) -> str:
    """"S41", or empty when the id carries no season."""
    found = SEASON.search(event_id or "")
    return found.group(1).upper() if found else ""


def series_key(event_id: str) -> str:
    """What stays the same from one week and one region to the next.

    Strips the vendor prefix, the season and the trailing region, so every
    edition of the same cup in every region collapses onto one key.
    """
    key = re.sub(r"^epicgames[_-]", "", event_id or "", flags=re.I)
    key = SEASON.sub("_", key)
    key = REGION_SUFFIX.sub("", key)
    return re.sub(r"_+", "_", key).strip("_")


def occurrence_of(window_id: str) -> int:
    """Which running of the cup this window belongs to - the edition."""
    found = OCCURRENCE.search(window_id or "")
    return int(found.group(1)) if found else 0


def round_of(window_id: str) -> int:
    """Which session inside that running. 0 when there is only one.

    Finals and semi-finals are numbered high rather than by position, so they
    sort last and never collide with a Day 2 that happens to be the final day
    of a different format.
    """
    text = window_id or ""
    found = ROUND.search(text)
    if found:
        # Round 1 is not a round. A cup played in a single session calls that
        # session Round1 in its id, and treating it as a numbered stage splits
        # the category in two: the editions Epic happened to number land under
        # "Cup - Round 1" and the rest under "Cup", so neither half ever gathers
        # enough editions to measure anything. `db.round_of` collapses it the
        # same way when it reads a stage label; this is the same rule, applied
        # where the number comes out of the id instead.
        number = int(found.group(1))
        return 0 if number <= 1 else number
    if SEMI.search(text):
        return 8
    if FINAL.search(text):
        return 9
    return 0




CONSOLE_ONLY = {"ps4", "ps5", "xboxone", "xsx", "switch", "switch2"}


# Epic's ranked ladder, as the `currentRanking:<ladder>:<n>` requirement counts
# it. The older ladders stop at Unreal = 17; the "combined" ones split Elite
# and Champion into three tiers each and put Unreal at 21. Read off the
# catalogue: "DiamondTestCup" asks for 12 on both, "RankedCupElite" for 15 on
# the old one, "1MillionUnrealCup" for 17 on the old one, and a Solo Victory
# Cup week that admitted Unreal alone asked for 21 on the combined one.
RANK_TIERS = ("Bronze", "Silver", "Gold", "Platinum", "Diamond")
RANK_TOP_OLD = {15: "Elite", 16: "Champion", 17: "Unreal"}
RANK_TOP_COMBINED = {15: "Elite", 18: "Champion", 21: "Unreal"}


def entry_requirement(window: dict) -> str:
    """Who may enter, as Epic states it: "ranked-br-combined:12", or "" for anyone.

    The window's `additionalRequirements` list tokens, some nested a level
    down, and the one that matters is `currentRanking:<ladder>:<n>` - the
    lowest ranked division a player must hold. Several ladders may be named
    (a Reload cup names its own and a track); the highest bar is the one kept.
    A bar of 0 is no bar.
    """
    best, best_n = "", -1

    def walk(items):
        nonlocal best, best_n
        for item in items or []:
            if isinstance(item, list):
                walk(item)
                continue
            m = re.match(r"currentRanking:([a-z0-9-]+):(\d+)$", str(item))
            if m and int(m.group(2)) > best_n:
                best, best_n = f"{m.group(1)}:{m.group(2)}", int(m.group(2))
    walk(window.get("additionalRequirements"))
    return best if best_n > 0 else ""


def rank_name(entry: str) -> str:
    """"Diamond I or above" for "ranked-br-combined:12"; "" when open to all."""
    m = re.match(r"([a-z0-9-]+):(\d+)$", str(entry or ""))
    if not m:
        return ""
    ladder, n = m.group(1), int(m.group(2))
    if n <= 0:
        return ""
    combined = "combined" in ladder
    top = RANK_TOP_COMBINED if combined else RANK_TOP_OLD
    if n < 15:
        name = f"{RANK_TIERS[n // 3]} {('I', 'II', 'III')[n % 3]}"
    elif combined and n < 21:
        base = "Elite" if n < 18 else "Champion"
        name = f"{base} {('I', 'II', 'III')[n % 3]}"
    else:
        name = top.get(n) or top.get(max(k for k in top if k <= n), "Unreal")
    return name if name == "Unreal" else f"{name} or above"


def tags(event: dict, window: dict) -> list[str]:
    """Short labels a person would actually filter on.

    Derived, not typed: what the event pays out, who is allowed in, and what
    kind of competition it is. Anything a study might later want to slice by.
    """
    found = set()
    haystack = " ".join(str(x).lower() for x in (
        event.get("eventId") or "",
        (event.get("displayData") or {}).get("titleLine1") or "",
        (event.get("displayData") or {}).get("detailsDescription") or "",
    ))
    for needle, tag in (("fncs", "fncs"), ("champion series", "fncs"),
                        ("cash cup", "cash-cup"), ("victory cup", "victory-cup"),
                        ("elite series", "elite-series"), ("practice", "practice"),
                        ("open", "open"), ("ranked", "ranked"), ("blitz", "blitz")):
        if needle in haystack:
            found.add(tag)

    kinds = _payout_kinds(window)
    if any("usd" in k or "cash" in k or "money" in k or "currency" in k for k in kinds):
        found.add("cash")
    if any("cosmetic" in k or "item" in k or "skin" in k or "outfit" in k for k in kinds):
        found.add("cosmetic")
    if any("token" in k or "qualif" in k for k in kinds):
        found.add("qualifier")

    platforms = {str(p).lower() for p in (event.get("platforms") or [])}
    if platforms and not (platforms & {"windows", "mac", "pc"}):
        found.add("console-only")
    if "console" in haystack:
        found.add("console")

    season = season_of(event.get("eventId") or "")
    if season:
        found.add(season.lower())
    return sorted(found)
