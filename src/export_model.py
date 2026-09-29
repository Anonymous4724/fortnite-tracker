"""Export the model to one JSON file, for the standalone HTML predictor.

That predictor has no server, no database and no API: it forecasts from whatever
this script writes and nothing else. So `model.json` has to carry every number
`calibration.prior_prediction` reads out of a calibration, and none of what it
reads out of the tournament — field size, game count and scoring table arrive
from the form.

What makes a static export possible at all is that the three keys the cold
forecast touches — `curve`, `reference_pace`, `field_sizes` — are computed by
`calibration.calibrate` from `wide`, every finished tournament in the database,
and never from the comparable circle. Two tournaments in unrelated families get
byte-identical tables; only the lookup differs. The rest of a calibration (the
per-rank exponents, the model error weights) belongs to the live models, which
have readings to work from and are out of scope here.

    python src/export_model.py

Writes `model.json` in the repo root — but only once it has checked the export
against the Python model it claims to reproduce. See `verify`.
"""
from __future__ import annotations

import json
import os
import re
import math
import random
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import calibration
import db
import predict

HERE = Path(__file__).resolve().parent
# The code lives in `src/`; the database, the research layer and the export
# one level up. A flat copy still works.
ROOT = HERE.parent if HERE.name == "src" else HERE
# The same database the app reads, and the same way to point elsewhere.
DB_PATH = Path(os.environ.get("FNT_DB") or (ROOT / "data" / "tracker.db"))
OUT_PATH = ROOT / "model.json"

VERSION = "1.0"

# The file gets pasted into an HTML page, so it lives inside that page's weight
# budget rather than a download budget. 180 KB was sized when the training set
# was sixty tournaments; against ten thousand it threw three categories in four
# away, which is most of what the harvest exists to provide. A megabyte of JSON
# is a page that still opens instantly from disk and is a fifteenth of what an
# artifact may weigh — cheap next to a predictor that does not know the
# tournament you are in. Doubled again once each row began carrying what its
# ranks were actually worth, which is the measurement that halved the error.
SIZE_BUDGET = 2_000_000

# Replaying both models over every tournament, branch and rank is seconds at
# sixty tournaments and hours at ten thousand. Past this many, the check runs
# on a random sample instead: the branches are the same code paths whoever
# happens to exercise them, so a few hundred tournaments prove as much as all
# of them, and a check nobody waits for is a check nobody runs.
VERIFY_SAMPLE = 400

# Anything above this and the export is not the model it was measured on.
TOLERANCE = 0.005

# A value no tournament can carry, so a lookup made with it has to miss. Used
# only to force the branches the stored data never takes.
NONE = "__none__"


def load_competitions(conn) -> list[dict]:
    """Every tournament, its readings left out where its finals cover every rank.

    `db.all_full` brings the snapshots along — fine for 66 tournaments, ruinous
    for the thousands the API harvest will land, and pointless where every
    rank has its final: `predict.is_complete` wants only the latest timestamp,
    which one aggregate query covers for the lot. A rank with no final is read
    off the readings (`predict.final_value`) - a cup followed in the app and
    closed without copying its thresholds - so those few bring theirs along.
    """
    latest = {row["competition_id"]: row["ts"] for row in conn.execute(
        "SELECT competition_id, MAX(ts) AS ts FROM snapshot GROUP BY competition_id")}
    out = []
    for row in conn.execute("SELECT id FROM competition ORDER BY id"):
        comp = db.get_competition(conn, row["id"])
        if not comp:
            continue
        comp["finals"] = db.get_finals(conn, comp["id"])
        ts = latest.get(comp["id"])
        if ts and {int(r) for r in comp.get("ranks") or ()} - set(comp["finals"]):
            comp["snapshots"] = db.get_snapshots(conn, comp["id"])
        else:
            comp["snapshots"] = [{"ts": ts, "points": {}}] if ts else []
        out.append(comp)
    kept, dropped = db.keep_for_training(out)
    if dropped:
        print(f"{dropped} tournaments set aside by db.EXCLUDED — see the note there")
    return kept


def calibration_of(comps: list[dict]) -> dict:
    """The three tables the cold forecast reads, and nothing else.

    `calibration.calibrate` would also cross-validate every live model against
    every other tournament — quadratic work for numbers no static predictor can
    use. These three come out identical whichever comparable circle is passed,
    because `calibrate` computes them from the wide sample alone.
    """
    broad = calibration.broad_stats(comps)
    return {"curve": broad["curve"],
            "curves": broad.get("curves") or [],
            "reference_pace": broad["reference_pace"],
            "field_sizes": broad["field_sizes"],
            "shape": broad["shape"],
            "direct": broad.get("direct") or {},
            "direct_kin": broad.get("direct_kin") or {},
            "seasons": broad.get("seasons") or [],
            "season_shifts": broad.get("season_shifts") or {},
            "n_broad": broad["n_curve"]}


def messages(calib: dict) -> dict:
    """The refusals, asked of the app rather than retyped here.

    They are user-facing English and they get reworded; a copy pasted into this
    file would drift out of step with the app the export claims to mirror, and
    the drift would show up in the predictor as a wrong explanation of a right
    refusal.
    """
    blank = {"name": NONE, "family": NONE, "stage": "", "region": NONE,
             "team_mode": NONE, "game_mode": NONE, "max_games": 10, "field_size": 0,
             "scoring": {"placement": [[1, 1, 60]], "kill": 1.0}, "scoring_known": True}
    blank["kind"] = db.category_of(blank)

    def why(**over):
        return calibration.why_no_anchor({**blank, **over}, calib)

    no_field = calibration.prior_prediction(blank, calib, calibration.REFERENCE_RANK) or {}
    return {"no_field": for_page(no_field.get("reason", "")),
            "no_games": for_page(why(max_games=0)),
            "no_reference": for_page(why(scoring={})),
            "unconfirmed_scoring": for_page(why(scoring_known=False)),
            "no_anchor": for_page(why()),
            # A template: the page fills in the rank and the lobby size.
            "last_places": for_page(calibration.last_places_reason("{rank}", "{field}"))}


def for_page(text: str) -> str:
    """The app's refusal without the remedy that only the app has.

    The app ends a refusal with what to do about it there - "enter the real
    threshold below" - and the page has no such field. The diagnosis is the
    part both share; a sentence pointing at the app's form is dropped.
    """
    sentences = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(s for s in sentences if "below" not in s.lower()).strip()


def _grouped(comps: list[dict], key_of) -> dict:
    """Tournaments bucketed the way the calibration tables are keyed."""
    rows: dict = {}
    for comp in comps:
        for key, described in key_of(comp):
            row = rows.setdefault(key, dict(described, _games=[], last=""))
            games = int(comp.get("max_games") or 0)
            if games > 0:
                row["_games"].append(games)
            row["last"] = max(row["last"], str(comp.get("start_time") or "")[:10])
    return rows


def _attach(rows: dict, pace: dict | None = None, fields: dict | None = None,
            share: dict | None = None, shape: dict | None = None,
            direct: dict | None = None) -> list[dict]:
    """Hang the measured numbers on the groups, under the app's own keys.

    The tables come back from `calibration` keyed by `str(tuple)`. Rebuilding the
    tuples here rather than parsing those keys is what keeps the exported numbers
    the app's numbers: nothing is recomputed, only looked up. A table only
    carries the columns its own lookup reads — the field sizes and the levels
    part company at the mode, and a column of nulls in either would be an
    invitation to read it.
    """
    out = []
    for key, row in rows.items():
        level = (pace or {}).get(str(key)) or {}
        field = (fields or {}).get(str(key)) or {}
        shared = (share or {}).get(str(key)) or {}
        # A cup typed in without its field has no level and no field size,
        # and still its previous edition, which reads neither.
        if not level and not field and not shared and not (direct or {}).get(str(key)):
            continue                      # a group the calibration never measured
        games = row["_games"]
        row = {k: v for k, v in row.items() if k != "_games"}
        if pace is not None:
            row["n"] = level.get("n", 0)
            # How many editions exist, as opposed to how many the level was read
            # from: the band widens when a cup has run once, not when the
            # reading chose to use one edition of many.
            row["seen"] = level.get("seen", level.get("n", 0))
            row["level"] = level.get("median")
            # The season the level was read in: a level read across a turn of
            # season is moved, see calibration.season_move.
            if level.get("season") is not None:
                row["season"] = int(level["season"])
        if share is not None:
            row["share_n"] = shared.get("n", 0)
            row["share"] = shared.get("median")
        if fields is not None:
            row["field_n"] = field.get("n", 0)
            row["field"] = field.get("median")
            row["field_spread"] = field.get("spread")
        if pace is not None:
            # The games the stored level was measured over — the same editions,
            # not a separate median over the whole group. Two medians taken from
            # two samples would make the scaling below wrong by the difference.
            row["games"] = level.get("games") or (
                int(statistics.median(games)) if games else None)
        if shape is not None:
            # What each rank was actually worth here, relative to rank 20. Packed
            # as [median, editions, spread] rather than named keys: there are
            # thousands of these and the names would be most of the file.
            measured = shape.get(str(key)) or {}
            if measured:
                row["shape"] = {r: [v["median"], v["n"], v["rel"]]
                                for r, v in measured.items()}
        if direct is not None:
            # What the previous edition scored at each rank, read straight. The
            # first rung of the forecast; see calibration.direct_tables.
            last = direct.get(str(key)) or {}
            if last:
                # [value, editions, spread, field, entry bar, {other bar: [value, field]}]
                # - the field and the bar are what the first rung corrects for.
                # Bars are numbered into the model's `entry_bars` list rather
                # than spelt out eighteen thousand times, and the trailing
                # cells are left off when there is nothing to say.
                # When this cup was last played in this format: what decides
                # which other format a cup new to its own reads, see
                # calibration.direct_kin.
                row["latest"] = max((v.get("date") or "") for v in last.values())
                row["direct"] = {r: pack_direct(v, row["latest"], row.get("season"))
                                 for r, v in last.items()}
        out.append(row)
    return out


# The entry bars the direct tables name, numbered: a row says 3, the model's
# `entry_bars[3]` says "ranked-br-combined:12". Filled as rows are written.
ENTRY_BARS: list[str] = []


def bar_index(bar: str) -> int:
    """The bar's number in ENTRY_BARS, -1 for no bar."""
    if not bar:
        return -1
    if bar not in ENTRY_BARS:
        ENTRY_BARS.append(bar)
    return ENTRY_BARS.index(bar)


def pack_direct(v: dict, latest: str = "", row_season=None) -> list:
    """One direct row, packed:

        [value, editions, spread, field, entry bar, {other bar: [value, field]},
         season, date]

    The field and the bar are what the first rung corrects for. Bars are
    numbered into the model's `entry_bars` list rather than spelt out eighteen
    thousand times, and the trailing cells are left off when there is nothing
    to say - but each of them says "nothing" its own way, and the first bar of
    the list is numbered zero. Trimming trailing zeros blindly cost every row
    on that bar its bar; the page then read those editions as barless, did not
    widen where the tournament asked for another bar, and the export check
    refused the model over the difference.

    The season is the number of the one the edition read was played in, what
    a reading carried across a turn of season is moved by - written only
    where it is not the row's own (the season of the edition the level was
    read off), which a reader falls back on; the date is that edition's,
    written only where it is not the row's latest - a rank the latest
    edition did not publish, read off an older one - so the page can say
    which edition it read.
    """
    season = int(v["season"]) if v.get("season") is not None else -1
    if row_season is not None and season == int(row_season):
        season = -1
    # An edition under another bar: [value, field, season, date], its season
    # written as -1 where it is the row's own, like the season above.
    alt = {}
    for bar, a in (v.get("alt") or {}).items():
        theirs = int(a[3]) if len(a) > 3 and a[3] is not None else -1
        if row_season is not None and theirs == int(row_season):
            theirs = -1
        alt[str(bar_index(bar))] = [a[0], a[1] or 0, theirs, str(a[2]) if len(a) > 2 and a[2] else ""]
    cells = [v["value"], v["n"], v["rel"], v.get("field") or 0, bar_index(v.get("entry") or ""),
             alt or 0, season,
             v.get("date") if v.get("date") and v.get("date") != latest else ""]
    if not cells[7]:
        cells.pop()
        if cells[6] == -1:
            cells.pop()
            if not cells[5]:
                cells.pop()
                if cells[4] == -1:
                    cells.pop()
                    if not cells[3]:
                        cells.pop()
    return cells


def build_tables(comps: list[dict], calib: dict) -> dict:
    """The three anchor tables, plus the field sizes that go with them.

    Field sizes deserve a word: they follow their own cascade, and its third step
    is keyed on the region as well as the mode where the anchor's is not. So the
    two ride together down to the mode, where they part and `mode_fields` takes
    over.
    """
    broad = [c for c in comps if predict.is_complete(c)]
    pace, fields = calib["reference_pace"], calib["field_sizes"]
    shape = calib["shape"]

    def described(comp):
        # The format is part of what makes a row: the same cup in Trio and in
        # Duo is two rows, and the page asks for the one it is looking at.
        return {"category": comp["kind"],
                "family": (comp.get("family") or "").strip() or comp["kind"],
                "stage": (comp.get("stage") or "").strip(),
                "team_mode": comp.get("team_mode") or "",
                "game_mode": comp.get("game_mode") or ""}

    categories = _grouped(broad, lambda c: [
        ((c["kind"], c["region"], c.get("team_mode") or "", c.get("game_mode") or ""),
         dict(described(c), region=c["region"]))])
    families = _grouped(broad, lambda c: [
        ((c["kind"], c.get("team_mode") or "", c.get("game_mode") or ""), described(c))])
    # Both share keys, `(mode, team)` and `(mode, "")`, are rows here: the second
    # is what a mode borrows from when its own team mode was never measured.
    modes = _grouped(broad, lambda c: [
        ((c["game_mode"], team), {"game_mode": c["game_mode"], "team_mode": team})
        for team in (c["team_mode"], "")])
    mode_fields = _grouped(broad, lambda c: [
        ((c["game_mode"], c["team_mode"], c["region"]),
         {"game_mode": c["game_mode"], "team_mode": c["team_mode"], "region": c["region"]})])

    def cold_rows(c):
        sig = calibration.cold_signature(c)
        described = {"game_mode": sig[0], "team_mode": sig[1], "kind": sig[2],
                     "platform": sig[3], "stage": sig[4]}
        return [(sig + (c["region"],), dict(described, region=c["region"])),
                (sig, dict(described, region=""))]
    cold_starts = _grouped(broad, cold_rows)

    return {
        "categories": _attach(categories, pace=pace["category"], fields=fields["category"],
                              shape=shape["category"], direct=calib.get("direct") or {}),
        "families": _attach(families, pace=pace["family"], fields=fields["family"],
                            shape=shape["family"]),
        "modes": _attach(modes, pace=pace["mode"], share=pace["share_of_max"],
                         shape=shape["mode"]),
        "mode_fields": _attach(mode_fields, fields=fields["mode"]),
        "cold_starts": _attach(cold_starts, share=pace["share_of_max"]),
        # Closed lobbies by share of the lobby: one row per game mode and team
        # size, team size "" for the mode alone, game mode "*" for everything;
        # each bucket [q, share of max, rel, n].
        "lobby": lobby_rows(pace.get("lobby") or {}),
        # The ladder: every open queue's thresholds relative to rank 20, pooled
        # by band of field size and game mode ("" for the band across game
        # modes); each rank [median, boards, spread]. See calibration.LADDER_MIN.
        "ladders": ladder_rows(shape.get("ladder") or {}),
    }


def ladder_rows(table: dict) -> list[dict]:
    """The calibration's ladder, keyed by band index, as rows a port reads by
    field: [lower edge, upper edge (0: none)] and the game mode."""
    import ast
    edges = (0,) + tuple(calibration.LADDER_BANDS) + (0,)
    out = []
    for key, rows in table.items():
        band, game_mode = ast.literal_eval(key)
        out.append({"lo": int(edges[int(band)]), "hi": int(edges[int(band) + 1]),
                    "game_mode": game_mode or "",
                    "shape": {r: [v["median"], v["n"], v["rel"]] for r, v in rows.items()}})
    out.sort(key=lambda r: (r["lo"], r["game_mode"]))
    return out


def lobby_rows(table: dict) -> list[dict]:
    """The calibration's tuple-keyed table as rows a port can look up by field."""
    import ast
    out = []
    for key, rows in table.items():
        if key == "*":
            game, team = "*", ""
        else:
            game, team = ast.literal_eval(key)
        out.append({"game_mode": game, "team_mode": team, "rows": rows})
    out.sort(key=lambda r: (r["game_mode"], r["team_mode"]))
    return out


# Names the test harnesses use. A scoring called "Fuzz" once reached a shipped
# model.json and turned up in the predictor's dropdown; the export refuses them
# now rather than trusting whoever last pointed a test at the real database.
TEST_NAMES = {"fuzz", "control", "test", "s", "s2", "serie", "fuzz series"}


def scoring_presets(conn) -> list[dict]:
    """The saved scoring tables, so nobody has to retype 25 placement rows.

    Input, not model: the forecast never reads these, it reads the table the user
    picked. They ship because a predictor that opens on an empty scoring form is
    a predictor nobody gets to the end of.
    """
    out = []
    for row in conn.execute("SELECT name, kill, kill_cap, placement, team_mode, game_mode "
                            "FROM scoring_system ORDER BY name"):
        try:
            placement = json.loads(row["placement"])
        except (ValueError, TypeError):
            continue
        if not placement or (row["name"] or "").strip().lower() in TEST_NAMES:
            continue
        out.append({"name": row["name"], "kill": float(row["kill"] or 0),
                    "kill_cap": row["kill_cap"], "placement": placement,
                    "team_mode": row["team_mode"] or "", "game_mode": row["game_mode"] or ""})
    return out


def encoded(model: dict) -> str:
    return json.dumps(model, ensure_ascii=False, separators=(",", ":"))


def trim_to_budget(model: dict, budget: int | None = None) -> dict:
    """Give up the thinnest categories until the file fits.

    A category left out is not a category the predictor loses: the cascade falls
    through to its family, which is the same tournament measured across regions,
    and the band widens to say so. So rows go in the order that costs least —
    fewest editions first, oldest first among equals — and families are only
    touched once there is no category left to give.
    """
    budget = SIZE_BUDGET if budget is None else budget

    def size(x) -> int:
        # In bytes, as the file is written: a "·" in a label is two of them.
        return len(encoded(x).encode("utf-8"))

    # Written into the model as it is filled, so that its own bytes count too.
    dropped: dict = {}
    model.setdefault("source", {})["dropped"] = dropped
    for table in ("categories", "families"):
        rows = model[table]
        rows.sort(key=lambda r: (max(r["n"], r["field_n"]), r["last"]), reverse=True)
        while rows and size(model) > budget:
            excess, cut = size(model) - budget, 0
            while cut < len(rows) and excess > 0:
                cut += 1
                excess -= size(rows[-cut]) + 1
            del rows[len(rows) - cut:]
            dropped[table] = dropped.get(table, 0) + cut
    return dropped


QUALITY_PATH = ROOT / "analysis" / "validation.json"


def measured_quality() -> dict | None:
    """What analysis/validate.py last measured, for the page to show.

    None when it never ran: the page then prints a dash where the error and
    the coverage go, which is the truth. It is not recomputed here — the
    validation takes minutes and the export is meant to be quick — so the
    date inside says how old the figures are.
    """
    if not QUALITY_PATH.exists():
        return None
    try:
        found = json.loads(QUALITY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    keep = ("generated", "split", "targets", "pool", "from", "thresholds", "median_ape",
            "mean_ape", "coverage", "nominal", "cold_median_ape", "cold_share",
            "lobby_median_ape", "lobby_rows", "baselines", "bands", "band_n")
    return {k: found.get(k) for k in keep if k in found}


PACE_PATH = ROOT / "analysis" / "pace.json"
BLEND_PATH = ROOT / "analysis" / "blend.json"


def measured_pace() -> dict | None:
    """What analysis/live.py last measured about mid-cup thresholds.

    The share of its final value a threshold has reached at each point of the
    session, and how much that share varies from one cup to the next — the
    two numbers the live refinement rests on. None when never measured; the
    page then falls back on a linear pace and says so.
    """
    if not PACE_PATH.exists():
        return None
    try:
        found = json.loads(PACE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(found.get("curve"), dict):
        return None
    return {k: found.get(k) for k in ("generated", "boards", "curve", "dispersion", "carry",
                                     "tail", "tail_dispersion", "tail_feed",
                                     "games_curve", "games_dispersion",
                                     # The pace by kind of cup, the depth of a rank and the
                                     # width of a live answer: see analysis/live.py.
                                     "families", "categories", "depth", "live_bands")
            if k in found}


def measured_blend() -> dict | None:
    """How the page weighs a cup's readings against its forecast from history:
    each side's typical error in units of its own width, measured by
    analysis/blend.py on the evenings the feed followed. None when never
    measured; the page then weighs the two widths as they stand."""
    if not BLEND_PATH.exists():
        return None
    try:
        found = json.loads(BLEND_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    reading = found.get("reading") or {}
    if not isinstance(found.get("cold"), dict) or not reading.get("scale"):
        return None
    return {k: found.get(k) for k in ("generated", "evenings", "reading", "cold") if k in found}


def build_model(conn, comps: list[dict], calib: dict) -> dict:
    thresholds = conn.execute("SELECT COUNT(*) FROM final_result").fetchone()[0]
    a, b = calib["curve"]
    model = {
        "version": VERSION,
        "generated": date.today().isoformat(),
        "source": {
            "tournaments": len(comps),
            "measured_on": calib["n_broad"],
            "thresholds": thresholds,
            "note": "Every level is a median over the editions behind its row. A row "
                    "with n = 1 rests on one tournament, and its band is widened by "
                    "half again to say so.",
        },
        "curve": {"a": a, "b": b, "reference_rank": calibration.REFERENCE_RANK},
        # The curve by band of field size: [lower edge, upper edge (0: none),
        # a, b], read by `curve_for`; the pooled curve above answers for a
        # field whose band was not fitted. See calibration.FIELD_BANDS.
        "curves": [list(row) for row in (calib.get("curves") or [])],
        "game_minutes": dict(db.GAME_MINUTES),
        "reference_share": calibration.REFERENCE_SHARE,
        "anchor_spread": dict(calibration.ANCHOR_SPREAD),
        "shrinkage": {"prior_weight": 2, "thin_below": 3},
        "games_exponent": calibration.GAMES_EXPONENT,
        "spread": {"shape": calibration.SHAPE_FALLBACK_REL,
                   "field_entered": 0.15, "field_probe": 0.30},
        # Which scopes a level anchored on each source reads its shape from,
        # spelt out per source string so a port looks its source up as it is;
        # "ladder" is the pooled table by field size in `ladders`, read to any
        # depth with `ladder_min` boards, the cup's own tables to `max_rank`.
        "shape_rule": {"min_editions": calibration.SHAPE_MIN, "prior_weight": 4,
                       "max_rank": calibration.SHAPE_MAX_RANK,
                       "ladder_min": calibration.LADDER_MIN,
                       "scopes": {source: list(calibration.shape_scopes(source)) for source in
                                  ("category", "family", "scoring", "scoring (thin sample)",
                                   "scoring (prior)", "mode")}},
        "widen": {"single_edition": 1.5, "game_mode_only": 1.2,
                  "thin_sample": 1.3, "prior_only": 1.15, "cold_region": 0.9,
                  "entry": calibration.WIDEN_ENTRY},
        "field_move": {"cap": calibration.FIELD_MOVE_CAP, "rel": calibration.FIELD_MOVE_REL},
        # The re-scored boards' own dispersion, where a calendar row's table
        # does not carry its own. See calibration.replay_reading, rescore.py.
        "replay_rel": calibration.REPLAY_REL,
        # The seasons the history spans ([number, first day seen]), the
        # current one, and how the level moved at each turn of season per
        # band of rank: [[from, to, {band: [shift, spread, pairs]}], ...] with
        # the fallback spread for a turn no cup has crossed yet. See
        # calibration.season_move.
        "seasons": [[int(n), str(day)] for n, day in (calib.get("seasons") or [])],
        "season": max((int(n) for n, _ in (calib.get("seasons") or [])), default=None),
        "season_shifts": calib.get("season_shifts") or {"boundaries": [], "fallback": dict(calibration.SEASON_SPREAD)},
        "lobby_cap": {"large": dict(calibration.LOBBY_CAP), "small": dict(calibration.LOBBY_CAP_SMALL)},
        "lobby_rule": {"thin": calibration.LOBBY_THIN, "widen": dict(calibration.LOBBY_WIDEN),
                       # past this share of the lobby, the last places: no forecast
                       "last": calibration.LOBBY_LAST},
        "messages": messages(calib),
        "quality": measured_quality(),
        "pace": measured_pace(),
        "blend": measured_blend(),
    }
    ENTRY_BARS.clear()
    model.update(build_tables(comps, calib))
    # Filled while the category rows were written, so it comes after them.
    model["entry_bars"] = list(ENTRY_BARS)
    model["scoring_presets"] = scoring_presets(conn)
    model["source"]["dropped"] = trim_to_budget(model)
    model["categories"].sort(key=lambda r: (r["category"], r["region"], r["team_mode"], r["game_mode"]))
    model["families"].sort(key=lambda r: (r["category"], r["team_mode"], r["game_mode"]))
    model["modes"].sort(key=lambda r: (r["game_mode"], r["team_mode"]))
    model["mode_fields"].sort(key=lambda r: (r["game_mode"], r["team_mode"], r["region"]))
    model["cold_starts"].sort(key=lambda r: (r["game_mode"], r["team_mode"], r["kind"],
                                             r["platform"], r["stage"], r["region"]))
    return model


# Everything below reads `model` and the tournament in front of it, never the
# database and never `calibration`. It is the reference implementation: the
# JavaScript in the HTML predictor is a line-by-line port of it, and the check in
# `verify` is what says the port has something faithful to copy.
_INDEXES: dict[tuple, dict] = {}


def row_for(rows: list[dict], **match) -> dict | None:
    """First row matching every field given.

    Indexed on the fields asked for and cached, because the verification asks a
    few hundred thousand times and a linear scan of two thousand categories
    turns a two-minute check into an afternoon. A port should do the same - the
    JavaScript one builds its lookups once at load.
    """
    fields = tuple(sorted(match))
    key = (id(rows), len(rows), fields)
    index = _INDEXES.get(key)
    if index is None:
        index = {}
        for row in rows:
            index.setdefault(tuple(row.get(name) for name in fields), row)
        _INDEXES[key] = index
    return index.get(tuple(match[name] for name in fields))


def category_row(model: dict, t: dict) -> dict | None:
    """This cup, in this region, in this format."""
    return row_for(model["categories"], category=t.get("category"), region=t.get("region"),
                   team_mode=t.get("team_mode") or "", game_mode=t.get("game_mode") or "")


def family_row(model: dict, t: dict) -> dict | None:
    """This cup across regions, in this format."""
    return row_for(model["families"], category=t.get("category"),
                   team_mode=t.get("team_mode") or "", game_mode=t.get("game_mode") or "")


_KIN: dict[tuple, dict] = {}


def kin_rows(model: dict, t: dict) -> list[dict]:
    """The other formats this cup has run in, in this region: the port of
    calibration.direct_kin. Newest first, ties broken on the format's names."""
    key = (id(model["categories"]), len(model["categories"]))
    index = _KIN.get(key)
    if index is None:
        index = {}
        for row in model["categories"]:
            if row.get("direct"):
                index.setdefault((row.get("category"), row.get("region")), []).append(row)
        for rows in index.values():
            rows.sort(key=lambda r: (r.get("latest") or "", r.get("team_mode") or "",
                                     r.get("game_mode") or ""), reverse=True)
        _KIN[key] = index
    return [r for r in index.get((t.get("category"), t.get("region")), [])
            if (r.get("team_mode") or "", r.get("game_mode") or "")
            != (t.get("team_mode") or "", t.get("game_mode") or "")]


def curve_for(model: dict, field: int) -> dict:
    """The port of calibration.curve_for: the field band's curve, else the pooled one."""
    pooled = model["curve"]
    field = int(field or 0)
    if field > 0:
        # A band runs from its lower edge up to, not including, its upper
        # one; the last band has no upper edge (0).
        for row in model.get("curves") or []:
            lo, hi = int(row[0]), int(row[1])
            if field >= lo and (hi == 0 or field < hi):
                return {"a": float(row[2]), "b": float(row[3]),
                        "reference_rank": pooled["reference_rank"]}
    return pooled


def field_move(rank: int, field_now: int, field_then: int, curve: dict, cap: float) -> float:
    if rank < 1 or not field_now or not field_then or field_now <= 0 or field_then <= 0:
        return 0.0
    q_now = min(max(rank / field_now, 1e-9), 1.0)
    q_then = min(max(rank / field_then, 1e-9), 1.0)
    move = -curve["a"] * (q_now ** curve["b"] - q_then ** curve["b"])
    return max(-cap, min(cap, move))


def shape_ratio(rank: int, field: int, curve: dict) -> float:
    if not field or field <= 0 or rank < 1:
        return 1.0
    q = min(max(rank / field, 1e-9), 1.0)
    qref = min(max(curve["reference_rank"] / field, 1e-9), 1.0)
    return math.exp(-curve["a"] * (q ** curve["b"] - qref ** curve["b"]))


def field_sensitivity(rank: int, field: int, curve: dict) -> float:
    if not field or field <= 0 or rank < 1:
        return 0.0
    q = min(max(rank / field, 1e-9), 1.0)
    qref = min(max(curve["reference_rank"] / field, 1e-9), 1.0)
    return abs(curve["a"] * curve["b"] * (q ** curve["b"] - qref ** curve["b"]))


def max_game_score(scoring: dict) -> float:
    """A win plus a full run of eliminations: the most one game can pay."""
    rows = (scoring or {}).get("placement") or []
    best = float(min(rows, key=lambda r: r[0])[2]) if rows else 0.0
    cap = scoring.get("kill_cap")
    return best + float(cap or 8) * float(scoring.get("kill") or 0)


def guess_field(model: dict, t: dict) -> tuple[int, str, float] | None:
    candidates = (
        ("category", category_row(model, t)),
        ("family", family_row(model, t)),
        ("mode", row_for(model["mode_fields"], game_mode=t.get("game_mode"),
                         team_mode=t.get("team_mode"), region=t.get("region"))),
    )
    for source, row in candidates:
        if row and row.get("field_n", 0) >= 1 and row.get("field"):
            return int(row["field"]), source, row.get("field_spread", 0.20)
    return None


def cold_signature(t: dict) -> tuple:
    """The port of calibration.cold_signature, from the form's fields."""
    name = f"{t.get('category') or ''} {t.get('name') or ''}".lower()
    kind = next((tag for needle, tag in calibration.COLD_KINDS if needle in name), "other")
    platform = "mobile" if "mobile" in name else ("console" if "console" in name else "pc")
    label = str(t.get("category") or "")
    if label.endswith((" Final", "Semi-final")):
        stage = "final"
    elif calibration.LATER_ROUND.search(label):
        stage = "later"
    else:
        stage = "open"
    return (t.get("game_mode") or "", t.get("team_mode") or "", kind, platform, stage)


def season_band(rank: int) -> str:
    """The port of calibration.season_band: "100", "500" or "0"."""
    if rank <= 100:
        return "100"
    if rank <= 500:
        return "500"
    return "0"


def tournament_season(model: dict, t: dict):
    """The season the tournament is played in: the form's - the page reads it
    off the calendar row's event id, and hands a typed tournament the model's
    current one - or, for a form that says nothing, the model's current one.
    A form that names it and leaves it empty is a tournament of no known
    season, and is not moved."""
    if "season" not in t:
        return model.get("season")
    season = t.get("season")
    if season is None or season == "":
        return None
    return int(season)


def season_move(model: dict, since, until, rank: int) -> tuple[float, float, int]:
    """The port of calibration.season_move: (shift, spread, pairs) from one
    season to a later one at this rank, the turns of season between them
    summed; an unknown turn at the fallback spread and no shift."""
    if since is None or until is None or int(until) <= int(since):
        return 0.0, 0.0, 0
    since, until = int(since), int(until)
    shifts = model.get("season_shifts") or {}
    known = [int(entry[0]) for entry in model.get("seasons") or []]
    band = season_band(rank)
    steps = [n for n in known if since < n <= until]
    if until not in known:
        steps.append(until)
    table = {(int(b[0]), int(b[1])): b[2] for b in shifts.get("boundaries") or []}
    fallback = float((shifts.get("fallback") or {}).get(band, 0.12))
    shift, var, pairs, previous = 0.0, 0.0, 0, since
    for step in steps:
        row = (table.get((previous, step)) or {}).get(band)
        if row:
            shift += float(row[0])
            var += float(row[1]) ** 2
            pairs += int(row[2])
        else:
            var += fallback ** 2
        previous = step
    return shift, math.sqrt(var), pairs


def anchor_level(model: dict, t: dict) -> dict | None:
    games = float(t.get("max_games") or 0)
    if games <= 0:
        return None
    spread, widen = model["anchor_spread"], model["widen"]
    until = tournament_season(model, t)

    def measured(source, row):
        if not row or row.get("n", 0) < 1:
            return None
        # The port of calibration.anchor_level: the stored level is what those
        # editions reached over the games they played, so a target running the
        # same number is not rescaled at all.
        theirs = float(row.get("games") or 0) or games
        value = row["level"] * (games / theirs) ** model["games_exponent"]
        once = (row.get("seen") if row.get("seen") is not None else row["n"]) == 1
        rel = spread[source] * (widen["single_edition"] if once else 1.0)
        # A level read across a turn of season moves with the season.
        shift, moved, _ = season_move(model, row.get("season"), until, model["curve"]["reference_rank"])
        if moved > 0:
            value *= math.exp(shift)
            rel = math.sqrt(rel ** 2 + moved ** 2)
        return {"value": value, "source": source, "n": row["n"], "rel": rel}

    for source, row in (("category", category_row(model, t)), ("family", family_row(model, t))):
        found = measured(source, row)
        if found:
            return found

    scoring = t.get("scoring") or {}
    if t.get("scoring_known") is not False and scoring.get("placement"):
        best = max_game_score(scoring)
        entry, factor = None, widen["prior_only"]
        # Most specific first, the port of calibration.anchor_level: this kind
        # of cup on this platform at this stage in this region, then without the
        # region, then the game mode and team size, then the game mode alone.
        sig = cold_signature(t)
        probes = [(lambda: row_for(model["cold_starts"], game_mode=sig[0], team_mode=sig[1],
                                   kind=sig[2], platform=sig[3], stage=sig[4],
                                   region=t.get("region") or ""), widen["cold_region"]),
                  (lambda: row_for(model["cold_starts"], game_mode=sig[0], team_mode=sig[1],
                                   kind=sig[2], platform=sig[3], stage=sig[4], region=""), 1.0),
                  (lambda: row_for(model["modes"], game_mode=t.get("game_mode"),
                                   team_mode=t.get("team_mode") or ""), 1.0),
                  (lambda: row_for(model["modes"], game_mode=t.get("game_mode"),
                                   team_mode=""), widen["game_mode_only"])]
        for probe, fallback in probes:
            row = probe()
            if row and row.get("share_n", 0) >= 1:
                entry, factor = row, fallback
                break
        n = entry["share_n"] if entry else 0
        thin = model["shrinkage"]["thin_below"]
        if n:
            weight = n / (n + model["shrinkage"]["prior_weight"])
            share = weight * entry["share"] + (1 - weight) * model["reference_share"]
            if n < thin:
                factor = max(factor, widen["thin_sample"])
        else:
            share = model["reference_share"]
        if best > 0:
            return {"value": share * best * games,
                    "rel": spread["scoring"] * (1.0 if n >= thin else factor),
                    "source": "scoring" if n >= thin else
                              ("scoring (thin sample)" if n else "scoring (prior)"),
                    "n": n}

    return measured("mode", row_for(model["modes"], game_mode=t.get("game_mode"),
                                    team_mode=t.get("team_mode")))


def why_no_anchor(model: dict, t: dict) -> str:
    if float(t.get("max_games") or 0) <= 0:
        return model["messages"]["no_games"]
    if not (t.get("scoring") or {}).get("placement"):
        return model["messages"]["no_reference"]
    if t.get("scoring_known") is False:
        return model["messages"]["unconfirmed_scoring"]
    return model["messages"]["no_anchor"]


def bracket_of(table: dict, rank: int, usable) -> tuple | None:
    """The port of calibration.bracket: the measured ranks either side."""
    rank = int(rank)
    lo = hi = None
    for key, entry in (table or {}).items():
        try:
            at = int(key)
        except (TypeError, ValueError):
            continue
        if not usable(entry):
            continue
        if at < rank and (lo is None or at > lo):
            lo = at
        elif at > rank and (hi is None or at < hi):
            hi = at
    if lo is None or hi is None:
        return None
    f = (math.log(rank) - math.log(lo)) / (math.log(hi) - math.log(lo))
    return lo, hi, f


def between(v_lo: float, v_hi: float, f: float) -> float:
    return math.exp((1 - f) * math.log(v_lo) + f * math.log(v_hi))


def ladder_rows_for(model: dict, field: int, game_mode) -> list[dict]:
    """The ladder rows a field of this size reads, the game mode's first."""
    rows = [r for r in model.get("ladders") or []
            if field >= int(r["lo"]) and (int(r["hi"]) == 0 or field < int(r["hi"]))]
    return ([r for r in rows if r["game_mode"] == (game_mode or "")]
            + [r for r in rows if r["game_mode"] == ""])


def shape_from_model(model: dict, t: dict, rank: int, source: str, field: int = 0):
    """(ratio, relative uncertainty, scope) at this rank, from the exported tables.

    The port of `calibration.shape_from`, and it has to stay one: the scope
    order, the minimum edition count and the blending weight are all read from
    the model rather than retyped, so a change on the Python side arrives here
    through the file instead of through somebody remembering.
    """
    rule = model.get("shape_rule") or {}
    least, prior = rule.get("min_editions", 3), rule.get("prior_weight", 4)
    max_rank, ladder_min = rule.get("max_rank", 500), rule.get("ladder_min", 20)
    order = tuple((rule.get("scopes") or {}).get(source, ()))

    def read(entry):
        median, n, rel = entry
        weight = n / (n + prior)
        return float(median), math.sqrt(weight * rel ** 2
                                        + (1 - weight) * model["spread"]["shape"] ** 2)

    def off(table, floor):
        def usable(entry):
            return bool(entry) and entry[1] >= floor and bool(entry[0])
        entry = table.get(str(int(rank)))
        if usable(entry):
            return read(entry)
        found = bracket_of(table, rank, usable)
        if not found:
            return None
        lo, hi, f = found
        (r_lo, b_lo), (r_hi, b_hi) = read(table[str(lo)]), read(table[str(hi)])
        return between(r_lo, r_hi, f), max(b_lo, b_hi)

    for scope in order:
        if scope == "ladder":
            if int(field or 0) <= 0:
                continue
            for row in ladder_rows_for(model, int(field), t.get("game_mode")):
                got = off(row.get("shape") or {}, ladder_min)
                if got:
                    return got[0], got[1], "ladder"
            continue
        if int(rank) > max_rank:
            continue
        if scope == "category":
            row = category_row(model, t)
        elif scope == "family":
            row = family_row(model, t)
        else:
            row = row_for(model["modes"], game_mode=t.get("game_mode"),
                          team_mode=t.get("team_mode"))
        got = off((row or {}).get("shape") or {}, least)
        if got:
            return got[0], got[1], scope
    return None


def lobby_cap(model: dict, team_mode, game_mode) -> int:
    mode = str(game_mode or "").lower()
    caps = model.get("lobby_cap") or {}
    table = caps.get("small" if ("reload" in mode or "blitz" in mode) else "large") or {}
    return int(table.get(str(team_mode or ""), 0) or 0)


def single_lobby(model: dict, t: dict, field: int) -> bool:
    cap = lobby_cap(model, t.get("team_mode"), t.get("game_mode"))
    return bool(cap) and 0 < int(field or 0) <= cap


def lobby_read(rows: list, q: float) -> tuple[float, float, int]:
    """The port of calibration.lobby_read: log-linear between the two buckets
    either side of q; the end buckets hold beyond their own q."""
    if q <= rows[0][0]:
        return rows[0][1], rows[0][2], rows[0][3]
    if q >= rows[-1][0]:
        return rows[-1][1], rows[-1][2] * 1.5, rows[-1][3]
    for lo, hi in zip(rows, rows[1:]):
        if lo[0] <= q <= hi[0]:
            f = (math.log(q) - math.log(lo[0])) / (math.log(hi[0]) - math.log(lo[0]))
            share = math.exp(math.log(lo[1]) + f * (math.log(hi[1]) - math.log(lo[1])))
            return share, max(lo[2], hi[2]), min(lo[3], hi[3])
    return rows[-1][1], rows[-1][2], rows[-1][3]


def lobby_estimate(model: dict, t: dict, rank: int, field: int) -> dict | None:
    """The port of calibration.lobby_estimate."""
    table = model.get("lobby") or []
    games = float(t.get("max_games") or 0)
    scoring = t.get("scoring") or {}
    if games <= 0 or t.get("scoring_known") is False or not scoring.get("placement"):
        return None
    best = predict.max_game_score(scoring, kills=scoring.get("kill_cap") or 8)
    if best <= 0:
        return None
    rule = model.get("lobby_rule") or {}
    widen_by = rule.get("widen") or {}
    game, team = t.get("game_mode") or "", t.get("team_mode") or ""
    for probe, widen in (((game, team), widen_by.get("team", 1.0)),
                         ((game, ""), widen_by.get("mode", 1.15)),
                         (("*", ""), widen_by.get("any", 1.3))):
        found = row_for(table, game_mode=probe[0], team_mode=probe[1])
        rows = (found or {}).get("rows") or []
        if len(rows) >= 2:
            break
    else:
        return None
    share, rel, n = lobby_read(rows, rank / field)
    if n < int(rule.get("thin", 5)):
        widen *= widen_by.get("thin", 1.3)
    return {"value": share * best * games, "rel": rel * widen, "n": n}


FIELD_BELOW_CUT = ("This cup sends more players on than the field its kind of cup usually has, so the field "
                   "is not known yet and no forecast is made. It comes once the cup's first board has "
                   "been read.")


def predict_from_model(model: dict, tournament: dict) -> dict | None:
    """A rank's threshold before the tournament starts, from the export alone.

    `tournament` is the form: category, region, team_mode, game_mode, max_games,
    rank, and optionally field_size and a scoring table. Nothing here reaches for
    the database — that is the point of it.
    """
    rank = int(tournament.get("rank") or 0)
    field = int(tournament.get("field_size") or 0)
    if field < 0:
        field = 0
    guessed, field_spread = None, 0.0
    if not field:
        found = guess_field(model, tournament)
        if not found:
            return {"ok": False, "reason": model["messages"]["no_field"]}
        field, guessed, field_spread = found
        # A qualification cut deeper than the field guessed for the cup says
        # the guess is wrong, not where the cut is: the first session of the
        # FNCS Solo qualifiers sends 8,000 players on in Europe, where the
        # cups of its mode that the guess reads have fields of three
        # thousand. No forecast is better than one priced on a field of the
        # wrong size; the session's own board, once read, gives the field.
        if int(tournament.get("cut_max") or 0) > field:
            return {"ok": False, "reason": FIELD_BELOW_CUT}
    if rank < 1:
        return None

    # Rung zero, the port of calibration.direct_from: this cup, this region,
    # this format, this rank, last time.
    row = category_row(model, tournament)
    table = (row or {}).get("direct") or {}
    curve = curve_for(model, field)

    want = str(tournament.get("entry") or "")
    # A count, or nothing: the page's fields are typed or read off a cut, so
    # always counts; a stored tournament's is a count unless the harvest hit
    # the API's ceiling, which as_input says.
    field_now = int(tournament.get("field_size") or 0) if tournament.get("field_counted", True) else 0
    until = tournament_season(model, tournament)
    latest = str((row or {}).get("latest") or "")
    row_season = (row or {}).get("season")

    def direct_read(entry):
        value, n, measured = float(entry[0]), int(entry[1]), entry[2]
        fallback = model["anchor_spread"]["category"]
        if measured is None:
            rel = fallback * model["widen"]["single_edition"]
        else:
            pairs = max(n - 1, 0)
            weight = pairs / (pairs + 2)
            rel = math.sqrt(weight * measured ** 2 + (1 - weight) * fallback ** 2)
        bars = model.get("entry_bars") or []
        edition_field = int(entry[3]) if len(entry) > 3 and entry[3] else 0
        theirs = bars[int(entry[4])] if len(entry) > 4 and isinstance(entry[4], int) and entry[4] >= 0 else ""
        alt = entry[5] if len(entry) > 5 and isinstance(entry[5], dict) else {}
        note = None
        # The season and date of the edition actually read: the latest, or
        # the last one under the bar asked for; the row's own season and its
        # latest date where a cell says nothing.
        since = entry[6] if len(entry) > 6 and isinstance(entry[6], int) and entry[6] >= 0 else row_season
        date = entry[7] if len(entry) > 7 and entry[7] else latest
        if want and theirs and want != theirs:
            other = alt.get(str(bars.index(want))) if want in bars else None
            # Read only when played in the same season as the latest edition.
            other_season = (other[2] if len(other) > 2 and isinstance(other[2], int) and other[2] >= 0
                            else row_season) if other else None
            if other and (other_season is None or since is None or int(other_season) == int(since)):
                value, edition_field = float(other[0]), int(other[1] or 0)
                since = other_season
                if len(other) > 3 and other[3]:
                    date = other[3]
                note = ("same_entry",)
            else:
                rel *= model["widen"]["entry"]
                note = ("other_entry", theirs)
        effect = 0.0
        if field_now > 0 and edition_field > 0 and edition_field != field_now:
            move = field_move(rank, field_now, edition_field, curve, model["field_move"]["cap"])
            value *= math.exp(move)
            rel = math.sqrt(rel ** 2 + (model["field_move"]["rel"] * move) ** 2)
            effect = 100 * (math.exp(move) - 1)
        # Read across a turn of season, the value moves.
        shift, moved, pairs = season_move(model, since, until, rank)
        season = {"effect": 0.0, "pairs": 0, "since": None, "until": None, "date": date}
        if moved > 0:
            value *= math.exp(shift)
            rel = math.sqrt(rel ** 2 + moved ** 2)
            season = {"effect": 100 * (math.exp(shift) - 1), "pairs": pairs,
                      "since": int(since), "until": int(until), "date": date}
        return value, rel, n, note, effect, season

    def from_table(table):
        entry = table.get(str(rank))
        if entry and entry[0]:
            value, rel, n, note, effect, season = direct_read(entry)
            return (value, rel, n, note, effect, season)
        found = bracket_of(table, rank, lambda e: bool(e) and bool(e[0]))
        if not found:
            return None
        lo, hi, f = found
        (v_lo, rel_lo, n_lo, note, e_lo, s_lo), (v_hi, rel_hi, n_hi, _, e_hi, s_hi) = \
            direct_read(table[str(lo)]), direct_read(table[str(hi)])
        return (between(v_lo, v_hi, f), max(rel_lo, rel_hi), min(n_lo, n_hi), note, (1 - f) * e_lo + f * e_hi,
                dict(s_lo, effect=(1 - f) * s_lo["effect"] + f * s_hi["effect"]))

    direct = from_table(table)
    # A cup that has never run in this format reads its last edition in
    # another one, band widened - unless it is a single lobby, which the
    # closed-lobby rung below prices better than a lobby of another size.
    if not direct and not table and not single_lobby(model, tournament, field):
        for other in kin_rows(model, tournament):
            latest, row_season = str(other.get("latest") or ""), other.get("season")
            direct = from_table(other.get("direct") or {})
            if direct:
                value, rel, n, note, effect, season = direct
                direct = (value, rel * model["widen"]["entry"], n,
                          ("other_format", other.get("team_mode") or "", other.get("game_mode") or ""),
                          effect, season)
                break
    if direct:
        value, rel, n, note, effect, season = direct
        return {"ok": True, "shape_source": "direct", "value": round(value, 1),
                "low": round(max(0.0, value * (1 - rel)), 1),
                "high": round(value * (1 + rel), 1), "n": n, "source": "previous edition",
                "games": int(tournament.get("max_games") or 0), "field": field,
                "share": round(100 * rank / field, 2), "guessed_field": guessed,
                "field_effect": round(effect, 1), "entry_note": note,
                "edition": season["date"], "season_effect": round(season["effect"], 1),
                "season_pairs": season["pairs"], "season_from": season["since"], "season_to": season["until"],
                "level": round(value, 1), "ref_rank": rank}

    # The last places of a single lobby: teams that left, not a threshold.
    if single_lobby(model, tournament, field) and rank / field > model["lobby_rule"]["last"]:
        return {"ok": False, "reason": model["messages"]["last_places"]
                .replace("{rank}", str(int(rank))).replace("{field}", str(int(field)))}

    top = anchor_level(model, tournament)

    # The port of calibration's closed-lobby rung: a final in one lobby that
    # the model has no editions of is read off the finals of its mode, by
    # share of the lobby, never off the open-queue anchor and curve.
    if single_lobby(model, tournament, field) and (not top or top["source"] not in ("category", "family")):
        lobby = lobby_estimate(model, tournament, rank, field)
        if lobby:
            value, rel = lobby["value"], lobby["rel"]
            return {"ok": True, "shape_source": "lobby", "value": round(value, 1),
                    "low": round(max(0.0, value * (1 - rel)), 1),
                    "high": round(value * (1 + rel), 1), "n": lobby["n"], "source": "closed lobby",
                    "games": int(tournament.get("max_games") or 0), "field": field,
                    "share": round(100 * rank / field, 2), "guessed_field": guessed,
                    "field_effect": 0.0, "level": round(value, 1), "ref_rank": rank}

    # The port of calibration.replay_reading: the cup nobody has seen in this
    # region, recent boards replayed under its table (the calendar's `cold`).
    replay = None if single_lobby(model, tournament, field) else replay_reading(model, tournament, rank, field)
    if replay and (not top or top["source"] not in ("category", "family")):
        value, rel = replay["value"], replay["rel"]
        slope = field_sensitivity(rank, field, curve)
        return {"ok": True, "shape_source": replay["shape_source"], "value": round(value, 1),
                "low": round(max(0.0, value * (1 - rel)), 1),
                "high": round(value * (1 + rel), 1), "n": replay["n"], "source": "re-scored boards",
                "games": int(tournament.get("max_games") or 0), "field": field,
                "share": round(100 * rank / field, 2), "guessed_field": guessed,
                "field_effect": round(100 * slope * model["spread"]["field_probe"], 1) if rank > replay["deep"] else 0.0,
                "level": round(value, 1), "ref_rank": rank, "replay_deep": replay["deep"]}

    if not top:
        return {"ok": False, "reason": why_no_anchor(model, tournament)}

    slope = field_sensitivity(rank, field, curve)
    found = shape_from_model(model, tournament, rank, top["source"], field)
    if found:
        ratio, shape_rel, scope = found
        shape_source, field_part = ("ladder" if scope == "ladder" else "measured"), 0.0
    else:
        ratio, shape_rel = shape_ratio(rank, field, curve), model["spread"]["shape"]
        shape_source = "curve"
        field_part = slope * (field_spread if guessed else model["spread"]["field_entered"])
    value = top["value"] * ratio
    rel = math.sqrt(top["rel"] ** 2 + shape_rel ** 2 + field_part ** 2)
    source, replay_deep = top["source"], None
    if replay and source == "family":
        value = math.sqrt(value * replay["value"])
        rel = math.sqrt(rel ** 2 + replay["rel"] ** 2) / 2
        source, replay_deep = "family + re-scored boards", replay["deep"]
    return {
        "ok": True,
        "shape_source": shape_source,
        "value": round(value, 1),
        "low": round(max(0.0, value * (1 - rel)), 1),
        "high": round(value * (1 + rel), 1),
        "n": top["n"],
        "source": source,
        "games": int(tournament.get("max_games") or 0),
        "field": field,
        "share": round(100 * rank / field, 2),
        "guessed_field": guessed,
        "field_effect": round(100 * slope * model["spread"]["field_probe"], 1),
        "level": round(top["value"], 1),
        "ref_rank": curve["reference_rank"],
        "replay_deep": replay_deep,
    }


def replay_reading(model: dict, t: dict, rank: int, field: int) -> dict | None:
    """The port of calibration.replay_reading, off the form's `cold` table."""
    cold = t.get("cold")
    if not isinstance(cold, dict) or not cold.get("ranks"):
        return None
    if cold.get("sig") and cold["sig"] != calibration.table_signature(t.get("scoring"), t.get("max_games")):
        return None
    table = {}
    for pair in cold["ranks"]:
        try:
            at, value = int(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if at >= 1 and value > 0:
            table[at] = value
    if not table:
        return None
    rel = float(cold.get("rel") or model.get("replay_rel") or 0.12)
    donors = int(cold.get("donors") or 0)
    deepest = max(table)
    rank = int(rank)
    if rank in table:
        return {"value": table[rank], "rel": rel, "n": donors, "shape_source": "replay", "deep": deepest}
    if rank < deepest:
        found = bracket_of({str(k): v for k, v in table.items()}, rank, lambda v: bool(v))
        if not found:
            return None
        lo, hi, f = found
        return {"value": between(table[lo], table[hi], f), "rel": rel, "n": donors,
                "shape_source": "replay", "deep": deepest}
    here = shape_from_model(model, t, rank, "scoring", field)
    there = shape_from_model(model, t, deepest, "scoring", field)
    if here and there:
        ratio, shape_rel, shape_source = here[0] / there[0], math.sqrt(here[1] ** 2 + there[1] ** 2), "ladder"
    else:
        curve = curve_for(model, field)
        ratio = shape_ratio(rank, field, curve) / shape_ratio(deepest, field, curve)
        shape_rel, shape_source = model["spread"]["shape"], "curve"
    return {"value": table[deepest] * ratio, "rel": math.sqrt(rel ** 2 + shape_rel ** 2),
            "n": donors, "shape_source": shape_source, "deep": deepest}


def gave_up(model: dict, want: dict, t: dict) -> bool:
    """Did the export answer from further down the cascade on purpose?

    A row traded away for the size budget is not a broken port: the predictor
    falls through to the family, or to the mode, exactly as it would for a
    category it had never seen. Worth separating from a real disagreement, and
    worth counting — it is the price of the budget, in forecasts.

    Only a row the budget did take away: one missing from a table the trim
    never cut is a broken export, not a trade. And each rung is traced to the
    row it read: the previous edition reads a category row - this cup's, or
    the other format's it fell back on - and a family averaged with the
    re-scored boards reads the family's.
    """
    dropped = (model.get("source") or {}).get("dropped") or {}
    for source in (want.get("source"), want.get("guessed_field")):
        source, asked = str(source or "").split(" + ")[0], t
        if source == "previous edition":
            note = want.get("entry_note") or ()
            if note and note[0] == "other_format":
                asked = dict(t, team_mode=note[1], game_mode=note[2])
            source = "category"
        if source == "category" and dropped.get("categories") and not category_row(model, asked):
            return True
        if source == "family" and dropped.get("families") and not family_row(model, asked):
            return True
    return False


def as_input(comp: dict) -> dict:
    """A stored tournament rewritten as the predictor's form."""
    return {"category": comp["kind"], "name": comp.get("name") or "", "region": comp.get("region"),
            "team_mode": comp.get("team_mode"), "game_mode": comp.get("game_mode"),
            "max_games": comp.get("max_games"), "field_size": comp.get("field_size"),
            "entry": comp.get("entry") or "",
            "field_counted": calibration.counted_field(comp) > 0 or not int(comp.get("field_size") or 0),
            # The season, as the model dates the tournament into it: the
            # page reads it off the calendar row's event id instead.
            "season": comp.get("_season"),
            # The re-scored table, when the calendar wrote one beside the row.
            "cold": comp.get("cold"),
            "scoring": comp.get("scoring"), "scoring_known": comp.get("scoring_known")}


def js_round(x: float) -> int:
    """JavaScript's Math.round: halves up, where Python's round goes to even."""
    return int(math.floor(float(x) + 0.5))


def season_of_event(event_id) -> int | None:
    """The season an event id names (`epicgames_S42_...`), as the page reads it."""
    found = re.search(r"(?:^|_)S(\d+)_", str(event_id or ""), re.I)
    return int(found.group(1)) if found else None


def closed_lobby(model: dict, row: dict) -> tuple[int, bool] | None:
    """The port of the page's closedLobby: (field, guessed) when the calendar
    row is a single lobby, else None. A later round's field is the cut of the
    round before and the calendar carries it; otherwise the field the model's
    editions of the cup ranked, and a final of a cup it has never seen is a
    lobby's worth."""
    cap = lobby_cap(model, row.get("team"), row.get("mode"))
    field = int(row.get("field") or 0)
    if field > 0:
        return (field, False) if cap and field <= cap else None
    asked = {"category": row.get("kind") or row.get("name"), "region": row.get("region"),
             "team_mode": row.get("team"), "game_mode": row.get("mode")}
    known = category_row(model, asked) or family_row(model, asked)
    if known and known.get("field") and cap and known["field"] <= cap:
        return js_round(known["field"]), False
    if cap and int(row.get("stage") or 0) in (8, 9):
        return cap, True
    return None


def calendar_tournament(model: dict, row: dict, scorings: list) -> dict | None:
    """The tournament the page builds when a calendar row is clicked - the
    port of pickFromCalendar and asTournament for a row the page fills in
    whole. None for a row the page completes from what was on screen before
    (no team size, a mode it does not name, no scoring table): a forecast
    here would not be the one the page shows.

    The category is the row's own label: the calendar names a cup the way the
    model does, so a cup it knows reads its editions and one it does not is
    priced as new - see fromCalendar in the page."""
    team, mode = str(row.get("team") or ""), str(row.get("mode") or "")
    index = row.get("scoring")
    table = scorings[index] if isinstance(index, int) and 0 <= index < len(scorings) else None
    games = int(row.get("games") or 0)
    if not team or not mode or mode == "Other" or not table or not table.get("placement") or games < 1:
        return None
    closed = closed_lobby(model, row)
    field = closed[0] if closed else int(row.get("field") or 0)
    season = season_of_event(row.get("event"))
    name = str(row.get("name") or "").strip()
    return {
        "category": str(row.get("kind") or "") or name, "name": name,
        "region": row.get("region"), "team_mode": team, "game_mode": mode,
        "max_games": games, "field_size": field,
        "scoring": {"kill": table.get("kill") or 0, "kill_cap": table.get("kill_cap"),
                    "placement": table["placement"]},
        "entry": str(row.get("entry") or ""),
        "cold": row.get("cold") if isinstance(row.get("cold"), dict) else None,
        # The deepest qualification cut, the port of the page's cutMax.
        "cut_max": max([int(c[1]) for c in row.get("tiers") or []
                        if c and c[0] == "q" and isinstance(c[1], (int, float))] or [0]),
        "season": model.get("season") if season is None else season,
        "scoring_known": True,
        "single_lobby": bool(closed),
    }


def cut_rank(cut: list, field: int) -> int:
    """The rank a cut stands for: a percentile of the field, or a rank never
    deeper than the field. The port of the page's cutRank."""
    if cut[0] == "p":
        return max(1, math.ceil(float(cut[1]) * field)) if field else 0
    rank = max(1, js_round(float(cut[1] or 0)))
    return min(rank, field) if field else rank


def default_cut(cuts: list, field: int) -> tuple[list, int] | None:
    """(cut, rank): the widest qualification cut, else the widest of any kind -
    the rank the page asks first. The port of defaultCut."""
    ranked = [(cut, cut_rank(cut, field)) for cut in cuts or []]
    ranked = [x for x in ranked if x[1] > 0]
    qualify = [x for x in ranked if x[0][0] in ("q", "p")]
    pool = qualify or ranked
    best = None
    for item in pool:
        if best is None or item[1] > best[1]:
            best = item
    return best


def calendar_forecast(model: dict, row: dict, scorings: list) -> dict | None:
    """What the page would answer for a calendar row, at the ranks a list of
    the week can show: the cut the cup pays out on, and ranks 100 and 1,000
    of an open queue where the field reaches past them - the win and the top
    ten of a final of a hundred or fewer, the win of a single lobby.

    {"cut": rank or 0, "field": field, "lobby": bool, "ranks": [[rank, value,
    rel, source], ...]} - `rel` the forecast's half-width, which the page turns
    into its ranges with the model's measured multipliers (`quality.bands`)."""
    t = calendar_tournament(model, row, scorings)
    if not t:
        return None
    field = int(t["field_size"] or 0)
    if field <= 0:
        found = guess_field(model, t)
        field = js_round(found[0]) if found else 0
    cut = default_cut(row.get("tiers") or [], field)
    lobby = bool(t.pop("single_lobby"))
    wanted = [cut[1]] if cut else []
    if lobby:
        wanted.append(1)
    elif field and field <= 100:
        wanted += [1, 10]
    else:
        wanted += [r for r in (100, 1000) if not field or r < field]
    out = []
    for rank in sorted(set(wanted)):
        got = predict_from_model(model, dict(t, rank=rank))
        if got and got.get("ok") and got.get("value"):
            value = float(got["value"])
            rel = max(0.0, float(got["high"]) / value - 1) if value > 0 else 0.0
            out.append([rank, round(value, 1), round(rel, 4), got.get("source")])
    if not out:
        return None
    return {"cut": cut[1] if cut else 0, "field": field, "lobby": lobby, "ranks": out}


def variants(comp: dict):
    """The same tournament, entered the ways that force each branch.

    Every stored tournament is a category of its own, so replaying the database
    as it stands only ever exercises the first step of the cascade: family,
    scoring, prior and mode would ship unchecked, and those are the branches a
    new tournament — the only kind the predictor ever sees — actually takes.
    """
    unseen = dict(comp, family=NONE, stage="")
    unseen["kind"] = db.category_of(unseen)
    nowhere = dict(unseen, region=NONE, game_mode=NONE)
    # The same cup a season on: the previous edition read across a turn of
    # season, and the level with it.
    later = (comp.get("_season") or 0) + 1
    # A re-scored table like the calendar's, made of this cup's own finals
    # bent a little, to the depth a three-page board is trusted to: what the
    # replay rung reads, alone and averaged with the family's reading, and
    # one replayed under another table, which must not be read.
    finals = {int(r): float(v) for r, v in (comp.get("finals") or {}).items() if float(v) > 0}
    cold = {"ranks": [[r, round(v * 0.97, 1)] for r, v in sorted(finals.items()) if r <= 100],
            "donors": 4, "deep": max([r for r in finals if r <= 100], default=0), "rel": 0.12,
            "sig": calibration.table_signature(comp.get("scoring"), comp.get("max_games"))}
    stale = dict(cold, sig=cold["sig"] + "|edited")
    return [
        ("category", comp),
        ("category, next season", dict(comp, season=f"S{later}", _season=later)),
        # The same cup under another entry bar than its last edition's, and
        # with a field typed in that is not the edition's: the first rung's
        # two corrections.
        ("category, other entry bar", dict(comp, entry="ranked-br-combined:21")),
        ("category, field typed", dict(comp, field_size=max(1, int((comp.get("field_size") or 0) * 1.5)) or 300)),
        # The same cup in a team size it has never run in: the first rung's
        # other-format fallback, or the closed-lobby rung when it is one lobby.
        ("category, other format", dict(comp, team_mode="Trio" if comp.get("team_mode") != "Trio" else "Duo")),
        ("family", dict(comp, region=NONE)),
        ("scoring", unseen),
        ("re-scored boards", dict(unseen, cold=cold)),
        ("family + re-scored boards", dict(comp, region=NONE, cold=cold)),
        ("re-scored under another table", dict(unseen, cold=stale)),
        ("scoring prior", dict(unseen, game_mode=NONE)),
        ("scoring, team mode unmeasured", dict(unseen, team_mode=NONE)),
        # A final in one lobby the model has never seen: twenty teams, or the
        # lobby's own size when the tournament already is one.
        ("closed lobby", dict(unseen, field_size=min(int(comp.get("field_size") or 20), 20) or 20)),
        ("closed lobby, mode alone", dict(unseen, team_mode=NONE, field_size=20)),
        ("mode", dict(unseen, scoring_known=False)),
        ("guessed field", dict(comp, field_size=0)),
        ("guessed field by family", dict(comp, region=NONE, field_size=0)),
        ("guessed field by mode", dict(unseen, field_size=0)),
        ("refused: no games", dict(unseen, max_games=0)),
        ("refused: no field", dict(nowhere, field_size=0)),
        ("refused: no scoring", dict(nowhere, scoring={})),
        ("refused: unconfirmed", dict(nowhere, scoring_known=False)),
        ("refused: worthless scoring",
         dict(nowhere, scoring={"placement": [[1, 1, 0]], "kill": 0})),
    ]


def verify(model: dict, comps: list[dict], calib: dict) -> dict:
    """Both models, on every tournament and every branch, and the gap between.

    A shipped export that quietly disagrees with the model it was measured on
    would be worse than no export: the HTML would carry the README's error bars
    and not the README's model.
    """
    diffs, covered, mismatches, traded = [], {}, [], []
    ranks = sorted({1, 5, 8, 15, 20, 75, 100, 1000})   # 8, 15 and 75: between measured ranks
    checked = comps
    if len(comps) > VERIFY_SAMPLE:
        # Seeded, so a disagreement found today can be reproduced tomorrow.
        checked = random.Random(20260903).sample(comps, VERIFY_SAMPLE)
    for comp in checked:
        comp["_season"] = calibration.season_of(comp, calib.get("seasons"))
        every = sorted(set(ranks) | set(comp.get("ranks") or []) | set(comp.get("finals") or {}))
        for branch, variant in variants(comp):
            for rank in every:
                form = dict(as_input(variant), rank=rank)
                want = calibration.prior_prediction(variant, calib, rank)
                got = predict_from_model(model, form)
                # Keyed on what the model actually answered, not on what the
                # variant was built to provoke: a branch nobody reaches would
                # otherwise be reported as covered.
                reached = (want or {}).get("source") or "refused"
                if (want or {}).get("guessed_field"):
                    reached += f", field from {want['guessed_field']}"
                covered[(branch, reached)] = covered.get((branch, reached), 0) + 1
                if want is None or not want.get("ok"):
                    same = (want is None and got is None) or (
                        want is not None and got is not None
                        and want.get("ok") == got.get("ok")
                        and for_page(want.get("reason")) == got.get("reason"))
                    # A refusal does not say where its field came from: the
                    # last places of a lobby whose size was read off a row.
                    guessed = None if int(form.get("field_size") or 0) > 0 else \
                        (calibration.guess_field(variant, calib) or (0, None))[1]
                    if not same and not gave_up(model, dict(want or {}, guessed_field=guessed), form):
                        mismatches.append((comp["name"], branch, rank, want, got))
                    continue
                agreed = bool(got and got.get("ok")) and all(
                    want.get(key) == got.get(key) for key in
                    ("value", "low", "high", "n", "source", "field", "level",
                     "share", "guessed_field", "field_effect", "entry_note",
                     "edition", "season_effect", "season_pairs", "season_from", "season_to",
                     "shape_source", "replay_deep"))
                if agreed:
                    diffs.append(0.0)
                elif gave_up(model, want, form):
                    traded.append(abs((got or {}).get("value", 0.0) - want["value"])
                                  / want["value"] if want["value"] else 0.0)
                else:
                    mismatches.append((comp["name"], branch, rank, want, got))
                    diffs.append(abs(got["value"] - want["value"]) / want["value"]
                                 if want["value"] and got.get("ok") else 1.0)
    return {"diffs": diffs, "covered": covered, "mismatches": mismatches, "traded": traded,
            "checked": len(checked), "of": len(comps),
            "max": max(diffs) if diffs else 0.0,
            "median": statistics.median(diffs) if diffs else 0.0,
            "traded_median": statistics.median(traded) if traded else 0.0}


# A curve that has moved this much invalidates the error figures measured on the
# old one. Five per cent is roughly where a change stops being the noise of one
# more tournament and starts being a different model.
CURVE_DRIFT = 0.05


def previous_curve():
    """The curve the last export shipped, if there was one."""
    if not OUT_PATH.exists():
        return None
    try:
        return json.loads(OUT_PATH.read_text(encoding="utf-8")).get("curve")
    except (OSError, ValueError):
        return None


def report_drift(curve: dict) -> None:
    """Say so, loudly, when the shape of the model has changed under us.

    The README quotes a median error and a coverage measured against a
    particular a and b. Re-exporting silently with different ones leaves those
    figures describing a model that no longer exists - which is a worse kind of
    wrong than a stale number, because nothing looks broken.
    """
    was = previous_curve()
    if not was:
        return
    moved = {name: abs(curve[name] - was[name]) / max(abs(was[name]), 1e-9)
             for name in ("a", "b") if name in was}
    if not any(share > CURVE_DRIFT for share in moved.values()):
        return
    print("\n  THE CURVE HAS MOVED:")
    for name, share in moved.items():
        print(f"    {name}: {was[name]} -> {curve[name]}  ({100 * share:+.0f} %)")
    print("    The published error and coverage figures were measured on the old")
    print("    one. Re-run  python -m analysis.validate  and update both READMEs")
    print("    before publishing this model.")


def field_complaint(comps: list[dict]) -> str | None:
    """Refuse to ship a model whose field sizes record the harvest, not the event.

    Every forecast this file exports is a function of `q = rank / field_size`, so
    a field that is really "how many pages we downloaded" makes the whole model
    wrong in a way that looks perfectly healthy from the inside: the tables are
    full, the verification passes, the numbers are plausible. It shows up only as
    the model losing to a baseline that never looks at the field at all.

    The test itself lives in `db.suspicious_fields`, so the exporter and the
    research layer cannot drift into disagreeing about what a bad field is.
    """
    found = db.suspicious_fields(comps)
    if not found:
        return None
    common, share = found
    sized = sum(1 for c in comps if c.get("field_size"))
    return (f"{100 * share:.0f} % of {sized} tournaments record a field of exactly "
            f"{common}, which is also\nthe largest field in the database. Real field sizes "
            f"do not agree to the team; a harvest that stopped "
            f"after a fixed\nnumber of pages does. Since every forecast here is a function "
            f"of rank / field,\nthis model would be wrong everywhere and look fine.\n\n"
            f"    python src/harvest_osirion.py --rebuild\n\n"
            f"re-derives the field from each leaderboard's own page count, using the pages\n"
            f"already on disk — nothing is downloaded again. Then export.\n"
            f"Pass --allow-flat-fields to export anyway.")


def main() -> int:
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    comps = load_competitions(conn)
    # `--before 2026-09-05`: the model as it would have been exported that
    # morning, for replaying evenings that came after it without letting them
    # read their own result. `--out path`: where to write it, so a backtest
    # never touches the model the site ships.
    before = argument("--before")
    if before:
        comps = [c for c in comps if str(c.get("start_time") or "")[:10] < before]
        print(f"as of {before}: {len(comps)} tournaments started before that day")
    complaint = field_complaint(comps)
    if complaint and "--allow-flat-fields" not in sys.argv:
        print(f"\nNOT EXPORTED\n\n{complaint}\n", file=sys.stderr)
        return 1
    calib = calibration_of(comps)
    model = build_model(conn, comps, calib)
    payload = encoded(model)

    print(f"{model['source']['tournaments']} tournaments, "
          f"{model['source']['measured_on']} of them finished, "
          f"{model['source']['thresholds']} thresholds")
    print(f"  curve            a = {model['curve']['a']}, b = {model['curve']['b']}, "
          f"reference rank {model['curve']['reference_rank']}")
    for table in ("categories", "families", "modes", "mode_fields", "cold_starts",
                  "scoring_presets"):
        print(f"  {table:<16} {len(model[table])} rows"
              + (f", {model['source']['dropped'][table]} dropped to fit the budget"
                 if model["source"]["dropped"].get(table) else ""))
    quality = model.get("quality")
    if quality:
        print(f"  quality          median error {quality.get('median_ape')} %, coverage "
              f"{round(100 * (quality.get('coverage') or 0))} %, measured "
              f"{quality.get('generated')} on {quality.get('targets')} tournaments")
    else:
        print("  quality          not measured yet — the page shows dashes until "
              "`python -m analysis.validate` has run")
    pace = model.get("pace")
    if pace:
        boards = pace.get("boards") or {}
        print(f"  pace             measured {pace.get('generated')} on {boards.get('open', 0)} open "
              f"and {boards.get('closed', 0)} closed boards replayed game by game")
        for kind, label in (("open_by_time", "open queue"), ("closed_by_game", "closed lobby")):
            got = (pace.get("carry") or {}).get(kind) or {}
            if got:
                print(f"                   {label:<13} a reading carries {got['slope']:.2f} of itself "
                      f"to another rank (correlation {got['correlation']:+.2f})")
    else:
        print("  pace             not measured — the live refinement assumes a linear pace "
              "until `python -m analysis.live` has run")

    check = verify(model, comps, calib)
    refusals = sum(check["covered"].values()) - len(check["diffs"]) - len(check["traded"])
    scope = (f"all {check['of']}" if check["checked"] == check["of"]
             else f"{check['checked']} of {check['of']}")
    print(f"\nprior_prediction against predict_from_model, on {scope} tournaments: "
          f"{len(check['diffs'])} forecasts and {refusals} refusals")
    for (branch, reached), count in sorted(check["covered"].items()):
        print(f"  {branch:<30} -> {reached:<30} {count}")
    print(f"  max relative difference  {100 * check['max']:.4f} %")
    print(f"  median                   {100 * check['median']:.4f} %")
    if check["traded"]:
        print(f"  {len(check['traded'])} forecasts answer from a wider anchor because "
              f"their row was trimmed to fit the size budget, at a median cost of "
              f"{100 * check['traded_median']:.1f} % — the cascade doing what it is for.")

    if check["max"] > TOLERANCE or check["mismatches"]:
        print(f"\nDISAGREEMENT: {len(check['mismatches'])} mismatched forecasts, "
              f"worst relative gap {100 * check['max']:.3f} % against a "
              f"{100 * TOLERANCE:.1f} % tolerance.", file=sys.stderr)
        for name, branch, rank, want, got in check["mismatches"][:5]:
            print(f"  {name} [{branch}] rank {rank}:\n    python {want}\n    export {got}",
                  file=sys.stderr)
        print("Nothing written: an export that disagrees with the model is worse than "
              "no export.", file=sys.stderr)
        return 1
    if not check["diffs"]:
        # Nothing compared, nothing proved: a database with no finished cup -
        # a fresh one, or `--before` a day with none - makes a model that
        # forecasts nothing, and it would replace one that does.
        print("\nNOT EXPORTED: not one forecast was checked against the model, for want "
              "of a finished tournament to learn from. Nothing written.", file=sys.stderr)
        return 1

    out = Path(argument("--out") or OUT_PATH)
    if out == OUT_PATH:
        report_drift(model["curve"])
    out.write_text(payload, encoding="utf-8")
    print(f"\nWrote {out} — {len(payload.encode('utf-8')):,} bytes")
    return 0


def argument(flag: str) -> str | None:
    """The value after `flag` on the command line, or None."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


if __name__ == "__main__":
    raise SystemExit(main())
