"""How far off is the forecast made before each cup, measured on the database?

`coldbench` reads the forecasts the week's list published; this reads none. It
makes them again, for every cup the database holds over a span of days, with
the model as it stood before the cup and the page's own port of the forecast,
and sets each against the cup's final standings. What it measures is the code
as it is today, out of sample: the number a change to the forecast has to
beat, and where the error lives.

The model a cup is priced from is the one the page has when the cup starts
under the daily update. The update collects at 18:00 Paris time (UPDATES),
16:00 UTC in summer time and 17:00 in winter time, and publishes the model
half an hour later, with every tournament over by the time it ran: an evening
cup in Europe reads the morning's editions in Asia and Oceania, a cup that
starts before the model is online reads the one of the day before. `--update`
tries other hours, or several updates a day. The model is built in memory, as
`export_model.build_model` builds it, without the check the export runs
against the app (the same model, forty times faster), from the database as it
stands: rows the harvest only caught up with later are in it, and on a day
the update was skipped or run again by hand the page had another model
(`coldbench` reads what the list really published). `--cutoff day` prices
every cup of a day from the tournaments that started before the day instead,
exactly what `export_model.py --before DAY` writes, a cup still running at
midnight included.

Each cup is priced the way the page prices it when its row of the list is
clicked:

- the row the list carried, rebuilt from the Osirion catalogue the harvest
  keeps on disk (`calendar_snapshot.entry`, with the field a cut of the round
  before feeds the window) - its cuts, its entry bar, its day of a round;
- for a cup with no finished edition in its region, the replay of the boards
  played before the model's cutoff that the list would have hung on the row
  (`rescore.cold_table`), with "no finished edition" asked of the editions
  the model holds, not of the database as it stands, which holds the cup's own
  result. The replay reads the raw leaderboard pages the harvest keeps
  (data/osirion/leaderboards, or `--leaderboards`): a run without them prices
  those cups off other rungs, says so, and is not comparable with one that had
  them;
- the page's tournament (`export_model.calendar_tournament`), its field
  guessed by the model unless the row gives it, and the forecast of
  `export_model.predict_from_model`, which the page is a line-by-line port of.

A cup the catalogue does not hold - typed in by hand, imported - is priced off
its database row instead: the same tournament with no cuts, its field left to
the model. The ranks priced are 10, 25, 100, 250, 500 and 1,000, the cut the
page asks first (`export_model.default_cut`) and every rank past 1,000 the
final standings hold, each where the final standings hold it.

The error is a ratio to the result, as in `coldbench`: 100 (forecast / result
- 1), positive when the forecast said more than the cup took. The ranges are
built the page's way, from the model's `quality.bands` in units of the
half-width, the inner one claiming half of the results and the outer nine in
ten, with their ends unrounded where the page prints whole numbers; every
model carries the bands of today's `analysis/validation.json`, in part
measured on the same cups. A bias is a group of forecasts that leans one way
on three family-days out of four at least, over two dates or more (a family
played the same evening in seven regions is one draw, not seven), and its cost
the most absolute error one factor on its forecasts can take away. Measured on
the same forecasts, the cost is an upper bound on what a correction learned
from other cups can do. The cuts on what was only known once the cup was over,
the field it drew, are shown but never offered as biases.

    python -m analysis.bench                          the last 7 days, a short pass
    python -m analysis.bench --cold --weeks 6         the last six weeks
    python -m analysis.bench --cold --since 2026-08-19 --until 2026-09-30
    python -m analysis.bench --cold --cutoff day      each day from the model of the day before
    python -m analysis.bench --cold --update 16:00    the daily update at another hour (Paris time)
    python -m analysis.bench --cold --update 18:00,22:15   two updates a day
    python -m analysis.bench --cold --json out.json   every forecast, the tables and the biases
    python -m analysis.bench --cold --cache DIR       where the models are kept
    python -m analysis.bench --cold --leaderboards DIR   the raw leaderboard pages the replays read

The span holds the cups that started from `--since` up to, not including,
`--until` (default today, UTC). Each model is kept in the cache (the system's
temporary folder by default, `--cache none` for none), compressed, under a key
made of the database's content, the code that builds the model, the measured
tables it carries and the lines here that pick its tournaments, so a model is
built again whenever any of them changes; the models of other keys go once
they are CACHE_DAYS old. The database is opened read-only. A copy given with
`--db` whose -wal is empty is read as immutable, which leaves nothing beside it
and works in a folder nobody may write; the app's own database, which the
update may be writing, is read the ordinary read-only way, and SQLite then
leaves its -wal and -shm files beside it (harmless: the next write clears
them).
"""
from __future__ import annotations

import argparse
import functools
import glob
import gzip
import hashlib
import inspect
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# The app's modules, whichever way this file is run: `-m analysis.bench` puts
# them on the path through the package, a direct run does not.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CODE = os.path.join(_ROOT, "src") if os.path.exists(os.path.join(_ROOT, "src", "db.py")) else _ROOT
for _path in (_CODE, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import calendar_snapshot  # noqa: E402
import calibration  # noqa: E402
import db  # noqa: E402
import export_model  # noqa: E402
import osirion  # noqa: E402
import rescore  # noqa: E402
from analysis import coldbench  # noqa: E402

CATALOGUE = os.path.join(_ROOT, "data", "osirion", "catalogue")

# The daily update: when it collects, in Paris time, the clock the scheduled
# update runs on, and how long its model then takes to go online.
UPDATES = ("18:00",)
PUBLISH_MINUTES = 30

# The ranks every cup is priced at, where its final standings hold them, on
# top of the cut and every deeper rank the standings hold.
RANKS = (10, 25, 100, 250, 500, 1000)
DEEP = 1000
SHORT_DAYS = 7

# What goes into a model: the code that builds it and the measured tables it
# carries. A change to any of them is a new model.
MODEL_CODE = ("export_model.py", "calibration.py", "predict.py", "db.py")
MODEL_TABLES = ("validation.json", "pace.json", "blend.json")
# A kept model of another key is dropped once this many days old.
CACHE_DAYS = 14

RANK_BANDS = ((1, 10), (11, 25), (26, 100), (101, 250), (251, 500), (501, 1000),
              (1001, 2500), (2501, 10 ** 9))
SEASON_WEEK = 7

TOP_FAMILIES = 30
TOP_BIASES = 12


# --------------------------------------------------------------------------- #
# The model before a cup
# --------------------------------------------------------------------------- #
def last_sunday(year: int, month: int) -> date:
    end = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return end - timedelta(days=(end.weekday() + 1) % 7)


def paris_in_utc(day: date, clock: str) -> datetime:
    """`clock` ("HH:MM") on `day` in Paris, as a UTC time: two hours back in
    summer time, from the last Sunday of March to the last Sunday of October,
    one hour back otherwise. Right for any hour past 03:00, when the clocks
    have changed on those two Sundays."""
    hours, minutes = (int(part) for part in clock.split(":"))
    summer = last_sunday(day.year, 3) <= day < last_sunday(day.year, 10)
    return datetime(day.year, day.month, day.day, hours, minutes) - timedelta(hours=2 if summer else 1)


def cutoff_of(start: str, by: str = "update", updates: tuple = UPDATES) -> str:
    """The model a cup starting at `start` ("YYYY-MM-DD HH:MM:SS", UTC) is
    priced from, named by its cutoff: the collection time in UTC ("YYYY-MM-DD
    16:00:00" for 18:00 in Paris in summer time) of the last update whose model
    was online when the cup started, `updates` being their hours in Paris; or
    with `by="day"` the cup's day ("YYYY-MM-DD"), everything that started
    before it."""
    day = str(start or "")[:10]
    if by == "day":
        return day
    begins = datetime.fromisoformat(str(start)[:19])
    online = timedelta(minutes=PUBLISH_MINUTES)
    for back in range(3):
        found = [at for clock in updates
                 for at in [paris_in_utc(date.fromisoformat(day) - timedelta(days=back), clock)]
                 if at + online <= begins]
        if found:
            return max(found).strftime("%Y-%m-%d %H:%M:%S")
    raise ValueError(f"no update before {start}")


def over_by(comp: dict) -> str:
    """When a tournament was over, as far as the database says."""
    return str(comp.get("end_time") or comp.get("start_time") or "")[:19]


def held(comp: dict, cutoff: str) -> bool:
    """Is the tournament in the model of `cutoff`? One over by the update's
    collection; for a cutoff that is a day, one started before it."""
    if len(cutoff) == 10:
        return str(comp.get("start_time") or "")[:10] < cutoff
    return over_by(comp) <= cutoff


def digest(paths: list[str]) -> str:
    """One SHA-1 over the files, by name and content. The code and the tables
    are read with their line endings made plain, so a checkout on Windows and
    one elsewhere give the same key for the same text."""
    h = hashlib.sha1()
    for path in paths:
        h.update(os.path.basename(path).encode())
        try:
            with open(path, "rb") as fh:
                if path.endswith((".py", ".json")):
                    h.update(fh.read().replace(b"\r\n", b"\n"))
                else:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        h.update(block)
        except OSError:
            h.update(b"-")
    return h.hexdigest()


def file_sha1(path: str) -> str:
    """The file's own SHA-1, as `sha1sum` prints it."""
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def model_key(db_path: str) -> str:
    """What a model is a function of, besides its cutoff: the database, the
    code that builds it, the tables it carries, and the lines here that choose
    its tournaments and build it."""
    # Rows a writer has committed may still sit in the -wal beside the file.
    pending = [db_path + "-wal"] if os.path.exists(db_path + "-wal") and os.path.getsize(db_path + "-wal") else []
    h = hashlib.sha1(digest([db_path] + pending + [os.path.join(_CODE, name) for name in MODEL_CODE]
                            + [os.path.join(_ROOT, "analysis", name) for name in MODEL_TABLES]).encode())
    for chooser in (over_by, held, Models.get):
        h.update(inspect.getsource(chooser).encode())
    return h.hexdigest()[:16]


class Models:
    """The model as it stood at each cutoff, built once and kept."""

    def __init__(self, conn, comps: list[dict], key: str, cache: str | None):
        # The tournaments handed in are part of the key: which ones the app
        # loads (db.EXCLUDED, FNT_KEEP_EXCLUDED) is not in the files hashed.
        ids = ",".join(str(c.get("id")) for c in sorted(comps, key=lambda c: str(c.get("id"))))
        self.key = hashlib.sha1(f"{key}|{ids}".encode()).hexdigest()[:16]
        self.conn, self.comps, self.cache = conn, comps, cache
        self.built, self.read = 0, 0

    def path(self, cutoff: str) -> str | None:
        name = cutoff.replace(" ", "T").replace(":", "")[:15]
        return os.path.join(self.cache, f"model-{name}-{self.key}.json.gz") if self.cache else None

    def get(self, cutoff: str) -> dict:
        path = self.path(cutoff)
        text = None
        if path and os.path.exists(path):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    text = fh.read()
                self.read += 1
            except (OSError, EOFError):
                text = None
        if text is None:
            kept = [c for c in self.comps if held(c, cutoff)]
            model = export_model.build_model(self.conn, kept, export_model.calibration_of(kept))
            # Read back through JSON, as the page reads it: the port looks its
            # ranks up as strings, the way the file spells them.
            text = export_model.encoded(model)
            self.built += 1
            if path:
                try:
                    os.makedirs(self.cache, exist_ok=True)
                    with gzip.open(path + ".part", "wt", encoding="utf-8") as fh:
                        fh.write(text)
                    os.replace(path + ".part", path)
                except OSError:
                    pass
        # The port's lookups are cached by the identity of the lists they
        # index; a model read after another may reuse an identity.
        export_model._INDEXES.clear()
        export_model._KIN.clear()
        return json.loads(text)

    def prune(self, days: int = CACHE_DAYS) -> int:
        """Drop the models of other keys nobody has built or read for `days`
        days: each new database is a new key, and a new set of models."""
        if not self.cache or not os.path.isdir(self.cache):
            return 0
        horizon, dropped = time.time() - days * 86400, 0
        for path in glob.glob(os.path.join(self.cache, "model-*.json.gz")):
            if path.endswith(f"-{self.key}.json.gz"):
                continue
            try:
                if os.path.getmtime(path) < horizon:
                    os.remove(path)
                    dropped += 1
            except OSError:
                pass
        return dropped


# --------------------------------------------------------------------------- #
# The list's rows, rebuilt
# --------------------------------------------------------------------------- #
def load_catalogue(folder: str) -> dict:
    """(event id, window id) -> (event, window, region), off the catalogue files
    the harvest keeps: the payloads `calendar_snapshot` reads from the API, with
    every window since long before the span. The region is the event's own,
    chosen as `calendar_snapshot.collect` chooses it."""
    events: dict = {}
    for path in sorted(glob.glob(os.path.join(folder, "*.json.gz"))):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                found = json.load(fh)
        except (OSError, ValueError):
            continue
        for event in found if isinstance(found, list) else []:
            if isinstance(event, dict) and event.get("eventId"):
                events.setdefault(event["eventId"], event)
    index = {}
    for event in events.values():
        own = osirion.event_regions(event)
        if not own:
            continue
        region = next((r for r in own if r in db.REGIONS), own[0][:8])
        for window in event.get("eventWindows") or []:
            if window.get("eventWindowId"):
                index[(event["eventId"], window["eventWindowId"])] = (event, window, region)
    return index


def listed_row(event: dict, window: dict, region: str) -> dict | None:
    """The row the week's list carried for this window, as
    `calendar_snapshot.collect` writes it - None for one it leaves out."""
    row = calendar_snapshot.entry(event, window, region)
    if not row or not row.get("games"):
        return None
    if db.is_excluded({"family": row["kind"], "name": row["name"]}):
        return None
    event_id, window_id = event.get("eventId") or "", window.get("eventWindowId") or ""
    row["field"] = calendar_snapshot.fed_field(
        calendar_snapshot.fed_by(event), osirion.humanise_token(window_id, event_id)) \
        or calendar_snapshot.fed_next(event).get(window_id, 0)
    return row


def database_row(comp: dict) -> dict:
    """A row for a cup the catalogue does not hold, off its database row: no
    cuts, no field but the one the model knows the cup's lobby by, and no
    scoring table where the database only assumes one."""
    begin = str(comp.get("start_time") or "")
    return {"kind": comp.get("kind") or "", "name": comp.get("name") or "",
            "event": comp.get("event_id") or "", "window": comp.get("window_id") or "",
            "stage": db.round_of(comp), "region": comp.get("region") or "",
            "team": comp.get("team_mode") or "", "mode": comp.get("game_mode") or "",
            "games": int(comp.get("max_games") or 0),
            "begin": begin[:16].replace(" ", "T") + "Z" if begin else "",
            "scoring": comp.get("scoring") if comp.get("scoring_known") else None,
            "tiers": [], "entry": comp.get("entry") or "", "field": 0}


def has_edition_before(conn, kind: str, region: str, team_mode: str, game_mode: str, cutoff: str) -> bool:
    """`rescore.has_edition`, asked of the editions the model of `cutoff` holds."""
    held_by = "c.start_time < ?" if len(cutoff) == 10 else "COALESCE(c.end_time, c.start_time) <= ?"
    rows = conn.execute(
        "SELECT c.name, c.family, c.stage, c.source, c.round_no FROM competition c WHERE c.region = ? "
        f"AND c.team_mode = ? AND c.game_mode = ? AND {held_by} "
        "AND EXISTS (SELECT 1 FROM final_result f WHERE f.competition_id = c.id)",
        (region, team_mode, game_mode, cutoff)).fetchall()
    return any(db.category_of(dict(row)) == kind for row in rows)


def donors_over_by(pool, cutoff: str):
    """`rescore.candidates` (passed as `pool`), keeping only the boards the
    model of `cutoff` holds. The list asks for boards that started before a
    date, but on a day of the past the database also holds the final
    standings of a board still running at the update, which nobody had then."""
    def candidates(*args, **kwargs):
        return [c for c in pool(*args, **kwargs) if held(c, cutoff)]
    return candidates


def cold_cell(conn, row: dict, cutoff: str) -> dict | None:
    """The replay the list would have hung on the row, beside the model of
    `cutoff`.

    `rescore.cold_table` is the list's own code. The two questions it asks of
    the database as it stands are asked here of what the model holds, since on
    a day of the past the database also holds what came after: has the cup a
    finished edition in its region, and which boards are over to replay.
    """
    asked, pool = rescore.has_edition, rescore.candidates
    rescore.has_edition = lambda conn_, kind, region, team, mode: has_edition_before(
        conn_, kind, region, team, mode, cutoff)
    rescore.candidates = donors_over_by(pool, cutoff)
    try:
        return rescore.cold_table(conn, row, before=cutoff)
    finally:
        rescore.has_edition, rescore.candidates = asked, pool


def tournament_of(model: dict, row: dict) -> tuple[dict, int, tuple | None, bool] | None:
    """(the page's tournament, its field, the cut it asks first, a closed
    lobby) for a row - the first half of `export_model.calendar_forecast` - or
    None for a row the page does not open by itself."""
    scoring = row.get("scoring") if isinstance(row.get("scoring"), dict) else None
    t = export_model.calendar_tournament(model, dict(row, scoring=0), [scoring] if scoring else [])
    if not t:
        return None
    field = int(t["field_size"] or 0)
    if field <= 0:
        found = export_model.guess_field(model, t)
        field = export_model.js_round(found[0]) if found else 0
    cut = export_model.default_cut(row.get("tiers") or [], field)
    lobby = bool(t.pop("single_lobby"))
    return t, field, cut, lobby


def edition_field(model: dict, t: dict, rank: int, got: dict) -> int:
    """The field of the edition a previous-edition forecast read, 0 for none."""
    if got.get("source") != "previous edition":
        return 0
    note = got.get("entry_note") or ()
    asked = dict(t, team_mode=note[1], game_mode=note[2]) if note and note[0] == "other_format" else t
    table = (export_model.category_row(model, asked) or {}).get("direct") or {}
    cell = table.get(str(rank))
    if not cell:
        found = export_model.bracket_of(table, rank, lambda e: bool(e) and bool(e[0]))
        cell = table.get(str(found[0])) if found else None
    if not cell:
        return 0
    if note and note[0] == "same_entry":
        bars = model.get("entry_bars") or []
        want = str(t.get("entry") or "")
        other = (cell[5] if len(cell) > 5 and isinstance(cell[5], dict) else {}).get(
            str(bars.index(want)) if want in bars else "")
        return int(other[1] or 0) if other else 0
    return int(cell[3]) if len(cell) > 3 and cell[3] else 0


def refusal(got: dict | None) -> str:
    """A refusal, named by the start of its reason."""
    if not got:
        return "no answer"
    reason = str(got.get("reason") or "no value").split(".")[0]
    return reason[:70]


# --------------------------------------------------------------------------- #
# Forecasts and results, paired
# --------------------------------------------------------------------------- #
def season_starts(conn) -> dict[int, str]:
    """The first day the database saw each season on."""
    out: dict[int, str] = {}
    for season, first in conn.execute(
            "SELECT season, MIN(start_time) FROM competition WHERE season != '' GROUP BY season"):
        number = calibration.season_number(season)
        if number is not None and first:
            out[number] = min(out.get(number, str(first)[:10]), str(first)[:10])
    return out


# Hours from UTC to each region's evening clock, to date a session the way its
# players do: a North American evening begins after midnight UTC.
REGION_HOURS = {"NAW": -7, "NAC": -4, "NAE": -4, "BR": -3, "EU": 1, "ME": 4, "ASIA": 9, "OCE": 10}


def local_day(start, region) -> str:
    """The date a session starting at `start` (UTC) has where it is played."""
    begins = datetime.fromisoformat(str(start or "")[:19])
    return (begins + timedelta(hours=REGION_HOURS.get(str(region or ""), 0))).strftime("%Y-%m-%d")


def round_day(window_id) -> str:
    """"one session", "day 1 of several" or "day 2 or later"."""
    if export_model.later_day(window_id):
        return "day 2 or later"
    return "day 1 of several" if export_model.DAY_OF_ROUND.search(str(window_id or "")) else "one session"


def price(conn, model: dict, comp: dict, catalogue: dict, cutoff: str, starts: dict,
          skipped: Counter, refused: Counter, replayed: Counter | None = None) -> list[dict]:
    """Every rank of one cup the page prices and its final standings hold."""
    finals = {int(r): float(v) for r, v in (comp.get("finals") or {}).items() if v and float(v) > 0}
    if not finals:
        skipped["no final standings"] += 1
        return []
    found = catalogue.get((comp.get("event_id"), comp.get("window_id")))
    row, source = (listed_row(*found), "calendar") if found else (None, "database")
    if row is None:
        if found:
            skipped["catalogue row the list leaves out, priced off the database"] += 1
        row, source = database_row(comp), "database"
    reads = replayed["reads"] if replayed is not None else 0
    row["cold"] = cold_cell(conn, row, cutoff)
    if replayed is not None:
        if row["cold"]:
            replayed["cups"] += 1
        elif replayed["reads"] > reads:
            replayed["short"] += 1
    priced = tournament_of(model, row)
    if not priced:
        skipped["a row the page does not open by itself"] += 1
        return []
    t, field, cut, lobby = priced
    cut_rank = int(cut[1]) if cut else 0
    wanted = set(RANKS) | {r for r in finals if r > DEEP} | ({cut_rank} if cut_rank else set())
    if cut_rank and cut_rank not in finals:
        skipped["cups whose cut the final standings do not hold"] += 1
    day = str(comp.get("start_time") or "")[:10]
    season = calibration.season_number(comp.get("season")) or export_model.season_of_event(comp.get("event_id"))
    start = starts.get(season) if season is not None else None
    first_week = bool(start) and day < (date.fromisoformat(start) + timedelta(days=SEASON_WEEK)).isoformat()
    bands = (model.get("quality") or {}).get("bands") or None
    out = []
    for rank in sorted(r for r in wanted if r in finals):
        got = export_model.predict_from_model(model, dict(t, rank=rank))
        if not got or not got.get("ok") or not got.get("value"):
            refused[refusal(got)] += 1
            continue
        value = float(got["value"])
        note = got.get("entry_note") or ()
        out.append({
            "day": day, "local_day": local_day(comp.get("start_time"), comp.get("region")), "model": cutoff,
            "window": f"{comp.get('event_id')}|{comp.get('window_id')}" if comp.get("event_id") else f"#{comp['id']}",
            "id": comp["id"], "name": comp.get("name") or "", "category": comp.get("kind") or "",
            "family": comp.get("family") or comp.get("kind") or "",
            "region": comp.get("region") or "", "team_mode": comp.get("team_mode") or "",
            "game_mode": comp.get("game_mode") or "", "entry": str(t.get("entry") or ""),
            "lobby": lobby, "round_day": round_day(comp.get("window_id")), "first_week": first_week,
            "season": season, "input": source,
            "rank": rank, "cut": cut_rank, "at_cut": rank == cut_rank,
            "forecast": value, "rel": max(0.0, float(got["high"]) / value - 1) if value > 0 else 0.0,
            "result": finals[rank], "bands": bands,
            "source": got.get("source") or "", "shape_source": got.get("shape_source") or "",
            "guessed_field": got.get("guessed_field") or "", "field": field,
            "result_field": calibration.counted_field(comp), "previous_field": edition_field(model, t, rank, got),
            "entry_note": note[0] if note else "", "season_effect": float(got.get("season_effect") or 0.0),
            "later_day": int(got.get("later_day") or 0), "cold": bool(t.get("cold")),
        })
    return out


# --------------------------------------------------------------------------- #
# The cuts
# --------------------------------------------------------------------------- #
def moved_label(pair: dict) -> str:
    """The cup's field against the field of the edition its forecast read."""
    now, then = pair["result_field"], pair["previous_field"]
    if pair["source"] != "previous edition" or then <= 0:
        return "no edition read"
    if now <= 0:
        return "not counted"
    move = now / then - 1
    return "shrank > 10 %" if move < -0.1 else "grew > 10 %" if move > 0.1 else "within 10 %"


def page_field_label(pair: dict) -> str:
    """The field the page priced the cup for: the row's, or the model's guess."""
    field = int(pair["field"] or 0)
    if field <= 0:
        return "none"
    if field >= calibration.FIELD_CAP:
        return f"{calibration.FIELD_CAP:,}+"
    return coldbench.band_label(field, coldbench.FIELD_BANDS)


CUTS = OrderedDict([
    ("rank band", lambda p: coldbench.band_label(p["rank"], RANK_BANDS)),
    ("at the cut", lambda p: "the cut" if p["at_cut"] else "other ranks"),
    ("region", lambda p: p["region"]),
    ("game mode", lambda p: p["game_mode"]),
    ("team mode", lambda p: p["team_mode"]),
    ("field the page used", page_field_label),
    ("field guessed from", lambda p: p["guessed_field"] or "the row"),
    ("field drawn", coldbench.field_label),
    ("field drawn against the edition read", moved_label),
    ("rung", lambda p: p["source"] or "?"),
    ("entry bar", lambda p: p["entry"] or "none"),
    ("day of the round", lambda p: p["round_day"]),
    ("week of the season", lambda p: "first week" if p["first_week"] else "later"),
    ("closed-lobby final", lambda p: "closed lobby" if p["lobby"] else "open queue"),
    ("input", lambda p: "list row" if p["input"] == "calendar" else "database row"),
    ("family", lambda p: p["family"]),
])
# The value of a cut that says "nothing special": a lean there is the other
# causes it holds, which the other cuts name.
PLAIN = {("at the cut", "other ranks"), ("entry bar", "none"), ("day of the round", "one session"),
         ("week of the season", "later"), ("closed-lobby final", "open queue"), ("input", "list row")}
# Cuts on what was only known once the cup was over: shown in the tables, never
# offered as a bias, since no forecast made before the cup could correct by them.
AFTER = {"field drawn", "field drawn against the edition read"}


def gain_of(errors: list[float]) -> tuple[float, float]:
    """(median, the most absolute error one factor on the group's forecasts can
    take away). A forecast can be scaled, not moved by points of a result
    nobody has yet. With x = forecast / result, the scaled error is |x y - 1|,
    whose sum is least at the median of 1/x weighted by x: measured on the
    forecasts it was found on, an upper bound for any correction by a factor."""
    middle = statistics.median(errors)
    ratios = [1 + e / 100 for e in errors if e > -100]
    if not ratios:
        return middle, 0.0
    ranked, half, seen, factor = sorted((1 / x, x) for x in ratios), sum(ratios) / 2, 0.0, 1.0
    for inverse, weight in ranked:
        seen += weight
        if seen >= half:
            factor = inverse
            break
    after = 100 * sum(abs(x * factor - 1) for x in ratios)
    return middle, sum(abs(e) for e in errors) - after


def biases(pairs: list[dict], top: int = TOP_BIASES) -> list[dict]:
    """The groups of the cuts above that lean one way, costliest first, each
    with the costlier one holding most of its forecasts: `coldbench.biases`,
    with one draw per family and local day, the cost a correction by a factor
    can have, and no cut on what the cup's outcome told."""
    groups: dict = {}
    for p in pairs:
        for name, cut in CUTS.items():
            groups.setdefault((name, cut(p)), []).append(p)
    found = []
    for (name, value), members in groups.items():
        if name in AFTER or (name, value) in PLAIN or len(members) < coldbench.MIN_PAIRS:
            continue
        # One draw per family and local day: the regions of one event share
        # its evening, and a lean seen on one date only is one draw too.
        units: dict = {}
        for p in members:
            units.setdefault((p["family"], p.get("local_day") or p["day"]), []).append(coldbench.error(p))
        if len(units) < coldbench.MIN_WINDOWS or len({day for _, day in units}) < 2:
            continue
        middle, gain = gain_of([coldbench.error(p) for p in members])
        leans = [statistics.median(v) for v in units.values()]
        if gain <= 0 or sum(1 for x in leans if x * middle > 0) < coldbench.CONSISTENT * len(leans):
            continue
        found.append({"cut": name, "value": value, "pairs": len(members),
                      "cups": len({p["window"] for p in members}), "family_days": len(units),
                      "lean": middle, "cost": gain, "members": {id(p) for p in members}})
    found.sort(key=lambda b: -b["cost"])
    found = found[:top]
    for n, b in enumerate(found):
        share, k = max(((len(b["members"] & a["members"]) / len(b["members"]), j + 1)
                        for j, a in enumerate(found[:n])), default=(0.0, None))
        b["within"] = k if share >= 0.5 else None
    for b in found:
        del b["members"]
    return found


def tables(pairs: list[dict]) -> dict:
    """{cut: {value: summary}}, every cut."""
    out = {}
    for name, cut in CUTS.items():
        grouped: dict = {}
        for p in pairs:
            grouped.setdefault(cut(p), []).append(p)
        out[name] = {str(value): coldbench.summary(members) for value, members in
                     sorted(grouped.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))}
    return out


def report(pairs: list[dict], heading: str) -> tuple[dict, list[dict]]:
    print("\n" + "=" * 96)
    print(f"  {heading}")
    print("=" * 96)
    if not pairs:
        print("  nothing to measure")
        return {}, []
    print("  error in % of the result, + when the forecast said more than the cup took;"
          " the ranges are the page's")
    coldbench.print_table("all", [("all ranks", coldbench.summary(pairs)),
                                  ("at the cut", coldbench.summary([p for p in pairs if p["at_cut"]]))])
    found = tables(pairs)
    for name, rows in found.items():
        shown = list(rows.items())
        if name == "family" and len(shown) > TOP_FAMILIES:
            shown = shown[:TOP_FAMILIES]
            name = f"family (the {TOP_FAMILIES} with the most forecasts)"
        coldbench.print_table(f"by {name}", shown)
    leaning = biases(pairs)
    total = sum(abs(coldbench.error(p)) for p in pairs)
    print(f"\nbiases, costliest first (a group of {coldbench.MIN_WINDOWS}+ family-days (f-days: a family on one local"
          f" date) over 2+ dates and {coldbench.MIN_PAIRS}+ forecasts,\n  {100 * coldbench.CONSISTENT:.0f} % of its"
          f" family-days leaning its way; lean = its median error; cost = the most |error| one factor on\n  its"
          f" forecasts takes away, of {total:,.0f} in all, on these same forecasts (an upper bound);\n  within = the"
          f" costlier group that holds most of its forecasts)")
    print(f"  {'':<4}{'cut':<32}{'value':<30}{'pairs':>6}{'cups':>6}{'f-days':>7}{'lean':>8}{'cost':>8}"
          f"{'share':>7}{'within':>8}")
    for n, b in enumerate(leaning, 1):
        print(f"  {n:<4}{b['cut'][:31]:<32}{str(b['value'])[:29]:<30}{b['pairs']:>6}{b['cups']:>6}{b['family_days']:>7}"
              f"{b['lean']:>+8.1f}{b['cost']:>8.0f}{100 * b['cost'] / total:>6.0f}%"
              f"{('#' + str(b['within'])) if b['within'] else '':>8}")
    return found, leaning


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def span_of(args) -> tuple[str, str]:
    until = args.until or datetime.now(timezone.utc).date().isoformat()
    days = 7 * args.weeks if args.weeks else (args.days or SHORT_DAYS)
    since = args.since or (date.fromisoformat(until) - timedelta(days=days)).isoformat()
    return since, until


def cold(conn, since: str, until: str, catalogue: dict, cache: str | None, key: str,
         by: str = "update", progress: bool = True, updates: tuple = UPDATES) -> dict:
    """Every cup that started in [since, until) and finished, priced from the
    model before it (see `cutoff_of`). {"pairs", "skipped", "refused",
    "models", "cups", "built", "read", "model_key", "replays", "bands"}."""
    comps = export_model.load_competitions(conn)
    targets: dict = {}
    for comp in comps:
        day = str(comp.get("start_time") or "")[:10]
        if since <= day < until:
            targets.setdefault(cutoff_of(str(comp.get("start_time") or ""), by, updates), []).append(comp)
    models = Models(conn, comps, key, cache)
    starts = season_starts(conn)
    pairs, skipped, refused, sizes, replayed, bands = [], Counter(), Counter(), {}, Counter(), None
    # A board replayed for one cup is often a donor for the next: read once,
    # and noted with the rosters it gave, so two runs can tell whether they
    # replayed the same boards.
    reader, boards = rescore.rosters_of, {}

    def read_and_note(event_id, window_id):
        rosters = reader(event_id, window_id)
        content = repr([(r.get("rank"), r.get("points"), r.get("games")) for r in rosters])
        boards[(str(event_id), str(window_id))] = (len(rosters), hashlib.sha1(content.encode()).hexdigest()[:8])
        return rosters

    kept = functools.lru_cache(maxsize=256)(read_and_note)

    def read(event_id, window_id):
        replayed["reads"] += 1
        return kept(event_id, window_id)

    rescore.rosters_of = read
    began = time.time()
    try:
        for n, cutoff in enumerate(sorted(targets), 1):
            model = models.get(cutoff)
            sizes[cutoff] = {"tournaments": model["source"]["tournaments"], "cups": len(targets[cutoff])}
            quality = model.get("quality") or {}
            bands = bands or {"generated": quality.get("generated"), "from": quality.get("from")}
            before = len(pairs)
            for comp in sorted(targets[cutoff], key=lambda c: (str(c.get("start_time")), c["id"])):
                pairs += price(conn, model, comp, catalogue, cutoff, starts, skipped, refused, replayed)
            if progress:
                print(f"  {cutoff[:16]:<16}  model of {model['source']['tournaments']:>5} tournaments, "
                      f"{len(targets[cutoff]):>3} cups, {len(pairs) - before:>4} forecasts"
                      f"   ({n}/{len(targets)}, {time.time() - began:.0f} s)", flush=True)
            del model
    finally:
        rescore.rosters_of = reader
    models.prune()
    listed = sorted(f"{event}|{window}|{n}|{sha}" for (event, window), (n, sha) in boards.items())
    return {"pairs": pairs, "skipped": skipped, "refused": refused, "models": sizes,
            "cups": sum(len(v) for v in targets.values()), "built": models.built, "read": models.read,
            "model_key": models.key,
            "replays": {"cups": replayed["cups"], "cups_short_of_boards": replayed["short"],
                        "boards_asked": len(listed), "boards_with_pages": sum(1 for n, _ in boards.values() if n),
                        "digest": hashlib.sha1("\n".join(listed).encode()).hexdigest()[:16]},
            "bands": bands}


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cold", action="store_true",
                        help="the forecast made before each cup (the only bench so far, and the default)")
    parser.add_argument("--weeks", type=int, default=0, help="the span, in weeks back from --until")
    parser.add_argument("--days", type=int, default=0, help=f"the span, in days (default {SHORT_DAYS})")
    parser.add_argument("--since", default="", help="the first day (YYYY-MM-DD)")
    parser.add_argument("--until", default="", help="the day after the last (default: today, UTC)")
    parser.add_argument("--cutoff", choices=("update", "day"), default="update",
                        help="the model a cup reads: the last daily update's (default), or the "
                             "tournaments that started before its day")
    parser.add_argument("--update", type=clocks, default=UPDATES, metavar="HH:MM[,HH:MM]",
                        help=f"the daily updates, Paris time (default {','.join(UPDATES)})")
    parser.add_argument("--db", default="", help="the database (default: the app's)")
    parser.add_argument("--catalogue", default=CATALOGUE, help="the harvest's catalogue folder")
    parser.add_argument("--leaderboards", default="",
                        help="the raw leaderboard pages the replayed tables read "
                             "(default: data/osirion/leaderboards, where the harvest keeps them)")
    parser.add_argument("--cache", default="", help="where the models are kept ('none': nowhere)")
    parser.add_argument("--json", default="", help="write every forecast, the tables and the biases here")
    return parser


def clocks(text: str) -> tuple:
    """"18:00" or "18:00,22:15" -> ("18:00", "22:15"), each checked."""
    out = []
    for part in text.split(","):
        try:
            hours, minutes = (int(x) for x in part.strip().split(":"))
        except ValueError:
            raise argparse.ArgumentTypeError(f"not an hour: {part!r}")
        if not (3 <= hours <= 23 and 0 <= minutes <= 59):
            raise argparse.ArgumentTypeError(f"not an hour between 03:00 and 23:59: {part!r}")
        out.append(f"{hours:02d}:{minutes:02d}")
    return tuple(sorted(set(out)))


def pages_in(folder: str) -> int:
    """How many events the raw leaderboard folder holds pages of (0: none):
    a folder one level up or down, or holding something else, counts none."""
    try:
        events = [entry.path for entry in os.scandir(folder) if entry.is_dir()]
    except OSError:
        return 0
    found = 0
    for event in events:
        try:
            found += any(rescore.PAGE.search(name) for name in os.listdir(event))
        except OSError:
            pass
    return found


def open_read_only(path: str, live: bool):
    """The database, read-only: as immutable for a copy nothing writes, the
    ordinary way for the app's own or for a copy with rows still in its -wal."""
    wal = path + "-wal"
    frozen = not live and not (os.path.exists(wal) and os.path.getsize(wal))
    uri = Path(path).resolve().as_uri() + "?mode=ro" + ("&immutable=1" if frozen else "")
    return sqlite3.connect(uri, uri=True)


def same_file(a: str, b: str) -> bool:
    """Are the two paths one file? By the file itself where both exist, so a
    link or another spelling of the app's database is still the app's."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def main(argv: list[str] | None = None) -> int:
    args = argument_parser().parse_args(argv)
    path = args.db or db.DB_PATH
    if not os.path.exists(path):
        print(f"No database at {path}: nothing measured.")
        return 0
    since, until = span_of(args)
    cache = None if args.cache.lower() == "none" else (
        args.cache or os.path.join(tempfile.gettempdir(), "fortnite-tracker-bench"))
    catalogue = load_catalogue(args.catalogue)
    boards = os.path.abspath(args.leaderboards or rescore.RAW)
    events = pages_in(boards)
    which = (f"the last daily update online before it ({', '.join(args.update)} Paris time, online "
             f"{PUBLISH_MINUTES} min later)" if args.cutoff == "update" else "the tournaments started before its day")
    print(f"Cold bench, cups from {since} to {until} (not included), each priced from the model of {which}")
    print(f"  database {path}; catalogue: {len(catalogue):,} windows"
          + ("" if catalogue else " - every cup is priced off its database row"))
    print(f"  raw leaderboard pages: {events:,} events in {boards}")
    if not events:
        print("  WARNING: no raw leaderboard pages there. The replayed tables are off: the cups new in their region\n"
              "  fall back on other rungs, and this run does not compare with one that had the pages (--leaderboards).")
    key = model_key(path)
    conn = open_read_only(path, live=same_file(path, str(db.DB_PATH)))
    conn.row_factory = sqlite3.Row
    began = time.time()
    kept_boards, rescore.RAW = rescore.RAW, boards
    try:
        run = cold(conn, since, until, catalogue, cache, key, args.cutoff, updates=args.update)
    finally:
        rescore.RAW = kept_boards
        conn.close()
    pairs = run["pairs"]
    print(f"\n{run['cups']} cups, {len(pairs)} forecasts, {run['built']} models built and "
          f"{run['read']} read from {cache or 'nowhere'}, in {time.time() - began:.0f} s")
    replays = run["replays"]
    print(f"Replayed tables on {replays['cups']} cups; {replays['cups_short_of_boards']} more asked for one and found "
          f"fewer than two usable boards. Boards: {replays['boards_with_pages']} with pages of "
          f"{replays['boards_asked']} asked (digest {replays['digest']}).")
    if replays["boards_asked"] and not replays["boards_with_pages"]:
        print("WARNING: not one board the replays asked for had pages: the replayed tables are off in this run.")
    if run["bands"]:
        print(f"Ranges from the validation generated {run['bands'].get('generated')}, the same for every model.")
    for title, found in (("Cups and forecasts left out:", run["skipped"]),
                         ("Forecasts refused by the model:", run["refused"])):
        if found:
            print(f"\n{title}")
            for why, n in found.most_common():
                print(f"  {n:>5}  {why}")
    found, leaning = report(pairs, f"cold: the forecast before each cup ({since} to {until}, "
                                   f"cutoff: {args.cutoff})")
    if args.json:
        payload = {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "bench": "cold", "since": since, "until": until, "cutoff": args.cutoff,
            "update": {"paris": list(args.update), "online_after_minutes": PUBLISH_MINUTES}
            if args.cutoff == "update" else None,
            "database": {"sha1": file_sha1(path), "bytes": os.path.getsize(path),
                         **({"wal_sha1": file_sha1(path + "-wal"), "wal_bytes": os.path.getsize(path + "-wal")}
                            if os.path.exists(path + "-wal") and os.path.getsize(path + "-wal") else {})},
            "catalogue": {"windows": len(catalogue),
                          "files": len(glob.glob(os.path.join(args.catalogue, "*.json.gz")))},
            "leaderboards": {"events": events}, "replays": run["replays"], "bands": run["bands"],
            "model_key": run["model_key"], "ranks": list(RANKS), "cups": run["cups"],
            "models": run["models"], "skipped": dict(run["skipped"]), "refused": dict(run["refused"]),
            "all": coldbench.summary(pairs) if pairs else None,
            "at_cut": coldbench.summary([p for p in pairs if p["at_cut"]]) if pairs else None,
            "tables": found, "biases": leaning, "pairs": pairs,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
