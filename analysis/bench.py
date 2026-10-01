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
midnight included. `--cutoff published` prices each cup from the model the
page really served when it started, the last model.json committed to the
page's repository before it (`--ladder`), rebuilt with today's code from the
rows the database held by the commit (`created_at`) and over by then, and its
replays from the same rows: the tournaments of every model served since the
database began dating its rows (CREATED_SINCE), but for the odd board still
running at the commit, which the page held half-played.

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
    python -m analysis.bench --cold --cutoff published --since 2026-09-06   the model the page served
    python -m analysis.bench --cold --update 16:00    the daily update at another hour (Paris time)
    python -m analysis.bench --cold --update 18:00,22:15   two updates a day
    python -m analysis.bench --cold --json out.json   every forecast, the tables and the biases
    python -m analysis.bench --cold --cache DIR       where the models are kept
    python -m analysis.bench --cold --leaderboards DIR   the raw leaderboard pages the replays read
    python -m analysis.bench --cold --jobs 3          the cutoffs priced in three processes
    python -m analysis.bench --cold --variant my_idea.py   today's code and a variant of it, side by side
    python -m analysis.bench --compare a.json b.json   two runs written with --json, side by side
    python -m analysis.bench --live --weeks 3         the forecast during each cup (analysis/bench_live.py)

`--jobs` prices the models' cutoffs in that many processes, each with its own
read-only connection; the forecasts, the counts and the file written are the
same as one process gives, in the same order. A run starts no more processes
than it has cutoffs, nor than the system has processors, and says how many.
Each process holds its own models, about 0.5 to 1.4 GB depending on their
size: the memory a run takes grows with `--jobs`. `--variant` names a Python
file that replaces some of the app's functions (see `Variant`): the run prices
the same cups twice from the same database, catalogue and pages, as the code
is and with the file's replacements, writes the second run beside the first
(`--json out.json` also writes out.variant.json) and sets the two against each
other. The file is read as the run starts, and what runs is that text, in
every process, whatever becomes of the file meanwhile. It only runs once
today's run is over and written: a variant that fails, exits or is refused
leaves today's run written, no out.variant.json beside it, and ends with a
code that says so.

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
import ast
import contextlib
import functools
import glob
import gzip
import hashlib
import importlib
import inspect
import io
import json
import multiprocessing
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import types
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor
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
# The page's own repository, whose history holds every model.json it served.
LADDER = os.path.join(os.path.dirname(_ROOT), "threshold-ladder")
# The database dates each row (`created_at`, Paris time) from this moment (UTC)
# only: every row it held then was given that evening's date.
CREATED_SINCE = "2026-09-05 20:00:00"

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
# Windows waits on 63 handles at most, two of them the pool's own: the most
# processes `--jobs` runs there.
WINDOWS_JOBS = 61
# The app's modules a variant makes its replacements on: each time it is
# applied, they are put back after as they were before.
VARIANT_MODULES = ("export_model", "calibration", "predict", "rescore", "db", "calendar_snapshot")

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


def cutoff_of(start: str, by: str = "update", updates: tuple = UPDATES, served: list | None = None) -> str:
    """The model a cup starting at `start` ("YYYY-MM-DD HH:MM:SS", UTC) is
    priced from, named by its cutoff: the collection time in UTC ("YYYY-MM-DD
    16:00:00" for 18:00 in Paris in summer time) of the last update whose model
    was online when the cup started, `updates` being their hours in Paris; with
    `by="day"` the cup's day ("YYYY-MM-DD"), everything that started before
    it; with `by="published"` the time (UTC) the model the page served then was
    committed, `served` being those times, oldest first."""
    day = str(start or "")[:10]
    if by == "day":
        return day
    if by == "published":
        found = [at for at in served or () if at <= str(start)[:19]]
        if not found:
            raise ValueError(f"no model served before {start}")
        return found[-1]
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


def served(ladder: str) -> list[str]:
    """When each model.json the page served was committed to its repository,
    in UTC ("YYYY-MM-DD HH:MM:SS"), oldest first; none where there is no such
    history."""
    try:
        out = subprocess.run(["git", "-C", ladder, "log", "--format=%cI", "--", "model.json"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return sorted(datetime.fromisoformat(line.strip()).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                  for line in out.splitlines() if line.strip())


def paris_of(at: str) -> str:
    """A UTC time ("YYYY-MM-DD HH:MM:SS") as the clock in Paris read it, the
    clock the database writes `created_at` on."""
    when = datetime.fromisoformat(at)
    summer = last_sunday(when.year, 3) <= when.date() < last_sunday(when.year, 10)
    return (when + timedelta(hours=2 if summer else 1)).strftime("%Y-%m-%d %H:%M:%S")


def created_before(comp: dict, clock: str | None) -> bool:
    """Was the row in the database by `clock` (Paris time)? Always, for none."""
    return clock is None or str(comp.get("created_at") or "") < clock


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


def catalogue_sha1(folder: str) -> str:
    """One SHA-1 over what `load_catalogue` reads: each catalogue file by name
    and by its content once uncompressed, so two runs can tell whether they
    rebuilt their rows from the same catalogue."""
    h = hashlib.sha1()
    for path in sorted(glob.glob(os.path.join(folder, "*.json.gz"))):
        h.update(os.path.basename(path).encode() + b"\0")
        try:
            with gzip.open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
        except (OSError, EOFError):
            h.update(b"-")
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

    def prune(self, days: int = CACHE_DAYS, keep=()) -> int:
        """Drop the models of other keys built `days` days ago or more: each
        new database is a new key, and a new set of models. Reading a model
        leaves its file's date as it was, so the keys a run read besides its
        own (`keep`) stay too."""
        if not self.cache or not os.path.isdir(self.cache):
            return 0
        horizon, dropped, spared = time.time() - days * 86400, 0, {self.key, *keep}
        for path in glob.glob(os.path.join(self.cache, "model-*.json.gz")):
            if os.path.basename(path)[:-len(".json.gz")].rsplit("-", 1)[-1] in spared:
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


def has_edition_before(conn, kind: str, region: str, team_mode: str, game_mode: str, cutoff: str,
                       clock: str | None = None) -> bool:
    """`rescore.has_edition`, asked of the editions the model of `cutoff` holds
    (and, given a `clock`, the database held by then)."""
    held_by = "c.start_time < ?" if len(cutoff) == 10 else "COALESCE(c.end_time, c.start_time) <= ?"
    rows = conn.execute(
        "SELECT c.name, c.family, c.stage, c.source, c.round_no FROM competition c WHERE c.region = ? "
        f"AND c.team_mode = ? AND c.game_mode = ? AND {held_by} "
        + ("AND c.created_at < ? " if clock else "")
        + "AND EXISTS (SELECT 1 FROM final_result f WHERE f.competition_id = c.id)",
        (region, team_mode, game_mode, cutoff) + ((clock,) if clock else ())).fetchall()
    return any(db.category_of(dict(row)) == kind for row in rows)


def donors_over_by(pool, cutoff: str, clock: str | None = None):
    """`rescore.candidates` (passed as `pool`), keeping only the boards the
    model of `cutoff` holds (and, given a `clock`, the database held by then).
    The list asks for boards that started before a date, but on a day of the
    past the database also holds the final standings of a board still running
    at the update, which nobody had then."""
    def candidates(*args, **kwargs):
        return [c for c in pool(*args, **kwargs) if held(c, cutoff) and created_before(c, clock)]
    return candidates


def cold_cell(conn, row: dict, cutoff: str, clock: str | None = None) -> dict | None:
    """The replay the list would have hung on the row, beside the model of
    `cutoff`.

    `rescore.cold_table` is the list's own code. The two questions it asks of
    the database as it stands are asked here of what the model holds, since on
    a day of the past the database also holds what came after: has the cup a
    finished edition in its region, and which boards are over to replay.
    """
    asked, pool = rescore.has_edition, rescore.candidates
    rescore.has_edition = lambda conn_, kind, region, team, mode: has_edition_before(
        conn_, kind, region, team, mode, cutoff, clock)
    rescore.candidates = donors_over_by(pool, cutoff, clock)
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
          skipped: Counter, refused: Counter, replayed: Counter | None = None,
          clock: str | None = None) -> list[dict]:
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
    row["cold"] = cold_cell(conn, row, cutoff, clock)
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


class Variant:
    """A Python file that replaces some of the app's functions for a run, to
    measure a change to the forecast against the code as it is.

    The file defines `apply()`, which makes its replacements on the app's
    modules (`export_model.predict_from_model = ...`, the modules imported by
    their own names, as the app imports them) and returns a function that puts
    them back, or None when nothing needs putting back:

        import export_model

        def apply():
            plain = export_model.predict_from_model

            def lower(model, t):
                got = plain(model, t)
                if got and got.get("value"):
                    got = dict(got, value=got["value"] * 0.97)
                return got

            export_model.predict_from_model = lower
            return lambda: setattr(export_model, "predict_from_model", plain)

    Its run builds its own models, kept under a key that adds the SHA-1 of the
    file (line endings made plain) to today's: an edit to the file is a new set
    of models. A file whose replacements are only read once a model is built
    (the forecast from the model, the row, the replay) may say so with
    `CHANGES_MODEL = False`, a plain True or False set at the top of the file;
    its run then reads today's models, after the last model of the span, built
    again with the file applied, has been found the same as today's.

    The file is read once, and not run until its own run: `apply()` and
    `CHANGES_MODEL` are found in its text, so a replacement it makes as it is
    run (outside `apply()`) reaches its run only, never today's. What runs is
    the text read then, whose SHA-1 the run writes, in this process and in
    every other: an edit to the file during a run reaches the next run only.
    Each time it is applied, the text is run afresh and the app's modules
    (VARIANT_MODULES) are put back after as they were before, whatever
    `apply()` returned.
    """

    def __init__(self, path: str, source: bytes | None = None):
        self.path = os.path.abspath(path)
        if source is None:
            with open(self.path, "rb") as fh:
                source = fh.read()
        self.source = source
        self.sha1 = hashlib.sha1(source.replace(b"\r\n", b"\n")).hexdigest()
        tree = ast.parse(source, self.path)
        if not any(isinstance(node, ast.FunctionDef) and node.name == "apply" for node in tree.body):
            raise ValueError(f"{self.path} defines no apply()")
        self.changes_model = True
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            else:
                continue
            if any(isinstance(target, ast.Name) and target.id == "CHANGES_MODEL" for target in targets):
                try:
                    self.changes_model = bool(ast.literal_eval(node.value))
                except (ValueError, TypeError):
                    raise ValueError(f"{self.path}: CHANGES_MODEL is not a plain True or False") from None

    def load(self) -> types.ModuleType:
        """The text read, run as a module of its own: for its run only."""
        module = types.ModuleType(f"bench_variant_{self.sha1[:12]}")
        module.__file__ = self.path
        exec(compile(self.source, self.path, "exec"), module.__dict__)
        if not callable(getattr(module, "apply", None)):
            raise ValueError(f"{self.path} defines no apply()")
        return module

    def key(self, key: str) -> str:
        """The key its models are kept under, today's being `key`."""
        if not self.changes_model:
            return key
        return hashlib.sha1(f"{key}|variant|{self.sha1}".encode()).hexdigest()[:16]

    @contextlib.contextmanager
    def applied(self):
        kept = [(module, dict(vars(module))) for module in map(importlib.import_module, VARIANT_MODULES)]
        try:
            undo = self.load().apply()
            try:
                yield self
            finally:
                if callable(undo):
                    undo()
        finally:
            for module, names in kept:
                found = vars(module)
                for name in set(found) - set(names):
                    del found[name]
                found.update(names)


class Share:
    """What one process prices its cutoffs with: a connection, the tournaments
    the app loads, their models, and the boards the replays read through it."""

    def __init__(self, conn, comps: list[dict], catalogue: dict, cache: str | None, key: str):
        self.conn, self.catalogue, self.cache, self.key = conn, catalogue, cache, key
        self.loaded, self.comps = comps, {comp["id"]: comp for comp in comps}
        self.models = Models(conn, comps, key, cache)
        self.starts = season_starts(conn)
        # A board replayed for one cup is often a donor for the next: read
        # once, and noted with the rosters it gave, so two runs can tell
        # whether they replayed the same boards.
        self.reader, self.boards = rescore.rosters_of, {}
        self.kept = functools.lru_cache(maxsize=256)(self.read_and_note)

    def read_and_note(self, event_id, window_id):
        rosters = self.reader(event_id, window_id)
        content = repr([(r.get("rank"), r.get("points"), r.get("games")) for r in rosters])
        self.boards[(str(event_id), str(window_id))] = (len(rosters), hashlib.sha1(content.encode()).hexdigest()[:8])
        return rosters

    def price_cutoff(self, cutoff: str, ids: list, clock: str | None = None) -> dict:
        """The cups of one cutoff (their ids, in the order they started),
        priced from its model: their forecasts and what the run counts. Given
        a `clock` (Paris time), the model and the replays read only the rows
        the database held by then."""
        skipped, refused, replayed = Counter(), Counter(), Counter()
        models = self.models if clock is None else Models(
            self.conn, [c for c in self.loaded if created_before(c, clock)], self.key, self.cache)
        noted, built, read_before = set(self.boards), models.built, models.read

        def read(event_id, window_id):
            replayed["reads"] += 1
            return self.kept(event_id, window_id)

        rescore.rosters_of = read
        try:
            model = models.get(cutoff)
            pairs = []
            for comp_id in ids:
                pairs += price(self.conn, model, self.comps[comp_id], self.catalogue, cutoff, self.starts,
                               skipped, refused, replayed, clock)
        finally:
            rescore.rosters_of = self.reader
        quality = model.get("quality") or {}
        return {"cutoff": cutoff, "pairs": pairs, "skipped": skipped, "refused": refused,
                "size": {"tournaments": model["source"]["tournaments"], "cups": len(ids)},
                "bands": {"generated": quality.get("generated"), "from": quality.get("from")},
                "replayed": {"cups": replayed["cups"], "short": replayed["short"]},
                "boards": {board: found for board, found in self.boards.items() if board not in noted},
                "built": models.built - built, "read": models.read - read_before, "key": models.key}


# The share of a worker process of `cold`, set up once by `_start`.
_SHARE: Share | None = None


def _start(path: str, live: bool, catalogue: dict, boards: str, cache: str | None, key: str,
           variant: tuple | None) -> None:
    """A worker of `cold`: its own read-only connection, the raw pages the
    parent reads, and the variant, if any, applied by itself for good: the
    text the parent read (its path and bytes), not the file as it is now."""
    global _SHARE
    rescore.RAW = boards
    if variant:
        Variant(*variant).load().apply()
    conn = open_read_only(path, live)
    conn.row_factory = sqlite3.Row
    with contextlib.redirect_stdout(io.StringIO()):
        comps = export_model.load_competitions(conn)
    _SHARE = Share(conn, comps, catalogue, cache, key)


def _price(task: tuple) -> dict:
    return _SHARE.price_cutoff(*task)


def cold(conn, since: str, until: str, catalogue: dict, cache: str | None, key: str,
         by: str = "update", progress: bool = True, updates: tuple = UPDATES,
         jobs: int = 1, database: tuple | None = None, variant: Variant | None = None,
         served: list | None = None, prune: bool = True) -> dict:
    """Every cup that started in [since, until) and finished, priced from the
    model before it (see `cutoff_of`). {"pairs", "skipped", "refused",
    "models", "cups", "built", "read", "model_key", "replays", "bands",
    "seconds"}.

    With `jobs` above one the cutoffs are priced in that many processes, each
    opening `database` ((path, live), as `open_read_only` takes them) for
    itself and reading the raw pages `rescore.RAW` names; the result is the
    one a single process gives, in the same order. With a `variant`, the run is
    the variant's: applied for the whole run, in each process, and its models
    kept under its own key unless it says it leaves them alone. With
    `by="published"`, each cup reads the model the page served when it started
    (`served`, the commit times of its model.json), rebuilt from the rows the
    database held then, over by then: a cup the page priced from a model built
    before the database dated its rows (CREATED_SINCE) is left out, and counted.
    With `prune`, the models of other keys CACHE_DAYS old leave the cache after
    the run, but for the keys it read or built (a published run reads each
    model under a key of its own); a run beside another of another key (a
    variant's) keeps them.
    """
    began = time.time()
    if variant is not None:
        key = variant.key(key)
    with variant.applied() if variant is not None else contextlib.nullcontext():
        comps = export_model.load_competitions(conn)
        targets: dict = {}
        early = Counter()
        for comp in comps:
            day = str(comp.get("start_time") or "")[:10]
            if since <= day < until:
                try:
                    cutoff = cutoff_of(str(comp.get("start_time") or ""), by, updates, served)
                except ValueError:
                    early["cups started before the first model the page served"] += 1
                    continue
                if by == "published" and cutoff < CREATED_SINCE:
                    early["cups the page priced from a model built before the database dated its rows"] += 1
                    continue
                targets.setdefault(cutoff, []).append(comp)
        tasks = [(cutoff, [c["id"] for c in sorted(targets[cutoff], key=lambda c: (str(c.get("start_time")),
                                                                                   c["id"]))],
                  paris_of(cutoff) if by == "published" else None)
                 for cutoff in sorted(targets)]
        models = Models(conn, comps, key, cache)
        pairs, skipped, refused, sizes, replayed, bands, boards = [], Counter(early), Counter(), {}, Counter(), None, {}
        built = read = 0
        used = {models.key}
        with contextlib.ExitStack() as stack:
            if jobs > 1 and len(tasks) > 1:
                if not database:
                    raise ValueError("several jobs need the database's path, to open it in each")
                workers = min(jobs_allowed(jobs)[0], len(tasks))
                if progress:
                    print(f"  {workers} processes for {len(tasks)} cutoffs, each with its own read-only connection",
                          flush=True)
                # Spawned, not forked: a worker starts from the code as it is on
                # disk and applies the variant itself, never twice.
                pool = stack.enter_context(ProcessPoolExecutor(
                    max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                    initializer=_start, initargs=(database[0], database[1], catalogue, rescore.RAW, cache, key,
                                                  (variant.path, variant.source) if variant is not None else None)))
                done = pool.map(_price, tasks)
            else:
                share = Share(conn, comps, catalogue, cache, key)
                done = (share.price_cutoff(*task) for task in tasks)
            for n, part in enumerate(done, 1):
                cutoff = part["cutoff"]
                pairs += part["pairs"]
                skipped.update(part["skipped"])
                refused.update(part["refused"])
                sizes[cutoff] = part["size"]
                bands = bands or part["bands"]
                replayed.update(part["replayed"])
                boards.update(part["boards"])
                built, read = built + part["built"], read + part["read"]
                used.add(part["key"])
                if progress:
                    print(f"  {cutoff[:16]:<16}  model of {part['size']['tournaments']:>5} tournaments, "
                          f"{part['size']['cups']:>3} cups, {len(part['pairs']):>4} forecasts"
                          f"   ({n}/{len(tasks)}, {time.time() - began:.0f} s)", flush=True)
    if prune:
        models.prune(keep=used)
    listed = sorted(f"{event}|{window}|{n}|{sha}" for (event, window), (n, sha) in boards.items())
    return {"pairs": pairs, "skipped": skipped, "refused": refused, "models": sizes,
            "cups": sum(len(v) for v in targets.values()) + sum(early.values()), "built": built, "read": read,
            "model_key": models.key,
            "replays": {"cups": replayed["cups"], "cups_short_of_boards": replayed["short"],
                        "boards_asked": len(listed), "boards_with_pages": sum(1 for n, _ in boards.values() if n),
                        "digest": hashlib.sha1("\n".join(listed).encode()).hexdigest()[:16],
                        "boards": listed},
            "bands": bands, "seconds": time.time() - began}


def same_model(conn, cutoff: str, cache: str | None, key: str, variant: Variant) -> bool:
    """Does the variant leave the model of `cutoff` as today's code builds it?
    Asked of a variant that says so before its run reads today's models: the
    model is built again with the variant applied, and set against today's."""
    with contextlib.redirect_stdout(io.StringIO()):
        plain = Models(conn, export_model.load_competitions(conn), key, cache).get(cutoff)
        with variant.applied():
            changed = Models(conn, export_model.load_competitions(conn), key, None).get(cutoff)
    return plain == changed


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cold", action="store_true",
                        help="the forecast made before each cup (the default)")
    parser.add_argument("--live", action="store_true",
                        help="the forecast the page shows during each cup the live feed followed "
                             "(analysis/bench_live.py)")
    parser.add_argument("--arrival", default="feed", metavar="feed|stamp|MINUTES",
                        help="with --live, when a reading reaches the page: after the feed's pass that could "
                             "take it (default), at its stamp, or this many minutes after its stamp")
    parser.add_argument("--weeks", type=int, default=0, help="the span, in weeks back from --until")
    parser.add_argument("--days", type=int, default=0, help=f"the span, in days (default {SHORT_DAYS})")
    parser.add_argument("--since", default="", help="the first day (YYYY-MM-DD)")
    parser.add_argument("--until", default="", help="the day after the last (default: today, UTC)")
    parser.add_argument("--cutoff", choices=("update", "day", "published"), default="update",
                        help="the model a cup reads: the last daily update's (default), the "
                             "tournaments that started before its day, or the model the page served then "
                             "(from the history of --ladder, since %s UTC)" % CREATED_SINCE[:16])
    parser.add_argument("--ladder", default=LADDER,
                        help="the page's repository, whose history dates each model.json it served "
                             "(for --cutoff published)")
    parser.add_argument("--update", type=clocks, default=UPDATES, metavar="HH:MM[,HH:MM]",
                        help=f"the daily updates, Paris time (default {','.join(UPDATES)})")
    parser.add_argument("--db", default="", help="the database (default: the app's)")
    parser.add_argument("--catalogue", default=CATALOGUE, help="the harvest's catalogue folder")
    parser.add_argument("--leaderboards", default="",
                        help="the raw leaderboard pages the replayed tables read "
                             "(default: data/osirion/leaderboards, where the harvest keeps them)")
    parser.add_argument("--cache", default="", help="where the models are kept ('none': nowhere)")
    parser.add_argument("--json", default="", help="write every forecast, the tables and the biases here")
    parser.add_argument("--jobs", type=jobs_count, default=1, metavar="N",
                        help="price the cutoffs in N processes, each with its own connection and models, "
                             "about 0.5 to 1.4 GB each (default 1; no more than the cutoffs or the processors)")
    parser.add_argument("--variant", default="", metavar="FILE.py",
                        help="also price every cup with the replacements FILE.py's apply() makes, and set the "
                             "two runs against each other (with --json X.json, the variant's goes to "
                             "X.variant.json)")
    parser.add_argument("--compare", action="store_true",
                        help="BASE.json VARIANT.json: set two runs written with --json side by side, forecast "
                             "against forecast, and measure nothing; the rest of the line goes to "
                             "analysis/bench_compare.py (its --help)")
    return parser


def jobs_allowed(jobs: int) -> tuple[int, str]:
    """How many of `jobs` processes a run starts at most, and what holds it to
    fewer ("" when nothing does): the processors the system has, and on
    Windows the most its pool runs (WINDOWS_JOBS)."""
    caps = [(os.cpu_count() or 1, "the processors this system has")]
    if sys.platform == "win32":
        caps.append((WINDOWS_JOBS, "the most this system runs in one pool"))
    cap, why = min(caps)
    return (cap, why) if cap < jobs else (jobs, "")


def jobs_count(text: str) -> int:
    try:
        jobs = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if jobs < 1:
        raise argparse.ArgumentTypeError(f"one job at least: {text!r}")
    return jobs


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
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--compare" in argv:
        from analysis import bench_compare
        return bench_compare.main([arg for arg in argv if arg != "--compare"])
    args = argument_parser().parse_args(argv)
    if args.live:
        # The live bench has its own loop: what only the cold one does is
        # refused, not dropped, so that no run passes for what it is not.
        cold_only = [name for name, asked in (("--cutoff published", args.cutoff == "published"),
                                              ("--jobs", args.jobs > 1), ("--variant", bool(args.variant)))
                     if asked]
        if cold_only:
            print(f"{', '.join(cold_only)}: the cold bench's, not the live bench's (--live). Nothing measured.")
            return 2
        from analysis import bench_live
        return bench_live.run(args)
    if args.arrival != "feed":
        print("--arrival is the live bench's (--live), not the cold bench's. Nothing measured.")
        return 2
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
             f"{PUBLISH_MINUTES} min later)" if args.cutoff == "update" else "the tournaments started before its day"
             if args.cutoff == "day" else "the model.json the page served when it started, rebuilt from the rows "
             "the database held then")
    commits = served(args.ladder) if args.cutoff == "published" else None
    if args.cutoff == "published" and not commits:
        print(f"No history of model.json in {args.ladder}: nothing measured (--ladder).")
        return 2
    print(f"Cold bench, cups from {since} to {until} (not included), each priced from the model of {which}")
    print(f"  database {path}; catalogue: {len(catalogue):,} windows"
          + ("" if catalogue else " - every cup is priced off its database row"))
    print(f"  raw leaderboard pages: {events:,} events in {boards}")
    if not events:
        print("  WARNING: no raw leaderboard pages there. The replayed tables are off: the cups new in their region\n"
              "  fall back on other rungs, and this run does not compare with one that had the pages (--leaderboards).")
    variant = None
    if args.variant:
        try:
            variant = Variant(args.variant)
        except (OSError, ValueError, SyntaxError) as exc:
            print(f"No variant from {args.variant}: {exc}")
            return 2
    jobs, why = jobs_allowed(args.jobs)
    if why:
        print(f"  --jobs {args.jobs}: {jobs} processes at most, {why}")
    key = model_key(path)
    live = same_file(path, str(db.DB_PATH))
    conn = open_read_only(path, live=live)
    conn.row_factory = sqlite3.Row
    kept_boards, rescore.RAW = rescore.RAW, boards
    heading = f"cold: the forecast before each cup ({since} to {until}, cutoff: {args.cutoff})"
    read = dict(since=since, until=until, path=path, catalogue=catalogue, events=events, commits=commits)
    payloads = []
    try:
        # Today's run is written as soon as it is over: the variant's, which
        # runs code from elsewhere, may fail, exit or be refused without losing it.
        run = cold(conn, since, until, catalogue, cache, key, args.cutoff, updates=args.update,
                   jobs=jobs, database=(path, live), served=commits, prune=variant is None)
        tables = shown(run, "", heading, cache)
        if args.json or variant is not None:
            payloads.append(run_json(args, run, tables, **read))
            if args.json:
                write_json(payloads[-1], args.json)
        if variant is not None:
            alone = f"today's run alone is written ({args.json})" if args.json else "today's run alone is shown"
            # A variant's file of an earlier run is not left beside this one.
            if args.json and os.path.exists(variant_json(args.json)):
                os.remove(variant_json(args.json))
            try:
                last = max(run["models"], default="")
                if last and not variant.changes_model and not same_model(conn, last, cache, key, variant):
                    print(f"\n{variant.path} says it leaves the models alone (CHANGES_MODEL = False), but the model "
                          f"of {last} is not the same with it: no run of the variant, {alone}.")
                    return 2
                print(f"\nThe same cups with the variant {variant.path} (SHA-1 {variant.sha1[:12]}), "
                      + ("its own models" if variant.changes_model else "today's models"))
                run = cold(conn, since, until, catalogue, cache, key, args.cutoff, updates=args.update,
                           jobs=jobs, database=(path, live), variant=variant, served=commits, prune=False)
                tables = shown(run, "variant: ", heading, cache)
                payloads.append(run_json(args, run, tables, variant=variant, **read))
            except (Exception, SystemExit) as exc:
                traceback.print_exc()
                print(f"\nThe variant's run failed ({type(exc).__name__}: {exc}): {alone}.")
                return 1
            if args.json:
                write_json(payloads[-1], variant_json(args.json))
    finally:
        rescore.RAW = kept_boards
        conn.close()
    if len(payloads) == 2:
        try:
            from analysis import bench_compare
        except ImportError:
            print("\nanalysis/bench_compare.py is not here: the two runs are written, not set against each other.")
        else:
            # One launch reads one database, catalogue and folder of pages, but a
            # variant may read fewer of the boards: the comparison then says so
            # rather than set the two runs side by side unasked.
            try:
                result = bench_compare.compare(payloads[0], payloads[1])
            except ValueError as exc:
                print(f"\nThe two runs are not set against each other: {exc}")
                # Only what one run lacks may go unchecked; a value that differs never.
                lacking = sorted(set(re.findall(r"--unchecked (\w+)", str(exc))))
                if args.json and lacking:
                    print(f'  python -m analysis.bench --compare "{args.json}" "{variant_json(args.json)}" '
                          f"--unchecked {','.join(lacking)} compares them all the same")
                return 2
            bench_compare.print_report(result)
    return 0


def shown(run: dict, mark: str, heading: str, cache: str | None) -> tuple[dict, list[dict]]:
    """Print what a run counted and its tables, `mark` before the variant's;
    the tables and the biases, as `report` gives them."""
    pairs = run["pairs"]
    print(f"\n{mark}{run['cups']} cups, {len(pairs)} forecasts, {run['built']} models built and "
          f"{run['read']} read from {cache or 'nowhere'}, in {run['seconds']:.0f} s")
    replays = run["replays"]
    print(f"Replayed tables on {replays['cups']} cups; {replays['cups_short_of_boards']} more asked for one and "
          f"found fewer than two usable boards. Boards: {replays['boards_with_pages']} with pages of "
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
    return report(pairs, mark + heading)


def run_json(args, run: dict, tables: tuple, since: str, until: str, path: str, catalogue: dict, events: int,
             commits: list | None, variant: Variant | None = None) -> dict:
    """What `--json` writes of a run, today's or a variant's: the same fields
    for both, the database, catalogue and pages it read among them, so that
    the two can be set against each other."""
    found, leaning = tables
    pairs = run["pairs"]
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "bench": "cold", "since": since, "until": until, "cutoff": args.cutoff,
        "update": {"paris": list(args.update), "online_after_minutes": PUBLISH_MINUTES}
        if args.cutoff == "update" else None,
        "database": {"sha1": file_sha1(path), "bytes": os.path.getsize(path),
                     **({"wal_sha1": file_sha1(path + "-wal"), "wal_bytes": os.path.getsize(path + "-wal")}
                        if os.path.exists(path + "-wal") and os.path.getsize(path + "-wal") else {})},
        "catalogue": {"windows": len(catalogue),
                      "files": len(glob.glob(os.path.join(args.catalogue, "*.json.gz"))),
                      "sha1": catalogue_sha1(args.catalogue)},
        "leaderboards": {"events": events}, "replays": run["replays"], "bands": run["bands"],
        "model_key": run["model_key"], "ranks": list(RANKS), "cups": run["cups"],
        "models": run["models"], "skipped": dict(run["skipped"]), "refused": dict(run["refused"]),
        "all": coldbench.summary(pairs) if pairs else None,
        "at_cut": coldbench.summary([p for p in pairs if p["at_cut"]]) if pairs else None,
        "tables": found, "biases": leaning, "pairs": pairs,
    }
    if commits:
        payload["published"] = {"ladder": os.path.abspath(args.ladder), "commits": len(commits),
                                "since": CREATED_SINCE, "served": sorted(run["models"])}
    if variant is not None:
        payload["variant"] = {"path": variant.path, "sha1": variant.sha1}
    return payload


def write_json(payload: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(f"\nWrote {path}")


def variant_json(path: str) -> str:
    """Where the variant's run is written beside `path`: out.json -> out.variant.json."""
    root, ext = os.path.splitext(path)
    return f"{root}.variant{ext}"


if __name__ == "__main__":
    raise SystemExit(main())
