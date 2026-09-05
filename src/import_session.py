"""Take an evening followed on the predictor into the database.

The predictor's live panel records one acquisition per game — what the
standings showed at two or three ranks, stamped with the game and the clock —
and "Terminer et sauvegarder" writes them to a file. This reads those files.

    python src/import_session.py session-*.json      # import them
    python src/import_session.py --dry-run FILE      # say what it would do
    python src/import_session.py --list              # what is already tracked

Why it matters more than it looks: the live models are the one part of the
forecast measured on almost nothing. `analysis.validate` counts a tournament as
genuinely tracked only when it carries three or more readings taken at
different points of the session — an imported final standing does not count,
because extrapolating from the answer proves nothing. Every file this imports
adds one such evening, and a few dozen of them are what would turn the live
refinement from a principled rule into a measured one.

A session whose tournament is already in the database — the usual case, since
the harvest downloads the same cups — attaches its readings to that row and
leaves its harvested thresholds alone: the API's whole board beats three ranks
typed by hand. A cup the harvest has never seen is created, and the last
acquisition, taken after the final game, becomes its result.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db

# How far apart a session's start and a stored tournament's start may be and
# still be the same event: Epic's published start against a person's clock,
# plus whatever the calendar snapshot rounded.
NEAR = timedelta(hours=3)


def when(text: str):
    """Epic's timestamps and the app's, or None."""
    if not text:
        return None
    text = str(text).strip().replace(" ", "T")
    for suffix in ("", ":00"):
        try:
            return datetime.fromisoformat((text + suffix).replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def stamp(moment: datetime | None) -> str | None:
    """The database's own spelling: 'YYYY-MM-DD HH:MM', UTC, no zone."""
    if not moment:
        return None
    if moment.tzinfo:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.strftime("%Y-%m-%d %H:%M")


def read(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  {path.name}: unreadable ({exc})")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("log"), list):
        print(f"  {path.name}: not a session file (no 'log')")
        return None
    if not isinstance(payload.get("tournament"), dict):
        print(f"  {path.name}: not a session file (no 'tournament')")
        return None
    return payload


def same_cup(stored: str, asked: str) -> float:
    """How much two tournament names agree, 0 to 1.

    `db.key_words` drops the words every cup shares and folds the accents, so
    what is left is what distinguishes them. Digits have to agree exactly:
    Division 2 and Division 3 share every word they have and are not remotely
    the same event — the same rule the predictor's own name matching uses.
    """
    a, b = db.key_words(stored), db.key_words(asked)
    if not a or not b:
        return 0.0
    if {w for w in a if w.isdigit()} != {w for w in b if w.isdigit()}:
        return 0.0
    return len(set(a) & set(b)) / min(len(set(a)), len(set(b)))


# Below this the names are different cups, whatever their start times say.
SAME_NAME = 0.7


def find(conn, tour: dict):
    """The stored tournament this session belongs to, or None.

    Both halves are needed. The start time alone matches the wrong cup — eight
    of them begin at 17:00 in EU on a Thursday — and the name alone matches
    last week's edition of the right one. So: same region, names that agree,
    and the closest start time among those.
    """
    begin = when(tour.get("begin"))
    region = (tour.get("region") or "").upper()
    asked = (tour.get("category") or tour.get("name") or "").strip()
    if not asked:
        return None
    best, best_gap = None, None
    for row in db.catalogue(conn):
        if (row.get("region") or "").upper() != region:
            continue
        agreement = max(same_cup(row.get("kind") or "", asked),
                        same_cup(row.get("name") or "", asked))
        if agreement < SAME_NAME:
            continue
        started = when(row.get("start_time"))
        if begin and started:
            if not started.tzinfo:
                started = started.replace(tzinfo=timezone.utc)
            gap = abs(started - begin)
            if gap > NEAR:
                continue                      # the right cup, a different edition
            if best_gap is None or gap < best_gap:
                best, best_gap = row, gap
        elif best is None:
            best = row                        # no clock to go on: the name alone
    return best


def readings_of(entry: dict) -> dict:
    out = {}
    for reading in entry.get("readings") or []:
        rank, points = reading.get("rank"), reading.get("points")
        if isinstance(rank, int) and rank >= 1 and isinstance(points, (int, float)) and points > 0:
            out[int(rank)] = float(points)
    return out


def moment_of(entry: dict, tour: dict):
    """When an acquisition was taken. The wall clock the browser stamped, or
    failing that the window's start plus the minutes elapsed."""
    got = when(entry.get("at"))
    if got:
        return got
    begin = when(tour.get("begin"))
    if begin and entry.get("elapsed") is not None:
        return begin + timedelta(minutes=float(entry["elapsed"]))
    return None


def import_one(conn, session: dict, path: Path, dry: bool) -> bool:
    tour = session["tournament"]
    entries = [e for e in session["log"] if readings_of(e)]
    if not entries:
        print(f"  {path.name}: no usable reading")
        return False
    entries.sort(key=lambda e: (e.get("game") or 0))
    final = next((e for e in reversed(entries) if (e.get("share") or 0) >= 1), None)

    existing = find(conn, tour)
    where = (f"attaching to #{existing['id']} {existing['name']} ({existing['region']})"
             if existing else "creating a new tournament")
    print(f"  {path.name}: {len(entries)} acquisition(s), "
          f"{'a final standing' if final else 'no final standing'} — {where}")
    if dry:
        for entry in entries:
            print(f"      game {entry.get('game')}, {round(100 * (entry.get('share') or 0))} % "
                  f"of the way{' (automatic)' if entry.get('auto') else ''}: {readings_of(entry)}")
        return True

    if existing:
        comp_id = existing["id"]
    else:
        comp_id = db.create_competition(
            conn, name=tour.get("name") or "Followed tournament",
            region=(tour.get("region") or "EU").upper(),
            team_mode=tour.get("team_mode") or "Solo",
            game_mode=tour.get("game_mode") or "Battle Royale",
            start_time=stamp(when(tour.get("begin"))) or stamp(moment_of(entries[0], tour)),
            end_time=stamp(when(tour.get("end"))),
            ranks=sorted({r for e in entries for r in readings_of(e)}),
            max_games=tour.get("games") or None,
            game_minutes=tour.get("game_minutes") or None,
            games_mode="sealed" if tour.get("format") == "sealed" else "max",
            scoring=tour.get("scoring") or None,
            notes="followed live in the predictor")
        db.update_competition(conn, comp_id, source="manual", tracking=1,
                              family=tour.get("name") or "", field_size=tour.get("field_size") or None)

    # Readings taken at the same moment as one already stored are the same
    # reading: importing a file twice must not double the evening's evidence.
    seen = {str(s["ts"])[:16] for s in db.get_snapshots(conn, comp_id)}
    written = 0
    for entry in entries:
        moment = moment_of(entry, tour)
        if stamp(moment) in seen:
            continue
        db.add_snapshot(conn, comp_id, ts=stamp(moment), points=readings_of(entry),
                        games=entry.get("game"),
                        note=f"predictor{' (automatic)' if entry.get('auto') else ''} · "
                             f"{round(100 * (entry.get('share') or 0))} % of the session")
        written += 1
    finals = db.get_finals(conn, comp_id)
    if final and not finals:
        db.set_finals(conn, comp_id, readings_of(final))
        print(f"      result recorded from the last acquisition: {readings_of(final)}")
    elif final and finals:
        print(f"      thresholds already known ({len(finals)} ranks) — the harvest's board is "
              f"deeper than a typed reading, so they are left alone")
    print(f"      {written} snapshot(s) written"
          + (f", {len(entries) - written} already there" if written < len(entries) else ""))
    return True


def show(conn) -> int:
    """Which tournaments carry enough readings to test a live model."""
    rows = []
    for comp in db.catalogue(conn):
        snaps = db.get_snapshots(conn, comp["id"])
        if len(snaps) >= 2:
            rows.append((len(snaps), comp["start_time"], comp["name"], comp["region"]))
    rows.sort(reverse=True)
    print(f"\n{len(rows)} tournament(s) with more than one reading; "
          f"{sum(1 for r in rows if r[0] >= 3)} with the three that make one testable.\n")
    for count, started, name, region in rows[:40]:
        print(f"  {count:>3} readings  {str(started)[:16]}  {region:<5} {name[:56]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="session files written by the predictor")
    parser.add_argument("--dry-run", action="store_true", help="say what would happen")
    parser.add_argument("--list", action="store_true", help="show what is already tracked")
    args = parser.parse_args()

    db.init_db()
    if args.list:
        with db.session() as conn:
            return show(conn)
    paths = [Path(f) for f in args.files]
    missing = [p for p in paths if not p.exists()]
    if missing or not paths:
        for path in missing:
            print(f"  {path}: no such file")
        if not paths:
            print("Nothing to import. Pass the session files, or --list to see what is tracked.")
        return 1

    print(f"{len(paths)} file(s){' — dry run, nothing is written' if args.dry_run else ''}")
    done = 0
    with db.session() as conn:
        for path in paths:
            session = read(path)
            if session and import_one(conn, session, path, args.dry_run):
                done += 1
    print(f"\n{done} of {len(paths)} imported."
          + ("" if args.dry_run else "  Re-export the model to use them: python src/refresh.py"))
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
