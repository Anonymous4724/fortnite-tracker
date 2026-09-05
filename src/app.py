"""Fortnite Comp Tracker - tracking and forecasting of point thresholds.

To run:
    pip install flask
    python src/app.py
then open http://127.0.0.1:5000
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import webbrowser
from datetime import datetime
from threading import Timer

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, url_for)

import calibration
import cito
import db
import i18n
import predict
import team_stats
import tracking

APP_VERSION = "7.9"

# The database migrates on import, so the server, the scripts and the tests all
# end up on the same schema whichever one is the entry point.
db.init_db()

app = Flask(__name__)
app.json.sort_keys = False
# template edits are picked up without restarting the server
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


def active_language() -> str:
    """The language this reader asked for, remembered in a cookie."""
    return i18n.normalise(request.cookies.get(i18n.COOKIE))


@app.context_processor
def inject_language():
    """`_()` in every template, plus what the language switch needs to render."""
    active = active_language()
    return {"_": lambda text: i18n.translate(text, active),
            "lang": active, "languages": i18n.LANGUAGES,
            "strings": i18n.catalogue(active)}


@app.get("/language/<code>")
def set_language(code: str):
    """Switch language and come back to the page the reader was on."""
    response = redirect(request.referrer or url_for("page_dashboard"))
    response.set_cookie(i18n.COOKIE, i18n.normalise(code),
                        max_age=60 * 60 * 24 * 365, samesite="Lax")
    return response


@app.context_processor
def inject_version():
    with db.session() as conn:
        quota = db.quota_view(conn, per_tournament=REQUESTS_PER_TOURNAMENT,
                              limit=cito.FREE_QUOTA)
    return {"app_version": APP_VERSION, "quota": quota, "has_key": cito.has_key()}


# Refreshing every 10 min over a 3 h session: 18 requests.
REQUESTS_PER_TOURNAMENT = 18
REFRESH_MINUTES = 10

# Live tournaments barely change, so the list is held for a few minutes rather
# than spending a request on every page load.
_LIVE_CACHE = {"at": None, "events": [], "error": None, "meta": {}}
LIVE_TTL_SECONDS = 240


def live_events(force: bool = False, path: str = "/tournaments/live", raw: bool = False):
    """The list, cached for a few minutes, plus enough to explain an empty one.

    The raw API response is kept alongside it: it sometimes carries the payout
    table and the tournament description, which the pages have no need to ship
    to the browser but which are what the real prize tiers are read from.
    """
    now = datetime.now()
    fresh = (_LIVE_CACHE["at"] is not None
             and (now - _LIVE_CACHE["at"]).total_seconds() < LIVE_TTL_SECONDS)
    if fresh and not force:
        return _strip(_LIVE_CACHE["events"], raw), _LIVE_CACHE["error"]
    try:
        with db.session() as conn:
            events, meta = cito.live_events(
                log=lambda e, s: db.log_api_call(conn, e, s), path=path)
        _LIVE_CACHE.update({"at": now, "events": events, "error": None, "meta": meta})
    except cito.CitoError as exc:
        _LIVE_CACHE.update({"at": now, "error": str(exc), "meta": {}})
    return _strip(_LIVE_CACHE["events"], raw), _LIVE_CACHE["error"]


def _strip(events, raw: bool):
    """Without the raw payload the cards stay light enough to send to the browser."""
    if raw:
        return events
    return [{k: v for k, v in e.items() if k != "raw"} for e in events]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def comp_payload(conn, comp_id: int):
    comp = db.get_competition_full(conn, comp_id)
    if comp is None:
        return None
    tl = predict.timeline(comp)
    start, total = tl["start"], tl["total_min"]
    comp["duration_min"] = round(total, 1) if total else None          # until scoring stops moving
    comp["official_min"] = round(tl["official_min"], 1) if tl["official_min"] else None
    comp["extra_min"] = round(tl["extra_min"], 1)
    comp["effective_end"] = tl["effective_end"].isoformat(sep=" ") if tl["effective_end"] else None
    comp["sealed"] = predict.is_sealed(comp)
    comp["schedule"] = [
        {"index": g["index"],
         "start": g["start"].isoformat(sep=" "),
         "visible_at": g["visible_at"].isoformat(sep=" "),
         "minutes": round((g["visible_at"] - tl["start"]).total_seconds() / 60, 1)}
        for g in predict.game_schedule(comp)
    ]
    for snap in comp["snapshots"]:
        ts = datetime.fromisoformat(snap["ts"])
        snap["minutes"] = round((ts - start).total_seconds() / 60, 1)
        snap["progress"] = round(snap["minutes"] / total, 4) if total else None
        if not snap.get("games") and snap["progress"] is not None:
            snap["games_est"] = round(predict.estimated_games(comp, snap["progress"]), 1)
    return comp


def similar_history(conn, comp: dict, exclude_id: int | None = None):
    """Comparable tournaments and the label describing how far we had to widen."""
    history, scope, _ = calibration.comparable_history(conn, db, comp, exclude_id)
    return history, scope


def calibration_scope(comp: dict, level: str) -> dict:
    return {"level": level, "series_id": comp.get("series_id"), "stage": comp.get("stage"),
            "region": comp["region"], "team_mode": comp["team_mode"],
            "game_mode": comp["game_mode"], "kind": db.category_of(comp)}


class BadInput(ValueError):
    """A form field we can't use. Comes back to the browser as a 400."""


# Ceilings on what a form may send. Nothing downstream defends itself: a field
# size of 10**18 goes straight into a logarithm, and one infinity turns every
# average that touches it into an infinity too. Each bound sits comfortably
# above anything Fortnite has actually produced, so a real entry never meets one.
MAX_RANK = 1_000_000        # deeper than any leaderboard Epic publishes
MAX_FIELD = 10_000_000      # no region has ever fielded a fraction of this
MAX_MINUTES = 10_000        # a week of play; past that it is a typo, not a session
MAX_GAMES = 100             # the longest formats run about a dozen
MAX_KILL_CAP = 1_000        # a cap above this is the same as no cap at all
# Scoring tables can dock points for a disqualification, which is why the floor
# is negative rather than zero.
MIN_POINTS, MAX_POINTS = -100_000, 1_000_000
MAX_TEXT = 200              # names, stages, labels
MAX_NOTE = 4_000            # free-form notes


def _seen(value) -> str:
    """The offending value, trimmed to what still fits in a toast."""
    shown = repr(value)
    return shown if len(shown) <= 60 else shown[:57] + "..."


def as_int(value, label: str, default=None, minimum=None, maximum=None):
    """An integer, or a readable error. Never a server error."""
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BadInput(f"'{label}' must be a whole number, got {_seen(value)}.")
    # inf and NaN survive float() and then slip past every comparison below —
    # NaN is false against any bound — so they have to be caught by name.
    if not math.isfinite(number):
        raise BadInput(f"'{label}' must be a whole number, got {_seen(value)}.")
    out = int(number)
    if minimum is not None and out < minimum:
        raise BadInput(f"'{label}' can't be lower than {minimum}.")
    if maximum is not None and out > maximum:
        raise BadInput(f"'{label}' can't be higher than {maximum}.")
    return out


def as_float(value, label: str, default=None, minimum=None, maximum=None):
    if value in (None, ""):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise BadInput(f"'{label}' must be a number, got {_seen(value)}.")
    if not math.isfinite(out):
        raise BadInput(f"'{label}' must be a number, got {_seen(value)}.")
    if minimum is not None and out < minimum:
        raise BadInput(f"'{label}' can't be lower than {minimum}.")
    if maximum is not None and out > maximum:
        raise BadInput(f"'{label}' can't be higher than {maximum}.")
    return out


def as_text(value, label: str, default="", maxlen=MAX_TEXT):
    """Free text, stripped. A number is fine — a field size typed into a name
    box is still a name — but a list or an object is a mistake worth naming.

    `default=None` marks the field as required: an empty box then answers with
    "is required" instead of silently writing an empty string.
    """
    if isinstance(value, (bool, list, tuple, set, dict)):
        raise BadInput(f"'{label}' must be text, got {_seen(value)}.")
    if isinstance(value, float) and not math.isfinite(value):
        raise BadInput(f"'{label}' must be text, got {_seen(value)}.")
    text = "" if value is None else str(value).strip()
    if not text:
        if default is None:
            raise BadInput(f"'{label}' is required.")
        return default
    # A megabyte of text in a name box is never a name, and the column it lands
    # in is displayed untruncated on every page that lists tournaments.
    if len(text) > maxlen:
        raise BadInput(f"'{label}' is too long: {len(text)} characters, "
                       f"{maxlen} allowed.")
    return text


# What a checkbox turns into on the way here: a real boolean over JSON, a word
# from a plain form post.
_TRUE_WORDS = {"true", "yes", "on", "1"}
_FALSE_WORDS = {"false", "no", "off", "0"}


def as_bool(value, label: str, default=False):
    """A yes/no field, whichever of the several shapes it arrives in."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _TRUE_WORDS:
        return True
    if isinstance(value, str) and value.strip().lower() in _FALSE_WORDS:
        return False
    raise BadInput(f"'{label}' must be yes or no, got {_seen(value)}.")


def as_choice(value, label: str, allowed, default=None):
    """One value out of a fixed set, the set spelled out when it isn't.

    A region that reaches the database misspelled is not caught later: it simply
    stops matching, and the tournament quietly drops out of every comparison
    drawn from its own region.
    """
    options = [str(option) for option in allowed]
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            raise BadInput(f"'{label}' is required — one of: {', '.join(options)}.")
        return default
    if not isinstance(value, str) or value.strip() not in options:
        raise BadInput(f"'{label}' must be one of: {', '.join(options)}. "
                       f"Got {_seen(value)}.")
    return value.strip()


def as_mapping(value, label: str) -> dict:
    """An object of key/value pairs. A JSON string holding one is accepted too:
    the older form posts still send points that way."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise BadInput(f"'{label}' isn't readable JSON: {_seen(value)}.")
    if not isinstance(value, dict):
        raise BadInput(f"'{label}' must be a set of values, got {_seen(value)}.")
    return value


def as_rows(value, label: str) -> list:
    """A list of objects — one per edition, threshold or tier."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise BadInput(f"'{label}' isn't readable JSON: {_seen(value)}.")
    if not isinstance(value, list):
        raise BadInput(f"'{label}' must be a list, got {_seen(value)}.")
    for i, row in enumerate(value, 1):
        if not isinstance(row, dict):
            raise BadInput(f"'{label}' line {i} must be a set of values, "
                           f"got {_seen(row)}.")
    return value


def as_scoring(value, label: str = "Scoring") -> dict:
    """Check a scoring table on the way in, not on the way out.

    The shape is {"kill": points, "kill_cap": n or nothing,
    "placement": [[first rank, last rank, points], ...]}.

    Every forecast this app makes runs through one of these, so a tier like
    [1, "abc", 60] does not fail where it was typed. It fails weeks later,
    inside a percentile, on a tournament nobody was editing at the time — and
    what surfaces is a wrong threshold rather than an error. Checking here is
    the only place the arithmetic downstream can be held to.
    """
    table = as_mapping(value, label)
    rows = table.get("placement")
    if isinstance(rows, str) and rows.strip():
        try:
            rows = json.loads(rows)
        except ValueError:
            raise BadInput(f"'{label}' placement must be a list of tiers, "
                           f"got {_seen(rows)}.")
    if rows is None or rows == "":
        rows = []
    if not isinstance(rows, (list, tuple)):
        raise BadInput(f"'{label}' placement must be a list of tiers, "
                       f"got {_seen(rows)}.")
    tiers = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise BadInput(f"'{label}' tier {i} must read "
                           f"[first rank, last rank, points], got {_seen(row)}.")
        low = as_int(row[0], f"{label} tier {i}, first rank",
                     minimum=1, maximum=MAX_RANK)
        high = as_int(row[1], f"{label} tier {i}, last rank",
                      minimum=1, maximum=MAX_RANK)
        points = as_float(row[2], f"{label} tier {i}, points",
                          minimum=MIN_POINTS, maximum=MAX_POINTS)
        if low is None or high is None or points is None:
            raise BadInput(f"'{label}' tier {i} needs three numbers, "
                           f"got {_seen(row)}.")
        # The tiers are read as ranges and interpolated between; one that runs
        # backwards covers no ranks at all and silently loses its points.
        if low > high:
            raise BadInput(f"'{label}' tier {i} runs from rank {low} to {high} — "
                           f"the first rank has to come first.")
        tiers.append([low, high, points])
    cap = as_int(table.get("kill_cap"), f"{label}: elimination cap",
                 minimum=0, maximum=MAX_KILL_CAP)
    return {"kill": as_float(table.get("kill"), f"{label}: points per elimination",
                             default=0, minimum=0, maximum=MAX_POINTS),
            # zero and blank both mean "uncapped", which is how db stores it
            "kill_cap": cap or None,
            "placement": tiers}


def as_ranks(value, label: str = "Tracked ranks"):
    """The ranks to follow, from a list or from the comma-separated line the
    settings form sends."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [part for part in value.replace(";", ",").split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise BadInput(f"'{label}' must be a list of ranks, got {_seen(value)}.")
    ranks = [as_int(rank, label, minimum=1, maximum=MAX_RANK) for rank in value]
    return sorted({rank for rank in ranks if rank is not None})


def as_timestamp(value, label: str, default=None):
    """A date we can store, or a clear error."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if not isinstance(value, str):
        raise BadInput(f"'{label}' isn't a valid date: {_seen(value)}.")
    try:
        return db.norm_ts(value)
    except ValueError:
        raise BadInput(f"'{label}' isn't a valid date: {_seen(value)}.")


@app.errorhandler(BadInput)
def _on_bad_input(exc):
    return jsonify({"ok": False, "error": str(exc)}), 400


@app.errorhandler(ValueError)
def _on_bad_value(exc):
    # Safety net: a conversion that failed somewhere is still a data-entry
    # mistake, not a crash. The user gets to read what went wrong.
    return jsonify({"ok": False, "error": str(exc)}), 400


def body() -> dict:
    return request.get_json(silent=True) or request.form.to_dict() or {}


def known_scoring_id(conn, scoring_id, label: str = "Scoring"):
    """A saved-scoring id that still exists.

    Storing one that has since been deleted fails as a foreign key error three
    layers down, or worse, quietly stores a link that resolves to nothing.
    """
    if scoring_id and db.get_scoring(conn, scoring_id) is None:
        raise BadInput(f"'{label}' points to a scoring table that no longer exists.")
    return scoring_id


def as_points(value, label: str, keep_blank: bool = False) -> dict:
    """A {rank: points} map, both sides checked.

    `keep_blank` maps a cleared box to None, which is how the update endpoints
    say "delete this rank" as opposed to "leave it alone".
    """
    out = {}
    for rank, points in as_mapping(value, label).items():
        rank = as_int(rank, f"{label}: rank", minimum=1, maximum=MAX_RANK)
        if rank is None:
            continue
        # "None" as a literal string is not a typo: a value that was NULL used to
        # be rendered straight into the input and posted back as that word.
        blank = points is None or (isinstance(points, str)
                                   and points.strip() in ("", "None"))
        if blank:
            if keep_blank:
                out[rank] = None
            continue
        out[rank] = as_float(points, f"{label} for rank {rank}",
                             minimum=MIN_POINTS, maximum=MAX_POINTS)
    return out


def competition_fields(data: dict) -> dict:
    """The competition columns present in `data`, each pulled through a check.

    One definition of what a tournament is, shared by the create, update and
    confirm endpoints — they used to disagree about which fields they checked.
    Only the keys actually sent come back, so a partial update stays partial,
    and a field backing a NOT NULL column is dropped when it arrives empty: a
    cleared box means "leave this alone", never "erase it".
    """
    fields = {}
    for key, label, maxlen in (("name", "Name", MAX_TEXT),
                               ("family", "Tournament", MAX_TEXT),
                               ("stage", "Stage", MAX_TEXT),
                               ("notes", "Notes", MAX_NOTE)):
        if key in data:
            fields[key] = as_text(data[key], label, maxlen=maxlen)
    for key, label, allowed in (("region", "Region", db.REGIONS),
                                ("team_mode", "Mode", db.TEAM_MODES),
                                ("game_mode", "Type", db.GAME_MODES),
                                ("games_mode", "Counting", db.GAMES_MODES)):
        if key in data:
            fields[key] = as_choice(data[key], label, allowed)
    if "qualifier" in data:
        fields["qualifier"] = as_bool(data["qualifier"], "Qualifying round")
    start = as_timestamp(data.get("start_time"), "Start")
    if start:
        fields["start_time"] = start
    if "end_time" in data:
        # unlike the start, an empty official end is meaningful: the tournament
        # simply has no announced one
        fields["end_time"] = as_timestamp(data["end_time"], "Official end")
    for key, label, low in (("game_minutes", "Game length", 1),
                            ("tracker_lag_min", "Tracker lag", 0),
                            ("slot_minutes", "Interval", 0)):
        value = as_float(data.get(key), label, minimum=low, maximum=MAX_MINUTES)
        if value is not None:
            fields[key] = value
    games = as_int(data.get("max_games"), "Games", minimum=1, maximum=MAX_GAMES)
    if games is not None:
        fields["max_games"] = games
    if "field_size" in data:
        # 0 is how the form says "not known yet", and the column is nullable
        fields["field_size"] = as_int(data["field_size"], "Expected teams",
                                      default=0, minimum=0, maximum=MAX_FIELD) or None
    ranks = as_ranks(data.get("ranks"))
    if ranks is not None:
        fields["ranks"] = ranks
    return fields


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.route("/manual")
def page_index():
    with db.session() as conn:
        comps = db.list_competitions(conn)
        for comp in comps:
            comp["n_snapshots"] = len(db.get_snapshots(conn, comp["id"]))
            comp["thresholds"] = db.final_points(conn, comp["id"])
            comp["finalised"] = bool(db.get_finals(conn, comp["id"]))
            comp["ranks_str"] = ", ".join(str(r) for r in comp["ranks"])
        scorings = db.list_scorings(conn)
        series = db.list_series(conn)
    return render_template("index.html", comps=comps, regions=db.REGIONS,
                           scorings=scorings, series=series,
                           team_modes=db.TEAM_MODES, game_modes=db.GAME_MODES,
                           default_ranks=db.DEFAULT_RANKS, game_minutes=db.GAME_MINUTES,
                           default_lag=db.DEFAULT_TRACKER_LAG,
                           default_max_games=db.DEFAULT_MAX_GAMES,
                           games_modes=db.GAMES_MODES,
                           now=datetime.now().strftime("%Y-%m-%dT%H:%M"))


@app.route("/competition/<int:comp_id>")
def page_competition(comp_id: int):
    with db.session() as conn:
        comp = comp_payload(conn, comp_id)
        if comp is None:
            return "Competition not found", 404
        scorings = db.list_scorings(conn)
        series_row = db.get_series(conn, comp["series_id"]) if comp.get("series_id") else None
    return render_template("competition.html", comp=comp, regions=db.REGIONS,
                           scorings=scorings, serie=series_row,
                           team_modes=db.TEAM_MODES, game_modes=db.GAME_MODES,
                           game_minutes=db.GAME_MINUTES, presets=db.SCORING_PRESETS,
                           games_modes=db.GAMES_MODES,
                           now=datetime.now().strftime("%Y-%m-%dT%H:%M"))


@app.route("/history")
def page_history():
    """History grouped by exact category, and how good the opening estimates were."""
    with db.session() as conn:
        groups = {}
        for row in db.list_competitions(conn):
            comp = db.get_competition_full(conn, row["id"])
            # A harvested tournament has final results and no readings: nobody
            # watched it live. Requiring a snapshot hid every one of them, and
            # writing a fake snapshot equal to the final result — which an
            # earlier import did — corrupts the live backtest instead.
            if not comp or not (comp["snapshots"] or comp.get("finals")):
                continue
            kind = db.category_of(comp)
            key = (kind, comp["region"])
            entry = groups.setdefault(key, {"kind": kind, "region": comp["region"],
                                            "family": comp["family"], "stage": comp["stage"],
                                            "comps": [], "ranks": set(), "no_scoring": 0,
                                            "scoring_ids": set()})
            if not comp.get("scoring_known"):
                entry["no_scoring"] += 1
            if comp.get("scoring_id"):
                entry["scoring_ids"].add(comp["scoring_id"])
            if not comp.get("field_size"):
                entry["no_field"] = entry.get("no_field", 0) + 1
            finals = db.final_points(conn, comp["id"])
            estimate = db.get_first_estimate(conn, comp["id"])
            entry["comps"].append({
                "id": comp["id"], "name": comp["name"], "date": comp["start_time"],
                "family": comp["family"], "stage": comp["stage"],
                "field_size": comp["field_size"],
                "n_snapshots": len(comp["snapshots"]),
                "done": predict.is_complete(comp),
                "max_games": comp["max_games"],
                "kill": (comp["scoring"] or {}).get("kill"),
                "thresholds": finals,
                "estimate": estimate.get("ranks") if estimate else {},
            })
            entry["ranks"].update(finals)

        views = []
        for entry in groups.values():
            entry["comps"].sort(key=lambda c: c["date"], reverse=True)
            ranks = sorted(entry["ranks"])[:6]
            errors = []
            for comp in entry["comps"]:
                per = [abs(g["value"] - comp["thresholds"][r]) / comp["thresholds"][r] * 100
                       for r, g in (comp["estimate"] or {}).items()
                       if comp["thresholds"].get(r)]
                comp["est_error"] = round(sum(per) / len(per), 1) if per else None
                if comp["est_error"] is not None:
                    errors.append(comp["est_error"])
            entry["ranks"] = ranks
            entry["n"] = len(entry["comps"])
            entry["done"] = sum(1 for c in entry["comps"] if c["done"])
            entry["est_error"] = round(sum(errors) / len(errors), 1) if errors else None
            entry["est_trend"] = errors
            views.append(entry)
        views.sort(key=lambda v: (-v["n"], v["kind"]))
        known = db.known_categories(conn)
        scoring_names = {s["id"]: s["name"] for s in db.list_scorings(conn)}
        for v in views:
            ids = [i for i in v.pop("scoring_ids", set()) if i in scoring_names]
            v["scoring_id"] = ids[0] if len(ids) == 1 else None
            v["scoring_name"] = scoring_names.get(v["scoring_id"]) if v["scoring_id"] else None
        scores = db.first_estimate_scores(conn)

    with db.session() as conn:
        scorings = db.list_scorings(conn)
        # The list arrives rendered rather than fetched: the page has to say
        # something on the first paint, and the most recent tournaments are the
        # answer to the question nobody has typed yet.
        found = db.search_competitions(conn, limit=WHOLE_HISTORY)
        initial = [_search_row(conn, comp) for comp in found[:DEFAULT_SEARCH]]
        counted = db.facets(conn)
        duplicates = [[_duplicate_row(conn, comp) for comp in pair]
                      for pair in db.find_duplicates(conn)]
    return render_template("history.html", views=views, scores=scores,
                           scorings=scorings,
                           families=sorted({c["family"] for c in known}),
                           stages=db.KNOWN_STAGES,
                           initial=initial, total=len(found), facets=counted,
                           duplicates=duplicates, page_size=MAX_SEARCH,
                           states=SEARCH_STATES)


@app.route("/train")
def page_train():
    with db.session() as conn:
        categories = db.known_categories(conn)
        scorings = db.list_scorings(conn)
        objectives = db.all_objectives(conn)
    return render_template("train.html", categories=categories, scorings=scorings,
                           families=sorted({c["family"] for c in categories}),
                           stages=db.KNOWN_STAGES,
                           objectives=objectives, regions=db.REGIONS,
                           team_modes=db.TEAM_MODES, game_modes=db.GAME_MODES,
                           default_scoring=db.DEFAULT_SCORING,
                           today=datetime.now().strftime("%Y-%m-%d"))


def _category_input(data: dict) -> dict:
    """The category description the Train page sends, checked once.

    Both the cold estimate and the save go through the same description, so
    they are read here rather than twice with two sets of assumptions.
    """
    return {"kind": as_text(data.get("kind"), "Tournament"),
            "stage": as_text(data.get("stage"), "Stage"),
            "region": as_choice(data.get("region"), "Region", db.REGIONS, default=""),
            "team_mode": as_choice(data.get("team_mode"), "Mode", db.TEAM_MODES,
                                   default="Duo"),
            "game_mode": as_choice(data.get("game_mode"), "Type", db.GAME_MODES,
                                   default="Battle Royale"),
            "games_mode": as_choice(data.get("games_mode"), "Counting",
                                    db.GAMES_MODES, default="max"),
            "max_games": as_int(data.get("max_games"), "Games",
                                default=db.DEFAULT_MAX_GAMES,
                                minimum=1, maximum=MAX_GAMES),
            "duration_min": as_float(data.get("duration_min"), "Duration",
                                     default=180.0, minimum=0, maximum=MAX_MINUTES),
            "field_size": as_int(data.get("field_size"), "Teams ranked",
                                 default=0, minimum=0, maximum=MAX_FIELD) or None,
            "scoring_id": as_int(data.get("scoring_id"), "Scoring", minimum=1),
            "scoring": as_scoring(data.get("scoring"), "Scoring")}


def _sample_comp(clean: dict, scoring: dict) -> dict:
    """A stand-in tournament to hang a cold estimate on."""
    family, stage = clean["kind"], clean["stage"]
    return {"id": -1, "name": family,
            "family": family,
            "stage": stage,
            "kind": f"{family} · {stage}" if stage else family,
            # a scoring table that was picked or typed in is data; a fallback
            # one is not, and the estimate must not lean on it
            "scoring_known": bool(clean["scoring"]["placement"] or clean["scoring_id"]),
            "region": clean["region"],
            "team_mode": clean["team_mode"],
            "game_mode": clean["game_mode"],
            "scoring": scoring,
            "max_games": clean["max_games"],
            "games_mode": clean["games_mode"],
            "field_size": clean["field_size"],
            "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": None, "ranks": [], "snapshots": [], "finals": {},
            "slot_minutes": 0}


def _resolve_scoring(conn, clean: dict) -> dict:
    """Which scoring table to use: the one typed in, the one picked, the category's."""
    if clean["scoring"]["placement"]:
        return clean["scoring"]
    if clean["scoring_id"]:
        found = db.scoring_payload(conn, clean["scoring_id"])
        if found:
            return found
    for cat in db.known_categories(conn):
        if cat["kind"] == clean["kind"] and cat["scoring"]:
            return cat["scoring"]
    return dict(db.DEFAULT_SCORING)


@app.get("/api/history/entries")
def api_manual_entries():
    """Hand-entered editions — the ones carrying the corrections."""
    with db.session() as conn:
        items = db.list_manual_entries(conn, request.args.get("family", ""),
                                       request.args.get("stage", ""),
                                       request.args.get("region", ""))
    return jsonify({"ok": True, "items": items})


def _entry_filter(data: dict) -> tuple:
    """The category the Train page wants listed back after a change."""
    return (as_text(data.get("family"), "Tournament"),
            as_text(data.get("stage"), "Stage"),
            as_choice(data.get("region"), "Region", db.REGIONS, default=""))


@app.put("/api/history/entries/<int:comp_id>")
def api_update_manual_entry(comp_id: int):
    data = body()
    date = as_timestamp(data.get("date"), "Date")
    # "" is not the same as absent here: it is how the form clears a field size,
    # which db turns into NULL. Absent means the box was never on screen.
    field_size = (as_int(data["field_size"], "Ranked teams", default=0,
                         minimum=0, maximum=MAX_FIELD)
                  if "field_size" in data else None)
    thresholds = (as_points(data["thresholds"], "Thresholds")
                  if data.get("thresholds") is not None else None)
    family, stage, region = _entry_filter(data)
    with db.session() as conn:
        comp = db.get_competition(conn, comp_id)
        if comp is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        if date and not as_bool(data.get("force"), "Force"):
            twin = db.find_duplicate(conn, comp["family"], comp["stage"], comp["region"],
                                     date, exclude_id=comp_id)
            if twin:
                return jsonify({"ok": False, "duplicate": True,
                                "error": f"'{twin['name']}' already sits on that date "
                                         f"in the same category and region."}), 409
        db.update_manual_entry(
            conn, comp_id, date=date,
            edition=(as_text(data["edition"], "Edition")
                     if data.get("edition") is not None else None),
            field_size=field_size,
            max_games=as_int(data.get("max_games"), "Games",
                             minimum=1, maximum=MAX_GAMES),
            thresholds=thresholds)
        calibration.invalidate(conn)
        items = db.list_manual_entries(conn, family, stage, region)
    return jsonify({"ok": True, "items": items})


@app.delete("/api/history/entries/<int:comp_id>")
def api_delete_manual_entry(comp_id: int):
    data = body()
    family, stage, region = _entry_filter(data)
    with db.session() as conn:
        db.delete_competition(conn, comp_id)
        calibration.invalidate(conn)
        items = db.list_manual_entries(conn, family, stage, region)
    return jsonify({"ok": True, "items": items})


# --------------------------------------------------------------------------- #
# API - searching the history
# --------------------------------------------------------------------------- #
# The states a tournament can be filtered on, spelled the way db.search_competitions
# reads them.
SEARCH_STATES = ["finished", "tracked", "no_scoring", "no_field", "no_thresholds"]

# What one search may hand back. Above this the answer stops being a list a
# person reads and becomes a download, and narrowing is the point of the page.
MAX_SEARCH = 5_000
DEFAULT_SEARCH = 400

# The history is scanned whole before anything is cut: the tag pass below drops
# rows, and a limit applied before it would return a short page rather than the
# first `limit` answers.
WHOLE_HISTORY = 1_000_000


def as_filters(values, label: str, allowed=None) -> list[str]:
    """One row of filter chips, out of the query string.

    The page sends `region=EU&region=NAC`; a link someone pasted sends
    `region=EU,NAC`. Both spellings mean the same row, so both are read, and a
    value outside the allowed set is named rather than silently matching nothing.
    """
    out: list[str] = []
    for raw in values or []:
        for part in str(raw).split(","):
            value = (as_choice(part, label, allowed, default="") if allowed
                     else as_text(part, label))
            if value and value not in out:
                out.append(value)
    return out


def as_day(value, label: str) -> str:
    """A calendar day, `YYYY-MM-DD`.

    Deliberately not `as_timestamp`: the search compares these against the
    stored start times as text, and an hour appended to a window bound would
    quietly move the window by a day.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if not isinstance(value, str):
        raise BadInput(f"'{label}' isn't a valid date: {_seen(value)}.")
    try:
        return db.norm_ts(value)[:10]
    except ValueError:
        raise BadInput(f"'{label}' isn't a valid date: {_seen(value)}.")


def _search_row(conn, comp: dict) -> dict:
    """One line of the history list.

    Trimmed on purpose: a whole competition carries its scoring table, and
    fifteen tiers repeated across a few thousand rows is megabytes the list
    never looks at.
    """
    finals = db.final_points(conn, comp["id"])
    return {"id": comp["id"], "name": comp["name"], "date": comp["start_time"],
            "kind": db.category_of(comp), "family": comp.get("family") or "",
            "stage": comp.get("stage") or "", "region": comp["region"],
            "team_mode": comp["team_mode"], "game_mode": comp["game_mode"],
            "season": comp.get("season") or "",
            "tags": json.loads(comp.get("tags") or "[]"),
            "max_games": comp.get("max_games"), "field_size": comp.get("field_size"),
            "thresholds": finals, "n_thresholds": len(finals),
            "finished": bool(comp.get("finished")), "tracking": bool(comp.get("tracking")),
            "scoring_known": bool(comp.get("scoring_known")),
            "source": comp.get("source") or "", "match": comp.get("match")}


def _duplicate_row(conn, comp: dict) -> dict:
    """One side of a suspected duplicate: enough of it to choose between the two."""
    finals = db.final_points(conn, comp["id"])
    return {"id": comp["id"], "name": comp["name"], "kind": db.category_of(comp),
            "region": comp["region"], "date": comp["start_time"],
            "field_size": comp.get("field_size"), "n_thresholds": len(finals),
            "source": comp.get("source") or ""}


@app.get("/api/history/search")
def api_history_search():
    """The history, narrowed the way a person narrows it: text, chips, dates, state."""
    args = request.args
    tags = as_filters(args.getlist("tag"), "Tag")
    limit = as_int(args.get("limit"), "Limit", default=DEFAULT_SEARCH,
                   minimum=1, maximum=MAX_SEARCH)
    with db.session() as conn:
        rows = db.search_competitions(
            conn,
            text=as_text(args.get("q"), "Search"),
            regions=as_filters(args.getlist("region"), "Region", db.REGIONS),
            team_modes=as_filters(args.getlist("team_mode"), "Mode", db.TEAM_MODES),
            game_modes=as_filters(args.getlist("game_mode"), "Type", db.GAME_MODES),
            season=as_text(args.get("season"), "Season"),
            since=as_day(args.get("since"), "From"),
            until=as_day(args.get("until"), "To"),
            state=as_choice(args.get("state"), "State", SEARCH_STATES, default=""),
            limit=WHOLE_HISTORY)
        # A row carrying any of the chosen tags answers, the way any other row of
        # chips answers. db.search_competitions asks a row to carry all of them,
        # which is a different question, so the tags are settled here instead.
        if tags:
            wanted = set(tags)
            rows = [row for row in rows
                    if wanted & set(json.loads(row.get("tags") or "[]"))]
        items = [_search_row(conn, comp) for comp in rows[:limit]]
    return jsonify({"ok": True, "items": items, "n": len(items),
                    "matched": len(rows), "limit": limit})


@app.get("/api/history/facets")
def api_history_facets():
    """What there is to filter on, counted, so the page offers only what exists."""
    with db.session() as conn:
        counted = db.facets(conn)
        total = len(db.list_competitions(conn))
    return jsonify({"ok": True, "facets": counted, "total": total})


@app.get("/api/history/duplicates")
def api_history_duplicates():
    """Pairs that look like the same session entered twice."""
    with db.session() as conn:
        groups = [[_duplicate_row(conn, comp) for comp in pair]
                  for pair in db.find_duplicates(conn)]
    return jsonify({"ok": True, "groups": groups, "n": len(groups)})


@app.post("/api/estimate")
def api_estimate():
    """Cold estimate of one threshold, saving nothing."""
    data = body()
    clean = _category_input(data)
    rank = as_int(data.get("rank"), "Rank", default=0, minimum=1, maximum=MAX_RANK)
    if not rank:
        return jsonify({"ok": False, "error": "Give the rank of the threshold."}), 400
    with db.session() as conn:
        scoring = _resolve_scoring(conn, clean)
        sample = _sample_comp(clean, scoring)
        result = _cold_estimate(conn, sample, [rank])
    guess = result["ranks"].get(rank)
    return jsonify({"ok": True, "rank": rank, "estimate": guess,
                    "reason": result.get("reason"), "version": APP_VERSION,
                    "n_comps": result["n_comps"], "scope": result["scope"],
                    "scoring": scoring, "max_games": sample["max_games"]})


@app.post("/api/history/manual")
def api_add_history():
    """Save several past editions at once. No API request."""
    data = body()
    clean = _category_input(data)
    kind, region, stage = clean["kind"], clean["region"], clean["stage"]
    if not kind or not region:
        return jsonify({"ok": False, "error": "Category and region are required."}), 400

    editions = []
    for i, row in enumerate(as_rows(data.get("rows"), "Editions"), 1):
        editions.append({
            "date": as_timestamp(row.get("date"), f"Edition {i}: date"),
            "label": as_text(row.get("label"), f"Edition {i}: label"),
            "values": as_points(row.get("values"), f"Edition {i}: thresholds"),
            "max_games": as_int(row.get("max_games"), f"Edition {i}: games",
                                default=clean["max_games"], minimum=1, maximum=MAX_GAMES),
            "field_size": as_int(row.get("field_size"), f"Edition {i}: ranked teams",
                                 default=0, minimum=0, maximum=MAX_FIELD)
                          or clean["field_size"],
        })
    if not editions:
        return jsonify({"ok": False, "error": "No edition to save."}), 400

    with db.session() as conn:
        scoring = _resolve_scoring(conn, clean)

        # what the cold estimate was BEFORE the entry, so the effect is visible
        sample = _sample_comp(clean, scoring)
        ranks = sorted({rank for edition in editions for rank in edition["values"]})
        before = _cold_estimate(conn, sample, ranks)

        # Forgetting to change the date is the easiest mistake to make, so flag
        # it before saving rather than counting the same edition twice in the
        # calibration.
        if not as_bool(data.get("force"), "Save anyway"):
            for edition in editions:
                twin = db.find_duplicate(conn, kind, stage, region, edition["date"])
                if twin:
                    return jsonify({
                        "ok": False, "duplicate": True,
                        "existing": {"id": twin["id"], "name": twin["name"],
                                     "date": twin["start_time"][:10]},
                        "error": f"'{twin['name']}' is already saved on "
                                 f"{twin['start_time'][:10]} in {region}. "
                                 f"Did you forget to change the date?"}), 409

        created = []
        for edition in editions:
            if not edition["values"]:
                continue
            created.append(db.create_history_entry(
                conn, kind=kind, region=region, date=edition["date"],
                values=edition["values"], max_games=edition["max_games"],
                scoring=scoring, team_mode=clean["team_mode"],
                game_mode=clean["game_mode"], label=edition["label"], stage=stage,
                duration_min=clean["duration_min"],
                field_size=edition["field_size"]))
        calibration.invalidate(conn)
        after = _cold_estimate(conn, sample, ranks)

    return jsonify({"ok": True, "created": len(created), "ranks": ranks,
                    "before": before, "after": after})


def _cold_estimate(conn, sample: dict, ranks: list[int]) -> dict:
    """Cold estimate for a stand-in tournament of this category."""
    history, scope, _ = calibration.comparable_history(conn, db, sample, exclude_id=-1)
    calib = calibration.calibrate(history, ranks, broad=calibration.shared_broad(conn, db))
    out = {"scope": scope, "n_comps": calib.get("n_comps", 0), "ranks": {}, "reason": None}
    for rank in ranks:
        guess = calibration.prior_prediction(sample, calib, rank)
        if guess and not guess.get("ok") and guess.get("reason"):
            out["reason"] = guess["reason"]
        if guess and guess.get("ok"):
            out["ranks"][rank] = {"value": guess["value"], "low": guess["low"],
                                  "high": guess["high"], "source": guess.get("source"),
                                  "share": guess.get("share"), "level": guess.get("level"),
                                  "ref_rank": guess.get("ref_rank"),
                                  "field": guess.get("field"),
                                  "guessed_field": guess.get("guessed_field"),
                                  "field_effect": guess.get("field_effect"),
                                  "n": guess.get("n", 0)}
    return out


@app.route("/scoring")
def page_scoring():
    with db.session() as conn:
        scorings = db.list_scorings(conn)
    return render_template("scoring.html", scorings=scorings,
                           presets=db.SCORING_PRESETS,
                           team_modes=db.TEAM_MODES, game_modes=db.GAME_MODES)


@app.route("/series")
def page_series():
    with db.session() as conn:
        series = db.list_series(conn)
        scorings = db.list_scorings(conn)
        for s in series:
            s["n_editions"] = len(db.series_editions(conn, s["id"]))
    return render_template("series.html", series=series, scorings=scorings,
                           regions=db.REGIONS, team_modes=db.TEAM_MODES,
                           game_modes=db.GAME_MODES, default_stages=db.DEFAULT_STAGES,
                           games_modes=db.GAMES_MODES,
                           today=datetime.now().strftime("%Y-%m-%d"))


@app.route("/series/<int:series_id>")
def page_series_detail(series_id: int):
    with db.session() as conn:
        series_row = db.get_series(conn, series_id)
        if series_row is None:
            return "Series not found", 404
        editions = db.series_editions(conn, series_id)
        scorings = db.list_scorings(conn)
    return render_template("series_detail.html", serie=series_row, editions=editions,
                           scorings=scorings, regions=db.REGIONS, games_modes=db.GAMES_MODES,
                           team_modes=db.TEAM_MODES, game_modes=db.GAME_MODES,
                           today=datetime.now().strftime("%Y-%m-%d"))



# --------------------------------------------------------------------------- #
# Dashboard and live tracking
# --------------------------------------------------------------------------- #
@app.route("/")
def page_dashboard():
    events, error = live_events(force=request.args.get("refresh") == "1")
    with db.session() as conn:
        removed = db.cleanup_unpredicted(conn)

        known = {}
        for event in events:
            comp = db.find_by_window(conn, event["event_id"], event["window_id"])
            event["competition_id"] = comp["id"] if comp else None
            event["tracked"] = bool(comp and comp["tracking"])
            event["kind"] = db.category_of(event["name"])
            event["objectives"] = db.get_objectives(conn, event["kind"],
                                                    tracking.region_of(event))
            known[event["event_id"]] = event

        past = []
        for row in db.list_competitions(conn):
            # "history" is the stored source tag, not display text
            if not row["tracking"] or row["source"] == "history":
                continue
            if any(e.get("competition_id") == row["id"] for e in events):
                continue
            row["n_snapshots"] = len(db.get_snapshots(conn, row["id"]))
            row["kind"] = db.category_of(row)
            row["thresholds"] = db.final_points(conn, row["id"])
            past.append(row)

        groups = {}
        for comp in past:
            groups.setdefault(f"{comp['kind']} · {comp['region']}", []).append(comp)

    return render_template("dashboard.html", events=events, error=error,
                           groups=groups, n_past=len(past), removed=removed,
                           refresh_minutes=REFRESH_MINUTES,
                           meta=_LIVE_CACHE.get("meta") or {},
                           fetched_at=_LIVE_CACHE.get("at"))


@app.route("/prepare/<int:comp_id>")
def page_prepare(comp_id: int):
    """Check the settings before the opening estimate is frozen."""
    with db.session() as conn:
        comp = comp_payload(conn, comp_id)
        if comp is None:
            return "Competition not found", 404
        kind = db.category_of(comp)
        objectives = db.objectives_or_default(conn, kind, comp["region"])
        scorings = db.list_scorings(conn)
        stats = db.match_stats(conn, comp_id)
        depth = db.standings_count(conn, comp_id)
        known = db.known_categories(conn)
    auto_family, auto_stage = db.split_name(comp["name"])
    comp["family"] = comp.get("family") or auto_family
    comp["stage"] = comp.get("stage") or auto_stage
    comp["field_size"] = comp.get("field_size") or depth or ""
    return render_template("prepare.html", comp=comp, kind=kind, objectives=objectives,
                           scorings=scorings, stats=stats, depth=depth,
                           families=sorted({c["kind"].split(" · ")[0] for c in known}),
                           stages=db.KNOWN_STAGES,
                           regions=db.REGIONS, team_modes=db.TEAM_MODES,
                           game_modes=db.GAME_MODES)


@app.post("/api/competitions/<int:comp_id>/confirm")
def api_confirm(comp_id: int):
    """Save the checked settings, then freeze the opening estimate."""
    data = body()
    with db.session() as conn:
        comp = db.get_competition(conn, comp_id)
        if comp is None:
            return jsonify({"ok": False, "error": "not found"}), 404

        fields = competition_fields(data)
        scoring = as_scoring(data.get("scoring"), "Scoring")
        if scoring["placement"]:
            fields["scoring"] = scoring
        db.update_competition(conn, comp_id, **fields)

        # the category comes from the confirmed fields, not from parsing the name
        kind = db.category_of(db.get_competition(conn, comp_id))
        region = fields.get("region") or comp["region"]
        items = []
        for i, row in enumerate(as_rows(data.get("objectives"), "Thresholds"), 1):
            rank = as_int(row.get("rank"), f"Threshold {i}: rank",
                          minimum=1, maximum=MAX_RANK)
            if not rank:
                continue
            items.append({"label": as_text(row.get("label"), f"Threshold {i}: name"),
                          "rank": rank})
        db.set_objectives(conn, kind, region, items)

        ranks = sorted({o["rank"] for o in items} | set(tracking.REFERENCE_RANKS))
        db.update_competition(conn, comp_id, ranks=ranks,
                              confirmed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        calibration.invalidate(conn)
        estimate = tracking.freeze_first_estimate(conn, comp_id)
    return jsonify({"ok": True, "first_estimate": estimate})


@app.route("/tracking/<int:comp_id>")
def page_tracking(comp_id: int):
    with db.session() as conn:
        comp = comp_payload(conn, comp_id)
        if comp is None:
            return "Competition not found", 404
        if not comp["confirmed"]:
            return redirect(url_for("page_prepare", comp_id=comp_id))
        kind = db.category_of(comp)
        objectives = db.objectives_or_default(conn, kind, comp["region"])
        depth = db.standings_count(conn, comp_id)
    return render_template("tracking.html", comp=comp, kind=kind,
                           objectives=objectives, depth=depth,
                           refresh_minutes=REFRESH_MINUTES,
                           regions=db.REGIONS)


# --------------------------------------------------------------------------- #
# API - automatic acquisition
# --------------------------------------------------------------------------- #
@app.get("/api/live")
def api_live():
    events, error = live_events(force=request.args.get("refresh") == "1",
                                path=request.args.get("path") or "/tournaments/live")
    return jsonify({"ok": error is None, "error": error, "events": events,
                    "meta": _LIVE_CACHE.get("meta") or {}})


@app.post("/api/live/search-wide")
def api_live_wide():
    """Second endpoint, for when /tournaments/live sees nothing. 1 request."""
    events, error = live_events(force=True, path="/matches/live")
    return jsonify({"ok": error is None, "error": error, "events": events,
                    "meta": _LIVE_CACHE.get("meta") or {}})


@app.post("/api/live/raw")
def api_live_raw():
    """Write the raw live-tournament response to a file.

    No request: this re-reads the cache. Useful to see what the API really says
    about a tournament — payout table, description, format — beyond the fields
    the app already uses.
    """
    events, error = live_events(raw=True)
    if error:
        return jsonify({"ok": False, "error": error}), 502
    # filename is quoted verbatim in dashboard.html, so it stays as it is
    path = os.path.join(os.path.dirname(db.DB_PATH), "raw_events.json")
    payload = [e.get("raw") or {} for e in events]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"n": len(payload), "events": payload}, fh,
                  ensure_ascii=False, indent=2)
    field_names = sorted({k for e in payload for k in e}) if payload else []
    return jsonify({"ok": True, "n": len(payload), "file": path, "fields": field_names})


@app.post("/api/live/import")
def api_live_import():
    data = body()
    event_id = as_text(data.get("event_id"), "Tournament", maxlen=MAX_TEXT)
    window_id = as_text(data.get("window_id"), "Session", maxlen=MAX_TEXT)
    events, error = live_events()
    event = next((e for e in events
                  if e["event_id"] == event_id and e["window_id"] == window_id), None)
    if event is None:
        return jsonify({"ok": False, "error": error or "Tournament not in the list."}), 404
    try:
        with db.session() as conn:
            result = tracking.import_and_track(conn, event)
            calibration.invalidate(conn)
        return jsonify({"ok": True, **{k: v for k, v in result.items() if k != "format"},
                        "format": result["format"]})
    except cito.CitoError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.post("/api/competitions/<int:comp_id>/refresh")
def api_refresh(comp_id: int):
    try:
        with db.session() as conn:
            result = tracking.refresh(conn, comp_id)
            calibration.invalidate(conn)
        return jsonify({"ok": True, **result})
    except cito.CitoError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/competitions/<int:comp_id>/standings")
def api_standings(comp_id: int):
    limit = min(int(request.args.get("limit", 100)), 1000)
    offset = max(int(request.args.get("offset", 0)), 0)
    with db.session() as conn:
        rows = db.get_standings(conn, comp_id, limit=limit, offset=offset)
        total = db.standings_count(conn, comp_id)
    return jsonify({"rows": rows, "total": total, "offset": offset})


@app.get("/api/competitions/<int:comp_id>/team/<int:rank>")
def api_team_matches(comp_id: int, rank: int):
    """One team's matches. No API request: it's all in the database."""
    with db.session() as conn:
        rows = db.get_team_matches(conn, comp_id, rank)
        board = db.get_standings(conn, comp_id, limit=1, offset=max(rank - 1, 0))
    return jsonify({"rank": rank, "team": board[0] if board else None, "matches": rows})


@app.get("/api/competitions/<int:comp_id>/team/<int:rank>/detail")
def api_team_detail(comp_id: int, rank: int):
    """Rank progression and team profile. No API request."""
    with db.session() as conn:
        board = db.get_standings(conn, comp_id, limit=1, offset=max(rank - 1, 0))
        report = team_stats.team_report(conn, db, comp_id, rank)
        steps = team_stats.progression(conn, db, comp_id, rank)
    return jsonify({"rank": rank, "team": board[0] if board else None,
                    "steps": steps, **report})


@app.get("/api/favourites")
def api_favourites():
    comp_id = request.args.get("competition", type=int)
    with db.session() as conn:
        items = db.list_favourites(conn)
        rows = db.favourite_rows(conn, comp_id) if comp_id else []
    return jsonify({"ok": True, "items": items, "rows": rows})


@app.post("/api/favourites")
def api_add_favourite():
    data = body()
    name = as_text(data.get("name"), "Nickname")
    note = as_text(data.get("note"), "Note", maxlen=MAX_NOTE)
    with db.session() as conn:
        item = db.add_favourite(conn, name, note)
        items = db.list_favourites(conn)
    if item is None:
        return jsonify({"ok": False, "error": "Empty name."}), 400
    return jsonify({"ok": True, "item": item, "items": items})


@app.delete("/api/favourites")
def api_remove_favourite():
    data = body()
    name = as_text(data.get("name"), "Nickname")
    with db.session() as conn:
        db.remove_favourite(conn, name)
        items = db.list_favourites(conn)
    return jsonify({"ok": True, "items": items})


@app.get("/api/competitions/<int:comp_id>/matches")
def api_match_stats(comp_id: int):
    with db.session() as conn:
        return jsonify(db.match_stats(conn, comp_id))


@app.get("/api/quota")
def api_quota():
    with db.session() as conn:
        return jsonify(db.quota_view(conn, per_tournament=REQUESTS_PER_TOURNAMENT,
                                     limit=cito.FREE_QUOTA))


@app.post("/api/cito-key")
def api_set_key():
    key = as_text(body().get("key"), "API key", maxlen=MAX_TEXT)
    if not key:
        return jsonify({"ok": False, "error": "Empty key."}), 400
    cito.save_key(key)
    _LIVE_CACHE["at"] = None
    return jsonify({"ok": True})


@app.get("/api/objectives")
def api_get_objectives():
    kind, region = request.args.get("kind", ""), request.args.get("region", "")
    with db.session() as conn:
        if kind and region:
            return jsonify(db.objectives_or_default(conn, kind, region))
        return jsonify(db.all_objectives(conn))


@app.put("/api/objectives")
def api_put_objectives():
    data = body()
    kind = as_text(data.get("kind"), "Category")
    region = as_choice(data.get("region"), "Region", db.REGIONS, default="")
    items = []
    for i, row in enumerate(as_rows(data.get("items"), "Thresholds"), 1):
        rank = as_int(row.get("rank"), f"Threshold {i}: rank", minimum=1, maximum=MAX_RANK)
        if not rank:
            continue
        items.append({"label": as_text(row.get("label"), f"Threshold {i}: name"),
                      "rank": rank})
    if not kind or not region:
        return jsonify({"ok": False, "error": "Category and region are required."}), 400
    with db.session() as conn:
        db.set_objectives(conn, kind, region, items)
        result = db.get_objectives(conn, kind, region)
    return jsonify({"ok": True, "items": result})


# --------------------------------------------------------------------------- #
# API - competitions
# --------------------------------------------------------------------------- #
@app.get("/api/competitions")
def api_list_comps():
    with db.session() as conn:
        return jsonify(db.list_competitions(
            conn, request.args.get("region"), request.args.get("team_mode"),
            request.args.get("game_mode")))


@app.post("/api/competitions")
def api_create_comp():
    data = body()
    fields = competition_fields(data)
    scoring_id = as_int(data.get("scoring_id"), "Scoring", minimum=1)
    typed = as_scoring(data.get("scoring"), "Scoring")
    with db.session() as conn:
        known_scoring_id(conn, scoring_id)
        scoring = typed if typed["placement"] else db.scoring_payload(conn, scoring_id)
        cid = db.create_competition(
            conn,
            name=fields.get("name") or "Unnamed",
            region=fields.get("region", "EU"),
            team_mode=fields.get("team_mode", "Solo"),
            game_mode=fields.get("game_mode", "Battle Royale"),
            start_time=fields.get("start_time"),
            end_time=fields.get("end_time"),
            ranks=fields.get("ranks") or db.DEFAULT_RANKS,
            notes=fields.get("notes", ""),
            game_minutes=fields.get("game_minutes"),
            tracker_lag_min=fields.get("tracker_lag_min"),
            max_games=fields.get("max_games"),
            scoring=scoring,
            games_mode=fields.get("games_mode", "max"),
            slot_minutes=fields.get("slot_minutes"),
        )
        if scoring_id:
            conn.execute("UPDATE competition SET scoring_id = ? WHERE id = ?",
                         (scoring_id, cid))
    return jsonify({"ok": True, "id": cid})


@app.get("/api/competitions/<int:comp_id>")
def api_get_comp(comp_id: int):
    with db.session() as conn:
        comp = comp_payload(conn, comp_id)
    return (jsonify(comp), 200) if comp else (jsonify({"error": "not found"}), 404)


@app.put("/api/competitions/<int:comp_id>")
def api_update_comp(comp_id: int):
    data = body()
    fields = competition_fields(data)
    scoring = as_scoring(data.get("scoring"), "Scoring")
    if scoring["placement"]:
        fields["scoring"] = scoring
    with db.session() as conn:
        if db.get_competition(conn, comp_id) is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        db.update_competition(conn, comp_id, **fields)
    return jsonify({"ok": True})


@app.delete("/api/competitions/<int:comp_id>")
def api_delete_comp(comp_id: int):
    with db.session() as conn:
        db.delete_competition(conn, comp_id)
        calibration.invalidate(conn)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# API - snapshots
# --------------------------------------------------------------------------- #
@app.post("/api/competitions/<int:comp_id>/snapshots")
def api_add_snapshot(comp_id: int):
    data = body()
    points = as_points(data.get("points"), "Points")
    my_points = as_float(data.get("my_points"), "My points",
                         minimum=MIN_POINTS, maximum=MAX_POINTS)
    if not points and my_points is None:
        return jsonify({"ok": False, "error": "No points entered."}), 400
    with db.session() as conn:
        if db.get_competition(conn, comp_id) is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        sid = db.add_snapshot(conn, comp_id,
                              ts=as_timestamp(data.get("ts"), "Time"),
                              points=points,
                              note=as_text(data.get("note"), "Note", maxlen=MAX_NOTE),
                              games=as_int(data.get("games"), "Games",
                                           minimum=0, maximum=MAX_GAMES),
                              my_points=my_points)
    return jsonify({"ok": True, "id": sid})


@app.put("/api/snapshots/<int:snapshot_id>")
def api_update_snapshot(snapshot_id: int):
    data = body()
    points = (as_points(data["points"], "Points", keep_blank=True)
              if data.get("points") is not None else None)
    # db reads "" as "clear this cell" and None as "leave it alone", so a box the
    # user emptied has to reach it as it arrived.
    games, my_points = data.get("games"), data.get("my_points")
    if games not in (None, ""):
        games = as_int(games, "Games", minimum=0, maximum=MAX_GAMES)
    if my_points not in (None, ""):
        my_points = as_float(my_points, "My points",
                             minimum=MIN_POINTS, maximum=MAX_POINTS)
    with db.session() as conn:
        # A reading deleted in another tab would otherwise fail as a foreign key
        # error when its points are written back.
        if conn.execute("SELECT 1 FROM snapshot WHERE id = ?",
                        (snapshot_id,)).fetchone() is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        db.update_snapshot(conn, snapshot_id,
                           ts=as_timestamp(data.get("ts"), "Time"),
                           note=(as_text(data["note"], "Note", maxlen=MAX_NOTE)
                                 if data.get("note") is not None else None),
                           points=points, games=games, my_points=my_points)
    return jsonify({"ok": True})


@app.delete("/api/snapshots/<int:snapshot_id>")
def api_delete_snapshot(snapshot_id: int):
    with db.session() as conn:
        db.delete_snapshot(conn, snapshot_id)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# API - final results
# --------------------------------------------------------------------------- #
@app.put("/api/competitions/<int:comp_id>/finals")
def api_set_finals(comp_id: int):
    data = body()
    # Older callers post the thresholds as the whole body rather than under a
    # "points" key, and both shapes are still in use.
    points = as_points(data.get("points", data), "Thresholds", keep_blank=True)
    with db.session() as conn:
        if db.get_competition(conn, comp_id) is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        db.set_finals(conn, comp_id, points)
        calibration.invalidate(conn)
        finals = db.get_finals(conn, comp_id)
    return jsonify({"ok": True, "finals": finals})


@app.delete("/api/competitions/<int:comp_id>/finals")
def api_clear_finals(comp_id: int):
    with db.session() as conn:
        db.clear_finals(conn, comp_id)
        calibration.invalidate(conn)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# API - closing, archiving, duplicating
# --------------------------------------------------------------------------- #
@app.post("/api/competitions/<int:comp_id>/finish")
def api_finish(comp_id: int):
    """Close a tournament: freeze its thresholds and write its archive file."""
    copy = as_bool(body().get("copy_finals"), "Copy the thresholds", default=True)
    try:
        with db.session() as conn:
            result = db.finish_competition(conn, comp_id, copy_finals=copy)
            db.update_competition(conn, comp_id, tracking=1)
            calibration.invalidate(conn)
        result["archive_name"] = os.path.basename(result["archive"])
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


@app.put("/api/category/scoring")
def api_category_scoring():
    """Apply a saved scoring table to a whole category."""
    data = body()
    family = as_text(data.get("family"), "Tournament")
    stage = as_text(data.get("stage"), "Stage")
    # an empty region means every region of the category, not a missing one
    region = as_choice(data.get("region"), "Region", db.REGIONS, default="")
    scoring_id = as_int(data.get("scoring_id"), "Scoring", minimum=1)
    typed = as_scoring(data.get("scoring"), "Scoring")
    if not family:
        return jsonify({"ok": False, "error": "Category missing."}), 400
    with db.session() as conn:
        known_scoring_id(conn, scoring_id)
        scoring = db.scoring_payload(conn, scoring_id) or typed
        if not scoring.get("placement"):
            return jsonify({"ok": False, "error": "Pick a scoring table."}), 400
        # A Duo table dropped on a Solo category would skew everything, so say so.
        source = db.get_scoring(conn, scoring_id) if scoring_id else None
        touched = db.apply_category_scoring(
            conn, family, stage, region, scoring,
            overwrite=as_bool(data.get("overwrite"), "Overwrite"),
            scoring_id=scoring_id)
        warning = None
        if source:
            formats = {(c["team_mode"], c["game_mode"]) for c in db.list_competitions(conn)
                       if (c["family"] or "").strip() == family}
            for team, game in formats:
                if source["team_mode"] and source["team_mode"] != team:
                    warning = (f"This scoring table is marked {source['team_mode']} while the "
                               f"category is {team} — check the placement table.")
                elif source["game_mode"] and source["game_mode"] != game:
                    warning = (f"This scoring table is marked {source['game_mode']} while the "
                               f"category is {game}.")
        calibration.invalidate(conn)
        # Only the scoring-to-points bridge is wanted here. Calibrating the whole
        # database against itself would also cross-validate every live model
        # against every other tournament — quadratic work, discarded on the next
        # line.
        broad = calibration.shared_broad(conn, db)
        part = (broad.get("reference_pace") or {}).get("share_of_max") or {}
    return jsonify({"ok": True, "touched": touched, "share_of_max": part,
                    "warning": warning})


@app.put("/api/category/field")
def api_category_field():
    """Set the field size for a whole category in one go."""
    data = body()
    family = as_text(data.get("family"), "Tournament")
    size = as_int(data.get("field_size"), "Ranked teams", default=0,
                  minimum=0, maximum=MAX_FIELD)
    if not family or size <= 0:
        return jsonify({"ok": False, "error": "Category and team count are required."}), 400
    with db.session() as conn:
        touched = db.apply_category_field(
            conn, family, as_text(data.get("stage"), "Stage"),
            as_choice(data.get("region"), "Region", db.REGIONS, default=""),
            size, overwrite=as_bool(data.get("overwrite"), "Overwrite"))
        calibration.invalidate(conn)
    return jsonify({"ok": True, "touched": touched})


@app.put("/api/competitions/<int:comp_id>/category")
def api_set_category(comp_id: int):
    """Reclassify a tournament: its family and its stage, hence its category."""
    data = body()
    fields = {"family": as_text(data.get("family"), "Tournament"),
              "stage": as_text(data.get("stage"), "Stage")}
    if "field_size" in data:
        fields["field_size"] = as_int(data["field_size"], "Ranked teams",
                                      default=0, minimum=0, maximum=MAX_FIELD) or None
    with db.session() as conn:
        if db.get_competition(conn, comp_id) is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        db.update_competition(conn, comp_id, **fields)
        calibration.invalidate(conn)
        comp = db.get_competition(conn, comp_id)
    return jsonify({"ok": True, "kind": db.category_of(comp),
                    "family": comp["family"], "stage": comp["stage"]})


@app.post("/api/competitions/<int:comp_id>/reopen")
def api_reopen(comp_id: int):
    with db.session() as conn:
        db.reopen_competition(conn, comp_id)
        calibration.invalidate(conn)
    return jsonify({"ok": True})


@app.post("/api/competitions/<int:comp_id>/duplicate")
def api_duplicate(comp_id: int):
    data = body()
    start = as_timestamp(data.get("start_time"), "Start")
    name = as_text(data.get("name"), "Name") or None
    try:
        with db.session() as conn:
            new_id = db.duplicate_competition(conn, comp_id, start_time=start, name=name)
        return jsonify({"ok": True, "id": new_id})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/competitions/<int:comp_id>/archive")
def api_archive(comp_id: int):
    with db.session() as conn:
        if db.get_competition(conn, comp_id) is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        path = db.archive_competition(conn, comp_id)
    return jsonify({"ok": True, "archive": path, "archive_name": os.path.basename(path)})


@app.get("/api/competitions/<int:comp_id>/export.json")
def api_export_one(comp_id: int):
    with db.session() as conn:
        comp = db.export_competition(conn, comp_id)
        if not comp:
            return jsonify({"error": "not found"}), 404
        name = db.archive_name(comp)
    return Response(json.dumps(comp, indent=2, ensure_ascii=False),
                    mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/api/scoring-presets")
def api_scoring_presets():
    return jsonify(db.SCORING_PRESETS)


# --------------------------------------------------------------------------- #
# API - saved scoring tables
# --------------------------------------------------------------------------- #
@app.get("/api/scorings")
def api_list_scorings():
    with db.session() as conn:
        return jsonify(db.list_scorings(conn))


@app.post("/api/scorings")
def api_create_scoring():
    # a saved scoring carries kill / kill_cap / placement at the top level of
    # the body, which is the same shape as the one stored on a competition
    data = body()
    table = as_scoring(data, "Scoring")
    if not table["placement"]:
        return jsonify({"ok": False,
                        "error": "A scoring table needs at least one placement "
                                 "tier."}), 400
    with db.session() as conn:
        sid = db.create_scoring(conn,
                                as_text(data.get("name"), "Name",
                                        default="New scoring table"),
                                kill=table["kill"], kill_cap=table["kill_cap"],
                                team_mode=as_choice(data.get("team_mode"), "Mode",
                                                    db.TEAM_MODES, default=""),
                                game_mode=as_choice(data.get("game_mode"), "Type",
                                                    db.GAME_MODES, default=""),
                                placement=table["placement"],
                                notes=as_text(data.get("notes"), "Notes",
                                              maxlen=MAX_NOTE))
        scoring = db.get_scoring(conn, sid)
    return jsonify({"ok": True, "scoring": scoring})


@app.put("/api/scorings/<int:scoring_id>")
def api_update_scoring(scoring_id: int):
    data = body()
    # Only what was sent gets written, so editing the name alone cannot wipe the
    # placement table. The cap is the exception: db has always cleared it when
    # the field is absent, and the form relies on that to remove one.
    placement = (as_scoring(data, "Scoring")["placement"]
                 if data.get("placement") is not None else None)
    with db.session() as conn:
        if db.get_scoring(conn, scoring_id) is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        db.update_scoring(
            conn, scoring_id,
            name=(as_text(data["name"], "Name") if data.get("name") is not None
                  else None),
            kill=as_float(data.get("kill"), "Points per elimination",
                          minimum=0, maximum=MAX_POINTS),
            kill_cap=as_int(data.get("kill_cap"), "Elimination cap",
                            minimum=0, maximum=MAX_KILL_CAP) or None,
            team_mode=(as_choice(data["team_mode"], "Mode", db.TEAM_MODES, default="")
                       if data.get("team_mode") is not None else None),
            game_mode=(as_choice(data["game_mode"], "Type", db.GAME_MODES, default="")
                       if data.get("game_mode") is not None else None),
            placement=placement,
            notes=(as_text(data["notes"], "Notes", maxlen=MAX_NOTE)
                   if data.get("notes") is not None else None))
        scoring = db.get_scoring(conn, scoring_id)
    return jsonify({"ok": True, "scoring": scoring})


@app.post("/api/scorings/<int:scoring_id>/duplicate")
def api_duplicate_scoring(scoring_id: int):
    with db.session() as conn:
        src = db.get_scoring(conn, scoring_id)
        if src is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        new_id = db.create_scoring(conn, src["name"] + " (copy)", kill=src["kill"],
                                   kill_cap=src["kill_cap"],
                                   team_mode=src["team_mode"], game_mode=src["game_mode"],
                                   placement=src["placement"], notes=src["notes"])
        scoring = db.get_scoring(conn, new_id)
    return jsonify({"ok": True, "scoring": scoring})


@app.delete("/api/scorings/<int:scoring_id>")
def api_delete_scoring(scoring_id: int):
    with db.session() as conn:
        db.delete_scoring(conn, scoring_id)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# API - series and editions
# --------------------------------------------------------------------------- #
@app.get("/api/series")
def api_list_series():
    with db.session() as conn:
        return jsonify(db.list_series(conn))


def as_stages(value, label: str = "Stages") -> list:
    """The stage template of a series, checked before db fills in the blanks.

    db.normalise_stages supplies a default for every field left empty, so this
    only has to make sure what did arrive is of the right kind.
    """
    stages = []
    for i, row in enumerate(as_rows(value, label), 1):
        stages.append({
            "name": as_text(row.get("name"), f"Stage {i}: name"),
            "day_offset": as_int(row.get("day_offset"), f"Stage {i}: day",
                                 default=0, minimum=0, maximum=365),
            "start": as_text(row.get("start"), f"Stage {i}: start time", maxlen=5),
            "duration_min": as_float(row.get("duration_min"), f"Stage {i}: duration",
                                     default=180.0, minimum=0, maximum=MAX_MINUTES),
            "ranks": as_ranks(row.get("ranks"), f"Stage {i}: ranks") or [],
            "max_games": as_int(row.get("max_games"), f"Stage {i}: games",
                                default=db.DEFAULT_MAX_GAMES,
                                minimum=1, maximum=MAX_GAMES),
            "game_minutes": as_float(row.get("game_minutes"), f"Stage {i}: game length",
                                     default=30.0, minimum=1, maximum=MAX_MINUTES),
            "tracker_lag_min": as_float(row.get("tracker_lag_min"),
                                        f"Stage {i}: tracker lag",
                                        default=float(db.DEFAULT_TRACKER_LAG),
                                        minimum=0, maximum=MAX_MINUTES),
            "games_mode": as_choice(row.get("games_mode"), f"Stage {i}: counting",
                                    db.GAMES_MODES, default="max"),
            "slot_minutes": as_float(row.get("slot_minutes"), f"Stage {i}: interval",
                                     default=0.0, minimum=0, maximum=MAX_MINUTES),
        })
    return stages


def series_fields(data: dict) -> dict:
    """The series columns present in `data`, each pulled through a check."""
    fields = {}
    if "name" in data:
        fields["name"] = as_text(data["name"], "Name")
    if "notes" in data:
        fields["notes"] = as_text(data["notes"], "Notes", maxlen=MAX_NOTE)
    for key, label, allowed in (("region", "Region", db.REGIONS),
                                ("team_mode", "Mode", db.TEAM_MODES),
                                ("game_mode", "Type", db.GAME_MODES)):
        if key in data:
            fields[key] = as_choice(data[key], label, allowed)
    if "scoring_id" in data:
        fields["scoring_id"] = as_int(data["scoring_id"], "Scoring", minimum=1)
    if "stages" in data:
        fields["stages"] = as_stages(data["stages"])
    return fields


@app.post("/api/series")
def api_create_series():
    fields = series_fields(body())
    with db.session() as conn:
        known_scoring_id(conn, fields.get("scoring_id"))
        sid = db.create_series(conn, name=fields.get("name") or "New series",
                               region=fields.get("region", "EU"),
                               team_mode=fields.get("team_mode", "Duo"),
                               game_mode=fields.get("game_mode", "Battle Royale"),
                               scoring_id=fields.get("scoring_id"),
                               stages=fields.get("stages"),
                               notes=fields.get("notes", ""))
    return jsonify({"ok": True, "id": sid})


@app.put("/api/series/<int:series_id>")
def api_update_series(series_id: int):
    fields = series_fields(body())
    with db.session() as conn:
        if db.get_series(conn, series_id) is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        known_scoring_id(conn, fields.get("scoring_id"))
        db.update_series(conn, series_id, **fields)
    return jsonify({"ok": True})


@app.delete("/api/series/<int:series_id>")
def api_delete_series(series_id: int):
    with db.session() as conn:
        db.delete_series(conn, series_id)
        calibration.invalidate(conn)
    return jsonify({"ok": True})


@app.post("/api/series/<int:series_id>/editions")
def api_create_edition(series_id: int):
    data = body()
    date = as_timestamp(data.get("date"), "Date of the first stage")
    label = as_text(data.get("label"), "Label")
    if not date:
        return jsonify({"ok": False, "error": "Give the date of the first stage."}), 400
    try:
        with db.session() as conn:
            ids = db.create_edition(conn, series_id, date, label)
        return jsonify({"ok": True, "ids": ids})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/series/<int:series_id>/editions")
def api_list_editions(series_id: int):
    with db.session() as conn:
        return jsonify(db.series_editions(conn, series_id))


@app.get("/api/archives")
def api_archives():
    return jsonify({"folder": db.archive_dir(), "files": db.list_archives()})


# --------------------------------------------------------------------------- #
# API - predictions
# --------------------------------------------------------------------------- #
@app.get("/api/competitions/<int:comp_id>/prediction")
def api_prediction(comp_id: int):
    with db.session() as conn:
        comp = comp_payload(conn, comp_id)
        if comp is None:
            return jsonify({"error": "not found"}), 404
        history, level = similar_history(conn, comp, exclude_id=comp_id)
        calib = calibration.load_or_compute(conn, db, calibration_scope(comp, level),
                                            history, comp["ranks"])
        live = predict.predict_live(comp, history, calib)
        curves = ({r: predict.trajectory(comp, r, history=history, calib=calib)
                   for r in comp["ranks"]} if live.get("ok") else {})
        hist = {r: predict.predict_next(history, r) for r in comp["ranks"]}
        me = predict.my_status(comp, live if live.get("ok") else {})
        first = db.get_first_estimate(conn, comp_id)
        truth = db.final_points(conn, comp_id)
        if first:
            for rank, guess in first["ranks"].items():
                real = truth.get(rank)
                if real:
                    guess["real"] = round(real, 1)
                    guess["error_pct"] = round(100 * (guess["value"] - real) / real, 1)
        calib_view = {
            "level": level,
            "n_comps": calib.get("n_comps", 0),
            "exponent": {r: calibration.exponent_for(calib, r) for r in comp["ranks"]},
            "model_error_pct": calib.get("model_error_pct", {}),
            "scale": round(calibration.session_scale(comp), 1),
            "sources": calib.get("computed_from", []),
        }
    return jsonify({"live": live, "trajectories": curves, "history": hist,
                    "me": me, "n_comparables": len(history), "calibration": calib_view,
                    "first_estimate": first})


@app.get("/api/history")
def api_history():
    """How the final thresholds move from one competition to the next."""
    region = request.args.get("region") or None
    team_mode = request.args.get("team_mode") or None
    game_mode = request.args.get("game_mode") or None
    with db.session() as conn:
        comps = []
        for row in db.list_competitions(conn, region, team_mode, game_mode):
            full = db.get_competition_full(conn, row["id"])
            if full and full["snapshots"]:
                comps.append(full)
        comps.sort(key=lambda c: c["start_time"])
        ranks = sorted({r for c in comps for r in c["ranks"]})
        finals = {c["id"]: db.final_points(conn, c["id"]) for c in comps}
        done = {c["id"]: predict.is_complete(c) for c in comps}
        series = {
            r: [{"id": c["id"], "name": c["name"], "date": c["start_time"],
                 "value": finals[c["id"]][r], "complete": done[c["id"]]}
                for c in comps if finals[c["id"]].get(r) is not None]
            for r in ranks
        }
        completed = [c for c in comps if done[c["id"]]]
        forecast = {r: predict.predict_next(completed, r) for r in ranks}
    return jsonify({"ranks": ranks, "series": series, "forecast": forecast,
                    "n": len(comps), "n_complete": len(completed)})


# --------------------------------------------------------------------------- #
# Export / import
# --------------------------------------------------------------------------- #
@app.get("/api/export.json")
def api_export_json():
    with db.session() as conn:
        payload = db.export_all(conn)
    return Response(json.dumps(payload, indent=2, ensure_ascii=False),
                    mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=fortnite_tracker.json"})


@app.get("/api/export.csv")
def api_export_csv():
    with db.session() as conn:
        rows = db.export_csv_rows(conn)
    buf = io.StringIO()
    csv.writer(buf, delimiter=";").writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=fortnite_tracker.csv"})


@app.post("/api/import")
def api_import():
    data = request.get_json(silent=True)
    if data is None and "file" in request.files:
        data = json.load(request.files["file"])
    if not data:
        return jsonify({"ok": False, "error": "Nothing to import."}), 400
    if not isinstance(data, dict):
        return jsonify({"ok": False,
                        "error": "An export file holds an object, not a "
                                 f"{type(data).__name__}."}), 400
    with db.session() as conn:
        n = db.import_all(conn, data, replace=bool(request.args.get("replace")))
        calibration.invalidate(conn)
    return jsonify({"ok": True, "imported": n})


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fortnite Comp Tracker")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Fortnite Comp Tracker  ->  {url}\n  (Ctrl+C to stop)\n")
    if not args.no_browser:
        Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=False)
