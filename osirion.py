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

# Deliberately below the published ceilings: 50 and 80 rather than 60 and 100.
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


# --------------------------------------------------------------------------- #
# Reading Epic's scoring rules
# --------------------------------------------------------------------------- #
PLACEMENT_STAT = "PLACEMENT_STAT_INDEX"
ELIMS_STAT = "TEAM_ELIMS_STAT_INDEX"
VICTORY_STAT = "VICTORY_ROYALE_STAT"


def _rules(window: dict) -> list[dict]:
    rules = window.get("scoringRules")
    if isinstance(rules, dict):
        rules = rules.get("rules") or rules.get("scoringRules")
    return [r for r in (rules or []) if isinstance(r, dict)]


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
    for rule in _rules(window):
        stat = rule.get("trackedStat")
        tier = rule.get("rewardTier")
        points = rule.get("pointsEarned")
        if points is None or tier is None:
            continue
        if stat == PLACEMENT_STAT and rule.get("matchRule") in ("lte", "LTE"):
            placement_rules.append((int(tier), float(points)))
        elif stat == ELIMS_STAT:
            if rule.get("multiplicative"):
                kill_points = float(points) / max(int(tier), 1)
            else:
                # A flat rule on eliminations is a cap dressed as a bonus:
                # "reach N elims, take these points, nothing beyond".
                kill_cap = int(tier)
                kill_points = kill_points or float(points) / max(int(tier), 1)

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
def payout_tiers(window: dict) -> list[dict]:
    """Prize and qualification cuts, which are the tiers a player aims at."""
    tiers = []
    for key in ("payoutTable", "payouts", "payoutsTable"):
        table = window.get(key)
        if isinstance(table, list):
            for row in table:
                if not isinstance(row, dict):
                    continue
                rank = row.get("scoreTier") or row.get("threshold") or row.get("rank")
                payouts = row.get("payouts") or []
                label = ""
                for payout in payouts:
                    if isinstance(payout, dict):
                        kind = (payout.get("rewardType") or payout.get("payoutType") or "")
                        amount = payout.get("value") or payout.get("quantity")
                        if kind and amount:
                            label = f"{kind} {amount}".strip()
                            break
                if rank:
                    tiers.append({"rank": int(rank), "label": label or "Payout"})
            break
    tiers.sort(key=lambda t: t["rank"])
    return tiers


TEAM_SIZES = {1: "Solo", 2: "Duo", 3: "Trio", 4: "Squad"}


def team_mode(event: dict, entries: list[dict] | None = None) -> str:
    """Solo/Duo/Trio/Squad — from the event if it says, else from the rosters."""
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
