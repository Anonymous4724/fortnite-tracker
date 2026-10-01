"""How far off is the forecast the page shows while a cup is running?

`bench --cold` measures the forecast made before each cup. This measures the
one the page draws once the live feed hands it the board: every cup the feed
followed - the `snapshot` and `points` rows `pull_live` filed, "live feed" in
their note - is replayed reading by reading, in the order the readings were
taken, and at fixed points of its session (a fifth of it, two fifths, and so
on to the close, then ten and twenty minutes past it) the forecast the page
would have shown with the readings taken by then is set against the cup's
final standings.

What the page had at a point of the session is what this hands it, and no
more:

- the readings that had reached it by then (`--arrival`, below), and no
  reading the feed called final before the window had closed: the feed only
  calls a window final once its clock has run out, so such a reading, even of
  a board stamped a minute before the buzzer, reached the page after it;
- the field the page reads a rank's depth against: the one the list carried,
  else the board's own count from the readings taken so far, else the one the
  model guesses - never the field the tournament's row was given later, which
  holds the feed's last count;
- the model of the last daily update online before the cup (`bench.cutoff_of`,
  the same models and the same cache as the cold bench), and the cold
  forecast the page starts from: the list's row rebuilt from the catalogue,
  the replay of earlier boards for a cup new to its region, and
  `export_model.predict_from_model`, exactly as `bench --cold` prices it.

When a reading reached the page (`--arrival`):

- `feed`, the default: the page's own account of the feed (app.html,
  `drawStatus`; worker.js, QUICK_BEFORE and `endgame`). The cron fires every
  five minutes; the full pass, which reads the pages the cuts fall on as well
  as the first, runs on the ten-minute marks; the quick pass in between reads
  the first page only (the top hundred), and only of the windows in their
  endgame, from ENDGAME_BEFORE minutes before the close on. A reading lands
  LANDS_AFTER minutes after its pass. A reading's stamp is the API's
  `updatedAt` of its page, never later than the pass that took it, so the
  reading is taken to land after the first pass at or after its stamp that
  could have read it: a five-minute mark if the window was in its endgame by
  then and the reading holds first-page ranks only, else a ten-minute mark.
  This is a model, not a record: the database keeps the stamp, not the pass,
  and a stamp is cut to the minute, so a board stamped a few seconds after a
  mark is taken to land with that mark's pass. A pass that failed or ran late
  is not seen either.
- `stamp`: at its stamp, sooner than any pass could have read it: the most
  the page could have had.
- a number of minutes: that long after its stamp.

A reading stamped more than EARLY_MINUTES before the window opened is left
out and counted (`early`): the feed only watches a window from a few minutes
before it opens, so such a reading belongs to another window filed under the
same tournament.

The feed's readings come back the way the page's own replay of an evening
builds them, and the loads are rebuilt rather than read: the feed's history
is not in the database. `pull_live` filed one row per stamp, splitting a pass
whose pages were stamped at different minutes, so a rebuilt load is one per
stamp, not one per pass, and it carries the pass's page count, count and
final mark on each of its parts. Each carries, for every rank read so far,
the latest number with the minute it was read, and the page folds them into its log
(`entryFrom`, `foldInto`), clocks each on its own stamp (`clocked`), keeps the
standing of each rank (`standing`), weighs the readings against the history
(`refine`, `forecastAt`, `readFloor`) and draws the ranges (`bandsOf`, with the
live multipliers until the close and the cold ones after it). Those steps are
ported here line for line. The share of its final a rank's board is expected
to have reached at a minute - the pace curve of its kind of cup, the depth of
the rank into the field, the tail past the close - is the one place the
analysis already mirrors the page, in `live.expected_share` and
`live.depth_ratio`, and the number measured is theirs. Where they part ways
with the page the forecast is flagged, with the page's own number beside it:
`ceiling` where `live.py` reads a rank's depth no deeper than DEPTH_CEILING_Q
on any field of 9,950 or more, where the page only does so while the count is
the API's ceiling; `kind` and `fncs` where the page reads the category label
as well as the name; `minutes` where `live.py` rounds the window's length to
five minutes before matching it; `other` for anything else. A closed lobby is
clocked on its games, which `live.py` does not mirror: the page's own rules,
ported, price it.

Ranks priced: every rank the feed read, 10, 25, 100, 250, 500 and 1,000 and the
cut the page asks first, each where the final standings hold it. The ranks the
feed read are gathered over all of a cup's readings, later ones included, and
priced at every point: which ranks are priced is chosen after the fact, but no
number read after a point reaches its forecast. The error is 100 (forecast /
result - 1), as on the cold bench; the ranges are counted as holding the
result when it falls inside them as they are written to the JSON (to a
thousandth of a point), ends included. The finals under SMALL_RESULT points
are counted apart beside every table, with the mean error without them: a
point or two off a final of six weighs on a mean out of all proportion.

    python -m analysis.bench --live --since 2026-09-05 --until 2026-09-30
    python -m analysis.bench --live --arrival stamp   each reading known at its stamp
    python -m analysis.bench --live --json out.json   every forecast and the tables

The other options are the cold bench's. Readings from the feed exist from
5 September 2026 on; a span that starts earlier measures the cups since then.
The pace tables, the live multipliers and the blend the page reads come with
the model as `analysis/pace.json` and `analysis/blend.json` hold them today,
measured on the feed's evenings: in part on the same cups.
"""
from __future__ import annotations

import functools
import glob
import hashlib
import json
import math
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone

from analysis import bench  # noqa: E402  (puts the app's modules on the path)

import calibration  # noqa: E402
import export_model  # noqa: E402
import harvest_osirion  # noqa: E402
import rescore  # noqa: E402
from analysis import coldbench, live  # noqa: E402

# The points of a session the forecast is measured at: a share of the window,
# or minutes past its close.
POINTS = (("20 %", 0.2, None), ("40 %", 0.4, None), ("60 %", 0.6, None), ("80 %", 0.8, None),
          ("100 %", 1.0, None), ("+10 min", None, 10), ("+20 min", None, 20))
FEED_NOTE = "live feed%"

# The page's own numbers, as app.html sets them.
PACE_MIN_SHARE = 0.2          # below this share the evening gives no live answer
CARRY_MIN = 0.05              # a smaller carry leaves an unread rank at its cold forecast
INTERP_REL = 0.04             # width per unit of log-gap between two readings
RECENT_MINUTES = 25           # an older reading of a rank read since is history
READ_MIN_SHARE = 0.1          # a rank's own reading counts from here
SETTLED_AFTER = 20            # minutes past the close by which the games have landed
HELD_MINUTES = 10             # a standing read again this long after is still
PAGES_CAP = 100               # the API's deepest page: a count there is a bound
PAGE_FAMILY_MINUTES = 10      # a window's length is matched to this, unrounded
PACE_DEFAULT = {"curve": {"open_by_time": {}, "closed_by_game": {}},
                "dispersion": {"open_by_time": {}, "closed_by_game": {}},
                "carry": {"open_by_time": {}, "closed_by_game": {}}}

# When a reading reaches the page (`--arrival feed`): the worker's cron and
# the page's own account of it (worker.js, QUICK_BEFORE; app.html,
# ENDGAME_BEFORE and drawStatus).
FULL_PASS_MINUTES = 10        # the full pass, first page and the cuts' pages
QUICK_PASS_MINUTES = 5        # the quick pass in between, first page only
ENDGAME_BEFORE = 20           # the quick pass reads a window from this long before its close
LANDS_AFTER = 2               # a reading lands this long after its pass
FIRST_PAGE = 100              # the ranks the first page of a board holds
ARRIVALS = ("feed", "stamp")
ARRIVAL_RULES = {
    "feed": f"{LANDS_AFTER} min after the first pass of the feed at or after its stamp that could read it "
            f"(every {QUICK_PASS_MINUTES} min for the first page from {ENDGAME_BEFORE} min before the close, "
            f"else every {FULL_PASS_MINUTES} min)",
    "stamp": "at its stamp",
    "minutes": "%s min after its stamp",
}
EARLY_MINUTES = 10            # a reading stamped this long before the opening is another window's
SMALL_RESULT = 10             # a final under this many points is counted apart

# Depth into the field, for the tables: below the first band the top of the
# board is its own reference; the last band is the casual half.
DEPTH_LABELS = ((0.0, 0.02), (0.02, 0.05), (0.05, 0.1), (0.1, 0.2), (0.2, 0.5), (0.5, 10.0))
TOP_FAMILIES = 25

FLAGS = OrderedDict([
    ("ceiling", "live.py reads a rank no deeper than q = 0.49 on any field of 9,950 or more; "
                "the page only while the field is the board's count at the API's ceiling"),
    ("kind", "live.py picks the kind of cup off the name alone; the page off the name and the category"),
    ("fncs", "live.py tells an FNCS qualifier off the name alone; the page off the name and the category"),
    ("minutes", "live.py rounds the window's length to 5 minutes before matching a pace row; "
                "the page matches the length itself"),
    ("other", "live.py and the page part ways for another reason"),
])


def js_round(x: float) -> int:
    return export_model.js_round(x)


def moment(text) -> datetime | None:
    """A database or list time ("2026-09-29 17:00:00", "2026-09-29T17:00Z") as a
    naive UTC datetime."""
    text = str(text or "").strip().replace("T", " ").replace("Z", "")
    if not text:
        return None
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        return None
    return found.replace(tzinfo=None) if found.tzinfo is None else \
        found.astimezone(timezone.utc).replace(tzinfo=None)


def minutes_from(begin: datetime, at: datetime) -> float:
    return (at - begin).total_seconds() / 60


# --------------------------------------------------------------------------- #
# The feed's readings
# --------------------------------------------------------------------------- #
def arrival_of(text) -> str | int:
    """`--arrival`: "feed", "stamp" or a whole number of minutes, else ValueError."""
    text = str(text if text is not None else "feed").strip().lower()
    if text in ARRIVALS:
        return text
    minutes = int(text)
    if minutes < 0:
        raise ValueError(f"a reading cannot land before its stamp: {text}")
    return minutes


def arrival_label(arrival) -> str:
    return arrival if isinstance(arrival, str) else f"stamp + {arrival} min"


def landed(begin: datetime, end: datetime | None, minute: float, ranks, arrival="feed") -> float:
    """When a reading stamped `minute` into the window reached the page, in
    minutes from the opening (see the module's notes on `--arrival`)."""
    if arrival == "stamp":
        return minute
    if arrival != "feed":
        return minute + float(arrival)
    # The cron's marks are on the clock, not on the window: whole seconds
    # since 1970.
    epoch = datetime(1970, 1, 1)
    opens = round((begin - epoch).total_seconds())
    stamp = opens + round(minute * 60)
    quick, full = (-(-stamp // (60 * every)) * 60 * every for every in (QUICK_PASS_MINUTES, FULL_PASS_MINUTES))
    endgame = (end - epoch).total_seconds() - ENDGAME_BEFORE * 60 if end else math.inf
    taken = quick if quick >= endgame and all(int(r) <= FIRST_PAGE for r in ranks) else full
    return (taken - opens) / 60 + LANDS_AFTER


def snapshots_of(conn, comp_id: int, begin: datetime, end: datetime | None = None,
                 arrival="stamp") -> list[dict]:
    """The feed's readings of a cup in the order they were filed: [{minute,
    known, final, games, pages, ranked, points}], `minute` counted from the
    window's opening and `known` the minute it reached the page (`landed`)."""
    out = []
    rows = conn.execute("SELECT id, ts, note, games, pages, ranked FROM snapshot WHERE competition_id = ? "
                        "AND note LIKE ? ORDER BY ts, id", (comp_id, FEED_NOTE)).fetchall()
    for sid, ts, note, games, pages, ranked in rows:
        at = moment(ts)
        if at is None:
            continue
        points = {int(r): float(p) for r, p in conn.execute(
            "SELECT rank, points FROM points WHERE snapshot_id = ? ORDER BY rank", (sid,))}
        minute = minutes_from(begin, at)
        known = landed(begin, end, minute, points, arrival)
        out.append({"id": sid, "minute": minute, "known": known, "final": "final" in str(note or ""),
                    "games": int(games or 0), "pages": int(pages or 0), "ranked": int(ranked or 0),
                    "points": points})
    return out


def available(snapshots: list[dict], at: float, close: float, strict: bool = True) -> list[dict]:
    """The readings the page could hold `at` minutes into the window: landed
    by then, and - with `strict` - none the feed called final before the
    window closed, since it only says so once the clock has run out."""
    return [s for s in snapshots if s.get("known", s["minute"]) <= at + 1e-9
            and not (strict and s["final"] and at <= close + 1e-9)]


def records_of(snapshots: list[dict]) -> list[dict]:
    """The readings as the feed handed them to the page: each carries, for
    every rank read so far, the latest number with the minute it was read
    (None for this reading's own minute). Two readings of one minute are one."""
    latest, out = {}, []
    for snap in snapshots:
        minute = snap["minute"]
        for rank, points in snap["points"].items():
            latest[rank] = (points, minute)
        reads = [(rank, points, None if m == minute else m) for rank, (points, m) in sorted(latest.items())]
        record = {"minute": minute, "pages": snap["pages"], "ranked": snap["ranked"], "final": snap["final"],
                  "games": snap["games"], "reads": reads}
        if out and out[-1]["minute"] == minute:
            out[-1] = record
        else:
            out.append(record)
    return out


def ranked_from(record: dict) -> str:
    """Where the feed's count of the board came from, as the page reads it. The
    feed's history keeps a count past the API's ceiling only once Epic's
    percentiles have pinned it, so a count beside a hundred pages is one."""
    if not record.get("ranked"):
        return ""
    return "percentile" if record.get("pages", 0) >= PAGES_CAP else "last page"


# --------------------------------------------------------------------------- #
# The page's tables, read the page's way
# --------------------------------------------------------------------------- #
def pace_at(table: dict, share: float):
    """paceAt: linear between the steps, linear in the share below the first."""
    steps = sorted((float(k), float(v)) for k, v in (table or {}).items())
    if not steps:
        return None
    if share <= steps[0][0]:
        return steps[0][1] * share / steps[0][0]
    for (a, va), (b, vb) in zip(steps, steps[1:]):
        if share <= b:
            f = (share - a) / (b - a)
            return va * (1 - f) + vb * f
    return steps[-1][1]


def tail_at(table: dict, minutes: float):
    """tailAt: linear between the minutes measured, flat outside them."""
    keys = sorted((float(k), k) for k in (table or {}))
    if not keys:
        return None
    if minutes <= keys[0][0]:
        return float(table[keys[0][1]])
    for (a, ka), (b, kb) in zip(keys, keys[1:]):
        if minutes <= b:
            f = (minutes - a) / (b - a)
            return float(table[ka]) * (1 - f) + float(table[kb]) * f
    return float(table[keys[-1][1]])


def band_table(rows: list, rank) -> dict | None:
    """bandTable: the table of the band of ranks a rank falls in."""
    for row in rows or []:
        low, high = float(row[0]), float(row[1])
        if rank >= low and (not high or rank <= high):
            return row[2]
    return None


def depth_at(pace: dict, q: float, share: float) -> float:
    """depthAt: the depth table's ratio at a share, ramped in over the tenth
    before DEPTH_FROM."""
    row = next((r for r in (pace or {}).get("depth") or [] if float(r[0]) <= q < float(r[1])), None)
    if not row or share < live.DEPTH_FROM - 0.1:
        return 1.0
    table = row[2] or {}
    keys = sorted(float(k) for k in table)
    if not keys:
        return 1.0

    def cell(k):
        return float(table.get(f"{k:.1f}") or 0) or 1.0

    d = cell(keys[-1])
    if share <= keys[0]:
        d = cell(keys[0])
    else:
        for a, b in zip(keys, keys[1:]):
            if share <= b:
                f = (share - a) / (b - a)
                d = cell(a) * (1 - f) + cell(b) * f
                break
    if share >= live.DEPTH_FROM:
        return d
    return 1 + (d - 1) * max(0.0, (share - (live.DEPTH_FROM - 0.1)) / 0.1)


def page_fncs(t: dict) -> bool:
    """fncsQualifier: the name and the category label."""
    name = f"{t.get('name') or ''} {t.get('category') or ''}".lower()
    return "fncs" in name and "qualifier" in name


def page_signature(t: dict) -> tuple:
    """coldSignature's kind of cup and platform, off the name and the category."""
    sig = calibration.cold_signature({"name": t.get("name") or "", "kind": t.get("category") or ""})
    return sig[2], sig[3]


def family_pace(pace: dict, t: dict, minutes: int, begin: datetime) -> dict | None:
    """familyPace: the cup's own recent editions, else its family by kind of
    cup and platform, else its family pooled."""
    name = live.category_key(t.get("name"))
    games = int(t.get("max_games") or 0)
    for row in (pace or {}).get("categories") or []:
        if not name or row[0] != name:
            continue
        if len(row) > 7 and float(row[6] or 0) > 0 and (abs(float(row[6]) - minutes) > PAGE_FAMILY_MINUTES
                                                        or int(row[7]) != games):
            continue
        if not (len(row) > 5 and row[5] == "feed") and len(row) > 4 and row[4]:
            try:
                latest = datetime.fromisoformat(str(row[4])[:10])
            except ValueError:
                latest = None
            if latest is not None and math.floor((begin - latest).total_seconds() / 86400) > live.CAT_MAX_DAYS:
                continue
        return {"curve": row[1] or None, "tail": row[2] or None, "match": "category"}
    rows = (pace or {}).get("families") or []
    mode, team = str(t.get("game_mode") or ""), str(t.get("team_mode") or "")
    for want in (page_signature(t), ("", "")):
        for row in rows:
            if row[0] == mode and row[1] == team and abs(float(row[2]) - minutes) <= PAGE_FAMILY_MINUTES \
                    and int(row[3]) == games \
                    and (str(row[8] or "") if len(row) > 9 else "", str(row[9] or "") if len(row) > 9 else "") \
                    == tuple(want):
                return {"curve": row[4] or None, "tail": row[5] or None, "match": "family"}
    return None


def guessed_field(model: dict, t: dict) -> int:
    """guessField's field, rounded the page's way."""
    for row in (export_model.category_row(model, t), export_model.family_row(model, t),
                export_model.row_for(model["mode_fields"], game_mode=t.get("game_mode"),
                                     team_mode=t.get("team_mode"), region=t.get("region"))):
        if row and (row.get("field_n") or 0) >= 1 and row.get("field"):
            return js_round(row["field"])
    return 0


# --------------------------------------------------------------------------- #
# One evening, as the page holds it
# --------------------------------------------------------------------------- #
class Evening:
    """A cup opened from the list, its model, and the feed's readings folded
    into its log the page's way."""

    def __init__(self, model: dict, t: dict, begin: datetime, end: datetime, sealed: bool,
                 counts: bool = True):
        self.model, self.t, self.begin, self.sealed = model, t, begin, sealed
        self.close = minutes_from(begin, end)                 # s.end, in minutes from the opening
        self.duration = js_round(self.close)                  # STATE.duration
        self.games = int(t.get("max_games") or 0)
        self.pace = model.get("pace") or PACE_DEFAULT
        self.counts = counts            # read the feed's count source (False: as a replay without it)
        self.log: list[dict] = []
        self.auto = None
        self._cold: dict = {}
        self.comp = {"name": t.get("name") or "", "start_time": begin.strftime("%Y-%m-%d %H:%M:%S"),
                     "end_time": end.strftime("%Y-%m-%d %H:%M:%S"), "game_mode": t.get("game_mode") or "",
                     "team_mode": t.get("team_mode") or "", "max_games": self.games}

    # The cold forecast: the page's predictOne, which is predict_from_model.
    def cold(self, rank: int) -> dict | None:
        if rank not in self._cold:
            got = export_model.predict_from_model(self.model, dict(self.t, rank=rank))
            if got and got.get("ok") and got.get("value"):
                value = float(got["value"])
                self._cold[rank] = {"ok": True, "value": value, "source": got.get("source") or "",
                                    "rel": max(0.0, float(got["high"]) / value - 1) if value > 0 else 0.0}
            elif got and got.get("reason") == export_model.FIELD_BELOW_CUT:
                self._cold[rank] = {"ok": False, "no_prior": True, "reason": bench.refusal(got)}
            else:
                self._cold[rank] = {"ok": False, "reason": bench.refusal(got)} if got else None
        return self._cold[rank]

    # The feed's readings, into the log.
    def live_share(self, game: int, elapsed: float) -> float:
        if self.sealed:
            return min(game / self.games, 1) if self.games else 0
        return min(elapsed / self.duration, 1) if self.duration else 0

    def live_after(self, elapsed: float) -> float:
        if self.sealed or not self.duration:
            return 0
        return max(0, elapsed - self.duration)

    def entry_from(self, record: dict) -> dict | None:
        """entryFrom, with `monotone`."""
        updated = record["minute"]
        readings = [{"rank": int(r), "points": float(p), "at": updated if m is None else m}
                    for r, p, m in record["reads"] if int(r) >= 1 and float(p) > 0]
        kept, last_of = [], {}
        for r in sorted(readings, key=lambda r: r["rank"]):
            if r["at"] not in last_of or r["points"] <= last_of[r["at"]]:
                kept.append(r)
                last_of[r["at"]] = r["points"]
        if not kept:
            return None
        games = int(record.get("games") or 0)
        if self.sealed and games < 1:
            return None
        entry = {"game": games if self.sealed else max(1, games or 1), "elapsed": max(0, js_round(updated)),
                 "at": updated, "seen": updated, "updated": updated, "readings": kept,
                 "final": bool(record.get("final")), "pages": int(record.get("pages") or 0),
                 "ranked": int(record.get("ranked") or 0),
                 "ranked_from": ranked_from(record) if self.counts else ""}
        entry["share"] = 1 if entry["final"] else self.live_share(entry["game"], entry["elapsed"])
        return entry

    def take(self, record: dict) -> None:
        """takeLive, then foldInto."""
        if self.auto is not None and self.auto["updated"] == record["minute"]:
            return
        entry = self.entry_from(record)
        if entry is None:
            return
        self.auto = entry
        kept = [e for e in self.log if e["updated"] != entry["updated"]]
        last = kept[-1] if kept else None
        if last is not None and last["game"] == entry["game"] and len(last["readings"]) == len(entry["readings"]) \
                and all(a["rank"] == b["rank"] and a["points"] == b["points"]
                        for a, b in zip(last["readings"], entry["readings"])) and entry["at"] >= last["at"]:
            last["final"] = last["final"] or entry["final"]
            last["seen"] = entry["at"]
            self.log = kept
            return
        self.log = sorted(kept + [entry], key=lambda e: e["at"])[-400:]

    def replay(self, records: list[dict]) -> "Evening":
        for record in records:
            self.take(record)
        return self

    def clocked(self, e: dict) -> dict:
        """clocked: the entry's share, and each reading's on its own stamp."""
        owes = self.sealed and 0 < e["game"] < self.games
        held = e["seen"] if e.get("seen") is not None else e["at"]
        gone = max(e["elapsed"], js_round(held))

        def early(at):
            return not self.sealed and at < self.close

        share = 1 if e["final"] and not owes and not early(held) else self.live_share(e["game"], gone)
        after = self.live_after(gone)
        readings = []
        for r in e["readings"]:
            own = r["at"] if (r["at"] != e["at"] and not self.sealed and (not e["final"] or early(r["at"]))) else None
            if own is None:
                readings.append(dict(r, elapsed=e["elapsed"], share=share, after=after))
            else:
                elapsed = max(0, js_round(own))
                readings.append(dict(r, elapsed=elapsed, share=self.live_share(e["game"], elapsed),
                                     after=self.live_after(elapsed)))
        return dict(e, share=share, after=after, readings=readings)

    # The evening's answer.
    def refine(self, mirror: bool = True) -> dict | None:
        """refine, with the pace a rank is expected to have reached taken from
        `live.expected_share` (`mirror`) or from the page's own rules."""
        entries = [self.clocked(e) for e in self.log if e["readings"]]
        if not entries:
            return None
        entry = None
        for e in entries:
            if entry is None or e["at"] >= entry["at"]:
                entry = e
        if self.games < 1:
            return None
        stood = entry["seen"] - entry["at"]
        clock = entry
        for e in entries:
            if (1 if e["final"] else e["share"]) >= (1 if clock["final"] else clock["share"]):
                clock = e
        share = 1 if clock["final"] else clock["share"]
        if not share >= PACE_MIN_SHARE:
            return None
        pace = self.pace
        kind = "closed_by_game" if self.sealed else "open_by_time"
        family = family_pace(pace, self.t, self.duration, self.begin) if kind == "open_by_time" else None
        typed = max(0, js_round(float(self.t.get("field_size") or 0)))
        board = None
        for e in entries:
            if ranked_so_far(e) > 0 and (board is None or e["at"] >= board["at"]):
                board = e
        counted = ranked_so_far(board) if kind == "open_by_time" and share >= live.FAMILY_FROM and board else 0
        field = typed or counted or (guessed_field(self.model, self.t) if kind == "open_by_time" else 0) or 0
        ceiling = not typed and counted > 0 and ranked_capped(board)
        fncs = page_fncs(self.t)

        def depth_for(rank, s):
            if not (kind == "open_by_time" and rank and rank >= 1 and field > 0):
                return 1.0
            q = rank / field
            if ceiling:
                q = min(q, live.DEPTH_CEILING_Q)
            if fncs:
                q = min(q, live.DEPTH_FNCS_Q)
            return depth_at(pace, q, s)

        pooled_curve = (pace.get("curve") or {}).get(kind)
        curve = (family and family.get("curve")) or pooled_curve
        spread = (pace.get("dispersion") or {}).get(kind)
        tail = (family and family.get("tail")) or (pace.get("tail") or {}).get(kind)
        tail_spread = (pace.get("tail_dispersion") or {}).get(kind)
        by_games = ((pace.get("games_curve") or {}).get(str(self.games)) or None) if kind == "closed_by_game" else None
        by_games_spread = ((pace.get("games_dispersion") or {}).get(str(self.games)) or None) \
            if kind == "closed_by_game" else None

        def game_of(s):
            return js_round(s * self.games)

        def tail_for(rank):
            return (family and family.get("tail")) or (
                band_table((pace.get("tail") or {}).get("by_rank"), rank) if rank and rank >= 1 else None) or tail

        def spread_for(rank):
            return (band_table((pace.get("tail_dispersion") or {}).get("by_rank"), rank)
                    if rank and rank >= 1 else None) or tail_spread

        def page_expected(s, after, rank=None):
            if after > 0:
                got = tail_at(tail_for(rank), after)
                if got is not None:
                    return max(min(1.0, got * depth_for(rank, 1)) if fncs else got, 0.05)
            if by_games and str(game_of(s)) in by_games:
                return max(float(by_games[str(game_of(s))]), 0.05)
            m = pace_at(curve, s)
            if m is None:
                return s
            if family and curve is not pooled_curve and s < live.FAMILY_FROM:
                base = pace_at(pooled_curve, s)
                if base is not None:
                    f = max(0.0, (s - (live.FAMILY_FROM - 0.1)) / 0.1)
                    m = base * (1 - f) + m * f
            return max(m * depth_for(rank, s), 0.05)

        pooled = {"curve": pooled_curve or {}, "dispersion": spread or {},
                  "tail": (pace.get("tail") or {}).get("open_by_time") or {},
                  "tail_by_rank": (pace.get("tail") or {}).get("by_rank") or []}
        comp = dict(self.comp, field_size=field)

        def mirror_expected(s, after, rank=None):
            return live.expected_share(comp, int(rank or 0), s, after, pooled, pace.get("families") or [],
                                       pace.get("categories") or [], pace.get("depth") or [])

        expected_at = mirror_expected if mirror and kind == "open_by_time" else page_expected
        feed_tail = (pace.get("tail_feed") or {}).get(kind) or None

        def rel_at(s, after, rank=None):
            if after > 0:
                games = tail_at(spread_for(rank), after)
                if feed_tail:
                    rel = feed_tail.get("held") if stood >= (feed_tail.get("held_minutes") or 10) \
                        else feed_tail.get("settling")
                    if rel is not None:
                        return rel if games is None else max(rel, games)
                if games is not None:
                    return games
            if by_games_spread and str(game_of(s)) in by_games_spread:
                return float(by_games_spread[str(game_of(s))])
            d = pace_at(spread, s)
            return 0.10 * math.sqrt((1 - s) / s) + 0.03 if d is None else d

        after = clock["after"] or 0
        rel_pace = rel_at(share, after)
        carried = ((pace.get("carry") or {}).get(kind) or {}).get("slope")
        best = standing(entries, entry["seen"] if entry.get("seen") is not None else entry["at"])
        ratios, by_rank, points, norm, rel_of, clock_of, solo, seen = [], {}, {}, {}, {}, {}, {}, {}
        for rank in sorted(best):
            r = best[rank]
            if not r["share"] >= READ_MIN_SHARE:
                continue
            seen[rank] = r["points"]
            cold = self.cold(rank)
            if cold and not cold["ok"] and cold.get("no_prior") and r["share"] >= PACE_MIN_SHARE:
                own = expected_at(r["share"], r["after"] or 0, rank)
                solo[rank] = r["points"] / own
                points[rank] = r["points"]
                norm[rank] = expected_at(share, after, rank) / own
                rel_of[rank] = rel_at(r["share"], r["after"] or 0, rank)
                clock_of[rank] = (r["share"], r["after"] or 0)
                continue
            if not (cold and cold["ok"] and cold["value"] > 0):
                continue
            exp = expected_at(r["share"], r["after"] or 0, rank)
            ratio = r["points"] / (cold["value"] * exp)
            ratios.append(ratio)
            by_rank[rank], points[rank] = ratio, r["points"]
            norm[rank] = expected_at(share, after, rank) / exp
            rel_of[rank] = rel_at(r["share"], r["after"] or 0, rank)
            clock_of[rank] = (r["share"], r["after"] or 0)
        if not ratios and not solo:
            return None
        ordered = sorted(ratios) if ratios else [1.0]
        mid = len(ordered) // 2
        ratio = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        dev = sorted(abs(x - ratio) / max(ratio, 1e-9) for x in ordered)
        mad = dev[len(dev) // 2] if dev else 0
        held = entry["seen"] - entry["at"]
        owes = kind == "closed_by_game" and 0 < int(clock["game"] or 0) < self.games
        settled = bool(clock["final"]) and not owes and held >= HELD_MINUTES \
            and (kind == "closed_by_game" or after - held >= SETTLED_AFTER)
        return {"ratio": ratio, "rel": math.sqrt(rel_pace * rel_pace + mad * mad), "rel_own": rel_pace,
                "carry": 0 if carried is None else carried, "share": share, "after": after, "kind": kind,
                "by_rank": by_rank, "points": points, "read": sorted(points), "norm": norm, "rel_of": rel_of,
                "clock_of": clock_of, "solo": solo, "seen": seen, "settled": settled, "final": bool(clock["final"]),
                "expected_for": lambda rank: expected_at(share, after, rank),
                "rel_for": lambda rank: rel_at(share, after, rank),
                "field": field, "typed": typed, "counted": counted, "ceiling": ceiling, "fncs": fncs,
                "family": (family or {}).get("match") or "", "entries": len(entries)}

    def forecast(self, rank: int, live_: dict | None) -> dict | None:
        """forecastAt: the cold forecast refined by the evening, and the basis
        the answer stands on."""
        cold = self.cold(rank)
        if cold and not cold["ok"] and cold.get("no_prior") and live_ and live_["solo"].get(rank, 0) > 0:
            if live_["settled"] and live_["points"].get(rank, 0) > 0:
                return {"value": live_["points"][rank], "rel": 0.0, "basis": "result", "live": None, "floor": 0}
            floor = read_floor(live_, rank)
            at = live_["clock_of"].get(rank) or (live_["share"], live_["after"])
            return {"value": max(live_["solo"][rank], floor), "rel": live_["rel_of"][rank], "floor": floor,
                    "basis": "readings only",
                    "live": {"share": at[0], "after": at[1] or 0, "settled": live_["settled"], "rank": rank}}
        if not cold or not cold["ok"]:
            return None
        if not live_:
            return {"value": cold["value"], "rel": cold["rel"], "basis": "cold", "live": None, "floor": 0}
        own = live_["by_rank"].get(rank)
        if live_["settled"] and live_["points"].get(rank, 0) > 0:
            return {"value": live_["points"][rank], "rel": 0.0, "basis": "result", "live": None, "floor": 0}
        between = bracket_read(live_, rank) if own is None else None
        if own is not None:
            ratio, rel_live, basis = own, live_["rel_of"].get(rank, live_["rel_own"]), "read"
        elif between:
            expected, rel = live_["expected_for"](rank), live_["rel_for"](rank)
            ratio = between_reads(between, rank) / (cold["value"] * expected)
            gap = INTERP_REL * math.log(between["b"] / between["a"])
            rel_live, basis = math.sqrt(rel * rel + gap * gap), "between"
        elif live_["carry"] >= CARRY_MIN:
            ratio = live_["ratio"] ** live_["carry"]
            rel_live = math.sqrt(max(0.0, live_["rel_for"](rank) ** 2 + live_["rel"] ** 2 - live_["rel_own"] ** 2))
            basis = "carried"
        else:
            return {"value": cold["value"], "rel": cold["rel"], "basis": "cold", "live": None, "floor": 0}
        from_readings = cold["value"] * ratio
        scale = cold_weight_scale(self.model, cold["source"]) if live_["kind"] == "open_by_time" else 1
        wl = 1 / max(rel_live, 0.01) ** 2
        wc = 1 / max(cold["rel"] * scale, 0.01) ** 2
        blended = (from_readings * wl + cold["value"] * wc) / (wl + wc)
        wc_band = 1 / max(cold["rel"], 0.01) ** 2
        floor = read_floor(live_, rank)
        at = (live_["clock_of"].get(rank) if own is not None else None) or (live_["share"], live_["after"])
        return {"value": max(blended, floor), "rel": 1 / math.sqrt(wl + wc_band), "floor": floor, "basis": basis,
                "weight": wl / (wl + wc),
                "live": {"share": at[0], "after": at[1] or 0, "settled": live_["settled"], "rank": rank}}

    def bands(self, answer: dict) -> tuple[list, list]:
        """bandsOf: (the 50 % range, the 90 % range). The live multipliers
        until the close; past it, or for a closed lobby, the cold ones."""
        value, rel, floor = answer["value"], answer["rel"], answer.get("floor") or 0
        table = self.live_bands(answer.get("live")) or (self.model.get("quality") or {}).get("bands")
        least = max(0.0, min(float(floor or 0), value))

        def span(key):
            b = table.get(key) if table else None
            return [max(least, value * math.exp(b[0] * rel)), value * math.exp(b[1] * rel)] if b else None

        near = span("50")
        wide = span("90") or [max(least, value * (1 - rel)), value * (1 + rel)]
        return near, wide

    def live_bands(self, at: dict | None):
        """liveBands and liveBin, with the guard past the close."""
        if not at or not (at.get("rank") or 0) >= 1 or at.get("settled") or self.sealed:
            return None
        table = band_table(self.pace.get("live_bands") or [], at["rank"])
        if (at.get("after") or 0) > 0:
            return None
        s = float(at.get("share") or 0)
        if s < 0.1:
            return None
        found = "0.1-0.3" if s < 0.3 else "0.3-0.5" if s < 0.5 else "0.5-0.7" if s < 0.7 else \
            "0.7-0.9" if s < 0.9 else "0.9-1.0"
        return table.get(found) if table else None


def ranked_so_far(entry: dict | None) -> int:
    """rankedSoFar: the feed's count, else a hundred a page and the last half full."""
    if not entry:
        return 0
    if entry.get("ranked"):
        return int(entry["ranked"])
    pages = int(entry.get("pages") or 0)
    return (pages - 1) * 100 + 50 if pages > 0 else 0


def ranked_capped(entry: dict | None) -> bool:
    """rankedCapped: a board at the API's last page whose count the
    percentiles have not pinned."""
    return bool(entry) and int(entry.get("pages") or 0) >= PAGES_CAP and entry.get("ranked_from") != "percentile"


def standing(entries: list[dict], newest: float) -> dict:
    """standing: per rank, the richest of the feed's recent readings."""
    best: dict = {}
    for e in entries:
        for r in e["readings"]:
            if not (r["rank"] >= 1 and r["points"] > 0):
                continue
            at = r["at"]
            if newest - at > RECENT_MINUTES and not e["final"]:
                continue
            cur = best.get(r["rank"])
            if cur is None or r["points"] > cur["points"] or (r["points"] == cur["points"] and at > cur["at"]):
                best[r["rank"]] = {"points": r["points"], "at": at, "share": r["share"], "after": r["after"]}
    return best


def read_floor(live_: dict, rank: int) -> float:
    """readFloor: what the board already shows at the rank or deeper."""
    seen = live_["seen"]
    if seen.get(rank, 0) > 0:
        return seen[rank]
    return max([p for r, p in seen.items() if r > rank] or [0])


def bracket_read(live_: dict, rank: int) -> dict | None:
    """bracketRead: the nearest ranks read on either side."""
    a = b = 0
    for r in live_["read"]:
        if r < rank:
            a = r
        elif r > rank:
            b = r
            break
    if not a or not b:
        return None
    norm, points = live_["norm"], live_["points"]
    return {"a": a, "b": b, "qa": points[a] * (norm.get(a) or 1), "qb": points[b] * (norm.get(b) or 1)}


def between_reads(between: dict, rank: int) -> float:
    """betweenReads: log-linear in rank between the two readings."""
    f = math.log(rank / between["a"]) / math.log(between["b"] / between["a"])
    return math.exp(math.log(between["qa"]) + (math.log(between["qb"]) - math.log(between["qa"])) * f)


def cold_weight_scale(model: dict, source: str) -> float:
    """coldWeightScale: the history's width in units of its typical error."""
    blend = model.get("blend") or {}
    reading = float((blend.get("reading") or {}).get("scale") or 0)
    row = (blend.get("cold") or {}).get(source)
    if not reading > 0 or not row or not float(row[0] or 0) > 0:
        return 1.0
    return float(row[0]) / reading


# --------------------------------------------------------------------------- #
# A cup, at each point of its session
# --------------------------------------------------------------------------- #
def point_minute(evening: Evening, share, past) -> float:
    return evening.close * share if share is not None else evening.close + past


def depth_label(q: float | None, answered: bool = True) -> str:
    if not answered:
        return "no live answer"
    if not q:
        return "no field"
    for low, high in DEPTH_LABELS:
        if low <= q < high:
            return f"{low:g}-{high:g}" if high < 10 else f"{low:g}+"
    return "?"


def cup_flags(evening: Evening, live_: dict | None) -> list[str]:
    """Why live.py's pace could part ways with the page's for this cup, at this
    point: the reasons, each of which may or may not move a number."""
    if evening.sealed or not live_:
        return []
    out = []
    if live_["field"] >= live.FIELD_CEILING and not live_["ceiling"] and not live_["fncs"]:
        out.append("ceiling")
    comp = dict(evening.comp, field_size=live_["field"])
    if live.kind_key(comp) != page_signature(evening.t):
        out.append("kind")
    if live.fncs_qualifier(comp) != page_fncs(evening.t):
        out.append("fncs")
    if live.family_key(comp)[2] != evening.duration:
        out.append("minutes")
    return out


def settled_by_feed(snapshots: list[dict], close: float) -> dict:
    """Per rank, the richest reading the feed took LATE_MINUTES or more past
    the close: what `pull_live` files as the final where the harvest has none."""
    out: dict = {}
    for snap in snapshots:
        if snap["minute"] - close >= live.LATE_MINUTES:
            for rank, points in snap["points"].items():
                out[rank] = max(out.get(rank, 0), points)
    return out


def price_cup(conn, model: dict, comp: dict, catalogue: dict, cutoff: str, skipped: Counter,
              refused: Counter, replayed: Counter | None = None, strict: bool = True, arrival="feed",
              early: Counter | None = None) -> tuple[list[dict], dict]:
    """Every rank of one cup the page prices, at each point of its session:
    (rows, a line about the cup). The readings stamped more than
    EARLY_MINUTES before the opening are left out, and counted in `early`
    per window."""
    finals = {int(r): float(v) for r, v in (comp.get("finals") or {}).items() if v and float(v) > 0}
    if not finals:
        skipped["no final standings"] += 1
        return [], {}
    found = catalogue.get((comp.get("event_id"), comp.get("window_id")))
    row, source = (bench.listed_row(*found), "calendar") if found else (None, "database")
    if row is None:
        if found:
            skipped["catalogue row the list leaves out, priced off the database"] += 1
        row, source = bench.database_row(comp), "database"
    reads = replayed["reads"] if replayed is not None else 0
    row["cold"] = bench.cold_cell(conn, row, cutoff)
    if replayed is not None:
        if row["cold"]:
            replayed["cups"] += 1
        elif replayed["reads"] > reads:
            replayed["short"] += 1
    priced = bench.tournament_of(model, row)
    if not priced:
        skipped["a row the page does not open by itself"] += 1
        return [], {}
    t, field, cut, lobby = priced
    begin = moment(row.get("begin")) or moment(comp.get("start_time"))
    end = moment(row.get("end")) or moment(comp.get("end_time"))
    if begin is None or end is None or end <= begin:
        skipped["no window to clock the readings on"] += 1
        return [], {}
    snapshots = snapshots_of(conn, comp["id"], begin, end, arrival)
    window = f"{comp.get('event_id')}|{comp.get('window_id')}" if comp.get("event_id") else f"#{comp['id']}"
    stray = sum(1 for snap in snapshots if snap["minute"] < -EARLY_MINUTES)
    if stray:
        snapshots = [snap for snap in snapshots if snap["minute"] >= -EARLY_MINUTES]
        if early is not None:
            early[window] += stray
    if not snapshots:
        skipped["no reading from the feed"] += 1
        return [], {}
    cut_rank = int(cut[1]) if cut else 0
    read_ranks = {rank for snap in snapshots for rank in snap["points"]}
    wanted = sorted(r for r in (set(bench.RANKS) | read_ranks | ({cut_rank} if cut_rank else set())) if r in finals)
    evening = Evening(model, t, begin, end, lobby)
    settled = settled_by_feed(snapshots, evening.close)
    bands = (model.get("quality") or {}).get("bands") or None
    base = {"window": window, "id": comp["id"], "name": comp.get("name") or "",
            "family": comp.get("family") or comp.get("kind") or "",
            "region": comp.get("region") or "", "model": cutoff, "input": source, "lobby": lobby,
            "day": str(comp.get("start_time") or "")[:10], "cut": cut_rank}
    rows, reasons_seen, deepest = [], set(), 0.0
    for label, share, past in POINTS:
        at = point_minute(evening, share, past)
        taken = available(snapshots, at, evening.close, strict)
        ev = Evening(model, t, begin, end, lobby)
        ev._cold = evening._cold
        ev.replay(records_of(taken))
        mine, page = ev.refine(mirror=True), ev.refine(mirror=False)
        reasons = cup_flags(ev, mine)
        reasons_seen.update(reasons)
        if mine and mine["field"]:
            deepest = max(deepest, max(wanted) / mine["field"])
        for rank in wanted:
            got = ev.forecast(rank, mine)
            if got is None:
                refused[bench.refusal(ev.cold(rank)) if ev.cold(rank) else "no answer"] += 1
                continue
            theirs = ev.forecast(rank, page)
            # The ranges as written, and whether they hold the result as
            # written: the JSON gives back every flag.
            near, wide = ([round(x, 3) for x in band] if band else None for band in ev.bands(got))
            result = finals[rank]
            q = rank / mine["field"] if mine and mine["field"] else None
            differs = theirs is None or abs(theirs["value"] - got["value"]) > 1e-9 * max(1.0, got["value"])
            flags = (reasons or ["other"]) if differs else []
            cold = ev.cold(rank)
            rows.append(dict(base, **{
                "point": label, "minute": round(at, 2), "share": share, "after_close": past,
                "live_share": round(mine["share"], 4) if mine else None,
                "live_after": round(mine["after"], 2) if mine else None,
                "rank": rank, "at_cut": rank == cut_rank,
                "forecast": round(got["value"], 4), "result": result,
                "error": round(100 * (got["value"] / result - 1), 4),
                "near": near, "wide": wide,
                "in_50": bool(near and near[0] <= result <= near[1]), "in_90": wide[0] <= result <= wide[1],
                "live_ranges": bool(ev.live_bands(got.get("live"))), "basis": got["basis"],
                "rung": cold["source"] if cold and cold.get("ok")
                else "none: " + str((cold or {}).get("reason") or ""),
                "cold": round(cold["value"], 4) if cold and cold.get("ok") else None,
                "field": mine["field"] if mine else 0,
                "field_from": ("typed" if mine["typed"] else "counted" if mine["counted"] else "guessed")
                if mine else "no live answer",
                "q": round(q, 4) if q else None, "depth": depth_label(q, mine is not None),
                "pace": ("games (closed lobby)" if lobby else mine["family"] or "pooled") if mine else "no live answer",
                "settled": bool(mine and mine["settled"]),
                "snapshots": len(taken), "entries": len(ev.log), "small": result < SMALL_RESULT,
                "final_from_feed": rank in settled and abs(settled[rank] - result) < 1e-9,
                "flags": flags,
                "page_forecast": round(theirs["value"], 4) if differs and theirs else None,
            }))
    line = {"id": comp["id"], "window": base["window"], "region": base["region"], "family": base["family"],
            "lobby": lobby, "snapshots": len(snapshots), "early": stray, "ranks": len(wanted), "cut": cut_rank,
            "pace_from": "page rules (closed lobby)" if lobby else "live.py",
            # Why live.py could read this cup otherwise than the page, at some
            # point of its session, and how many of its forecasts it moved.
            "reasons": sorted(reasons_seen), "moved": sum(1 for r in rows if r["flags"]),
            "deepest_q": round(deepest, 4)}
    return rows, line


def feed_cups(conn, comps: list[dict], since: str, until: str) -> list[dict]:
    """The cups that started in [since, until) and that the feed read."""
    followed = {cid for (cid,) in conn.execute(
        "SELECT DISTINCT competition_id FROM snapshot WHERE note LIKE ?", (FEED_NOTE,))}
    return [c for c in comps if c["id"] in followed and since <= str(c.get("start_time") or "")[:10] < until]


def measure(conn, since: str, until: str, catalogue: dict, cache: str | None, key: str, by: str = "update",
            progress: bool = True, updates: tuple = bench.UPDATES, strict: bool = True, arrival="feed") -> dict:
    """Every cup the feed followed in [since, until), replayed from the model
    before it, each reading landing as `arrival` has it. {"rows", "cups",
    "lines", "skipped", "refused", "early", "models", "built", "read",
    "model_key", "replays", "live_pages", "bands", "pace", "blend"}."""
    comps = export_model.load_competitions(conn)
    targets: dict = {}
    for comp in feed_cups(conn, comps, since, until):
        targets.setdefault(bench.cutoff_of(str(comp.get("start_time") or ""), by, updates), []).append(comp)
    models = bench.Models(conn, comps, key, cache)
    rows, lines, skipped, refused, sizes, replayed, early = [], [], Counter(), Counter(), {}, Counter(), Counter()
    bands = pace = blend = None
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

    # The live analysis reads raw pages too (`live.sessions_of`): counted the
    # same way, should any step of the replay ever ask for them.
    sessions, pages_read = live.sessions_of, {}

    def sessions_noted(comp, pages):
        teams, disagree = sessions(comp, pages)
        content = repr([[(s, str(t), p) for s, t, p in games] for games in teams])
        pages_read[(str(comp.get("event_id")), str(comp.get("window_id")))] = (
            len(teams), hashlib.sha1(content.encode()).hexdigest()[:8])
        return teams, disagree

    rescore.rosters_of, live.sessions_of = read, sessions_noted
    began = time.time()
    try:
        for n, cutoff in enumerate(sorted(targets), 1):
            model = models.get(cutoff)
            sizes[cutoff] = {"tournaments": model["source"]["tournaments"], "cups": len(targets[cutoff])}
            quality = model.get("quality") or {}
            bands = bands or {"generated": quality.get("generated"), "from": quality.get("from")}
            pace = pace or {"generated": (model.get("pace") or {}).get("generated")}
            blend = blend or {"generated": (model.get("blend") or {}).get("generated")}
            before = len(rows)
            for comp in sorted(targets[cutoff], key=lambda c: (str(c.get("start_time")), c["id"])):
                got, line = price_cup(conn, model, comp, catalogue, cutoff, skipped, refused, replayed, strict,
                                      arrival, early)
                rows += got
                if line:
                    lines.append(line)
            if progress:
                print(f"  {cutoff[:16]:<16}  model of {model['source']['tournaments']:>5} tournaments, "
                      f"{len(targets[cutoff]):>3} cups, {len(rows) - before:>5} forecasts"
                      f"   ({n}/{len(targets)}, {time.time() - began:.0f} s)", flush=True)
            del model
    finally:
        rescore.rosters_of, live.sessions_of = reader, sessions
    models.prune()
    listed = sorted(f"{event}|{window}|{n}|{sha}" for (event, window), (n, sha) in boards.items())
    paged = sorted(f"{event}|{window}|{n}|{sha}" for (event, window), (n, sha) in pages_read.items())
    return {"rows": rows, "lines": lines, "cups": sum(len(v) for v in targets.values()), "skipped": skipped,
            "refused": refused, "models": sizes, "built": models.built, "read": models.read,
            "early": {"readings": sum(early.values()), "cups": len(early), "windows": dict(sorted(early.items()))},
            "model_key": models.key,
            "replays": {"cups": replayed["cups"], "cups_short_of_boards": replayed["short"],
                        "boards_asked": len(listed), "boards_with_pages": sum(1 for n, _ in boards.values() if n),
                        "digest": hashlib.sha1("\n".join(listed).encode()).hexdigest()[:16]},
            "live_pages": {"boards_asked": len(paged),
                           "boards_with_pages": sum(1 for n, _ in pages_read.values() if n),
                           "digest": hashlib.sha1("\n".join(paged).encode()).hexdigest()[:16]},
            "bands": bands, "pace": pace, "blend": blend}


# --------------------------------------------------------------------------- #
# The tables
# --------------------------------------------------------------------------- #
def summary(rows: list[dict]) -> dict:
    """Every forecast, and beside it the mean and median |error| without the
    finals under SMALL_RESULT points (`small`: how many there are)."""
    errors = [r["error"] for r in rows]
    near = [r["in_50"] for r in rows if r["near"] is not None]
    if not errors:
        return {"forecasts": 0}
    big = [abs(r["error"]) for r in rows if r["result"] >= SMALL_RESULT]
    return {"forecasts": len(rows), "cups": len({r["window"] for r in rows}),
            "abs_mean": round(statistics.mean(abs(e) for e in errors), 3),
            "abs_median": round(statistics.median(abs(e) for e in errors), 3),
            "signed_mean": round(statistics.mean(errors), 3), "signed_median": round(statistics.median(errors), 3),
            "in_50": round(sum(near) / len(near), 4) if near else None,
            "in_90": round(sum(r["in_90"] for r in rows) / len(rows), 4),
            "small": len(rows) - len(big),
            "abs_mean_big": round(statistics.mean(big), 3) if big else None,
            "abs_median_big": round(statistics.median(big), 3) if big else None}


CUTS = OrderedDict([
    ("region", lambda r: r["region"]),
    ("depth", lambda r: r["depth"]),
    ("rank band", lambda r: rank_band(r["rank"])),
    ("basis", lambda r: r["basis"]),
    ("pace", lambda r: r["pace"]),
    ("field from", lambda r: r["field_from"]),
    ("closed lobby", lambda r: "closed lobby" if r["lobby"] else "open queue"),
    ("final", lambda r: f"under {SMALL_RESULT} points" if r["result"] < SMALL_RESULT else f"{SMALL_RESULT} or more"),
    ("family", lambda r: r["family"]),
])


def rank_band(rank: int) -> str:
    return coldbench.band_label(rank, bench.RANK_BANDS)


def tables(rows: list[dict]) -> dict:
    """{"all"|"at the cut": {point: summary}, cut: {value: {point: summary}}}."""
    by_point: dict = OrderedDict((label, []) for label, _, _ in POINTS)
    for r in rows:
        by_point[r["point"]].append(r)
    out = {"all": {p: summary(v) for p, v in by_point.items()},
           "at the cut": {p: summary([r for r in v if r["at_cut"]]) for p, v in by_point.items()}}
    for name, cut in CUTS.items():
        grouped: dict = {}
        for r in rows:
            grouped.setdefault(str(cut(r)), {}).setdefault(r["point"], []).append(r)
        order = sorted(grouped, key=lambda v: (-sum(len(x) for x in grouped[v].values()), v))
        out[name] = OrderedDict((value, OrderedDict((p, summary(grouped[value].get(p, [])))
                                                    for p, _, _ in POINTS)) for value in order)
    return out


def flag_summary(rows: list[dict], lines: list[dict]) -> dict:
    """Per reason: the cups it holds for at some point of their session, and
    the forecasts it moved."""
    out = {}
    for flag in FLAGS:
        hit = [r for r in rows if flag in r["flags"]]
        held = [line for line in lines if flag in line.get("reasons", ())]
        out[flag] = {"cups": len(held), "cups_moved": len({r["window"] for r in hit}), "forecasts": len(hit),
                     "windows": sorted(line["window"] for line in held)[:60]}
    return out


def print_tables(found: dict, rows: list[dict]) -> None:
    points = [p for p, _, _ in POINTS]
    head = f"  {'':<26}" + "".join(f"{p:>12}" for p in points)
    for which in ("all", "at the cut"):
        print(f"\n{which}: forecasts / cups / mean |error| / median |error| / mean error / in 50 % / in 90 %"
              f" / finals under {SMALL_RESULT} points, and the mean |error| without them")
        print(f"  {'point':<10}{'n':>7}{'cups':>6}{'mean|e|':>9}{'med|e|':>8}{'mean e':>8}{'in 50':>7}{'in 90':>7}"
              f"{'small':>7}{'mean|e|':>9}")
        for p in points:
            s = found[which][p]
            if not s.get("forecasts"):
                print(f"  {p:<10}{0:>7}")
                continue
            print(f"  {p:<10}{s['forecasts']:>7}{s['cups']:>6}{s['abs_mean']:>9.2f}{s['abs_median']:>8.2f}"
                  f"{s['signed_mean']:>+8.2f}{100 * (s['in_50'] or 0):>6.1f}%{100 * s['in_90']:>6.1f}%"
                  f"{s['small']:>7}{s['abs_mean_big'] or 0:>9.2f}")
    for name in CUTS:
        print(f"\nby {name}: median |error| % (forecasts) at each point")
        print(head)
        shown = list(found[name].items())
        if name == "family" and len(shown) > TOP_FAMILIES:
            shown = shown[:TOP_FAMILIES]
        for value, cells in shown:
            line = f"  {str(value)[:25]:<26}"
            for p in points:
                s = cells[p]
                if s.get("forecasts"):
                    line += f"{s['abs_median']:>6.1f}{'(' + str(s['forecasts']) + ')':>6}"
                else:
                    line += f"{'':>12}"
            print(line)


def write_json(path: str, payload: dict, rows: list[dict]) -> None:
    """The payload, with one forecast per line: a run of thirty thousand rows
    stays readable and diffable."""
    head = json.dumps(payload, ensure_ascii=False, indent=1)
    body = ",\n".join("  " + json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(head[:-2] + ',\n "forecasts": [\n' + body + "\n ]\n}\n")


def run(args) -> int:
    """`python -m analysis.bench --live`, with the cold bench's options but
    --cutoff published, --jobs and --variant, which `bench.main` refuses."""
    try:
        arrival = arrival_of(getattr(args, "arrival", "feed"))
    except ValueError:
        print(f"--arrival takes feed, stamp or a whole number of minutes, not {args.arrival!r}")
        return 2
    path = args.db or bench.db.DB_PATH
    if not os.path.exists(path):
        print(f"No database at {path}: nothing measured.")
        return 0
    since, until = bench.span_of(args)
    cache = None if args.cache.lower() == "none" else (
        args.cache or os.path.join(tempfile.gettempdir(), "fortnite-tracker-bench"))
    catalogue = bench.load_catalogue(args.catalogue)
    boards = os.path.abspath(args.leaderboards or rescore.RAW)
    events = bench.pages_in(boards)
    which = (f"the last daily update online before it ({', '.join(args.update)} Paris time, online "
             f"{bench.PUBLISH_MINUTES} min later)" if args.cutoff == "update"
             else "the tournaments started before its day")
    print(f"Live bench, cups the feed followed from {since} to {until} (not included), "
          f"each priced from the model of {which}")
    print(f"  a reading reaches the page: {ARRIVAL_RULES.get(arrival) or ARRIVAL_RULES['minutes'] % arrival}")
    print(f"  database {path}; catalogue: {len(catalogue):,} windows"
          + ("" if catalogue else " - every cup is priced off its database row"))
    print(f"  raw leaderboard pages: {events:,} events in {boards}")
    if not events:
        print("  WARNING: no raw leaderboard pages there. The replayed tables are off: the cups new in their region\n"
              "  start from other rungs, and this run does not compare with one that had the pages (--leaderboards).")
    key = bench.model_key(path)
    conn = bench.open_read_only(path, live=bench.same_file(path, str(bench.db.DB_PATH)))
    conn.row_factory = sqlite3.Row
    began = time.time()
    kept_boards, kept_raw = rescore.RAW, harvest_osirion.RAW
    rescore.RAW, harvest_osirion.RAW = boards, os.path.dirname(boards)
    try:
        found = measure(conn, since, until, catalogue, cache, key, args.cutoff, updates=args.update, arrival=arrival)
    finally:
        rescore.RAW, harvest_osirion.RAW = kept_boards, kept_raw
        conn.close()
    rows = found["rows"]
    print(f"\n{found['cups']} cups the feed followed, {len(found['lines'])} replayed, {len(rows)} forecasts, "
          f"{found['built']} models built and {found['read']} read from {cache or 'nowhere'}, "
          f"in {time.time() - began:.0f} s")
    replays = found["replays"]
    print(f"Replayed tables on {replays['cups']} cups; {replays['cups_short_of_boards']} more asked for one and found "
          f"fewer than two usable boards. Boards: {replays['boards_with_pages']} with pages of "
          f"{replays['boards_asked']} asked (digest {replays['digest']}).")
    if replays["boards_asked"] and not replays["boards_with_pages"]:
        print("WARNING: not one board the replays asked for had pages: the replayed tables are off in this run.")
    pages = found["live_pages"]
    print(f"Raw pages the live analysis read: {pages['boards_with_pages']} boards of {pages['boards_asked']} asked"
          f" (digest {pages['digest']}).")
    if found["bands"]:
        print(f"Ranges from the validation generated {found['bands'].get('generated')} (cold) and the pace tables "
              f"of {(found['pace'] or {}).get('generated')} (live), the same for every model.")
    early = found["early"]
    print(f"Readings stamped more than {EARLY_MINUTES} min before their window opened, left out: "
          f"{early['readings']} on {early['cups']} cups"
          + (f" ({', '.join(early['windows'])})" if early["cups"] else ""))
    for title, counted in (("Cups and forecasts left out:", found["skipped"]),
                           ("Forecasts refused by the model:", found["refused"])):
        if counted:
            print(f"\n{title}")
            for why, n in counted.most_common():
                print(f"  {n:>6}  {why}")
    print("\n" + "=" * 96)
    print(f"  live: the forecast the page shows during each cup ({since} to {until}, cutoff: {args.cutoff}, "
          f"arrival: {arrival_label(arrival)})")
    print("=" * 96)
    if not rows:
        print("  nothing to measure")
        return 0
    found_tables = tables(rows)
    print_tables(found_tables, rows)
    flagged = flag_summary(rows, found["lines"])
    print("\nwhere live.py and the page part ways: cups it holds for / cups and forecasts it moved"
          " (the number measured is live.py's)")
    for flag, meaning in FLAGS.items():
        f = flagged[flag]
        print(f"  {flag:<8}{f['cups']:>5}{f['cups_moved']:>5}{f['forecasts']:>7}   {meaning}")
    if args.json:
        payload = {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "bench": "live", "since": since, "until": until, "cutoff": args.cutoff,
            "update": {"paris": list(args.update), "online_after_minutes": bench.PUBLISH_MINUTES}
            if args.cutoff == "update" else None,
            "database": {"sha1": bench.file_sha1(path), "bytes": os.path.getsize(path),
                         **({"wal_sha1": bench.file_sha1(path + "-wal"), "wal_bytes": os.path.getsize(path + "-wal")}
                            if os.path.exists(path + "-wal") and os.path.getsize(path + "-wal") else {})},
            "catalogue": {"windows": len(catalogue),
                          "files": len(glob.glob(os.path.join(args.catalogue, "*.json.gz")))},
            "leaderboards": {"events": events}, "replays": found["replays"], "live_pages": found["live_pages"],
            "bands": found["bands"], "pace": found["pace"], "blend": found["blend"],
            "model_key": found["model_key"], "ranks": list(bench.RANKS),
            "points": [{"label": p, "share": s, "after_close": a} for p, s, a in POINTS],
            # Two runs compare only under the same arrival.
            "arrival": arrival,
            "rule": f"readings that reached the page by the point ({arrival_label(arrival)}: "
                    f"{ARRIVAL_RULES.get(arrival) or ARRIVAL_RULES['minutes'] % arrival}); "
                    f"none called final before the close; none stamped more than {EARLY_MINUTES} min "
                    f"before the opening",
            "arrival_model": {"full_pass_minutes": FULL_PASS_MINUTES, "quick_pass_minutes": QUICK_PASS_MINUTES,
                              "endgame_before": ENDGAME_BEFORE, "lands_after": LANDS_AFTER,
                              "first_page": FIRST_PAGE} if arrival == "feed" else None,
            "loads": "rebuilt from the database, one per stamp rather than one per pass of the feed: each part of "
                     "a pass carries the pass's pages, count and final mark",
            "ranks_priced": "every rank the feed read in any of the cup's readings, later ones included, with the "
                            "bench's ranks and the cut: chosen after the fact, but no later number reaches a point",
            "early": early,
            "small_result": SMALL_RESULT,
            "cups": found["cups"], "replayed": len(found["lines"]), "models": found["models"],
            "skipped": dict(found["skipped"]), "refused": dict(found["refused"]),
            "flags": {"meaning": FLAGS, "found": flagged},
            "tables": found_tables, "lines": found["lines"],
        }
        write_json(args.json, payload, rows)
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(bench.main(["--live"] + sys.argv[1:]))
