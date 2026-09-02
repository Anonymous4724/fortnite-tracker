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

    python export_model.py

Writes `model.json` in the repo root — but only once it has checked the export
against the Python model it claims to reproduce. See `verify`.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from datetime import date
from pathlib import Path

import calibration
import db
import predict

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "tracker.db"
OUT_PATH = ROOT / "model.json"

VERSION = "1.0"

# The file gets pasted into an HTML page, so it lives inside that page's weight
# budget rather than a download budget. 180 KB leaves room for the app itself.
SIZE_BUDGET = 180_000

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
    return out


def calibration_of(comps: list[dict]) -> dict:
    """The three tables the cold forecast reads, and nothing else.

    `calibration.calibrate` would also cross-validate every live model against
    every other tournament — quadratic work for numbers no static predictor can
    use. These three come out identical whichever comparable circle is passed,
    because `calibrate` computes them from the wide sample alone.
    """
    broad = [c for c in comps if predict.is_complete(c)]
    curve = calibration.fit_curve(broad)
    return {"curve": list(curve),
            "reference_pace": calibration.reference_pace(broad, curve),
            "field_sizes": calibration.field_sizes(broad),
            "n_broad": len(broad)}


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
    return {"no_field": no_field.get("reason", ""),
            "no_games": why(max_games=0),
            "no_reference": why(scoring={}),
            "unconfirmed_scoring": why(scoring_known=False),
            "no_anchor": why()}


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
            share: dict | None = None) -> list[dict]:
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
            row["level"] = level.get("median")
        if share is not None:
            row["share_n"] = shared.get("n", 0)
            row["share"] = shared.get("median")
        if fields is not None:
            row["field_n"] = field.get("n", 0)
            row["field"] = field.get("median")
            row["field_spread"] = field.get("spread")
        if pace is not None:
            row["games"] = int(statistics.median(games)) if games else None
        out.append(row)
    return out


def build_tables(comps: list[dict], calib: dict) -> dict:
    """The three anchor tables, plus the field sizes that go with them.

    Field sizes deserve a word: they follow their own cascade, and its third step
    is keyed on the region as well as the mode where the anchor's is not. So the
    two ride together down to the mode, where they part and `mode_fields` takes
    over.
    """
    broad = [c for c in comps if predict.is_complete(c)]
    pace, fields = calib["reference_pace"], calib["field_sizes"]

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

    return {
        "categories": _attach(categories, pace=pace["category"], fields=fields["category"]),
        "families": _attach(families, pace=pace["family"], fields=fields["family"]),
        "modes": _attach(modes, pace=pace["mode"], share=pace["share_of_max"]),
        "mode_fields": _attach(mode_fields, fields=fields["mode"]),
    }


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
        if not placement:
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
        "reference_share": calibration.REFERENCE_SHARE,
        "anchor_spread": dict(calibration.ANCHOR_SPREAD),
        "shrinkage": {"prior_weight": 2, "thin_below": 3},
        "spread": {"shape": 0.06, "field_entered": 0.15, "field_probe": 0.30},
        "widen": {"single_edition": 1.5, "game_mode_only": 1.2,
                  "thin_sample": 1.3, "prior_only": 1.15},
        "messages": messages(calib),
    }
    model.update(build_tables(comps, calib))
    model["scoring_presets"] = scoring_presets(conn)
    model["source"]["dropped"] = trim_to_budget(model)
    model["categories"].sort(key=lambda r: (r["category"], r["region"]))
    model["families"].sort(key=lambda r: r["category"])
    model["modes"].sort(key=lambda r: (r["game_mode"], r["team_mode"]))
    model["mode_fields"].sort(key=lambda r: (r["game_mode"], r["team_mode"], r["region"]))
    return model


# Everything below reads `model` and the tournament in front of it, never the
# database and never `calibration`. It is the reference implementation: the
# JavaScript in the HTML predictor is a line-by-line port of it, and the check in
# `verify` is what says the port has something faithful to copy.
def row_for(rows: list[dict], **match) -> dict | None:
    """First row matching every field given. A port should index these once."""
    for row in rows:
        if all(row.get(key) == value for key, value in match.items()):
            return row
    return None


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


def anchor_level(model: dict, t: dict) -> dict | None:
    games = float(t.get("max_games") or 0)
    if games <= 0:
        return None
    spread, widen = model["anchor_spread"], model["widen"]

    def measured(source, row):
        if not row or row.get("n", 0) < 1:
            return None
        return {"value": row["level"] * games, "source": source, "n": row["n"],
                "rel": spread[source] * (widen["single_edition"] if row["n"] == 1 else 1.0)}

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
        for team, fallback in ((t.get("team_mode") or "", 1.0),
                               ("", widen["game_mode_only"])):
            row = row_for(model["modes"], game_mode=t.get("game_mode"), team_mode=team)
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
    top = anchor_level(model, tournament)
    if not top:
        return {"ok": False, "reason": why_no_anchor(model, tournament)}

    curve = model["curve"]
    value = top["value"] * shape_ratio(rank, field, curve)
    slope = field_sensitivity(rank, field, curve)
    field_part = slope * (field_spread if guessed else model["spread"]["field_entered"])
    rel = math.sqrt(top["rel"] ** 2 + model["spread"]["shape"] ** 2 + field_part ** 2)
    return {
        "ok": True,
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
    return {"category": comp["kind"], "region": comp.get("region"),
            "team_mode": comp.get("team_mode"), "game_mode": comp.get("game_mode"),
            "max_games": comp.get("max_games"), "field_size": comp.get("field_size"),
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
        ("family", dict(comp, region=NONE)),
        ("scoring", unseen),
        ("scoring prior", dict(unseen, game_mode=NONE)),
        ("scoring, team mode unmeasured", dict(unseen, team_mode=NONE)),
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
    ranks = sorted({1, 5, 20, 100, 1000})
    for comp in comps:
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
                        and want.get("reason") == got.get("reason"))
                    if not same and not gave_up(model, want or {}, form):
                        mismatches.append((comp["name"], branch, rank, want, got))
                    continue
                agreed = bool(got and got.get("ok")) and all(
                    want.get(key) == got.get(key) for key in
                    ("value", "low", "high", "n", "source", "field", "level",
                     "share", "guessed_field", "field_effect"))
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
            "max": max(diffs) if diffs else 0.0,
            "median": statistics.median(diffs) if diffs else 0.0,
            "traded_median": statistics.median(traded) if traded else 0.0}


def main() -> int:
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    comps = load_competitions(conn)
    calib = calibration_of(comps)
    model = build_model(conn, comps, calib)
    payload = encoded(model)

    print(f"{model['source']['tournaments']} tournaments, "
          f"{model['source']['measured_on']} of them finished, "
          f"{model['source']['thresholds']} thresholds")
    print(f"  curve            a = {model['curve']['a']}, b = {model['curve']['b']}, "
          f"reference rank {model['curve']['reference_rank']}")
    for table in ("categories", "families", "modes", "mode_fields", "scoring_presets"):
        print(f"  {table:<16} {len(model[table])} rows"
              + (f", {model['source']['dropped'][table]} dropped to fit the budget"
                 if model["source"]["dropped"].get(table) else ""))

    check = verify(model, comps, calib)
    refusals = sum(check["covered"].values()) - len(check["diffs"]) - len(check["traded"])
    print(f"\nprior_prediction against predict_from_model, "
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

    OUT_PATH.write_text(payload, encoding="utf-8")
    print(f"\nWrote {OUT_PATH} — {len(payload.encode('utf-8')):,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
