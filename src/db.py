"""SQLite storage layer for point threshold tracking.

Model:
  competition  -> one tournament session (name, region, mode, start, end)
  snapshot     -> one reading taken at a given time during the competition
  points       -> the point threshold of a rank (top 1, 20, 50...) in a reading
"""
from __future__ import annotations

import json
import re
import os
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
# The code lives in `src/`, the data one level up in `data/`; a copy laid out
# flat still works, which is what keeps a move from breaking anything.
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "src" else HERE
DB_PATH = os.environ.get("FNT_DB", os.path.join(ROOT, "data", "tracker.db"))

DEFAULT_RANKS = [1, 20, 50, 100, 500, 1000]

REGIONS = ["EU", "NAC", "NAE", "NAW", "BR", "ASIA", "OCE", "ME", "ONSITE"]
TEAM_MODES = ["Solo", "Duo", "Trio", "Squad"]
GAME_MODES = ["Battle Royale", "Zero Build", "Reload", "Reload Zero Build",
              "Blitz Royale", "Other"]

# Longest a game can run, per mode: a game started just before the tournament
# closes keeps scoring points after the official end time.
GAME_MINUTES = {
    "Battle Royale": 30,
    "Zero Build": 30,
    "Reload": 18,
    "Reload Zero Build": 18,
    "Blitz Royale": 12,
    "Other": 25,
}
DEFAULT_TRACKER_LAG = 5      # minutes Fortnite Tracker takes to refresh
DEFAULT_MAX_GAMES = 10       # games allowed within one session

# Default scoring, modelled on the Solo Cash Cups (1st = 60 pts, 50th = 1 pt,
# 2 pts per elimination). Adjust to the tournament's official rules.
DEFAULT_SCORING = {
    "kill": 2,
    # How many eliminations score at most in a single game. None = no cap.
    # Reload cups often set one (10), and ignoring it throws the whole scoring
    # off.
    "kill_cap": None,
    "placement": [[1, 1, 60], [2, 2, 54], [3, 3, 48], [4, 4, 44], [5, 5, 40],
                  [6, 6, 36], [7, 7, 33], [8, 8, 30], [9, 9, 27], [10, 10, 24],
                  [11, 15, 20], [16, 20, 16], [21, 25, 12], [26, 30, 9],
                  [31, 35, 6], [36, 40, 4], [41, 45, 2], [46, 50, 1]],
}

SCORING_PRESETS = {
    "Cash Cup — Round 1 (2 pts / kill)": DEFAULT_SCORING,
    "Cash Cup — Round 2 (3 pts / kill)": {**DEFAULT_SCORING, "kill": 3},
    "Reload (1st = 30 pts, 1 pt / kill)": {
        "kill": 1,
        "placement": [[1, 1, 30], [2, 2, 25], [3, 3, 21], [4, 4, 18], [5, 5, 15],
                      [6, 8, 12], [9, 12, 9], [13, 16, 6], [17, 20, 3]],
    },
}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS competition (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    region      TEXT    NOT NULL DEFAULT 'EU',
    team_mode   TEXT    NOT NULL DEFAULT 'Solo',
    game_mode   TEXT    NOT NULL DEFAULT 'Battle Royale',
    start_time  TEXT    NOT NULL,
    end_time    TEXT,
    ranks       TEXT    NOT NULL DEFAULT '[1,20,50,100,500,1000]',
    notes       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    game_minutes    REAL NOT NULL DEFAULT 30,
    tracker_lag_min REAL NOT NULL DEFAULT 5,
    max_games       INTEGER NOT NULL DEFAULT 10,
    scoring         TEXT NOT NULL DEFAULT '',
    finished_at     TEXT,
    games_mode      TEXT NOT NULL DEFAULT 'max',
    slot_minutes    REAL NOT NULL DEFAULT 0,
    series_id       INTEGER REFERENCES series(id) ON DELETE SET NULL,
    scoring_id      INTEGER,
    stage           TEXT NOT NULL DEFAULT '',
    stage_order     INTEGER NOT NULL DEFAULT 0,
    edition         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshot (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id INTEGER NOT NULL REFERENCES competition(id) ON DELETE CASCADE,
    ts             TEXT    NOT NULL,
    note           TEXT    NOT NULL DEFAULT '',
    games          INTEGER,
    my_points      REAL
);

CREATE TABLE IF NOT EXISTS points (
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    rank        INTEGER NOT NULL,
    points      REAL    NOT NULL,
    PRIMARY KEY (snapshot_id, rank)
);

-- Scoring systems saved under a name, reusable from one tournament to the next.
CREATE TABLE IF NOT EXISTS scoring_system (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    kill       REAL NOT NULL DEFAULT 0,
    placement  TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- A series is a recurring tournament in one region (e.g. FNCS Duo EU) and its stages.
CREATE TABLE IF NOT EXISTS series (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    region     TEXT NOT NULL DEFAULT 'EU',
    team_mode  TEXT NOT NULL DEFAULT 'Duo',
    game_mode  TEXT NOT NULL DEFAULT 'Battle Royale',
    scoring_id INTEGER REFERENCES scoring_system(id) ON DELETE SET NULL,
    stages     TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Calibration output, recomputed whenever the history changes.
CREATE TABLE IF NOT EXISTS calibration_cache (
    key         TEXT PRIMARY KEY,
    n_comps     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    computed_at TEXT NOT NULL
);

-- Log of API calls, so the monthly quota spent can be shown.
CREATE TABLE IF NOT EXISTS api_call (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status   INTEGER NOT NULL
);

-- Last full standings fetched for a competition.
CREATE TABLE IF NOT EXISTS standing (
    competition_id INTEGER NOT NULL REFERENCES competition(id) ON DELETE CASCADE,
    rank           INTEGER NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    score          REAL NOT NULL,
    kills          INTEGER,
    games          INTEGER,
    PRIMARY KEY (competition_id, rank)
);

-- Estimate made BEFORE the first reading, from the scoring and the history alone.
-- Frozen so it can be compared with the real result once the tournament is over:
-- that comparison is what makes the cold forecast measurable, and so improvable.
CREATE TABLE IF NOT EXISTS first_estimate (
    competition_id INTEGER NOT NULL REFERENCES competition(id) ON DELETE CASCADE,
    rank           INTEGER NOT NULL,
    value          REAL NOT NULL,
    low            REAL,
    high           REAL,
    created_at     TEXT NOT NULL,
    n_comps        INTEGER NOT NULL DEFAULT 0,
    scope          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (competition_id, rank)
);

-- Game by game history of every team. It comes in the same response as the
-- standings, so storing it costs no extra request.
CREATE TABLE IF NOT EXISTS team_match (
    competition_id INTEGER NOT NULL REFERENCES competition(id) ON DELETE CASCADE,
    team_id        TEXT NOT NULL,
    rank           INTEGER NOT NULL,
    match_number   INTEGER NOT NULL,
    started_at     TEXT,
    placement      INTEGER,
    kills          INTEGER,
    points         REAL,
    time_alive     INTEGER,
    PRIMARY KEY (competition_id, team_id, match_number)
);

CREATE INDEX IF NOT EXISTS idx_match_comp_rank ON team_match(competition_id, rank);

-- Ranks that matter, per tournament type and per region: the scoring is the same
-- everywhere, but the rank that qualifies or that pays the skin is not.
CREATE TABLE IF NOT EXISTS objective (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind   TEXT NOT NULL,
    region TEXT NOT NULL,
    label  TEXT NOT NULL,
    rank   INTEGER NOT NULL
);

-- Definitive thresholds entered at the end of the competition (the official result).
CREATE TABLE IF NOT EXISTS final_result (
    competition_id INTEGER NOT NULL REFERENCES competition(id) ON DELETE CASCADE,
    rank           INTEGER NOT NULL,
    points         REAL    NOT NULL,
    PRIMARY KEY (competition_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_comp ON snapshot(competition_id, ts);
"""


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The app and a script run alongside it write to the same file. In WAL
    # journal mode a read no longer blocks a write, and the 15-second wait
    # absorbs the rest instead of failing on "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def session(path: str | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the fact: applied to databases created by an earlier
# version, without erasing anything.
MIGRATIONS = [
    ("competition", "game_minutes", "REAL NOT NULL DEFAULT 30"),
    ("competition", "tracker_lag_min", "REAL NOT NULL DEFAULT 5"),
    ("competition", "max_games", "INTEGER NOT NULL DEFAULT 10"),
    ("competition", "scoring", "TEXT NOT NULL DEFAULT ''"),
    ("competition", "finished_at", "TEXT"),
    ("snapshot", "games", "INTEGER"),
    ("snapshot", "my_points", "REAL"),
    ("competition", "series_id", "INTEGER"),
    ("competition", "scoring_id", "INTEGER"),
    ("competition", "stage", "TEXT NOT NULL DEFAULT ''"),
    ("competition", "stage_order", "INTEGER NOT NULL DEFAULT 0"),
    ("competition", "edition", "TEXT NOT NULL DEFAULT ''"),
    ("competition", "games_mode", "TEXT NOT NULL DEFAULT 'max'"),
    ("competition", "slot_minutes", "REAL NOT NULL DEFAULT 0"),
    ("competition", "event_id", "TEXT NOT NULL DEFAULT ''"),
    ("competition", "window_id", "TEXT NOT NULL DEFAULT ''"),
    ("competition", "source", "TEXT NOT NULL DEFAULT 'manual'"),
    ("competition", "tracking", "INTEGER NOT NULL DEFAULT 0"),
    ("competition", "scoring_accuracy", "REAL"),
    ("scoring_system", "kill_cap", "INTEGER"),
    ("competition", "confirmed_at", "TEXT"),
    ("competition", "qualifier", "INTEGER NOT NULL DEFAULT 0"),
    ("standing", "members", "TEXT NOT NULL DEFAULT ''"),
    ("scoring_system", "team_mode", "TEXT NOT NULL DEFAULT ''"),
    ("scoring_system", "game_mode", "TEXT NOT NULL DEFAULT ''"),
    ("competition", "family", "TEXT NOT NULL DEFAULT ''"),
    ("competition", "field_size", "INTEGER"),
    # Who may enter, as Epic's requirement spells it ("ranked-br-combined:12"):
    # the same cup admitting Unreal alone one week and Diamond upwards the
    # next is two fields of different sizes, and the previous edition to read
    # is the one with the same bar.
    ("competition", "entry", "TEXT NOT NULL DEFAULT ''"),
    # The three axes the sorting rests on, kept apart on purpose. The series is
    # the recurring cup; the round is which session inside one running of it;
    # the occurrence is which running. They used to be squeezed into one "stage"
    # field, which is how two consecutive weeks of the same cup ended up in two
    # different categories with one edition each.
    ("competition", "series_key", "TEXT NOT NULL DEFAULT ''"),
    ("competition", "season", "TEXT NOT NULL DEFAULT ''"),
    ("competition", "occurrence", "INTEGER NOT NULL DEFAULT 0"),
    ("competition", "round_no", "INTEGER NOT NULL DEFAULT 0"),
    ("competition", "tags", "TEXT NOT NULL DEFAULT ''"),
    # How many pages the board had when the feed read it - a hundred rosters
    # a page - so how many had played by then: the cup's arrival, minute by
    # minute, beside its points.
    ("snapshot", "pages", "INTEGER"),
    # ... and the exact count of rosters ranked, where the feed read the last
    # page as well; absent at the API's ceiling of a hundred pages.
    ("snapshot", "ranked", "INTEGER"),
]

# Key/value settings, one row per key.
SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FAVOURITE_TABLE = """
CREATE TABLE IF NOT EXISTS favourite (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

# No default objectives: depending on the tournament and the region, a rank pays
# a skin, cash, a qualification or nothing at all. Guessing would mislead, so the
# app offers a blank tier for you to name yourself.
DEFAULT_OBJECTIVES: list[tuple[str, int]] = []

# How the number of games in a session is counted:
#   max    -> everyone plays when they want, up to `max_games` games (open, cash cup)
#   sealed -> every game starts at a fixed time and everyone plays the same
#             number of them (final, set lobby)
# The keys are stored in competition.games_mode.
GAMES_MODES = {"max": "Maximum (everyone plays at their own pace)",
               "sealed": "Sealed (fixed start times, same count for everyone)"}

# Ranks tracked by default, per kind of stage.
STAGE_RANKS = {
    "open": [100, 500, 1000, 10000],
    "final": [1, 3, 5, 10, 20],
}

# Series template offered at creation (FNCS: one open, then a final).
DEFAULT_STAGES = [
    {"name": "Open", "day_offset": 0, "start": "19:00", "duration_min": 180,
     "ranks": STAGE_RANKS["open"], "max_games": 10, "game_minutes": 30, "tracker_lag_min": 5,
     "games_mode": "max", "slot_minutes": 0},
    {"name": "Final", "day_offset": 1, "start": "19:00", "duration_min": 180,
     "ranks": STAGE_RANKS["final"], "max_games": 6, "game_minutes": 30, "tracker_lag_min": 5,
     "games_mode": "sealed", "slot_minutes": 35},
]


def default_ranks_for_stage(stage_name: str) -> list[int]:
    """Ranks worth tracking for a stage, guessed from its name."""
    name = (stage_name or "").strip().lower()
    # Stage names are typed by hand and often French: "final" also catches
    # "finale", and "manche" is the French for "round".
    if "final" in name:
        return list(STAGE_RANKS["final"])
    if any(w in name for w in ("open", "qualif", "round", "manche")):
        return list(STAGE_RANKS["open"])
    return list(DEFAULT_RANKS)


def init_db(path: str | None = None) -> None:
    with session(path) as conn:
        conn.executescript(SCHEMA)
        conn.executescript(FAVOURITE_TABLE)
        conn.executescript(SETTINGS_TABLE)
        rename_legacy_values(conn)
        for table, column, decl in MIGRATIONS:
            # Interpolated SQL, but nothing here comes from a request: table,
            # column and decl are literals from MIGRATIONS above, and neither
            # PRAGMA nor ALTER TABLE accepts a bound parameter for a name.
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        # Competitions created before the category became an explicit field
        # inherit a guess drawn from their name. It stays editable from the
        # history page: it is a guess, not a fact.
        for row in conn.execute("SELECT id, name, source FROM competition WHERE family = ''"):
            family, stage = split_name(row["name"])
            # On a hand-entered tournament the suffix of the name is an edition
            # or tier label, never a stage, so guess nothing.
            if row["source"] == "history":
                stage = ""
            conn.execute("UPDATE competition SET family = ?, "
                         "stage = CASE WHEN stage = '' THEN ? ELSE stage END WHERE id = ?",
                         (family, stage, row["id"]))
        # Competitions written before the three axes existed carry them in one
        # "stage" string. Reading it back out is what merges two consecutive
        # weeks of the same cup, which used to sit in separate categories.
        for row in conn.execute("SELECT id, name, family, stage FROM competition "
                                "WHERE series_key = ''"):
            comp = dict(row)
            conn.execute("UPDATE competition SET series_key = ?, round_no = ? WHERE id = ?",
                         (series_of(comp), round_of(comp), comp["id"]))
        # competitions from before inherit the game length of their mode
        for row in conn.execute("SELECT id, game_mode FROM competition"):
            conn.execute("UPDATE competition SET game_minutes = ? WHERE id = ? AND game_minutes = 30",
                         (GAME_MINUTES.get(row["game_mode"], 30), row["id"]))
        seed_scoring_systems(conn)


# Values that used to be stored in French. Nothing compares against the old
# spellings any more, so reading them would work — but they would keep turning
# up in exports and in the SQL console, so they get rewritten once.
LEGACY_VALUES = [
    ("competition", "games_mode", "scelle", "sealed"),
    ("competition", "source", "historique", "history"),
    ("competition", "source", "manuel", "manual"),
    ("competition", "game_mode", "Autre", "Other"),
]


def rename_legacy_values(conn) -> None:
    """Carry a database written by an earlier, French-named version forward."""
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='reglage'").fetchone():
        conn.execute("INSERT OR IGNORE INTO setting (key, value) "
                     "SELECT cle, valeur FROM reglage")
        conn.execute("UPDATE setting SET key = 'scoring_presets_seeded' "
                     "WHERE key = 'baremes_semes'")
        conn.execute("DROP TABLE reglage")
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(competition)")}
    for table, column, before, after in LEGACY_VALUES:
        if column in columns:
            # Table and column are literals from LEGACY_VALUES; values are bound.
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                         (after, before))


def get_setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO setting (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def seed_scoring_systems(conn) -> None:
    """Create the bundled scoring systems, once and for good.

    "The table is empty" is not enough on its own: someone who deletes all their
    scoring systems would see them come back on the next start. So the seeding
    itself is recorded, and that record is what the check reads.
    """
    if get_setting(conn, "scoring_presets_seeded"):
        return
    if conn.execute("SELECT COUNT(*) c FROM scoring_system").fetchone()["c"]:
        set_setting(conn, "scoring_presets_seeded", _now())
        return
    set_setting(conn, "scoring_presets_seeded", _now())
    for name, s in SCORING_PRESETS.items():
        conn.execute(
            "INSERT INTO scoring_system (name, kill, placement, notes, created_at) "
            "VALUES (?,?,?,?,?)",
            (name, s["kill"], json.dumps(s["placement"]),
             "Bundled scoring — check the values against the tournament rules.", _now()),
        )


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def norm_ts(value: str | None) -> str:
    """Accepts '2026-08-28T21:30', '2026-08-28 21:30:00'... -> 'YYYY-MM-DD HH:MM:SS'."""
    if not value:
        return _now()
    value = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(microsecond=0).isoformat(sep=" ")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).replace(microsecond=0, tzinfo=None).isoformat(sep=" ")
    except ValueError:
        raise ValueError(f"Unreadable date or time: {value!r}")


# --------------------------------------------------------------------------- #
# Competitions
# --------------------------------------------------------------------------- #
def create_competition(conn, name, region="EU", team_mode="Solo",
                       game_mode="Battle Royale", start_time=None, end_time=None,
                       ranks=None, notes="", game_minutes=None, tracker_lag_min=None,
                       max_games=None, scoring=None, games_mode="max",
                       slot_minutes=None) -> int:
    ranks = sorted(set(int(r) for r in (ranks or DEFAULT_RANKS)))
    if game_minutes in (None, ""):
        game_minutes = GAME_MINUTES.get(game_mode, 30)
    cur = conn.execute(
        """INSERT INTO competition (name, region, team_mode, game_mode, start_time,
                                    end_time, ranks, notes, created_at,
                                    game_minutes, tracker_lag_min, max_games, scoring,
                                    games_mode, slot_minutes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name.strip(), region, team_mode, game_mode, norm_ts(start_time),
         norm_ts(end_time) if end_time else None, json.dumps(ranks), notes or "", _now(),
         float(game_minutes),
         float(DEFAULT_TRACKER_LAG if tracker_lag_min in (None, "") else tracker_lag_min),
         int(DEFAULT_MAX_GAMES if max_games in (None, "") else max_games),
         json.dumps(scoring) if scoring else "",
         games_mode if games_mode in GAMES_MODES else "max",
         float(slot_minutes) if slot_minutes not in (None, "") else 0.0),
    )
    return cur.lastrowid


def update_competition(conn, comp_id: int, **fields) -> None:
    allowed = {"name", "region", "team_mode", "game_mode", "start_time", "end_time",
               "ranks", "notes", "game_minutes", "tracker_lag_min", "max_games",
               "scoring", "scoring_accuracy", "finished_at", "games_mode", "slot_minutes",
               "event_id", "window_id", "source", "tracking", "confirmed_at", "qualifier",
               "family", "stage", "field_size", "entry",
               "series_key", "season", "occurrence", "round_no", "tags"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in ("start_time", "end_time", "finished_at"):
            value = norm_ts(value) if value else None
        if key == "ranks":
            value = json.dumps(sorted(set(int(r) for r in value)))
        if key == "scoring":
            value = json.dumps(value) if isinstance(value, dict) else (value or "")
        if key in ("game_minutes", "tracker_lag_min", "slot_minutes") and value not in (None, ""):
            value = float(value)
        if key == "games_mode" and value not in GAMES_MODES:
            value = "max"
        if key == "max_games" and value not in (None, ""):
            value = int(value)
        if key == "qualifier":
            value = 1 if value else 0
        if key == "field_size" and value not in (None, ""):
            value = int(value)
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return
    values.append(comp_id)
    # Interpolated SQL: only column names from `allowed` reach the string, and
    # every value stays a bound parameter.
    conn.execute(f"UPDATE competition SET {', '.join(sets)} WHERE id = ?", values)


def delete_competition(conn, comp_id: int) -> None:
    conn.execute("DELETE FROM competition WHERE id = ?", (comp_id,))


def _row_to_comp(row: sqlite3.Row) -> dict:
    comp = dict(row)
    comp["ranks"] = json.loads(comp["ranks"])
    if comp.get("scoring"):
        comp["scoring"] = json.loads(comp["scoring"])
        comp["scoring_known"] = True
    else:
        # For want of anything better we show a generic scoring, but it must
        # never be presented as derived: it is an assumption, not a measurement.
        comp["scoring"] = dict(DEFAULT_SCORING)
        comp["scoring_known"] = False
    comp["scoring"].setdefault("kill_cap", None)
    comp["finished"] = bool(comp.get("finished_at"))
    comp["confirmed"] = bool(comp.get("confirmed_at"))
    comp["qualifier"] = bool(comp.get("qualifier"))
    comp["kind"] = category_of(comp)
    return comp


def get_competition(conn, comp_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM competition WHERE id = ?", (comp_id,)).fetchone()
    return _row_to_comp(row) if row else None


def list_competitions(conn, region=None, team_mode=None, game_mode=None) -> list[dict]:
    sql = "SELECT * FROM competition WHERE 1=1"
    args: list = []
    for col, val in (("region", region), ("team_mode", team_mode), ("game_mode", game_mode)):
        if val:
            # Interpolated SQL: `col` is one of the three literals above, and the
            # value the caller passed is bound, not inlined.
            sql += f" AND {col} = ?"
            args.append(val)
    sql += " ORDER BY start_time DESC, id DESC"
    return [_row_to_comp(r) for r in conn.execute(sql, args)]


# Exactly the columns the taxonomy reads: series_of, round_of, category_of and
# category_key between them touch these and nothing else.
CATALOGUE_COLUMNS = ("id", "name", "region", "team_mode", "game_mode", "start_time",
                     "series_id", "stage", "family", "series_key", "round_no", "occurrence",
                     "source")


# Tournament families kept out of the training set, matched case-insensitively
# against the family and the name.
#
# Ranked Cups are here because Epic runs one per ranked division and gives them
# all the same title: "Duos Ranked Cup (Battle Royale)" is the name of the Gold
# cup, the Elite cup and the Unreal cup alike. The division never reaches the
# taxonomy, so every division lands in one category and its level becomes a
# median over populations with nothing in common — a number that describes no
# tournament anybody is about to play. Until the division can be read out of the
# event, these teach the model noise.
#
# They stay in the database. This list decides what the model is *fitted* on,
# not what is kept, so putting one back is a one-line edit and a re-export.
# Set FNT_KEEP_EXCLUDED=1 to include them anyway, which is how the cost of
# excluding them gets measured instead of assumed.
EXCLUDED = ("ranked cup",)


def is_excluded(comp) -> bool:
    """Is this tournament kept out of the training set? See `EXCLUDED`."""
    if os.environ.get("FNT_KEEP_EXCLUDED"):
        return False
    text = f"{comp.get('family') or ''} {comp.get('name') or ''}".lower()
    return any(pattern in text for pattern in EXCLUDED)


def keep_for_training(comps: list[dict]) -> tuple[list[dict], int]:
    """(the tournaments the model learns from, how many were set aside)."""
    kept = [c for c in comps if not is_excluded(c)]
    return kept, len(comps) - len(kept)


def suspicious_fields(comps) -> tuple[int, float] | None:
    """(size, share) when the stored field sizes look like a harvest depth.

    Every forecast the project makes is a function of `q = rank / field_size`, so
    a field that records how many pages were downloaded rather than how many
    teams were ranked is wrong everywhere while looking entirely healthy: the
    tables fill, the checks pass, the numbers stay plausible. It surfaces only as
    the model losing to a baseline that never reads the field at all.

    Two conditions together, because either alone gives false alarms. Real cups
    do repeat a popular size, so a common value proves nothing on its own; and
    some genuine size has to be the largest. But a harvest cap is *both* the most
    common size and the largest possible one — nothing can sit beyond the depth
    that was fetched — and it holds a clear majority. Real fields never do that.
    """
    sizes = [int(c["field_size"]) for c in comps if c.get("field_size")]
    if len(sizes) < 50:
        return None
    common, hits = Counter(sizes).most_common(1)[0]
    share = hits / len(sizes)
    if share > 0.5 and common == max(sizes):
        return common, share
    return None


def catalogue(conn) -> list[dict]:
    """Every competition, named and classified, with no JSON decoded.

    `list_competitions` parses two JSON columns per row. That is nothing at
    sixty-six tournaments and eighty per cent of a cross-validation at ten
    thousand — twelve million `json.loads` calls to answer questions about
    region and stage that never look at a scoring table.

    The rows this returns carry no `scoring` and no `ranks` *keys at all*, so
    code that wants them raises KeyError here instead of quietly reading a
    string where it expected a dict. Use it to decide which competitions you
    want, then load those with `get_competition_full`.
    """
    sql = f"SELECT {', '.join(CATALOGUE_COLUMNS)} FROM competition ORDER BY start_time DESC, id DESC"
    out = []
    for row in conn.execute(sql):
        comp = dict(row)
        comp["kind"] = category_of(comp)
        out.append(comp)
    return out


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
def add_snapshot(conn, comp_id: int, ts=None, points: dict | None = None, note="",
                 games=None, my_points=None, pages=None, ranked=None) -> int:
    ts = norm_ts(ts)
    cur = conn.execute(
        "INSERT INTO snapshot (competition_id, ts, note, games, my_points, pages, ranked) VALUES (?,?,?,?,?,?,?)",
        (comp_id, ts, note or "",
         int(games) if games not in (None, "") else None,
         float(my_points) if my_points not in (None, "") else None,
         int(pages) if pages not in (None, "", 0) else None,
         int(ranked) if ranked not in (None, "", 0) else None),
    )
    sid = cur.lastrowid
    set_points(conn, sid, points or {})
    return sid


def set_points(conn, snapshot_id: int, points: dict) -> None:
    for rank, value in points.items():
        if value is None or value == "":
            conn.execute("DELETE FROM points WHERE snapshot_id = ? AND rank = ?",
                         (snapshot_id, int(rank)))
            continue
        conn.execute(
            "INSERT INTO points (snapshot_id, rank, points) VALUES (?,?,?) "
            "ON CONFLICT(snapshot_id, rank) DO UPDATE SET points = excluded.points",
            (snapshot_id, int(rank), float(value)),
        )


def update_snapshot(conn, snapshot_id: int, ts=None, note=None, points=None,
                    games=None, my_points=None) -> None:
    if ts is not None:
        conn.execute("UPDATE snapshot SET ts = ? WHERE id = ?", (norm_ts(ts), snapshot_id))
    if note is not None:
        conn.execute("UPDATE snapshot SET note = ? WHERE id = ?", (note, snapshot_id))
    if games is not None:
        conn.execute("UPDATE snapshot SET games = ? WHERE id = ?",
                     (int(games) if games != "" else None, snapshot_id))
    if my_points is not None:
        conn.execute("UPDATE snapshot SET my_points = ? WHERE id = ?",
                     (float(my_points) if my_points != "" else None, snapshot_id))
    if points is not None:
        set_points(conn, snapshot_id, points)


def delete_snapshot(conn, snapshot_id: int) -> None:
    conn.execute("DELETE FROM snapshot WHERE id = ?", (snapshot_id,))


def get_snapshots(conn, comp_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM snapshot WHERE competition_id = ? ORDER BY ts, id", (comp_id,)
    ).fetchall()
    snaps = []
    for row in rows:
        pts = {
            int(p["rank"]): p["points"]
            for p in conn.execute(
                "SELECT rank, points FROM points WHERE snapshot_id = ? ORDER BY rank", (row["id"],)
            )
        }
        snaps.append({"id": row["id"], "ts": row["ts"], "note": row["note"], "points": pts,
                      "games": row["games"], "my_points": row["my_points"]})
    return snaps


def get_competition_full(conn, comp_id: int) -> dict | None:
    comp = get_competition(conn, comp_id)
    if comp is None:
        return None
    comp["snapshots"] = get_snapshots(conn, comp_id)
    comp["finals"] = get_finals(conn, comp_id)
    return comp


# --------------------------------------------------------------------------- #
# Definitive results
# --------------------------------------------------------------------------- #
def set_finals(conn, comp_id: int, points: dict) -> None:
    """Record the definitive thresholds. An empty value clears that rank."""
    for rank, value in points.items():
        if value is None or value == "":
            conn.execute("DELETE FROM final_result WHERE competition_id = ? AND rank = ?",
                         (comp_id, int(rank)))
            continue
        conn.execute(
            "INSERT INTO final_result (competition_id, rank, points) VALUES (?,?,?) "
            "ON CONFLICT(competition_id, rank) DO UPDATE SET points = excluded.points",
            (comp_id, int(rank), float(value)),
        )


def get_finals(conn, comp_id: int) -> dict[int, float]:
    return {int(r["rank"]): r["points"] for r in conn.execute(
        "SELECT rank, points FROM final_result WHERE competition_id = ? ORDER BY rank", (comp_id,))}


def add_finals_missing(conn, comp_id: int, points: dict) -> list[int]:
    """Record thresholds only at the ranks that have none yet; the ranks added.

    What the live feed's settled last reading fills in where the harvest did
    not read that deep: a rank the standings on disk hold keeps the harvest's
    number, and a later, deeper harvest replaces the feed's through
    `set_finals`. The tournament's rank list grows with it, so the model reads
    the new ranks.
    """
    held = get_finals(conn, comp_id)
    added = []
    for rank, value in points.items():
        rank = int(rank)
        if rank in held or value is None or value == "" or float(value) <= 0:
            continue
        conn.execute("INSERT INTO final_result (competition_id, rank, points) VALUES (?,?,?)",
                     (comp_id, rank, float(value)))
        added.append(rank)
    if added:
        row = conn.execute("SELECT ranks FROM competition WHERE id = ?", (comp_id,)).fetchone()
        ranks = set(json.loads(row["ranks"])) if row and row["ranks"] else set()
        update_competition(conn, comp_id, ranks=sorted(ranks | set(added)))
    return sorted(added)


def clear_finals(conn, comp_id: int) -> None:
    conn.execute("DELETE FROM final_result WHERE competition_id = ?", (comp_id,))


def latest_points(conn, comp_id: int) -> dict[int, float]:
    """Last threshold read for each rank (where it stands now, not the final word)."""
    out: dict[int, float] = {}
    for snap in get_snapshots(conn, comp_id):
        for rank, value in snap["points"].items():
            out[rank] = value
    return out


def final_points(conn, comp_id: int) -> dict[int, float]:
    """Best knowledge of the final threshold: the definitive result if it was
    entered, otherwise the last reading."""
    out = latest_points(conn, comp_id)
    out.update(get_finals(conn, comp_id))
    return out


# --------------------------------------------------------------------------- #
# Export / import
# --------------------------------------------------------------------------- #
def export_all(conn) -> dict:
    comps = [export_competition(conn, c["id"]) for c in list_competitions(conn)]
    return {"version": 3, "exported_at": _now(), "competitions": comps}


def export_competition(conn, comp_id: int) -> dict:
    comp = get_competition_full(conn, comp_id)
    return dict(comp) if comp else {}


def import_all(conn, payload: dict, replace: bool = False) -> int:
    if replace:
        conn.execute("DELETE FROM competition")
    comps = payload.get("competitions")
    if comps is None:                      # file holding a single tournament
        comps = [payload] if payload.get("name") else []
    count = 0
    for comp in comps:
        cid = create_competition(
            conn,
            name=comp.get("name", "Untitled"),
            region=comp.get("region", "EU"),
            team_mode=comp.get("team_mode", "Solo"),
            game_mode=comp.get("game_mode", "Battle Royale"),
            start_time=comp.get("start_time"),
            end_time=comp.get("end_time"),
            ranks=comp.get("ranks") or DEFAULT_RANKS,
            notes=comp.get("notes", ""),
            game_minutes=comp.get("game_minutes"),
            tracker_lag_min=comp.get("tracker_lag_min"),
            max_games=comp.get("max_games"),
            scoring=comp.get("scoring"),
            games_mode=comp.get("games_mode", "max"),
            slot_minutes=comp.get("slot_minutes"),
        )
        if comp.get("finished_at"):
            update_competition(conn, cid, finished_at=comp["finished_at"])
        for snap in comp.get("snapshots", []):
            add_snapshot(conn, cid, ts=snap.get("ts"),
                         points={int(k): v for k, v in (snap.get("points") or {}).items()},
                         note=snap.get("note", ""), games=snap.get("games"),
                         my_points=snap.get("my_points"))
        if comp.get("finals"):
            set_finals(conn, cid, {int(k): v for k, v in comp["finals"].items()})
        count += 1
    return count


# --------------------------------------------------------------------------- #
# Tournament archive: one file per tournament, readable name, sorts by date
# --------------------------------------------------------------------------- #
def slugify(text: str, maxlen: int = 45) -> str:
    # Tournament names are often typed in French, so accents are folded rather
    # than dropped: "Coupe d'été" gives "coupe-d-ete", not "coupe-d-t".
    accents = str.maketrans("àâäáãçéèêëíìîïñóòôöõúùûüýÿ", "aaaaaceeeeiiiinooooouuuuyy")
    text = (text or "").lower().translate(accents)
    out = []
    for ch in text:
        out.append(ch if ch.isalnum() else "-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:maxlen] or "tournament"


def archive_name(comp: dict) -> str:
    """e.g. 2026-08-28_1900_EU_Solo_battle-royale_solo-cash-cup-2.json"""
    start = datetime.fromisoformat(comp["start_time"])
    return "_".join([
        start.strftime("%Y-%m-%d_%H%M"),
        comp["region"],
        comp["team_mode"],
        slugify(comp["game_mode"], 20),
        slugify(comp["name"]),
    ]) + ".json"


def archive_dir() -> str:
    """The archive folder, moving an older French-named one across on the way."""
    root = os.path.dirname(os.path.abspath(DB_PATH))
    folder = os.path.join(root, "tournaments")
    legacy = os.path.join(root, "tournois")
    if os.path.isdir(legacy) and not os.path.isdir(folder):
        os.rename(legacy, folder)
    os.makedirs(folder, exist_ok=True)
    return folder


def archive_competition(conn, comp_id: int) -> str:
    """Write the tournament to its own JSON file and return the path."""
    comp = export_competition(conn, comp_id)
    folder = archive_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, archive_name(comp))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(comp, fh, indent=2, ensure_ascii=False)
    return path


def list_archives() -> list[dict]:
    folder = archive_dir()
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder), reverse=True):
        if name.endswith(".json"):
            full = os.path.join(folder, name)
            out.append({"name": name, "size": os.path.getsize(full)})
    return out


def finish_competition(conn, comp_id: int, copy_finals: bool = True) -> dict:
    """Close the tournament: mark it finished, freeze the thresholds, write the archive."""
    comp = get_competition_full(conn, comp_id)
    if comp is None:
        raise ValueError("No such competition")
    update_competition(conn, comp_id, finished_at=_now())
    if copy_finals and not comp["finals"] and comp["snapshots"]:
        set_finals(conn, comp_id, dict(comp["snapshots"][-1]["points"]))
    conn.commit()
    path = archive_competition(conn, comp_id)
    return {"finished_at": get_competition(conn, comp_id)["finished_at"],
            "archive": path, "finals": get_finals(conn, comp_id)}


def reopen_competition(conn, comp_id: int) -> None:
    conn.execute("UPDATE competition SET finished_at = NULL WHERE id = ?", (comp_id,))


def duplicate_competition(conn, comp_id: int, start_time=None, name=None) -> int:
    """Recreate a competition with the same settings, minus the readings."""
    comp = get_competition(conn, comp_id)
    if comp is None:
        raise ValueError("No such competition")
    old_start = datetime.fromisoformat(comp["start_time"])
    new_start = datetime.fromisoformat(norm_ts(start_time)) if start_time else old_start
    end = None
    if comp["end_time"]:
        end = new_start + (datetime.fromisoformat(comp["end_time"]) - old_start)
    return create_competition(
        conn, name=name or _next_name(comp["name"]), region=comp["region"],
        team_mode=comp["team_mode"], game_mode=comp["game_mode"],
        start_time=new_start.isoformat(sep=" "),
        end_time=end.isoformat(sep=" ") if end else None,
        ranks=comp["ranks"], notes=comp["notes"], game_minutes=comp["game_minutes"],
        tracker_lag_min=comp["tracker_lag_min"], max_games=comp["max_games"],
        scoring=comp["scoring"], games_mode=comp.get("games_mode", "max"),
        slot_minutes=comp.get("slot_minutes"),
    )


def _next_name(name: str) -> str:
    """'Cash Cup #3' -> 'Cash Cup #4'; anything else gets ' (copy)'."""
    import re
    m = re.search(r"(\d+)\s*$", name.strip())
    if m:
        return name[:m.start(1)] + str(int(m.group(1)) + 1) + name[m.end(1):]
    return name + " (copy)"


def export_csv_rows(conn) -> list[list]:
    rows = [["competition", "region", "team_mode", "game_mode", "start", "end", "finished", "type",
             "reading_time", "minutes_elapsed", "progress_%", "games", "my_points",
             "rank", "points"]]
    for comp in list_competitions(conn):
        start = datetime.fromisoformat(comp["start_time"])
        end = datetime.fromisoformat(comp["end_time"]) if comp["end_time"] else None
        total = (end - start).total_seconds() / 60 if end else None
        head = [comp["name"], comp["region"], comp["team_mode"], comp["game_mode"],
                comp["start_time"], comp["end_time"] or "", "yes" if comp["finished"] else "no"]
        for snap in get_snapshots(conn, comp["id"]):
            ts = datetime.fromisoformat(snap["ts"])
            mins = (ts - start).total_seconds() / 60
            prog = round(100 * mins / total, 2) if total else ""
            extra = [snap.get("games") or "", snap.get("my_points") or ""]
            for rank, value in sorted(snap["points"].items()):
                rows.append(head + ["reading", snap["ts"], round(mins, 1), prog]
                            + extra + [rank, value])
        for rank, value in sorted(get_finals(conn, comp["id"]).items()):
            rows.append(head + ["final_result", comp["end_time"] or "",
                                round(total, 1) if total else "", 100, "", "", rank, value])
    return rows


# --------------------------------------------------------------------------- #
# Saved scoring systems
# --------------------------------------------------------------------------- #
# None is a meaningful kill_cap (no cap at all), so update_scoring needs its own
# marker for "the caller said nothing about the cap".
_MISSING = object()


def _row_to_scoring(row) -> dict:
    return {"id": row["id"], "name": row["name"], "kill": row["kill"],
            "kill_cap": row["kill_cap"],
            # which format this scoring applies to: "" means "any"
            "team_mode": row["team_mode"], "game_mode": row["game_mode"],
            "placement": json.loads(row["placement"]), "notes": row["notes"],
            "created_at": row["created_at"]}


def list_scorings(conn) -> list[dict]:
    return [_row_to_scoring(r) for r in
            conn.execute("SELECT * FROM scoring_system ORDER BY name")]


def get_scoring(conn, scoring_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM scoring_system WHERE id = ?", (scoring_id,)).fetchone()
    return _row_to_scoring(row) if row else None


def _unique_scoring_name(conn, name: str, exclude_id=None) -> str:
    """Same name twice would make the saved list unreadable, so suffix a number."""
    name = (name or "Scoring").strip() or "Scoring"
    base, n = name, 2
    while True:
        row = conn.execute("SELECT id FROM scoring_system WHERE name = ?", (name,)).fetchone()
        if row is None or row["id"] == exclude_id:
            return name
        name = f"{base} ({n})"
        n += 1


def create_scoring(conn, name: str, kill=0, placement=None, notes="", kill_cap=None,
                   team_mode="", game_mode="") -> int:
    cur = conn.execute(
        "INSERT INTO scoring_system (name, kill, kill_cap, team_mode, game_mode, "
        "                            placement, notes, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (_unique_scoring_name(conn, name), float(kill or 0), _cap(kill_cap),
         team_mode if team_mode in TEAM_MODES else "",
         game_mode if game_mode in GAME_MODES else "",
         json.dumps(placement or DEFAULT_SCORING["placement"]), notes or "", _now()),
    )
    return cur.lastrowid


def _cap(value):
    """Elimination cap: a positive integer, or nothing at all."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def update_scoring(conn, scoring_id: int, name=None, kill=None, placement=None, notes=None,
                   kill_cap=_MISSING, team_mode=None, game_mode=None) -> None:
    sets, values = [], []
    if name is not None:
        sets.append("name = ?"); values.append(_unique_scoring_name(conn, name, scoring_id))
    if kill is not None:
        sets.append("kill = ?"); values.append(float(kill))
    if kill_cap is not _MISSING:
        sets.append("kill_cap = ?"); values.append(_cap(kill_cap))
    if team_mode is not None:
        sets.append("team_mode = ?")
        values.append(team_mode if team_mode in TEAM_MODES else "")
    if game_mode is not None:
        sets.append("game_mode = ?")
        values.append(game_mode if game_mode in GAME_MODES else "")
    if placement is not None:
        sets.append("placement = ?"); values.append(json.dumps(placement))
    if notes is not None:
        sets.append("notes = ?"); values.append(notes)
    if not sets:
        return
    values.append(scoring_id)
    # Interpolated SQL: `sets` only ever holds the literal "<column> = ?" clauses
    # written above, and the values themselves are bound.
    conn.execute(f"UPDATE scoring_system SET {', '.join(sets)} WHERE id = ?", values)


def delete_scoring(conn, scoring_id: int) -> None:
    conn.execute("DELETE FROM scoring_system WHERE id = ?", (scoring_id,))


def scoring_payload(conn, scoring_id) -> dict | None:
    """{'kill':..,'placement':..} ready to be copied into a competition."""
    s = get_scoring(conn, scoring_id) if scoring_id else None
    return {"kill": s["kill"], "kill_cap": s["kill_cap"],
            "placement": s["placement"]} if s else None


# --------------------------------------------------------------------------- #
# Series (recurring tournament) and their stages
# --------------------------------------------------------------------------- #
def _row_to_series(row) -> dict:
    out = dict(row)
    out["stages"] = json.loads(out["stages"])
    return out


def list_series(conn) -> list[dict]:
    return [_row_to_series(r) for r in
            conn.execute("SELECT * FROM series ORDER BY region, name")]


def get_series(conn, series_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone()
    return _row_to_series(row) if row else None


def normalise_stages(stages) -> list[dict]:
    """Fill in every field a stage needs, whatever the browser sent."""
    out = []
    for i, s in enumerate(stages or DEFAULT_STAGES):
        name = (s.get("name") or f"Stage {i + 1}").strip()
        ranks = s.get("ranks") or default_ranks_for_stage(name)
        out.append({
            "name": name,
            "day_offset": int(s.get("day_offset") or 0),
            "start": (s.get("start") or "19:00")[:5],
            "duration_min": float(s.get("duration_min") or 180),
            "ranks": sorted({int(r) for r in ranks}),
            "max_games": int(s.get("max_games") or DEFAULT_MAX_GAMES),
            "game_minutes": float(s.get("game_minutes") or 30),
            "tracker_lag_min": float(s.get("tracker_lag_min") or DEFAULT_TRACKER_LAG),
            "games_mode": s.get("games_mode") if s.get("games_mode") in GAMES_MODES else "max",
            "slot_minutes": float(s.get("slot_minutes") or 0),
        })
    return out


def create_series(conn, name, region="EU", team_mode="Duo", game_mode="Battle Royale",
                  scoring_id=None, stages=None, notes="") -> int:
    cur = conn.execute(
        "INSERT INTO series (name, region, team_mode, game_mode, scoring_id, stages, notes, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (name.strip(), region, team_mode, game_mode,
         int(scoring_id) if scoring_id else None,
         json.dumps(normalise_stages(stages)), notes or "", _now()),
    )
    return cur.lastrowid


def update_series(conn, series_id: int, **fields) -> None:
    allowed = {"name", "region", "team_mode", "game_mode", "scoring_id", "stages", "notes"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "stages":
            value = json.dumps(normalise_stages(value))
        if key == "scoring_id":
            value = int(value) if value else None
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return
    values.append(series_id)
    # Interpolated SQL: `key` passed the `allowed` check above, so only a known
    # column name lands in the string; the value is bound.
    conn.execute(f"UPDATE series SET {', '.join(sets)} WHERE id = ?", values)


def delete_series(conn, series_id: int) -> None:
    conn.execute("DELETE FROM series WHERE id = ?", (series_id,))


def create_edition(conn, series_id: int, date: str, label: str = "") -> list[int]:
    """Create every stage of an edition from the date of the first one."""
    series = get_series(conn, series_id)
    if series is None:
        raise ValueError("No such series")
    day0 = datetime.fromisoformat(norm_ts(date))
    label = label.strip() or day0.strftime("%d/%m/%Y")
    scoring = scoring_payload(conn, series["scoring_id"])
    ids = []
    for order, stage in enumerate(series["stages"]):
        hh, mm = (stage["start"].split(":") + ["0"])[:2]
        start = (day0 + timedelta(days=stage["day_offset"])).replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0)
        end = start + timedelta(minutes=stage["duration_min"])
        cid = create_competition(
            conn, name=f"{series['name']} — {stage['name']}",
            region=series["region"], team_mode=series["team_mode"],
            game_mode=series["game_mode"],
            start_time=start.isoformat(sep=" "), end_time=end.isoformat(sep=" "),
            ranks=stage["ranks"], game_minutes=stage["game_minutes"],
            tracker_lag_min=stage["tracker_lag_min"], max_games=stage["max_games"],
            scoring=scoring, games_mode=stage.get("games_mode", "max"),
            slot_minutes=stage.get("slot_minutes"),
        )
        conn.execute(
            "UPDATE competition SET series_id = ?, scoring_id = ?, stage = ?, "
            "stage_order = ?, edition = ? WHERE id = ?",
            (series_id, series["scoring_id"], stage["name"], order, label, cid),
        )
        ids.append(cid)
    return ids


def series_editions(conn, series_id: int) -> list[dict]:
    """The editions of a series, each with its stages."""
    rows = conn.execute(
        "SELECT * FROM competition WHERE series_id = ? ORDER BY start_time DESC, stage_order",
        (series_id,)).fetchall()
    editions: dict[str, dict] = {}
    for row in rows:
        comp = _row_to_comp(row)
        key = comp["edition"] or comp["start_time"][:10]
        ed = editions.setdefault(key, {"edition": key, "date": comp["start_time"], "stages": []})
        comp["n_snapshots"] = len(get_snapshots(conn, comp["id"]))
        # "finalised" = has definitive results; the key the series pages read.
        comp["finalised"] = bool(get_finals(conn, comp["id"]))
        ed["stages"].append(comp)
        ed["date"] = min(ed["date"], comp["start_time"])
    out = list(editions.values())
    for ed in out:
        ed["stages"].sort(key=lambda c: (c["stage_order"], c["start_time"]))
    out.sort(key=lambda e: e["date"], reverse=True)
    return out


# --------------------------------------------------------------------------- #
# API call quota
# --------------------------------------------------------------------------- #
def log_api_call(conn, endpoint: str, status: int) -> None:
    conn.execute("INSERT INTO api_call (ts, endpoint, status) VALUES (?,?,?)",
                 (_now(), endpoint, int(status)))


def quota_used(conn, month: str | None = None) -> int:
    """Requests charged this month: only the ones that reached the server count."""
    month = month or datetime.now().strftime("%Y-%m")
    row = conn.execute(
        "SELECT COUNT(*) c FROM api_call WHERE status > 0 AND substr(ts, 1, 7) = ?",
        (month,)).fetchone()
    return row["c"] if row else 0


def quota_view(conn, per_tournament: int = 18, limit: int = 500) -> dict:
    """What the "n/500" badge needs, plus how many tournaments are still trackable."""
    used = quota_used(conn)
    left = max(0, limit - used)
    return {
        "used": used,
        "limit": limit,
        "left": left,
        "pct": round(100 * used / limit, 1) if limit else 0,
        "per_tournament": per_tournament,
        "tournaments_left": left // per_tournament if per_tournament else 0,
        "tournaments_total": limit // per_tournament if per_tournament else 0,
    }


# --------------------------------------------------------------------------- #
# Full standings of a competition
# --------------------------------------------------------------------------- #
def set_standings(conn, comp_id: int, rows: list[dict]) -> None:
    conn.execute("DELETE FROM standing WHERE competition_id = ?", (comp_id,))
    conn.executemany(
        "INSERT INTO standing (competition_id, rank, name, members, score, kills, games) "
        "VALUES (?,?,?,?,?,?,?)",
        [(comp_id, int(r["rank"]), (r.get("name") or "")[:80],
          " + ".join(m for m in (r.get("members") or []) if m)[:160],
          float(r["score"]), r.get("kills"), r.get("games"))
         for r in rows if r.get("rank")],
    )


def get_standings(conn, comp_id: int, limit: int = 200, offset: int = 0) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT rank, name, members, score, kills, games FROM standing "
        "WHERE competition_id = ? ORDER BY rank LIMIT ? OFFSET ?",
        (comp_id, limit, offset))]


def standings_count(conn, comp_id: int) -> int:
    row = conn.execute("SELECT COUNT(*) c FROM standing WHERE competition_id = ?",
                       (comp_id,)).fetchone()
    return row["c"] if row else 0


# --------------------------------------------------------------------------- #
# Objectives (qualifying rank, skin rank) per category and region
# --------------------------------------------------------------------------- #
# A stage is not an edition. "Round 2" of an FNCS Practice is a different step —
# often with its own scoring — whereas "Week 3" is the same step a week later.
# No text rule separates the two for certain: the app proposes, you confirm on
# the Settings page.
# Names come from Epic's feed (English) or are typed by hand (often French), so
# every pattern matches both languages; the label it produces is English.
STAGE_PATTERNS = [
    (r"\bgrande?s?\s*finales?\b|\bgrand\s*finals?\b", "Grand final"),
    (r"\bdemi[- ]finales?\b|\bsemi[- ]?finals?\b", "Semi-final"),
    (r"\bfinales?\b|\bfinals?\b", "Final"),
    (r"\bqualifications?\b|\bqualifiers?\b|\bqualif\b", "Qualifier"),
    (r"\b(?:round|manche|r)\s*#?(\d+)\b", "Round {0}"),
    (r"\bday\s*#?(\d+)\b|\bjour\s*#?(\d+)\b", "Day {0}"),
]

EDITION_PATTERNS = [
    r"\b(?:week|semaine|wk)\s*#?\d+\b",
    r"\b(?:event|session)\s*#?\d+\b",
]

REGION_WORDS = (r"\b(eu|europe|nac|nace|naw|na east|na west|asia|asie|oce|oceania|"
                r"br|brazil|me|middle east)\b")

# Stages offered in the interface. The list is not closed: the field takes any
# text at all.
KNOWN_STAGES = ["", "Qualifier", "Round 1", "Round 2", "Round 3",
                "Semi-final", "Final", "Grand final"]


def split_name(name: str) -> tuple[str, str]:
    """(family, stage) proposed from the raw tournament name."""
    text = (name or "").strip()
    stage = ""
    for pattern, template in STAGE_PATTERNS:
        found = re.search(pattern, text, flags=re.I)
        if found:
            groups = [g for g in found.groups() if g]
            stage = template.format(*groups) if groups else template
            text = text[:found.start()] + " " + text[found.end():]
            break
    for pattern in EDITION_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.I)
    text = re.sub(REGION_WORDS, " ", text, flags=re.I)
    # "— S4", "#12": an edition number at the end of the name. A digit that is
    # part of the name itself ("Division 2") is left alone.
    text = re.sub(r"\s*[—–-]\s*[A-Za-z]{0,3}\s*#?\d+\s*$", "", text)
    text = re.sub(r"\s*#\s*\d+\s*$", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -–—:·")
    return text or (name or "Tournament").strip(), stage


def normalise(text: str) -> str:
    """Lowercase, unaccented, punctuation-free: a key, not a label."""
    accents = str.maketrans("àâäáãçéèêëíìîïñóòôöõúùûüýÿ", "aaaaaceeeeiiiinooooouuuuyy")
    text = (text or "").lower().translate(accents)
    return re.sub(r"[^a-z0-9]+", "", text)


def series_of(comp) -> str:
    """The recurring cup, free of season, region and edition.

    Prefers the key the harvester read out of Epic's own event id, because that
    is stable across renames and seasons. Falls back to the family name for
    anything typed by hand.
    """
    if isinstance(comp, dict):
        key = (comp.get("series_key") or "").strip()
        if key:
            return key
        family = (comp.get("family") or "").strip()
        if not family:
            family = split_name(comp.get("name") or "")[0]
        return normalise(family)
    return normalise(split_name(comp)[0])


def round_of(comp) -> int:
    """Which session inside one running of the cup. 0 when there is only one.

    A weekly cup played in a single session has no round, whatever the stage
    label says: "Round 1" and "Qualifier" both mean "the one session there is",
    and treating them as different rounds splits a category in half.
    """
    if not isinstance(comp, dict):
        return 0
    stored = comp.get("round_no")
    if stored:
        # A stored 1 collapses like a written "Round 1" does. Rows harvested
        # before that rule reached the reader still carry it, and re-deriving
        # them is a rebuild nobody should have to run to get their categories
        # joined back up.
        return 0 if int(stored) <= 1 else int(stored)
    if comp.get("source") == "osirion":
        # A harvested row's round came out of Epic's window id, the one field
        # that says which session this is. Its stage *label* used to be read
        # from Epic's `round` number, which is a week counter: "Week 2 Day 1"
        # carried round 2 and was filed as a second round, one category per
        # week. So for these rows the stored number is the whole truth and the
        # label is not consulted — a zero means the cup has no rounds.
        return 0
    stage = (comp.get("stage") or "").strip().lower()
    found = re.search(r"(?:round|manche|day|jour|week|semaine)\s*(\d+)", stage)
    if found:
        number = int(found.group(1))
        return 0 if number <= 1 else number
    if any(word in stage for word in ("final", "finale")):
        return 9
    if any(word in stage for word in ("semi", "demi")):
        return 8
    return 0


ROUND_LABELS = {0: "", 8: "Semi-final", 9: "Final"}


def round_label(number: int) -> str:
    if number in ROUND_LABELS:
        return ROUND_LABELS[number]
    return f"Round {number}"


def category_key(comp) -> tuple:
    """What makes two tournaments the same thing for the model.

    Series, round, region and both modes; seasons deliberately merged, so a cup
    keeps accumulating editions across the year instead of restarting from one
    every time Epic bumps the season number.
    """
    if not isinstance(comp, dict):
        return (series_of(comp), 0, "", "", "")
    return (series_of(comp), round_of(comp), comp.get("region") or "",
            comp.get("team_mode") or "", comp.get("game_mode") or "")


def category_of(comp) -> str:
    """The same grouping, written for a person to read."""
    if isinstance(comp, dict):
        family = (comp.get("family") or "").strip()
        if not family:
            family = split_name(comp.get("name") or "")[0]
        stage = round_label(round_of(comp))
    else:
        family, auto = split_name(comp)
        stage = round_label(round_of({"stage": auto}))
    return f"{family} · {stage}" if stage else family


NOISE_WORDS = {"cup", "cups", "tournament", "tournoi", "official", "the", "de",
               "du", "des", "la", "le", "les", "event", "series", "serie"}


def key_words(text: str) -> list[str]:
    """The words of a name that carry meaning, folded for comparison."""
    accents = str.maketrans("àâäáãçéèêëíìîïñóòôöõúùûüýÿ", "aaaaaceeeeiiiinooooouuuuyy")
    folded = (text or "").lower().translate(accents)
    # "div2" is two words typed as one; splitting letters from digits is what
    # makes a search box forgiving without making it vague.
    folded = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", folded)
    return [w for w in re.split(r"[^a-z0-9]+", folded) if w and w not in NOISE_WORDS]


def match_score(needle: str, haystack: str) -> float:
    """How well a name answers a search. 0 means it does not.

    Every word typed has to appear, but a prefix counts, so "fncs div2" finds
    "FNCS Division 2". Digits must match whole - division 2 is not division 20.
    The score is what puts the best answer first: a name that is mostly the
    words you typed beats one that merely contains them somewhere.
    """
    wanted, have = key_words(needle), key_words(haystack)
    if not wanted:
        return 1.0
    if not have:
        return 0.0
    hit = 0
    for word in wanted:
        if word.isdigit():
            if word not in have:
                return 0.0
        elif not any(w.startswith(word) or (word.startswith(w) and len(w) >= 3) for w in have):
            return 0.0
        hit += 1
    # Reward names that say little beyond what was asked for.
    return hit / len(have)


def search_competitions(conn, text: str = "", regions=None, team_modes=None,
                        game_modes=None, tags=None, season: str = "",
                        since: str = "", until: str = "", state: str = "",
                        limit: int = 400) -> list[dict]:
    """The history, narrowed the way a person would narrow it.

    Everything is optional and everything combines. The text search is
    deliberately forgiving, because nobody types a tournament's full name.
    """
    rows = [_row_to_comp(r) for r in conn.execute(
        "SELECT * FROM competition ORDER BY start_time DESC, id DESC")]
    out = []
    for comp in rows:
        if regions and comp["region"] not in regions:
            continue
        if team_modes and comp["team_mode"] not in team_modes:
            continue
        if game_modes and comp["game_mode"] not in game_modes:
            continue
        if season and (comp.get("season") or "").upper() != season.upper():
            continue
        if since and (comp["start_time"] or "") < since:
            continue
        if until and (comp["start_time"] or "") > until + " 23:59":
            continue
        if tags:
            carried = set(json.loads(comp.get("tags") or "[]"))
            if not carried.issuperset(tags):
                continue
        score = 1.0
        if text:
            score = max(match_score(text, comp["name"]),
                        match_score(text, comp.get("family") or ""))
            if score <= 0:
                continue
        comp["match"] = round(score, 3)
        if state:
            finals = conn.execute("SELECT COUNT(*) c FROM final_result "
                                  "WHERE competition_id = ?", (comp["id"],)).fetchone()["c"]
            if state == "finished" and not comp["finished"]:
                continue
            if state == "tracked" and not comp.get("tracking"):
                continue
            if state == "no_scoring" and comp["scoring_known"]:
                continue
            if state == "no_field" and comp.get("field_size"):
                continue
            if state == "no_thresholds" and finals:
                continue
        out.append(comp)
    if text:
        out.sort(key=lambda c: (-c["match"], c["start_time"] or ""), reverse=False)
    return out[:limit]


def find_duplicates(conn) -> list[list[dict]]:
    """Competitions that look like the same session entered twice.

    Same category and same hour is not enough on its own: an FNCS day runs
    several sessions that share both, and merging them would throw away real
    observations. What settles it is the results - a genuine re-entry has the
    same thresholds, a second session does not.
    """
    groups: dict[tuple, list[dict]] = {}
    for comp in list_competitions(conn):
        key = category_key(comp) + ((comp["start_time"] or "")[:13],)
        groups.setdefault(key, []).append(comp)

    duplicates = []
    for members in groups.values():
        if len(members) < 2:
            continue
        results = {c["id"]: final_points(conn, c["id"]) for c in members}
        for index, first in enumerate(members):
            for second in members[index + 1:]:
                shared = set(results[first["id"]]) & set(results[second["id"]])
                if not shared:
                    continue
                same = all(abs(results[first["id"]][r] - results[second["id"]][r])
                           <= 0.01 * max(results[first["id"]][r], 1) for r in shared)
                if same:
                    duplicates.append(sorted([first, second], key=lambda c: c["id"]))
    return duplicates


def facets(conn) -> dict:
    """What there is to filter on, counted, so the page can offer only what exists."""
    counted: dict[str, dict[str, int]] = {"region": {}, "team_mode": {}, "game_mode": {},
                                          "season": {}, "tag": {}}
    for comp in list_competitions(conn):
        for field in ("region", "team_mode", "game_mode", "season"):
            value = comp.get(field) or ""
            if value:
                counted[field][value] = counted[field].get(value, 0) + 1
        for tag in json.loads(comp.get("tags") or "[]"):
            counted["tag"][tag] = counted["tag"].get(tag, 0) + 1
    return {name: dict(sorted(values.items(), key=lambda kv: (-kv[1], kv[0])))
            for name, values in counted.items()}


def get_objectives(conn, kind: str, region: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, label, rank FROM objective WHERE kind = ? AND region = ? ORDER BY rank",
        (kind, region)).fetchall()
    return [dict(r) for r in rows]


def set_objectives(conn, kind: str, region: str, items: list[dict]) -> None:
    conn.execute("DELETE FROM objective WHERE kind = ? AND region = ?", (kind, region))
    for item in items:
        rank = item.get("rank")
        # A rank with no name is still useful: it is a tier to track, even when
        # you do not yet know what it pays.
        label = (item.get("label") or "").strip() or "Threshold"
        if not rank:
            continue
        conn.execute("INSERT INTO objective (kind, region, label, rank) VALUES (?,?,?,?)",
                     (kind, region, label, int(rank)))


def objectives_or_default(conn, kind: str, region: str) -> list[dict]:
    """The tiers defined for this category. Empty until something is entered."""
    found = get_objectives(conn, kind, region)
    if found or not DEFAULT_OBJECTIVES:
        return found
    set_objectives(conn, kind, region,
                   [{"label": label, "rank": rank} for label, rank in DEFAULT_OBJECTIVES])
    return get_objectives(conn, kind, region)


# --------------------------------------------------------------------------- #
# Favourites: teams or players followed from one tournament to the next
# --------------------------------------------------------------------------- #
def list_favourites(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, note FROM favourite ORDER BY name COLLATE NOCASE")]


def add_favourite(conn, name: str, note: str = "") -> dict | None:
    """A favourite is held by nickname: it follows the player from one edition to
    the next, even when they switch teammate."""
    name = (name or "").strip()
    if not name:
        return None
    # Both the insert and the lookup ignore case, so "Ninja" and "ninja" are one
    # favourite. Removing one used to remove both while adding kept them apart.
    existing = conn.execute("SELECT id, name, note FROM favourite "
                            "WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if existing:
        return dict(existing)
    conn.execute("INSERT OR IGNORE INTO favourite (name, note, created_at) VALUES (?,?,?)",
                 (name, note or "", _now()))
    row = conn.execute("SELECT id, name, note FROM favourite WHERE name = ? COLLATE NOCASE",
                       (name,)).fetchone()
    return dict(row) if row else None


def remove_favourite(conn, name: str) -> None:
    conn.execute("DELETE FROM favourite WHERE name = ? COLLATE NOCASE", ((name or "").strip(),))


def favourite_rows(conn, comp_id: int) -> list[dict]:
    """Standings rows that contain a favourite nickname."""
    names = [f["name"].lower() for f in list_favourites(conn)]
    if not names:
        return []
    out = []
    for row in conn.execute(
            "SELECT rank, name, members, score, kills, games FROM standing "
            "WHERE competition_id = ? ORDER BY rank", (comp_id,)):
        haystack = f"{row['name']} {row['members']}".lower()
        hits = [n for n in names if n in haystack]
        if hits:
            item = dict(row)
            item["matched"] = hits
            out.append(item)
    return out


def all_objectives(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT kind, region, label, rank FROM objective ORDER BY kind, region, rank")]


# --------------------------------------------------------------------------- #
# Competitions imported from the API
# --------------------------------------------------------------------------- #
def find_by_window(conn, event_id: str, window_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM competition WHERE event_id = ? AND window_id = ?",
        (event_id, window_id)).fetchone()
    return _row_to_comp(row) if row else None


def cleanup_unpredicted(conn) -> list[str]:
    """Delete imported tournaments that are over and were never tracked.

    A tournament goes only if it came from the API, is finished, was never put
    under prediction and holds no reading: nothing you worked on disappears.
    """
    rows = conn.execute(
        "SELECT c.id, c.name FROM competition c "
        "WHERE c.source = 'cito' AND c.tracking = 0 "
        "  AND c.end_time IS NOT NULL AND c.end_time < ? "
        "  AND NOT EXISTS (SELECT 1 FROM snapshot s WHERE s.competition_id = c.id)",
        (_now(),)).fetchall()
    names = [r["name"] for r in rows]
    for row in rows:
        conn.execute("DELETE FROM competition WHERE id = ?", (row["id"],))
    return names


def known_max_games(conn, kind: str, region: str) -> int:
    """Largest game count already seen on this category of tournament.

    Early in a session teams have only played three or four games, so the
    maximum read off the standings understates the format. Earlier editions of
    the same tournament, on the other hand, were watched to the end.
    """
    best = 0
    # family and stage have to come along: category_of reads them first and
    # only falls back to guessing from the name when they are missing. Select
    # just the name and every confirmed category silently misses.
    for row in conn.execute("SELECT name, family, stage, region, max_games "
                            "FROM competition"):
        if row["region"] != region:
            continue
        if category_of(dict(row)) != kind:
            continue
        best = max(best, int(row["max_games"] or 0))
    return best


# --------------------------------------------------------------------------- #
# Game by game history
# --------------------------------------------------------------------------- #
def set_matches(conn, comp_id: int, rows: list[dict]) -> int:
    """Store the per-game detail of every team in the standings."""
    conn.execute("DELETE FROM team_match WHERE competition_id = ?", (comp_id,))
    payload = []
    for row in rows:
        team = row.get("team_id") or f"r{row.get('rank')}"
        for game in row.get("sessions") or []:
            number = game.get("matchNumber")
            if not number:
                continue
            payload.append((
                comp_id, team, int(row["rank"]), int(number),
                game.get("startTime"), game.get("placement"),
                game.get("kills"), game.get("points"), game.get("timeAlive"),
            ))
    conn.executemany(
        "INSERT OR REPLACE INTO team_match (competition_id, team_id, rank, match_number, "
        "started_at, placement, kills, points, time_alive) VALUES (?,?,?,?,?,?,?,?,?)",
        payload)
    return len(payload)


def get_team_matches(conn, comp_id: int, rank: int) -> list[dict]:
    rows = conn.execute(
        "SELECT match_number, started_at, placement, kills, points, time_alive "
        "FROM team_match WHERE competition_id = ? AND rank = ? ORDER BY match_number",
        (comp_id, rank)).fetchall()
    return [dict(r) for r in rows]


def match_stats(conn, comp_id: int) -> dict:
    """Overview: how many games are stored, and the pace game by game."""
    row = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT team_id) teams, MAX(match_number) last_match "
        "FROM team_match WHERE competition_id = ?", (comp_id,)).fetchone()
    per_match = [dict(r) for r in conn.execute(
        "SELECT match_number, COUNT(*) teams, ROUND(AVG(points), 1) avg_points, "
        "       ROUND(AVG(kills), 2) avg_kills "
        "FROM team_match WHERE competition_id = ? GROUP BY match_number ORDER BY match_number",
        (comp_id,))]
    return {"total": row["n"], "teams": row["teams"], "last_match": row["last_match"],
            "per_match": per_match}


# --------------------------------------------------------------------------- #
# Opening estimate (before any reading)
# --------------------------------------------------------------------------- #
def set_first_estimate(conn, comp_id: int, items: dict, n_comps: int, scope: str) -> None:
    """Record the cold estimate. Never overwrite it once it is set."""
    if conn.execute("SELECT 1 FROM first_estimate WHERE competition_id = ? LIMIT 1",
                    (comp_id,)).fetchone():
        return
    now = _now()
    for rank, value in items.items():
        if not value or not value.get("ok"):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO first_estimate "
            "(competition_id, rank, value, low, high, created_at, n_comps, scope) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (comp_id, int(rank), float(value["value"]), value.get("low"),
             value.get("high"), now, int(n_comps), scope))


def get_first_estimate(conn, comp_id: int) -> dict:
    rows = conn.execute(
        "SELECT rank, value, low, high, created_at, n_comps, scope "
        "FROM first_estimate WHERE competition_id = ? ORDER BY rank", (comp_id,)).fetchall()
    if not rows:
        return {}
    return {
        "created_at": rows[0]["created_at"],
        "n_comps": rows[0]["n_comps"],
        "scope": rows[0]["scope"],
        "ranks": {int(r["rank"]): {"value": r["value"], "low": r["low"], "high": r["high"]}
                  for r in rows},
    }


def first_estimate_scores(conn) -> list[dict]:
    """Accuracy of the cold estimates, tournament by tournament.

    This is the measurement that says whether forecasting a tournament nobody
    has played yet is getting any better.
    """
    out = []
    for comp in list_competitions(conn):
        est = get_first_estimate(conn, comp["id"])
        if not est:
            continue
        truth = final_points(conn, comp["id"])
        errors = []
        for rank, guess in est["ranks"].items():
            real = truth.get(rank)
            if real and guess["value"]:
                errors.append(abs(guess["value"] - real) / real * 100)
        if not errors:
            continue
        out.append({
            "id": comp["id"], "name": comp["name"], "region": comp["region"],
            "kind": category_of(comp), "date": comp["start_time"],
            "n_comps": est["n_comps"], "scope": est["scope"],
            "error_pct": round(sum(errors) / len(errors), 1),
            "n_ranks": len(errors),
        })
    out.sort(key=lambda d: d["date"])
    return out


# --------------------------------------------------------------------------- #
# History entered by hand (training material for the cold estimate)
# --------------------------------------------------------------------------- #
def create_history_entry(conn, kind: str, region: str, date: str, values: dict,
                         max_games: int, scoring: dict, team_mode: str = "Duo",
                         game_mode: str = "Battle Royale", label: str = "",
                         duration_min: float = 180, stage: str = "",
                         field_size: int | None = None) -> int:
    """Create a past tournament from its final thresholds alone.

    That is all the calibration needs to learn the relation between a scoring
    and the thresholds it produces; the detail of the readings adds nothing.
    """
    start = datetime.fromisoformat(norm_ts(date)).replace(hour=19, minute=0, second=0)
    end = start + timedelta(minutes=float(duration_min or 180))
    ranks = sorted(int(r) for r in values)
    # The name has to carry the category, otherwise category_of() cannot find it
    # again: "Week 1" on its own would become a category of its own.
    label = (label or "").strip()
    name = f"{kind} — {label}" if label else kind
    comp_id = create_competition(
        conn, name=name, region=region, team_mode=team_mode,
        game_mode=game_mode, start_time=start.isoformat(sep=" "),
        end_time=end.isoformat(sep=" "), ranks=ranks or DEFAULT_RANKS,
        max_games=max_games, scoring=scoring,
        notes="entered by hand from Fortnite Tracker",
    )
    # "history" is the stored source value for a hand-entered tournament.
    update_competition(conn, comp_id, source="history", tracking=1,
                       family=kind, stage=stage, field_size=field_size,
                       finished_at=end.isoformat(sep=" "))
    # one reading only, placed at the end: it carries the definitive thresholds
    add_snapshot(conn, comp_id, ts=end.isoformat(sep=" "),
                 points={int(r): float(v) for r, v in values.items()},
                 note="final thresholds entered")
    set_finals(conn, comp_id, {int(r): float(v) for r, v in values.items()})
    return comp_id


def apply_category_scoring(conn, family: str, stage: str, region: str,
                           scoring: dict, overwrite: bool = False,
                           scoring_id: int | None = None) -> int:
    """Give one scoring to every tournament in a category.

    Imported tournaments arrive without a scoring — the API does not publish it.
    Yet a tournament whose scoring and threshold curve are both known is the only
    kind that lets the relation between the two be measured. Fill in a category's
    scoring once and its thirty editions become thirty measurements.
    """
    family = (family or "").strip()
    stage = (stage or "").strip()
    touched = 0
    for row in conn.execute("SELECT id, family, stage, region, scoring FROM competition"):
        if (row["family"] or "").strip() != family:
            continue
        if stage and (row["stage"] or "").strip() != stage:
            continue
        if region and row["region"] != region:
            continue
        if row["scoring"] and not overwrite:
            continue
        update_competition(conn, row["id"], scoring=scoring)
        if scoring_id:
            # keep the link to the named scoring so it can be shown
            conn.execute("UPDATE competition SET scoring_id = ? WHERE id = ?",
                         (int(scoring_id), row["id"]))
        touched += 1
    return touched


def find_duplicate(conn, family: str, stage: str, region: str, date: str,
                   exclude_id: int | None = None) -> dict | None:
    """An edition already entered on the same day, same category, same region.

    Forgetting to change the date is the easiest mistake to make when entering
    several editions in a row — and the most expensive: the same tournament
    twice counts twice in the calibration.
    """
    day = norm_ts(date)[:10] if date else None
    if not day:
        return None
    family = (family or "").strip()
    stage = (stage or "").strip()
    for row in conn.execute("SELECT * FROM competition WHERE substr(start_time,1,10) = ?",
                            (day,)):
        if exclude_id and row["id"] == exclude_id:
            continue
        if (row["family"] or "").strip() != family:
            continue
        if (row["stage"] or "").strip() != stage:
            continue
        if row["region"] != region:
            continue
        return _row_to_comp(row)
    return None


def list_manual_entries(conn, family: str = "", stage: str = "",
                        region: str = "") -> list[dict]:
    """Editions entered by hand, the ones the corrections act on."""
    out = []
    # 'history' and 'manual' are the stored source values, kept as they are.
    for row in conn.execute("SELECT * FROM competition WHERE source IN ('history','manual') "
                            "ORDER BY start_time DESC"):
        comp = _row_to_comp(row)
        if family and (comp["family"] or "").strip() != family.strip():
            continue
        if stage and (comp["stage"] or "").strip() != stage.strip():
            continue
        if region and comp["region"] != region:
            continue
        thresholds = {int(r["rank"]): r["points"] for r in conn.execute(
            "SELECT rank, points FROM final_result WHERE competition_id = ? ORDER BY rank",
            (comp["id"],))}
        edition = comp["name"].split(" — ", 1)[1] if " — " in comp["name"] else ""
        # "thresholds" (thresholds) is the key the history and training pages read.
        out.append({"id": comp["id"], "name": comp["name"], "family": comp["family"],
                    "stage": comp["stage"], "region": comp["region"],
                    "date": comp["start_time"][:10], "edition": edition,
                    "field_size": comp["field_size"], "max_games": comp["max_games"],
                    "team_mode": comp["team_mode"], "game_mode": comp["game_mode"],
                    "kind": comp["kind"], "thresholds": thresholds})
    return out


def update_manual_entry(conn, comp_id: int, date=None, edition=None, field_size=None,
                        max_games=None, thresholds=None) -> None:
    """Fix an entry: its date, its label, its field size, its thresholds."""
    comp = get_competition(conn, comp_id)
    if comp is None:
        return
    fields = {}
    if date:
        start = datetime.fromisoformat(norm_ts(date)).replace(hour=19, minute=0, second=0)
        duration = 180.0
        if comp["end_time"]:
            duration = (datetime.fromisoformat(comp["end_time"])
                        - datetime.fromisoformat(comp["start_time"])).total_seconds() / 60
        fields["start_time"] = start.isoformat(sep=" ")
        fields["end_time"] = (start + timedelta(minutes=duration)).isoformat(sep=" ")
        fields["finished_at"] = fields["end_time"]
    if edition is not None:
        base = (comp["family"] or comp["name"]).strip()
        fields["name"] = f"{base} — {edition.strip()}" if edition.strip() else base
    if field_size is not None:
        fields["field_size"] = int(field_size or 0) or None
    if max_games:
        fields["max_games"] = int(max_games)
    if fields:
        update_competition(conn, comp_id, **fields)
    if thresholds is not None:
        clean = {}
        for rank, value in thresholds.items():
            if str(value).strip() in ("", "None"):
                continue
            try:
                clean[int(rank)] = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"Threshold for rank {rank} must be a number, got {value!r}")
        conn.execute("DELETE FROM final_result WHERE competition_id = ?", (comp_id,))
        set_finals(conn, comp_id, clean)
        # the single reading has to follow the corrected thresholds
        conn.execute("DELETE FROM snapshot WHERE competition_id = ?", (comp_id,))
        end = fields.get("end_time") or comp["end_time"] or comp["start_time"]
        add_snapshot(conn, comp_id, ts=end, points=clean, note="thresholds corrected")
        update_competition(conn, comp_id, ranks=sorted(clean) or DEFAULT_RANKS)


def apply_category_field(conn, family: str, stage: str, region: str,
                         field_size: int, overwrite: bool = False) -> int:
    """Give the same field size to a whole category.

    Entering it edition by edition would be absurd when twelve editions of the
    same cup share a region, and so roughly the same number of teams.
    """
    family = (family or "").strip()
    stage = (stage or "").strip()
    touched = 0
    for row in conn.execute("SELECT id, family, stage, region, field_size FROM competition"):
        if (row["family"] or "").strip() != family:
            continue
        if stage and (row["stage"] or "").strip() != stage:
            continue
        if region and row["region"] != region:
            continue
        if row["field_size"] and not overwrite:
            continue
        update_competition(conn, row["id"], field_size=field_size)
        touched += 1
    return touched


def all_full(conn) -> list[dict]:
    """Every competition, loaded in full.

    The broad base for what does not depend on the category: the shape of the
    threshold curve, and a winner's pace per game mode.
    """
    out = []
    for row in conn.execute("SELECT id FROM competition"):
        comp = get_competition_full(conn, row["id"])
        if comp:
            out.append(comp)
    return out


def known_categories(conn) -> list[dict]:
    """Categories already known, with their region, scoring and game count."""
    seen = {}
    for row in conn.execute(
            "SELECT name, family, stage, region, max_games, scoring, team_mode, game_mode, "
            "       field_size, source FROM competition ORDER BY id DESC"):
        kind = category_of(dict(row))
        key = (kind, row["region"])
        if key in seen:
            seen[key]["n"] += 1
            continue
        scoring = json.loads(row["scoring"]) if row["scoring"] else None
        seen[key] = {"kind": kind, "family": row["family"] or kind.split(" · ")[0],
                     "stage": row["stage"] or "", "region": row["region"], "n": 1,
                     "field_size": row["field_size"],
                     "max_games": row["max_games"], "scoring": scoring,
                     "team_mode": row["team_mode"], "game_mode": row["game_mode"],
                     "has_scoring": bool(scoring)}
    return sorted(seen.values(), key=lambda d: (d["kind"], d["region"]))
