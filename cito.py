"""Cito API access: live tournaments and full leaderboards.

Two endpoints are enough:

  GET /fortnite/tournaments/live
      The tournaments currently running, with their schedule, their region,
      and a leaderboard truncated to the first 100.

  GET /fortnite/tournaments/live/{eventId}/{windowId}
      The **full** leaderboard for the window — measured at 1271 rows on a
      Division 2 Practice, with no pagination: the endpoint returns everything.

Each leaderboard row gives the score, the eliminations, the number of games
played, and the match-by-match detail (placement, elims, points). That detail
is what lets us recover the scoring table without typing anything in.

Every request goes through `call()`, which logs it to the database; the monthly
quota is then displayed in the interface.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://api.citoapi.com/api/v1/fortnite"
TIMEOUT = 25
HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILES = [os.path.join(HERE, "cito_key.txt"),
             os.path.join(HERE, "cle_cito.txt")]  # older installs

FREE_QUOTA = 500          # requests per month on Cito's free tier


class CitoError(RuntimeError):
    """A failed call, with the HTTP status when there is one."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Key
# --------------------------------------------------------------------------- #
def read_key() -> str:
    key = os.environ.get("CITO_API_KEY", "").strip()
    if key:
        return key
    for path in KEY_FILES:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    return ""


def save_key(key: str) -> None:
    with open(KEY_FILES[0], "w", encoding="utf-8") as fh:
        fh.write(key.strip() + "\n")


def has_key() -> bool:
    return bool(read_key())


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #
def call(path: str, log=None) -> dict:
    """One GET request. `log(endpoint, status)` is what counts against the quota."""
    key = read_key()
    if not key:
        raise CitoError("No Cito key saved.")
    url = BASE + path
    req = urllib.request.Request(url, headers={
        "x-api-key": key,
        "Accept": "application/json",
        "User-Agent": "fortnite-comp-tracker/5.0",
    })
    status = 0
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status = resp.status
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        detail = exc.read().decode("utf-8", "replace")[:200]
        if log:
            log(path, status)
        if status == 401:
            raise CitoError("Cito key rejected.", status)
        if status == 403:
            raise CitoError("This endpoint is not included in your plan.", status)
        if status == 429:
            raise CitoError("Cito quota exceeded, try again later.", status)
        raise CitoError(f"Error {status}: {detail}", status)
    except Exception as exc:
        if log:
            log(path, 0)
        raise CitoError(f"Server unreachable ({type(exc).__name__}).") from exc
    if log:
        log(path, status)
    return payload


# --------------------------------------------------------------------------- #
# Reading the responses
# --------------------------------------------------------------------------- #
def leaderboard_of(payload) -> list[dict]:
    """Find the leaderboard list whatever shape the response comes in."""
    if isinstance(payload, dict) and isinstance(payload.get("leaderboard"), list):
        return payload["leaderboard"]
    node = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(node, list):
        if node and isinstance(node[0], dict) and isinstance(node[0].get("leaderboard"), list):
            return node[0]["leaderboard"]
        return [r for r in node if isinstance(r, dict) and "rank" in r]
    if isinstance(node, dict):
        for key in ("leaderboard", "entries", "rows", "standings", "results"):
            if isinstance(node.get(key), list):
                return node[key]
    return []


def _iso(value) -> str | None:
    """'2026-08-31T09:00:01.000Z' -> '2026-08-31 11:00:01' in local time."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().replace(tzinfo=None, microsecond=0).isoformat(sep=" ")


def _dig(raw: dict, *names) -> list:
    """Every value carrying one of these names, at any depth.

    The API's envelopes change from one endpoint to the next; rather than bet
    on a path, we look for the key everywhere.
    """
    found = []
    stack = [raw]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in names:
                    found.append(value)
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return found


def prizes_of(raw: dict) -> list[str]:
    """What the tournament advertises as rewards, when it says so.

    Epic describes its tiers in a payout table: a starting rank and what you
    get there. It's the only reliable way to know whether a tournament pays
    cash, a skin, or a qualification — the tournament name doesn't say.
    """
    out = []
    for table in _dig(raw, "payoutTable", "payout_table", "payouts", "prizeBreakdown"):
        for entry in table if isinstance(table, list) else []:
            if not isinstance(entry, dict):
                continue
            rank = entry.get("threshold") or entry.get("rank") or entry.get("place")
            for payout in (entry.get("payouts") or [entry]):
                if not isinstance(payout, dict):
                    continue
                value = payout.get("value") or payout.get("amount") or payout.get("quantity")
                kind = (payout.get("rewardType") or payout.get("type")
                        or payout.get("reward") or "").strip()
                if rank and value:
                    label = {"cash": "$", "usd": "$"}.get(kind.lower(), f" {kind}".rstrip())
                    out.append(f"top {rank}: {value}{label}")
    seen, unique = set(), []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique[:6]


def normalise_event(raw: dict) -> dict:
    """A live tournament, cut down to the fields we use."""
    regions = raw.get("regions") or []
    return {
        "raw": raw,
        "prizes": prizes_of(raw),
        "format_hint": next((str(v) for v in _dig(raw, "shortDescription", "description",
                                                  "longDescription") if v), ""),
        "event_id": raw.get("eventId"),
        "window_id": raw.get("eventWindowId"),
        "name": (raw.get("name") or "Tournament").strip(),
        "region": (regions[0] if regions else "") or "",
        "regions": regions,
        "start_time": _iso(raw.get("startTime")),
        "end_time": _iso(raw.get("endTime")),
        "is_live": bool(raw.get("isLive")),
        "image": raw.get("squareImage") or raw.get("tileImage") or raw.get("imageUrl"),
        "color": (raw.get("displayColors") or {}).get("primary"),
        "preview_count": len(raw.get("leaderboard") or []),
        # teams already ranked: the only hint at field size the API gives before
        # we go and fetch the full leaderboard
        "field_size": raw.get("leaderboardCount"),
    }


def live_events(log=None, path: str = "/tournaments/live") -> tuple[list[dict], dict]:
    """Running tournaments, plus enough to explain an empty list. One request."""
    payload = call(path, log=log)
    raw = payload.get("data") if isinstance(payload, dict) else payload
    raw = [e for e in (raw or []) if isinstance(e, dict)]
    events = [normalise_event(e) for e in raw]
    kept = [e for e in events if e["event_id"] and e["window_id"]]
    meta = {
        "path": path,
        "raw": len(raw),
        "kept": len(kept),
        "message": payload.get("message") if isinstance(payload, dict) else None,
        "freshness": payload.get("freshness_status") if isinstance(payload, dict) else None,
        "updated": _iso(payload.get("last_updated")) if isinstance(payload, dict) else None,
        "fields": sorted(raw[0].keys())[:14] if raw else [],
    }
    return kept, meta


def window_leaderboard(event_id: str, window_id: str, log=None) -> list[dict]:
    """The full leaderboard for one window. One request."""
    payload = call(f"/tournaments/live/{event_id}/{window_id}", log=log)
    rows = []
    for raw in leaderboard_of(payload):
        if not isinstance(raw, dict):
            continue
        rows.append({
            "rank": raw.get("rank"),
            "name": raw.get("displayName") or "",
            "team_id": raw.get("teamId") or "",
            "members": [m.get("displayName") for m in (raw.get("teamMembers") or [])
                        if isinstance(m, dict) and m.get("displayName")],
            "score": raw.get("score"),
            "kills": raw.get("kills"),
            "games": raw.get("matchesPlayed"),
            "sessions": [s for s in (raw.get("sessions") or []) if isinstance(s, dict)],
        })
    rows = [r for r in rows if r["rank"] and r["score"] is not None]
    rows.sort(key=lambda r: r["rank"])
    return rows


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def thresholds(rows: list[dict], ranks) -> dict[int, float]:
    """Exact score at each requested rank, when the leaderboard reaches that far."""
    by_rank = {int(r["rank"]): float(r["score"]) for r in rows if r.get("rank")}
    out = {}
    for rank in ranks:
        rank = int(rank)
        if rank in by_rank:
            out[rank] = by_rank[rank]
    return out


def match_times(rows: list[dict], limit: int = 200) -> list[dict]:
    """Real match start times, read off the sessions of the leading teams.

    Enough to fill in the schedule of a sealed final without typing anything.
    """
    by_number: dict[int, list[str]] = {}
    for row in rows[:limit]:
        for session in row["sessions"]:
            number = session.get("matchNumber")
            start = _iso(session.get("startTime"))
            if number and start:
                by_number.setdefault(int(number), []).append(start)
    out = []
    for number in sorted(by_number):
        starts = sorted(by_number[number])
        out.append({"match": number, "start": starts[len(starts) // 2],
                    "n": len(starts)})
    return out


def played_games(rows: list[dict], top: int = 100) -> int:
    """Games played so far, read off the top of the leaderboard."""
    counts = [int(r["games"]) for r in rows[:top] if r.get("games")]
    if not counts:
        return 0
    counts.sort()
    return counts[len(counts) // 2]          # median, robust to stragglers
