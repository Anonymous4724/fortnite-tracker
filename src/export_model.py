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
DB_PATH = ROOT / "data" / "tracker.db"
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
    """Every tournament, without its points rows.

    `db.all_full` brings the snapshots along — fine for 66 tournaments, ruinous
    for the thousands the API harvest will land, and pointless: nothing the
    export measures reads a snapshot. Only `predict.is_complete` does, and only
    for its latest timestamp, which one aggregate query covers for the lot.
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
            "reference_pace": broad["reference_pace"],
            "field_sizes": broad["field_sizes"],
            "shape": broad["shape"],
            "direct": broad.get("direct") or {},
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
            "no_anchor": for_page(why())}


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
        if not level and not field and not shared:
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
                packed = {}
                for r, v in last.items():
                    cells = [v["value"], v["n"], v["rel"], v.get("field") or 0, bar_index(v.get("entry") or "")]
                    if v.get("alt"):
                        cells.append({str(bar_index(bar)): [a[0], a[1] or 0] for bar, a in v["alt"].items()})
                    while len(cells) > 3 and cells[-1] in (0, -1, None):
                        cells.pop()
                    packed[r] = cells
                row["direct"] = packed
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
        return {"category": comp["kind"],
                "family": (comp.get("family") or "").strip() or comp["kind"],
                "stage": (comp.get("stage") or "").strip()}

    categories = _grouped(broad, lambda c: [
        ((c["kind"], c["region"]), dict(described(c), region=c["region"]))])
    families = _grouped(broad, lambda c: [(c["kind"], described(c))])
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
    }


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
    dropped = {}
    for table in ("categories", "families"):
        rows = model[table]
        rows.sort(key=lambda r: (max(r["n"], r["field_n"]), r["last"]), reverse=True)
        while rows and len(encoded(model)) > budget:
            excess, cut = len(encoded(model)) - budget, 0
            while cut < len(rows) and excess > 0:
                cut += 1
                excess -= len(json.dumps(rows[-cut], ensure_ascii=False)) + 1
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
            "lobby_median_ape", "lobby_rows", "baselines")
    return {k: found.get(k) for k in keep if k in found}


PACE_PATH = ROOT / "analysis" / "pace.json"


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
    return {k: found.get(k) for k in ("generated", "boards", "curve", "dispersion", "carry")
            if k in found}


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
        "game_minutes": dict(db.GAME_MINUTES),
        "reference_share": calibration.REFERENCE_SHARE,
        "anchor_spread": dict(calibration.ANCHOR_SPREAD),
        "shrinkage": {"prior_weight": 2, "thin_below": 3},
        "games_exponent": calibration.GAMES_EXPONENT,
        "spread": {"shape": calibration.SHAPE_FALLBACK_REL,
                   "field_entered": 0.15, "field_probe": 0.30},
        "shape_rule": {"min_editions": calibration.SHAPE_MIN, "prior_weight": 4,
                       "max_rank": calibration.SHAPE_MAX_RANK,
                       "scopes": {k: list(v) for k, v
                                  in calibration.SHAPE_SCOPES.items()}},
        "widen": {"single_edition": 1.5, "game_mode_only": 1.2,
                  "thin_sample": 1.3, "prior_only": 1.15, "cold_region": 0.9,
                  "entry": calibration.WIDEN_ENTRY},
        "field_move": {"cap": calibration.FIELD_MOVE_CAP, "rel": calibration.FIELD_MOVE_REL},
        "lobby_cap": {"large": dict(calibration.LOBBY_CAP), "small": dict(calibration.LOBBY_CAP_SMALL)},
        "lobby_rule": {"thin": calibration.LOBBY_THIN, "widen": dict(calibration.LOBBY_WIDEN)},
        "messages": messages(calib),
        "quality": measured_quality(),
        "pace": measured_pace(),
    }
    ENTRY_BARS.clear()
    model.update(build_tables(comps, calib))
    # Filled while the category rows were written, so it comes after them.
    model["entry_bars"] = list(ENTRY_BARS)
    model["scoring_presets"] = scoring_presets(conn)
    model["source"]["dropped"] = trim_to_budget(model)
    model["categories"].sort(key=lambda r: (r["category"], r["region"]))
    model["families"].sort(key=lambda r: r["category"])
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
        ("category", row_for(model["categories"], category=t.get("category"),
                             region=t.get("region"))),
        ("family", row_for(model["families"], category=t.get("category"))),
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
    stage = "final" if label.endswith((" Final", "Semi-final")) else "open"
    return (t.get("game_mode") or "", t.get("team_mode") or "", kind, platform, stage)


def anchor_level(model: dict, t: dict) -> dict | None:
    games = float(t.get("max_games") or 0)
    if games <= 0:
        return None
    spread, widen = model["anchor_spread"], model["widen"]

    def measured(source, row):
        if not row or row.get("n", 0) < 1:
            return None
        # The port of calibration.anchor_level: the stored level is what those
        # editions reached over the games they played, so a target running the
        # same number is not rescaled at all.
        theirs = float(row.get("games") or 0) or games
        value = row["level"] * (games / theirs) ** model["games_exponent"]
        once = (row.get("seen") if row.get("seen") is not None else row["n"]) == 1
        return {"value": value, "source": source, "n": row["n"],
                "rel": spread[source] * (widen["single_edition"] if once else 1.0)}

    for source, row in (("category", row_for(model["categories"], category=t.get("category"),
                                             region=t.get("region"))),
                        ("family", row_for(model["families"], category=t.get("category")))):
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


def shape_from_model(model: dict, t: dict, rank: int, source: str):
    """(ratio, relative uncertainty) at this rank, read from the exported tables.

    The port of `calibration.shape_from`, and it has to stay one: the scope
    order, the minimum edition count and the blending weight are all read from
    the model rather than retyped, so a change on the Python side arrives here
    through the file instead of through somebody remembering.
    """
    rule = model.get("shape_rule") or {}
    least, prior = rule.get("min_editions", 3), rule.get("prior_weight", 4)
    if int(rank) > rule.get("max_rank", 500):
        return None
    order = tuple(model.get("shape_rule", {}).get("scopes", {}).get(source, ()))
    for scope in order:
        if scope == "category":
            row = row_for(model["categories"], category=t.get("category"),
                          region=t.get("region"))
        elif scope == "family":
            row = row_for(model["families"], category=t.get("category"))
        else:
            row = row_for(model["modes"], game_mode=t.get("game_mode"),
                          team_mode=t.get("team_mode"))
        table = (row or {}).get("shape") or {}

        def usable(entry):
            return bool(entry) and entry[1] >= least and bool(entry[0])

        def read(entry):
            median, n, rel = entry
            weight = n / (n + prior)
            return float(median), math.sqrt(weight * rel ** 2
                                            + (1 - weight) * model["spread"]["shape"] ** 2)

        entry = table.get(str(int(rank)))
        if usable(entry):
            median, blended = read(entry)
        else:
            found = bracket_of(table, rank, usable)
            if not found:
                continue
            lo, hi, f = found
            (r_lo, b_lo), (r_hi, b_hi) = read(table[str(lo)]), read(table[str(hi)])
            median, blended = between(r_lo, r_hi, f), max(b_lo, b_hi)
        return float(median), blended
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
    if rank < 1:
        return None

    # Rung zero, the port of calibration.direct_from: this cup, this region,
    # this rank, last time.
    row = row_for(model["categories"], category=tournament.get("category"),
                  region=tournament.get("region"))
    table = (row or {}).get("direct") or {}

    want = str(tournament.get("entry") or "")
    # A count, or nothing: the page's fields are typed or read off a cut, so
    # always counts; a stored tournament's is a count unless the harvest hit
    # the API's ceiling, which as_input says.
    field_now = int(tournament.get("field_size") or 0) if tournament.get("field_counted", True) else 0

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
        if want and theirs and want != theirs:
            other = alt.get(str(bars.index(want))) if want in bars else None
            if other:
                value, edition_field = float(other[0]), int(other[1] or 0)
                note = ("same_entry",)
            else:
                rel *= model["widen"]["entry"]
                note = ("other_entry", theirs)
        effect = 0.0
        if field_now > 0 and edition_field > 0 and edition_field != field_now:
            move = field_move(rank, field_now, edition_field, model["curve"], model["field_move"]["cap"])
            value *= math.exp(move)
            rel = math.sqrt(rel ** 2 + (model["field_move"]["rel"] * move) ** 2)
            effect = 100 * (math.exp(move) - 1)
        return value, rel, n, note, effect

    direct = None
    entry = table.get(str(rank))
    if entry and entry[0]:
        value, rel, n, note, effect = direct_read(entry)
        direct = (value, rel, n, note, effect)
    else:
        found = bracket_of(table, rank, lambda e: bool(e) and bool(e[0]))
        if found:
            lo, hi, f = found
            (v_lo, rel_lo, n_lo, note, e_lo), (v_hi, rel_hi, n_hi, _, e_hi) = direct_read(table[str(lo)]), direct_read(table[str(hi)])
            direct = (between(v_lo, v_hi, f), max(rel_lo, rel_hi), min(n_lo, n_hi), note, (1 - f) * e_lo + f * e_hi)
    if direct:
        value, rel, n, note, effect = direct
        return {"ok": True, "shape_source": "direct", "value": round(value, 1),
                "low": round(max(0.0, value * (1 - rel)), 1),
                "high": round(value * (1 + rel), 1), "n": n, "source": "previous edition",
                "games": int(tournament.get("max_games") or 0), "field": field,
                "share": round(100 * rank / field, 2), "guessed_field": guessed,
                "field_effect": round(effect, 1), "entry_note": note,
                "level": round(value, 1), "ref_rank": rank}

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

    if not top:
        return {"ok": False, "reason": why_no_anchor(model, tournament)}

    curve = model["curve"]
    slope = field_sensitivity(rank, field, curve)
    found = shape_from_model(model, tournament, rank, top["source"])
    if found:
        ratio, shape_rel = found
        shape_source, field_part = "measured", 0.0
    else:
        ratio, shape_rel = shape_ratio(rank, field, curve), model["spread"]["shape"]
        shape_source = "curve"
        field_part = slope * (field_spread if guessed else model["spread"]["field_entered"])
    value = top["value"] * ratio
    rel = math.sqrt(top["rel"] ** 2 + shape_rel ** 2 + field_part ** 2)
    return {
        "ok": True,
        "shape_source": shape_source,
        "value": round(value, 1),
        "low": round(max(0.0, value * (1 - rel)), 1),
        "high": round(value * (1 + rel), 1),
        "n": top["n"],
        "source": top["source"],
        "games": int(tournament.get("max_games") or 0),
        "field": field,
        "share": round(100 * rank / field, 2),
        "guessed_field": guessed,
        "field_effect": round(100 * slope * model["spread"]["field_probe"], 1),
        "level": round(top["value"], 1),
        "ref_rank": curve["reference_rank"],
    }


def gave_up(model: dict, want: dict, t: dict) -> bool:
    """Did the export answer from further down the cascade on purpose?

    A row traded away for the size budget is not a broken port: the predictor
    falls through to the family, or to the mode, exactly as it would for a
    category it had never seen. Worth separating from a real disagreement, and
    worth counting — it is the price of the budget, in forecasts.
    """
    for source in (want.get("source"), want.get("guessed_field")):
        if source == "category" and not row_for(model["categories"],
                                                category=t.get("category"),
                                                region=t.get("region")):
            return True
        if source == "family" and not row_for(model["families"], category=t.get("category")):
            return True
    return False


def as_input(comp: dict) -> dict:
    """A stored tournament rewritten as the predictor's form."""
    return {"category": comp["kind"], "name": comp.get("name") or "", "region": comp.get("region"),
            "team_mode": comp.get("team_mode"), "game_mode": comp.get("game_mode"),
            "max_games": comp.get("max_games"), "field_size": comp.get("field_size"),
            "entry": comp.get("entry") or "",
            "field_counted": calibration.counted_field(comp) > 0 or not int(comp.get("field_size") or 0),
            "scoring": comp.get("scoring"), "scoring_known": comp.get("scoring_known")}


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
    return [
        ("category", comp),
        # The same cup under another entry bar than its last edition's, and
        # with a field typed in that is not the edition's: the first rung's
        # two corrections.
        ("category, other entry bar", dict(comp, entry="ranked-br-combined:21")),
        ("category, field typed", dict(comp, field_size=max(1, int((comp.get("field_size") or 0) * 1.5)) or 300)),
        ("family", dict(comp, region=NONE)),
        ("scoring", unseen),
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
                    if not same and not gave_up(model, want or {}, form):
                        mismatches.append((comp["name"], branch, rank, want, got))
                    continue
                agreed = bool(got and got.get("ok")) and all(
                    want.get(key) == got.get(key) for key in
                    ("value", "low", "high", "n", "source", "field", "level",
                     "share", "guessed_field", "field_effect", "entry_note"))
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

    report_drift(model["curve"])
    OUT_PATH.write_text(payload, encoding="utf-8")
    print(f"\nWrote {OUT_PATH} — {len(payload.encode('utf-8')):,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
