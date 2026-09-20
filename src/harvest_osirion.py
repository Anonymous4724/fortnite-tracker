"""Build the training set from the free Osirion public API.

Three phases, each restartable, because the whole thing runs for hours:

    catalogue   one call per region: every event, its windows, its scoring rules
    fetch       the standings, page by page, saved to disk as they arrive
    build       turn what is on disk into competitions the model can learn from

Every response is written to `data/osirion/` gzipped and untouched before
anything is derived from it. That costs a few hundred megabytes and buys the
right to change your mind later: the per-game placements, the elimination
counts, the roster of every team and the size of every field are all in there,
and none of it has to be downloaded twice to answer a question this script was
not written to answer.

The standings go wide before they go deep. Reaching the API's maximum depth on
every window is days of downloading, so the fetch runs in passes: the first
covers every window shallowly, later ones come back for more pages. Stop after
any pass and the coverage is even rather than a complete third of the calendar
and nothing of the rest.

    python src/harvest_osirion.py --check          three calls, tells you what it sees
    python src/harvest_osirion.py --catalogue --fetch    download only
    python src/harvest_osirion.py --build          re-derive from what is on disk
    python src/harvest_osirion.py --passes 5,101   shallow everywhere, then as deep
                                               as the API allows

Interrupting is safe. Starting again picks up where it stopped.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone

import db
import osirion

HERE = os.path.dirname(os.path.abspath(__file__))
# The code lives in `src/`, the data and the launchers one level up; a copy
# laid out flat still works, which is what keeps a move from breaking anything.
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "src" else HERE
RAW = os.path.join(ROOT, "data", "osirion")

# Ranks worth recording. Anything past the fourth page of standings is noise for
# a threshold model, and the deep ranks are what the curve extrapolates anyway.
# The cuts a window pays - its qualification and prize ranks, see `cut_ranks` -
# are recorded beside these: they are the ranks people ask the model about.
RANKS = [1, 3, 5, 10, 20, 25, 50, 100, 120, 250, 500, 1000, 2500, 5000, 10000]

# The API refuses a page index above 100, and serves 100 entries a page, so the
# deepest rank anyone can reach through it is 10,100.
PAGE_CAP = 101
PAGE_SIZE = 100


# --------------------------------------------------------------------------- #
# Running for hours, unattended
# --------------------------------------------------------------------------- #
# Windows sleeps a machine nobody has touched, and a sleeping machine has no
# network. Rather than ask the owner to disable sleep and remember to put it
# back, the run declares itself the way a download manager or a video player
# does, and the setting lapses the moment the process ends. The display is
# deliberately left out: the screen can go dark, the download continues.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def machine_awake():
    """Keep the computer from sleeping for as long as this block runs."""
    kernel = None
    if sys.platform == "win32":
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            if not kernel.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                kernel = None
        except Exception:
            kernel = None
    try:
        yield kernel is not None
    finally:
        if kernel is not None:
            kernel.SetThreadExecutionState(ES_CONTINUOUS)


class Tee:
    """Print to the console and to a file at once.

    The console is where you watch it; the file is what you read the morning
    after, once the window has been closed or the scrollback has rolled over.
    """

    def __init__(self, stream, path):
        self.stream = stream
        self.file = open(path, "a", encoding="utf-8", buffering=1)
        self.file.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M} =====\n")

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()


# --------------------------------------------------------------------------- #
# Files on disk
# --------------------------------------------------------------------------- #
def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "")[:120]


def raw_path(*parts: str) -> str:
    path = os.path.join(RAW, *[slug(p) for p in parts])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def save(path: str, payload) -> None:
    """Write the page whole or not at all.

    `refresh.py --build` may be reading this folder while a fetch is still
    running; a page that appears on disk half-written would be read as a
    truncated board and derive a wrong competition. The rename is atomic.
    """
    temp = path + ".part"
    with gzip.open(temp, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temp, path)


def load(path: str):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- #
# Phase 1 — the calendar
# --------------------------------------------------------------------------- #
def catalogue(regions: list[str]) -> dict:
    """One call per region. Cheap, so it always runs fresh."""
    events: dict[str, dict] = {}
    for region in regions:
        try:
            found = osirion.tournaments(region=region, historic=True)
        except osirion.OsirionError as exc:
            print(f"  {region:<7} failed: {exc}")
            continue
        save(raw_path("catalogue", f"{region}.json.gz"), found)
        for event in found:
            key = event.get("eventId")
            if key:
                events.setdefault(key, event)
        print(f"  {region:<7} {len(found):>4} events")
    save(raw_path("catalogue", "all.json.gz"), list(events.values()))
    return events


def windows_of(events: dict, from_year: int) -> list[dict]:
    """Flatten to a work list: one entry per (event, window) worth fetching."""
    work = []
    for event in events.values():
        for window in event.get("eventWindows") or []:
            begin = window.get("beginTime") or ""
            if not begin[:4].isdigit() or int(begin[:4]) < from_year:
                continue
            window_id = (window.get("eventWindowId") or window.get("windowId")
                         or window.get("id"))
            if not window_id:
                continue
            work.append({"event_id": event["eventId"], "window_id": window_id,
                         "begin": begin, "end": window.get("endTime") or "",
                         "round": window.get("round"), "event": event,
                         "window": window})
    # Newest first. The whole calendar is days of downloading, so whatever an
    # interrupted run did reach should be the part the model wants most: this
    # season's formats, not a cup that stopped running eighteen months ago.
    work.sort(key=lambda w: w["begin"], reverse=True)
    return work


# --------------------------------------------------------------------------- #
# Phase 2 — the standings
# --------------------------------------------------------------------------- #
def page_path(work: dict, page: int) -> str:
    return raw_path("leaderboards", work["event_id"],
                    f"{slug(work['window_id'])}_p{page:03d}.json.gz")


def read_page(work: dict, page: int):
    """A saved page, or None when it was never fetched or cannot be read."""
    path = page_path(work, page)
    if not os.path.exists(path):
        return None
    try:
        return load(path)
    except (OSError, ValueError, EOFError):
        # EOFError is what gzip raises for a file cut short — not an OSError.
        return None


def known_pages(work: dict):
    """How many pages this window has, if page 0 is already on disk."""
    first = read_page(work, 0)
    return osirion.total_pages(first) if first is not None else None


def cut_ranks(work: dict) -> list[int]:
    """The ranks this window is played for: its qualification and prize cuts.

    A first pass three pages deep records the top 250 and never reaches a
    cut at four thousand, and the deeper passes take days to come round. The
    model then read that rank off whichever older edition happened to hold
    it - last season's, hand-typed - while last week's edition sat in the
    database without it. One more page per cut, fetched with the first pass,
    is what puts the rank people actually ask about in the latest edition.
    """
    ranks = set()
    for tier in osirion.payout_tiers(work.get("window") or {}, work.get("event_id") or ""):
        rank = tier.get("rank")
        if isinstance(rank, int) and 0 < rank <= PAGE_CAP * PAGE_SIZE:
            ranks.add(rank)
    return sorted(ranks)


def cut_pages(work: dict, total) -> list[int]:
    """The pages the cuts fall on, within the board and the API's reach."""
    limit = min(PAGE_CAP, int(total)) if total else PAGE_CAP
    pages = []
    for rank in cut_ranks(work):
        page = (rank - 1) // PAGE_SIZE
        if page < limit and page not in pages:
            pages.append(page)
    return pages


# A board settles a few minutes after its window closes; before that it is
# still collecting the games that were under way.
SETTLE_MINUTES = 30


def too_early(work: dict) -> bool:
    """Was this window read before its cup had finished, and is it over now?

    Read before it starts, the API answers with an empty board and
    `totalPages: 0`; read while it runs, with a board that is still moving.
    Either one, left on disk, is read by every later pass as "this window is
    done": the cup is never harvested, its result never enters the database,
    and the live feed's readings have nothing to attach themselves to. So a
    page written before the window's end - plus the minutes the board takes to
    settle - is not an answer, and the window is read again from page zero.

    Both halves matter. A cup still to come has nothing to say either, and the
    calendar runs weeks ahead: asking it again every night would be hundreds
    of calls a run for an empty board we can predict. Its empty page stays on
    disk until the cup has actually been played.
    """
    end = str(work.get("end") or "")[:19].replace(" ", "T")
    if len(end) < 16:
        return False
    try:
        closed = datetime.strptime(end[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    settled = closed.timestamp() + SETTLE_MINUTES * 60
    if time.time() < settled:
        return False
    try:
        written = os.path.getmtime(page_path(work, 0))
    except OSError:
        return False
    return written < settled


def fetch_window(work: dict, pages: int) -> int:
    """Download the pages we are missing, and no more than the window has.

    The response says how many pages there are, so a window with three of them
    costs three calls rather than three plus an empty probe - and a later,
    deeper pass reads that count off the disk and skips the window without
    asking at all.
    """
    calls, page = 0, 0
    limit = min(pages, PAGE_CAP)
    # Pages taken before the cup was over say nothing about how it ended: the
    # window is read again, from page zero, and what is on disk is ignored.
    stale = too_early(work)
    settled = None if stale else known_pages(work)
    if settled is not None:
        limit = min(limit, settled)

    while page < limit:
        saved = None if stale else read_page(work, page)
        if saved is None:
            try:
                saved = osirion.leaderboard_page(work["event_id"], work["window_id"], page)
            except osirion.OsirionError as exc:
                # A window that never ran 404s on page 0. Note it so no future
                # pass asks again.
                save(page_path(work, page), {"error": str(exc), "leaderboard":
                                             {"entries": [], "totalPages": page}})
                return calls + 1
            save(page_path(work, page), saved)
            calls += 1
        settled = osirion.total_pages(saved)
        if settled is not None:
            limit = min(limit, settled)
        if not osirion.entries_of(saved):
            break
        page += 1
    # The cuts' pages, past the ones just read: one request per cut, on the
    # first pass, so the latest edition carries the rank it was played for.
    for number in cut_pages(work, settled):
        if number < page or (not stale and read_page(work, number) is not None):
            continue
        try:
            saved = osirion.leaderboard_page(work["event_id"], work["window_id"], number)
        except osirion.OsirionError:
            continue
        save(page_path(work, number), saved)
        calls += 1
    return calls


# The newest windows are read deeper than the pass asks: the boards the
# replay of a cup nobody has seen is made of (see rescore.py) are the most
# recent ones of its format, and a replay is trusted a third as deep as the
# board was loaded, so ten pages puts rank 250 within reach where three put
# rank 100. Seven more pages for each of the week's sixty-odd windows is a
# few minutes at the API's rate, once.
RECENT_DAYS = 21
RECENT_PAGES = 10


def recent(work: dict, days: int = RECENT_DAYS) -> bool:
    """Did this window begin inside the last `days`?"""
    begin = str(work.get("begin") or "")[:19].replace(" ", "T")
    if len(begin) < 16:
        return False
    try:
        started = datetime.strptime(begin[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return time.time() - started.timestamp() <= days * 86400


def fetch(work_list: list[dict], depths: list[int], recent_pages: int = RECENT_PAGES,
          recent_days: int = RECENT_DAYS) -> None:
    """Breadth first, then depth.

    Reaching the API's maximum depth on every window is days of downloading, so
    the passes go wide before they go deep: after the first one every window
    has its top few hundred ranks, which is already enough to rebuild the
    model. Later passes only fetch the pages the earlier ones did not, so
    stopping after any pass leaves a usable, evenly covered set rather than a
    complete third of the calendar and nothing of the rest. The newest
    windows go `recent_pages` deep on every pass, see RECENT_PAGES.
    """
    for number, depth in enumerate(depths, 1):
        print(f"\n  pass {number}/{len(depths)} — up to {depth} pages a window"
              + (f", {recent_pages} for the last {recent_days} days" if recent_pages > depth else ""))
        started, done, skipped = time.monotonic(), 0, 0
        for index, work in enumerate(work_list, 1):
            calls = fetch_window(work, max(depth, recent_pages) if recent(work, recent_days) else depth)
            done += calls
            skipped += calls == 0
            if index % 25 == 0 or index == len(work_list):
                elapsed = time.monotonic() - started
                rate = done / elapsed if elapsed else 0
                eta = (len(work_list) - index) * (done / index) / rate / 60 if rate else 0
                print(f"    {index:>5}/{len(work_list)} windows · {done} calls · "
                      f"{rate * 60:.0f}/min · about {eta:.0f} min left in this pass")
        print(f"    pass done: {done} calls, {skipped} windows already complete")


# --------------------------------------------------------------------------- #
# Phase 3 — competitions the model can read
# --------------------------------------------------------------------------- #
def index_window(work: dict, entries: list[dict], payload: dict | None) -> dict:
    """Everything the raw pages say about a window that is not a threshold.

    Written next to the pages and never read by the model. It exists because
    the interesting questions come later — how many teams entered a cup, which
    regions grew, whether console-only events draw differently — and none of
    them are worth a second download.
    """
    event, window = work["event"], work["window"]
    countries = {}
    for entry in entries:
        for player in entry.get("players") or []:
            flag = (player.get("flagToken") or "").rsplit("_", 1)[-1]
            if flag:
                countries[flag] = countries.get(flag, 0) + 1
    return {
        "event_id": work["event_id"], "window_id": work["window_id"],
        "series": osirion.series_key(work["event_id"]),
        "season": osirion.season_of(work["event_id"]),
        "occurrence": osirion.occurrence_of(work["window_id"]),
        "round": osirion.round_of(work["window_id"]),
        "begin": work["begin"], "end": work["end"],
        "title": family_of(event),
        "regions": osirion.event_regions(event),
        "platforms": event.get("platforms") or [],
        "tags": osirion.tags(event, window),
        "team_mode": osirion.team_mode(event, entries, window),
        "match_cap": osirion.match_cap(window),
        "playlist": window.get("playlistId") or "",
        "game_mode": osirion.game_mode(event),
        "entries_seen": len(entries),
        # What this derivation read; a later build derives the window again
        # when more pages are on disk than this.
        "pages_seen": pages_on_disk(work),
        "cuts": cut_ranks(work),
        "deepest_rank": max((e.get("rank") or 0) for e in entries) if entries else 0,
        "total_pages": (payload or {}).get("totalPages"),
        "games": osirion.match_cap(window) or max(
            (len(osirion._sessions(e)) for e in entries[:200]), default=0),
        "players_per_team": sorted({len(e.get("players") or []) for e in entries[:50]}),
        "countries": dict(sorted(countries.items(), key=lambda kv: -kv[1])[:30]),
        "payout_tiers": osirion.payout_tiers(window),
    }


def saved_entries(work: dict, pages: int) -> list[dict]:
    """Every standings row on disk for this window, in rank order.

    The pages read in sequence, then the cuts' pages beyond them: those sit
    alone past a gap, and a walk that stopped at the gap would leave the rank
    the cup was played for on disk and out of the database.
    """
    rows, page = [], 0
    for page in range(min(max(pages, 1), PAGE_CAP)):
        payload = read_page(work, page)
        if payload is None:
            break
        found = osirion.entries_of(payload)
        rows += found
        if not found:
            break
    for number in cut_pages(work, known_pages(work)):
        if number <= page:
            continue
        payload = read_page(work, number)
        if payload is not None:
            rows += osirion.entries_of(payload)
    rows.sort(key=lambda e: e.get("rank") or 10 ** 9)
    return rows


def thresholds_of(entries: list[dict], cuts: list[int] | tuple = ()) -> dict[int, float]:
    """Points held by the team sitting exactly on each rank we care about.

    Zero is dropped rather than stored. Deep in a big leaderboard whole blocks of
    teams sit on nothing — they registered and never played — and "you need 0
    points to finish 5000th" is not a threshold anyone can be wrong about. The
    model is multiplicative, so it cannot produce a zero at all; keeping them
    puts a division by zero in every error measurement downstream.

    `cuts` are the window's own ranks - what it qualifies or pays at - kept
    beside the fixed list.
    """
    by_rank = {}
    for entry in entries:
        rank, points = entry.get("rank"), entry.get("pointsEarned")
        if rank and points is not None and float(points) > 0:
            by_rank.setdefault(int(rank), float(points))
    wanted = sorted(set(RANKS) | {int(r) for r in cuts})
    return {r: by_rank[r] for r in wanted if r in by_rank}


def field_of(work: dict, entries: list[dict]) -> int:
    """How many teams the leaderboard ranks — not how many we chose to download.

    This is the number that broke the model. Taking the deepest rank on disk
    makes every tournament exactly as wide as the harvest was deep: run three
    pages and all ten thousand tournaments record a field of 300, whether the
    real event ranked 400 teams or 40,000. The model's only positional variable
    is `q = rank / field`, so a field wrong by a factor of a hundred is a `q`
    wrong by a factor of a hundred, and the fitted shape bends itself around the
    error until it fits nothing.

    The API says how many pages the board has, page 0 carries it, and page 0 is
    always on disk — so the true size is recoverable without another request.
    Only the length of the final page is unknown, which is worth half a page:
    against a thousand teams that is 5 %, and at an elasticity of 0.05 it moves
    a threshold by a quarter of a percent.
    """
    deepest = max((e.get("rank") or 0) for e in entries) if entries else 0
    first = read_page(work, 0)
    total = osirion.total_pages(first) if first is not None else None
    if not total or total <= 0:
        return deepest
    # Measured, not assumed: if Epic changes the page size this follows it.
    per_page = len(osirion.entries_of(first)) if total > 1 else deepest
    if per_page <= 0:
        return deepest
    held = math.ceil(len(entries) / per_page)
    if held >= total:
        return deepest                       # the whole board is on disk
    return max(deepest, (total - 1) * per_page + per_page // 2)


def stage_of(work: dict) -> str:
    """Final, Semi-final, Round 3 — read from the window id, and only from it.

    The id is the one reliable field. Epic's `round` number beside it is a
    week counter, not a stage: `Week2Day1` carries `round: 2`, `Division2_Event3`
    carries `round: 2`, `Finals_Day3` carries `round: 3`, and a survey of the
    catalogue found no window where that number said something the id did not.
    Reading it as a stage filed week 2 of a weekly cup as its "Round 2", one
    category per week, with one edition each — the very split the whole
    classification exists to avoid. A window whose id names no round is the
    only session there is, and its stage is empty.
    """
    window = work["window"]
    return db.round_label(osirion.round_of(work.get("window_id")
                                           or window.get("eventWindowId") or ""))


REGION_TAG = re.compile(r"^\[(?:" + "|".join(osirion.REGIONS) + r")\]\s*|\s*\[(?:"
                        + "|".join(osirion.REGIONS) + r")\]$", re.I)


def family_of(event: dict) -> str:
    """The cup's name: both title lines, because the second is not decoration.

    Epic writes "Solo | Victory Cup", "FNCS | Division 2", "Reload Duos |
    Cash Cup (Console)", "Fortnite | Performance Evaluation". Read alone the
    first line named three divisions "FNCS" and a weekly cup "Fortnite", and
    merged every cup that shares a first word. Joined, the older two-line titles
    even land on the names the newer one-line titles use — "Solo Victory Cup" —
    so a cup keeps its history across the seasons Epic restyled it.
    """
    display = event.get("displayData") or {}
    lines = [str(display.get(key) or "").strip() for key in ("titleLine1", "titleLine2")]
    name = " ".join(line for line in lines if line)
    # A region tag in the title - "[NAW] Solo Victory Cup Battle Royale", which
    # Epic started writing on the North American events in September 2026 -
    # is not part of the cup's name: the region is a column of its own, and a
    # name carrying it filed the NAW edition apart from the six others, then,
    # being the newest, renamed the whole series after it. Only a bracketed
    # region code is stripped: "(BR)" in "The Mandalorian Cup (BR)" is the
    # game mode, and stays.
    name = REGION_TAG.sub("", name).strip()
    if name:
        # Epic ran a Battle Royale and a Zero Build "Override Series" under one
        # title in the same hour; only the id tells them apart, and they are
        # not the same cup. Said in the name when the title leaves it out.
        event_id = event.get("eventId") or ""
        zb_in_id = re.search(r"(?:^|_)zb(?:_|$)|zerobuild|nobuild", event_id, re.I)
        low = name.lower()
        if zb_in_id and not ("zb" in re.split(r"[^a-z]+", low) or "zero build" in low
                             or "no build" in low):
            name += " (Zero Build)"
        return name[:80]
    for key in ("shortDescription", "title", "longFormatTitle"):
        value = display.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return (event.get("eventId") or "Tournament").replace("epicgames_", "")[:80]


# Bumped whenever family_of, stage_of or the naming of a competition changes.
# A build compares it with the stamp the last build left in the database and
# re-derives everything when they differ: an incremental build would otherwise
# file this week's editions under the new names and leave last season's under
# the old, and no category would ever join up again.
RULES_VERSION = "2026-09-13 the cuts' ranks recorded; a renamed series keeps one name"


def rules_changed(conn) -> bool:
    return db.get_setting(conn, "harvest_rules") != RULES_VERSION


def stamp_rules(conn) -> None:
    db.set_setting(conn, "harvest_rules", RULES_VERSION)


def has_pages(work: dict) -> bool:
    """Cheap test: did the fetch ever reach this window?"""
    return os.path.exists(page_path(work, 0))


CANONICAL_WINDOWS = 14


def canonical_names(work_list: list[dict]) -> dict[str, str]:
    """One name per series, the newest edition's: what a renamed cup is filed under.

    Epic renames cups between seasons and keeps the id - "Solo Victory Cup"
    became "Solo Victory Cup Battle Royale" at season 42, "Console Solo Victory
    Cup (ZB)" became "Console Zero Build Solo Victory Cup" - and the model's
    categories are keyed on the name. Filed under the name of the day, the
    first editions of the new season had no history at all, and a cup's past
    stayed under a name nothing on the calendar would ever match again. The
    series key is the identity; the newest name is the label, so the calendar
    the site shows, which carries today's name, finds the whole history.
    """
    # The name most of the series' newest windows carry - two weeks or so of
    # a weekly cup across its regions - the newest breaking a tie. The newest
    # alone was the rule once, and one oddly titled window renamed a thousand
    # rows for a week; a rename Epic means still wins, a week after its
    # schedule is published, since the catalogue runs ahead.
    recent: dict[str, list] = {}
    for work in work_list:                        # newest first
        key = osirion.series_key(work["event_id"])
        if key and len(recent.setdefault(key, [])) < CANONICAL_WINDOWS:
            recent[key].append(family_of(work["event"]))
    names: dict[str, str] = {}
    for key, seen in recent.items():
        counts: dict[str, int] = {}
        for name in seen:
            counts[name] = counts.get(name, 0) + 1
        names[key] = max(seen, key=lambda name: (counts[name], -seen.index(name)))
    return names


def former_names(work_list: list[dict], canonical: dict[str, str]) -> dict[str, str]:
    """Every other name a series has run under, mapped to its newest one."""
    former: dict[str, str] = {}
    for work in work_list:
        key = osirion.series_key(work["event_id"])
        name = family_of(work["event"])
        new = canonical.get(key)
        if new and name != new and name not in canonical.values():
            former[name] = new
    return former


def unify_names(conn, canonical: dict[str, str], former: dict[str, str] | None = None) -> int:
    """File every edition of a series under its newest name; count the renames.

    Harvested rows follow their series key. Hand-typed rows, which have no key,
    follow the name: one typed under a former name of a series moves with it.
    The objectives - the cuts a person tracks per category - move too, so the
    tiers entered under the old name are not left keyed to a name no
    tournament carries any more.
    """
    former = dict(former or {})
    renamed, applied = 0, set()
    rows = conn.execute("SELECT id, family, name, stage, series_key, source FROM competition "
                        "WHERE series_key != ''").fetchall()
    current = set(canonical.values())
    for row in rows:
        new = canonical.get(row["series_key"])
        old = (row["family"] or "").strip()
        if not new or not old or old == new:
            continue
        # A name another series still runs under is not a former name.
        if old not in current:
            former[old] = new
        name = f"{new} - {row['stage']}" if (row["stage"] or "").strip() else new
        conn.execute("UPDATE competition SET family = ?, name = ? WHERE id = ?",
                     (new, name, row["id"]))
        renamed += 1
        applied.add((old, new))
    for old, new in former.items():
        # Rows the harvest did not write carry no key of Epic's - a key the
        # tracker made up from the name, or none - and follow the name.
        for row in conn.execute("SELECT id, stage, series_key FROM competition WHERE family = ?",
                                (old,)).fetchall():
            if row["series_key"] in canonical:
                continue
            name = f"{new} - {row['stage']}" if (row["stage"] or "").strip() else new
            conn.execute("UPDATE competition SET family = ?, name = ? WHERE id = ?",
                         (new, name, row["id"]))
            renamed += 1
            applied.add((old, new))
        # An objective keyed on the old name, or on "old name · stage", follows;
        # one the new name already has keeps the new name's.
        for row in conn.execute("SELECT id, kind, region FROM objective WHERE kind = ? "
                                "OR kind LIKE ?", (old, old + " · %")).fetchall():
            kind = new + row["kind"][len(old):]
            taken = conn.execute("SELECT 1 FROM objective WHERE kind = ? AND region = ? LIMIT 1",
                                 (kind, row["region"])).fetchone()
            if taken:
                conn.execute("DELETE FROM objective WHERE id = ?", (row["id"],))
            else:
                conn.execute("UPDATE objective SET kind = ? WHERE id = ?", (kind, row["id"]))
    if renamed:
        print(f"  {renamed} tournament(s) renamed, to the name Epic now gives the cup:")
        for old, new in sorted(applied):
            print(f"    {old!r} -> {new!r}")
    return renamed


def pages_on_disk(work: dict) -> int:
    """How many pages of this window are on disk: the run from page zero, plus
    the cuts' pages past it. Cheap - it looks, it does not read."""
    count = 0
    while count < PAGE_CAP and os.path.exists(page_path(work, count)):
        count += 1
    for number in cut_pages(work, None):
        if number >= count and os.path.exists(page_path(work, number)):
            count += 1
    return count


def built_pages(work: dict):
    """How many pages the last derivation of this window read, or None when it
    never was derived - or was, by a version that did not note it."""
    path = raw_path("index", work["event_id"], f"{slug(work['window_id'])}.json.gz")
    if not os.path.exists(path):
        return None
    try:
        return int(load(path).get("pages_seen"))
    except (OSError, ValueError, EOFError, AttributeError, TypeError):
        return None


def build(work_list: list[dict], pages: int, limit: int | None = None,
          chunk: int = 200, rebuild: bool = False) -> dict:
    """Write one competition per window, with its measured thresholds.

    A window already in the database is derived again, in place, when more
    of its standings are on disk than the last derivation saw - a deeper pass
    came round, or its cut's page arrived - and on `rebuild` for all of them.
    In place, because a tournament's row is what the live feed's readings are
    filed against: deleting it to write it afresh, as an earlier version did,
    took every reading the feed had made of that cup with it.

    Reports on every window rather than on every write. An earlier version
    printed only when it wrote something, so a run over eleven thousand windows
    whose standings had not been downloaded yet said nothing at all for ten
    minutes and looked hung.

    Committed in chunks, because one transaction around eleven thousand
    tournaments is a long time to hold a lock and a lot to lose to a Ctrl-C.
    """
    counts = {"written": 0, "updated": 0, "already": 0, "no_pages": 0, "too_few": 0,
              "scoring_ok": 0, "scoring_guessed": 0, "renamed": 0}
    todo = work_list[:limit]
    ready = [w for w in todo if has_pages(w)]
    print(f"  {len(todo)} windows, {len(ready)} with standings on disk")
    if not ready:
        counts["no_pages"] = len(todo)
        return counts
    canonical = canonical_names(work_list)

    started = time.monotonic()
    for offset in range(0, len(ready), chunk):
        with db.session() as conn:
            known = {(c["event_id"], c["window_id"]): c["id"]
                     for c in db.list_competitions(conn)
                     if c.get("event_id") and c.get("window_id")}
            for work in ready[offset:offset + chunk]:
                comp_id = known.get((work["event_id"], work["window_id"]))
                if comp_id is not None and not rebuild:
                    seen = built_pages(work)
                    if seen is not None and pages_on_disk(work) <= seen:
                        counts["already"] += 1
                        continue
                entries = saved_entries(work, pages)
                finals = thresholds_of(entries, cut_ranks(work))
                if len(finals) < 3:
                    counts["too_few"] += 1
                    continue
                save(raw_path("index", work["event_id"],
                              f"{slug(work['window_id'])}.json.gz"),
                     index_window(work, entries, None))

                event, window = work["event"], work["window"]
                scoring, agreement, kind = osirion.best_scoring(window, entries)
                if scoring and agreement >= 0.9:
                    counts["scoring_ok"] += 1
                else:
                    scoring, kind = None, "unverified"
                    counts["scoring_guessed"] += 1

                regions = osirion.event_regions(event)
                region = next((r for r in regions if r in db.REGIONS), regions[0][:8])
                # Epic states the game cap; counting sessions only sees what the
                # busiest team actually played, which is the same or fewer.
                games = (osirion.match_cap(window)
                         or max((len(osirion._sessions(e)) for e in entries[:200]), default=0))
                field = field_of(work, entries)

                stage = stage_of(work)
                family = canonical.get(osirion.series_key(work["event_id"])) or family_of(event)
                name = f"{family} - {stage}" if stage else family
                start = work["begin"].replace("T", " ").replace("Z", "")[:16]
                end = work["end"].replace("T", " ").replace("Z", "")[:16] or None
                if comp_id is None:
                    comp_id = db.create_competition(
                        conn, name=name, region=region,
                        team_mode=osirion.team_mode(event, entries, window),
                        game_mode=osirion.game_mode(event),
                        start_time=start, end_time=end,
                        ranks=sorted(finals), max_games=games or None, scoring=scoring,
                        notes=f"osirion · scoring {kind}")
                    counts["written"] += 1
                else:
                    # The same row, derived again: the feed's readings stay
                    # attached, and a rank the feed filed as a final keeps it
                    # unless the standings now hold that rank themselves.
                    held = db.get_finals(conn, comp_id)
                    db.update_competition(
                        conn, comp_id, name=name, region=region,
                        team_mode=osirion.team_mode(event, entries, window),
                        game_mode=osirion.game_mode(event),
                        start_time=start, end_time=end,
                        ranks=sorted(set(finals) | set(held)), max_games=games or None,
                        scoring=scoring, notes=f"osirion · scoring {kind}")
                    counts["updated"] += 1
                db.update_competition(
                    conn, comp_id, source="osirion", family=family,
                    stage=stage, field_size=field or None,
                    entry=osirion.entry_requirement(window),
                    event_id=work["event_id"], window_id=work["window_id"], tracking=1,
                    finished_at=work["end"][:19].replace("T", " ") or None,
                    # The three axes, read out of Epic's ids rather than guessed
                    # from the title, plus the labels a person filters on.
                    series_key=osirion.series_key(work["event_id"]),
                    season=osirion.season_of(work["event_id"]),
                    occurrence=osirion.occurrence_of(work["window_id"]),
                    round_no=osirion.round_of(work["window_id"]),
                    tags=json.dumps(osirion.tags(event, window), ensure_ascii=False))
                db.set_finals(conn, comp_id, finals)

                tiers = osirion.payout_tiers(window, work["event_id"])
                if tiers:
                    db.set_objectives(conn,
                                      db.category_of(db.get_competition(conn, comp_id)),
                                      region, tiers[:12])

        done = min(offset + chunk, len(ready))
        elapsed = time.monotonic() - started
        eta = (len(ready) - done) * elapsed / done / 60 if done else 0
        print(f"    {done:>6}/{len(ready)} · written {counts['written']} · derived again "
              f"{counts['updated']} · already there {counts['already']} · too few "
              f"thresholds {counts['too_few']} · about {eta:.0f} min left")
    with db.session() as conn:
        counts["renamed"] = unify_names(conn, canonical, former_names(work_list, canonical))
    return counts


# --------------------------------------------------------------------------- #
def check(regions: list[str]) -> int:
    """Three calls that answer the questions this script's estimates rest on."""
    print("\nOne catalogue call:")
    events = catalogue(regions[:1])
    if not events:
        print("  nothing came back — the API may be down or the region wrong")
        return 1
    work = windows_of(events, 2000)
    years = {}
    for item in work:
        years[item["begin"][:4]] = years.get(item["begin"][:4], 0) + 1
    print(f"  {len(events)} events, {len(work)} windows")
    print(f"  windows by year: {dict(sorted(years.items()))}")

    if not work:
        return 1
    recent = [w for w in work if w["begin"][:4] >= "2025"] or work
    sample = recent[len(recent) // 2]
    print(f"\nOne leaderboard call on {sample['event_id']} / {sample['window_id']}:")
    payload = osirion.leaderboard_page(sample["event_id"], sample["window_id"], 0)
    entries = osirion.entries_of(payload)
    pages = osirion.total_pages(payload)
    print(f"  {len(entries)} entries on page 0, {pages} page(s) in this window")
    if not entries:
        # The reader once looked for a key the API does not use and found
        # nothing, quietly, for hours. Nothing else in the check matters if
        # this fails, so it stops here rather than printing a tidy report
        # about an empty response.
        print("\n  THE READER FOUND NO ROWS in a response the API answered.")
        print(f"  Top-level keys: {sorted(payload)}")
        print("  The response shape has changed. Fix osirion.board() before")
        print("  downloading anything - a harvest against this would store")
        print("  pages nothing can read.\n")
        return 1
    first = entries[0]
    print(f"  rank 1 has {first.get('pointsEarned')} points over "
          f"{len(osirion._sessions(first))} games")
    scoring, agreement, kind = osirion.best_scoring(sample["window"], entries)
    if scoring:
        print(f"  scoring read as {kind}: kill={scoring['kill']} "
              f"cap={scoring['kill_cap']} tiers={len(scoring['placement'])}")
        print(f"  reproduces {agreement:.0%} of the published totals exactly")
    else:
        print("  no scoring rules on this window")

    print(f"  season {osirion.season_of(sample['event_id']) or '?'} · "
          f"series {osirion.series_key(sample['event_id'])} · "
          f"edition {osirion.occurrence_of(sample['window_id'])} · "
          f"round {osirion.round_of(sample['window_id'])}")
    print(f"  tags: {', '.join(osirion.tags(sample['event'], sample['window'])) or 'none'}")

    per_page = len(entries)
    windows_2025 = len([w for w in work if w["begin"][:4] >= "2025"])
    print(f"\nWhat it would cost, at {per_page} entries a page and {windows_2025} "
          f"windows from 2025 on:")
    print("  (an upper bound: a window stops at its own last page, and this one "
          f"has {pages})")
    for depth in (3, 10, 30, PAGE_CAP):
        calls = windows_2025 * depth
        print(f"  {depth:>3} pages a window (to rank {depth * per_page:>6}): "
              f"up to {calls:>7} calls, about {calls / osirion.LEADERBOARD_PER_MIN / 60:>5.1f} h")
    print("  Money: nothing. This API is free and takes no key.\n")
    return 0


def purge_manual() -> int:
    """Drop the hand-typed history, once the harvested set has replaced it."""
    with db.session() as conn:
        rows = [c for c in db.list_competitions(conn)
                if c.get("source") in ("manual", "history", "import")]
        harvested = len([c for c in db.list_competitions(conn)
                         if c.get("source") == "osirion"])
        print(f"\n  {len(rows)} hand-entered competition(s) would be deleted.")
        print(f"  {harvested} harvested competition(s) are in the database.")
        if harvested < 50:
            print("  Refusing: too few harvested tournaments to replace them.\n")
            return 1
        if input("  Delete the hand-entered ones? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  Cancelled.\n")
            return 0
        for comp in rows:
            db.delete_competition(conn, comp["id"])
    print(f"  {len(rows)} deleted. Manual entry still works — this only cleared "
          f"what was there.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--regions", default=",".join(osirion.REGIONS))
    parser.add_argument("--from-year", type=int, default=2025)
    parser.add_argument("--passes", default="3,10,30,101",
                        help="depths to fetch, shallowest first (API caps at 101)")
    parser.add_argument("--recent-pages", type=int, default=RECENT_PAGES,
                        help=f"pages to read of the newest windows on every pass (default {RECENT_PAGES})")
    parser.add_argument("--recent-days", type=int, default=RECENT_DAYS,
                        help=f"how new a window has to be for that (default {RECENT_DAYS})")
    parser.add_argument("--limit", type=int, help="stop after this many windows")
    parser.add_argument("--log", default=os.path.join(RAW, "harvest.log"),
                        help="where to copy the progress; empty string for none")
    parser.add_argument("--allow-sleep", action="store_true",
                        help="do not keep the computer awake while downloading")
    parser.add_argument("--rate", type=int, default=osirion.LEADERBOARD_PER_MIN,
                        help=f"leaderboard requests a minute, capped at "
                             f"{osirion.LEADERBOARD_LIMIT} by the API")
    parser.add_argument("--only", default="",
                        help="keep windows whose event id or title contains this")
    parser.add_argument("--check", action="store_true", help="three calls, then a report")
    parser.add_argument("--catalogue", action="store_true", help="phase 1 only")
    parser.add_argument("--fetch", action="store_true", help="phase 2 only")
    parser.add_argument("--build", action="store_true", help="phase 3 only")
    parser.add_argument("--rebuild", action="store_true",
                        help="derive every harvested tournament again, in place")
    parser.add_argument("--purge-manual", action="store_true",
                        help="delete hand-entered tournaments once harvesting is done")
    args = parser.parse_args()
    regions = [r.strip().upper() for r in args.regions.split(",") if r.strip()]
    rate = osirion.set_rate(args.rate)

    if args.log:
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        sys.stdout = Tee(sys.stdout, args.log)
        print(f"  progress is also written to {args.log}")

    if args.purge_manual:
        return purge_manual()
    if args.check:
        return check(regions)

    everything = not (args.catalogue or args.fetch or args.build)
    if (args.build or everything) and not args.rebuild:
        with db.session() as conn:
            if rules_changed(conn):
                print("\n  The naming rules have changed since the last build "
                      f"({RULES_VERSION}).\n  Re-deriving every harvested tournament so "
                      "old and new editions file under the same names — about seven "
                      "minutes.")
                args.rebuild = True
    if args.rebuild:
        # In place: the rows stay, and with them the live feed's readings.
        print("\n  Deriving every harvested tournament again, in place.")
        print("  Hand-entered tournaments are untouched.")
        args.build = True
    started = datetime.now()
    keep_awake = contextlib.nullcontext(False) if args.allow_sleep else machine_awake()

    stack = contextlib.ExitStack()
    if stack.enter_context(keep_awake):
        print("  the computer will not fall asleep while this runs")
    elif not args.allow_sleep and sys.platform == "win32":
        print("  could not hold sleep off — set Power & battery > Sleep to Never")

    print("\nCatalogue")
    events_file = raw_path("catalogue", "all.json.gz")
    if everything or args.catalogue or not os.path.exists(events_file):
        events = catalogue(regions)
    else:
        events = {e["eventId"]: e for e in load(events_file) if e.get("eventId")}
        print(f"  {len(events)} events read from disk")
    work = windows_of(events, args.from_year)
    print(f"  {len(work)} windows from {args.from_year} on")
    if args.only:
        needle = args.only.lower()
        work = [w for w in work
                if needle in w["event_id"].lower() or needle in family_of(w["event"]).lower()]
        print(f"  {len(work)} of them match {args.only!r}")
    if args.limit:
        work = work[:args.limit]
        print(f"  limited to the {len(work)} most recent")

    depths = sorted({min(max(int(p), 1), 101)
                     for p in args.passes.split(",") if p.strip().isdigit()})
    recent_pages = min(max(int(args.recent_pages or 0), 0), PAGE_CAP)
    if everything or args.fetch:
        print(f"\nStandings, in {len(depths)} pass(es): {depths}, at {rate} requests a minute")
        fetch(work, depths, recent_pages, max(int(args.recent_days or 0), 0))

    if everything or args.build:
        print("\nBuilding competitions")
        counts = build(work, max(depths + [recent_pages]), limit=args.limit, rebuild=args.rebuild)
        print(f"\n  written {counts['written']}, derived again {counts['updated']}, "
              f"already there {counts['already']}, "
              f"no standings downloaded {counts['no_pages']}, "
              f"too few thresholds {counts['too_few']}, renamed {counts['renamed']}")
        print(f"  scoring verified against the standings {counts['scoring_ok']}, "
              f"unverified {counts['scoring_guessed']}")
        if counts["no_pages"] and not counts["written"]:
            print("\n  Nothing was built because nothing has been downloaded yet.")
            print("  Run the fetch first:  python src/harvest_osirion.py --catalogue --fetch\n")
        elif not args.limit:
            # A complete pass under the current rules: the next build can be
            # incremental again.
            with db.session() as conn:
                stamp_rules(conn)

    stack.close()
    print(f"\nDone in {(datetime.now() - started).seconds // 60} min.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
