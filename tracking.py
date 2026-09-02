"""Import a live tournament and keep its tracking up to date.

One request covers everything: the full leaderboard carries the scores, the
games played and the detail of every match. From that single response we get

  · the thresholds at the ranks that matter (qualification, skin, reference);
  · the number of games already played;
  · the format — sealed or free — inferred from the spread of start times;
  · the scoring table, inferred from placements and points scored.

Nothing is typed in by hand.
"""
from __future__ import annotations

import statistics
from datetime import datetime

import calibration
import cito
import db
import scoring_infer

# Recorded on top of your own targets: these feed the curve and the calibration
# even when nobody looks at them.
REFERENCE_RANKS = [1, 5, 10, 20, 50, 100, 250, 500, 1000]

REGION_ALIASES = {"NAE": "NAC", "NA EAST": "NAC", "NAW": "NAW", "NA WEST": "NAW",
                  "EUROPE": "EU", "OCEANIA": "OCE", "BRAZIL": "BR", "MIDDLE EAST": "ME"}


def _logger(conn):
    return lambda endpoint, status: db.log_api_call(conn, endpoint, status)


def region_of(event: dict) -> str:
    raw = (event.get("region") or "").strip().upper()
    return REGION_ALIASES.get(raw, raw) or "EU"


def team_mode_of(rows: list[dict]) -> str:
    """Solo, Duo, Trio or Squad, from the team sizes on the leaderboard."""
    sizes = [len(r.get("members") or []) for r in rows[:50] if r.get("members")]
    if not sizes:
        return "Solo"
    size = statistics.median(sizes)
    return {1: "Solo", 2: "Duo", 3: "Trio", 4: "Squad"}.get(int(round(size)), "Solo")


def game_mode_of(name: str) -> str:
    text = (name or "").lower()
    if "reload" in text:
        return "Reload Zero Build" if "zero build" in text else "Reload"
    if "zero build" in text or "zerobuild" in text:
        return "Zero Build"
    if "blitz" in text:
        return "Blitz Royale"
    return "Battle Royale"


def detect_format(rows: list[dict]) -> dict:
    """Sealed or free? The match schedule answers on its own.

    In a sealed format every team starts the same match in the same minute; in
    a free format each one goes at its own pace and the start times spread out
    across the whole session.
    """
    starts: dict[int, list[datetime]] = {}
    for row in rows[:150]:
        for session in row.get("sessions") or []:
            number, when = session.get("matchNumber"), session.get("startTime")
            if not number or not when:
                continue
            iso = cito._iso(when)
            if iso:
                starts.setdefault(int(number), []).append(datetime.fromisoformat(iso))

    spreads, medians = [], {}
    for number, values in starts.items():
        if len(values) < 5:
            continue
        seconds = sorted(v.timestamp() for v in values)
        medians[number] = seconds[len(seconds) // 2]
        middle = seconds[len(seconds) // 10: max(1, len(seconds) - len(seconds) // 10)]
        if len(middle) > 2:
            spreads.append((max(middle) - min(middle)) / 60)

    games = [int(r["games"]) for r in rows[:200] if r.get("games")]
    out = {"games_mode": "max", "slot_minutes": 0.0,
           "max_games": max(games) if games else db.DEFAULT_MAX_GAMES,
           "played": statistics.median(games) if games else 0,
           "spread_min": round(statistics.median(spreads), 1) if spreads else None,
           "matches_seen": len(medians)}

    if spreads and statistics.median(spreads) <= 6 and len(medians) >= 2:
        gaps = [(medians[b] - medians[a]) / 60
                for a, b in zip(sorted(medians), sorted(medians)[1:])]
        gaps = [g for g in gaps if g > 0]
        if gaps:
            # "sealed" is the value stored in the database and read by the
            # templates; it stays French on purpose.
            out["games_mode"] = "sealed"
            out["slot_minutes"] = round(statistics.median(gaps))
    return out


def ranks_for(conn, kind: str, region: str, depth: int) -> tuple[list[int], list[dict]]:
    """Ranks to record: your own targets first, then the reference ranks."""
    objectives = db.objectives_or_default(conn, kind, region)
    ranks = {o["rank"] for o in objectives} | set(REFERENCE_RANKS)
    return sorted(r for r in ranks if r <= max(depth, 1)), objectives


# --------------------------------------------------------------------------- #
# Import and refresh
# --------------------------------------------------------------------------- #
def import_event(conn, event: dict) -> int:
    """Create the competition from a live tournament. No request here."""
    existing = db.find_by_window(conn, event["event_id"], event["window_id"])
    if existing:
        return existing["id"]

    region = region_of(event)
    comp_id = db.create_competition(
        conn,
        name=event["name"],
        region=region,
        team_mode="Solo",
        game_mode=game_mode_of(event["name"]),
        start_time=event["start_time"],
        end_time=event["end_time"],
        ranks=REFERENCE_RANKS,
    )
    db.update_competition(conn, comp_id, event_id=event["event_id"],
                          window_id=event["window_id"], source="cito",
                          field_size=event.get("field_size"))
    return comp_id


def refresh(conn, comp_id: int, first: bool = False) -> dict:
    """One request: full leaderboard, thresholds, format, scoring, snapshot."""
    comp = db.get_competition(conn, comp_id)
    if comp is None:
        raise cito.CitoError("Competition not found.")
    if not comp["event_id"] or not comp["window_id"]:
        raise cito.CitoError("This competition isn't linked to an API tournament.")

    rows = cito.window_leaderboard(comp["event_id"], comp["window_id"], log=_logger(conn))
    if not rows:
        raise cito.CitoError("The leaderboard is empty for now.")

    db.set_standings(conn, comp_id, rows)
    # the match detail rides along in the same response, so keep it
    n_matches = db.set_matches(conn, comp_id, rows)
    depth = max(r["rank"] for r in rows)
    kind = db.category_of(comp)
    ranks, objectives = ranks_for(conn, kind, comp["region"], depth)
    values = cito.thresholds(rows, ranks)

    fmt = detect_format(rows)
    # While the tournament is still running, the largest rank on the leaderboard
    # is only a floor: keep the biggest value known, including the ones from
    # earlier editions of the same tournament.
    updates = {"field_size": max(depth, int(comp["field_size"] or 0)),
               "max_games": max(int(comp["max_games"] or 0),
                                int(fmt["max_games"] or 0),
                                db.known_max_games(conn, kind, comp["region"]))
               or db.DEFAULT_MAX_GAMES}
    if fmt["games_mode"] == "sealed":
        updates["games_mode"] = "sealed"
        updates["slot_minutes"] = fmt["slot_minutes"]
    if comp["team_mode"] == "Solo":
        updates["team_mode"] = team_mode_of(rows)
    if set(comp["ranks"]) != set(ranks):
        updates["ranks"] = ranks

    scoring = None
    if first or not comp["scoring"]:
        guess = scoring_infer.infer(rows)
        if guess.get("ok"):
            scoring = {"kill": guess["kill"], "kill_cap": guess.get("kill_cap"),
                       "placement": guess["placement"]}
            updates["scoring"] = scoring
            updates["scoring_accuracy"] = guess.get("accuracy")

    db.update_competition(conn, comp_id, **updates)

    played = int(fmt["played"] or 0) or None
    first_estimate = db.get_first_estimate(conn, comp_id) if first else None
    snapshot_id = db.add_snapshot(conn, comp_id, ts=None, points=values,
                                  note="automatic snapshot", games=played)
    db.update_competition(conn, comp_id, tracking=1)

    return {
        "snapshot_id": snapshot_id,
        "depth": depth,
        "ranks": ranks,
        "values": values,
        "played": played,
        "format": fmt,
        "scoring": scoring,
        "objectives": objectives,
        "n_rows": len(rows),
        "n_matches": n_matches,
        "first_estimate": first_estimate,
    }


def freeze_first_estimate(conn, comp_id: int) -> dict | None:
    """Freeze the estimate made before any snapshot, to score it at the end.

    Called once, when the tournament's settings are confirmed: that is the
    moment the scoring table and the game count are known to be right.
    """
    comp = db.get_competition_full(conn, comp_id)
    if comp is None:
        return None
    ranks = comp["ranks"] or REFERENCE_RANKS
    history, scope, exact = calibration.comparable_history(conn, db, comp,
                                                           exclude_id=comp_id)
    wide = [c for c in db.all_full(conn) if c["id"] != comp_id]
    calib = calibration.calibrate(history, ranks, wide=wide)
    guesses = {r: calibration.prior_prediction(comp, calib, r) for r in ranks}
    # History taken from outside the exact category (a Division 5 estimated off
    # Division 1 results) doesn't deserve the same credit: widen the band.
    if not exact:
        for guess in guesses.values():
            if guess and guess.get("ok"):
                span = (guess["high"] - guess["low"]) / 2 * 1.8
                guess["low"] = round(max(0, guess["value"] - span), 1)
                guess["high"] = round(guess["value"] + span, 1)
    db.set_first_estimate(conn, comp_id, guesses, calib.get("n_comps", 0),
                          scope if exact else f"⚠ {scope}")
    return db.get_first_estimate(conn, comp_id)


def import_and_track(conn, event: dict) -> dict:
    """Import then first snapshot, in one go. One request."""
    comp_id = import_event(conn, event)
    result = refresh(conn, comp_id, first=True)
    result["competition_id"] = comp_id
    return result
