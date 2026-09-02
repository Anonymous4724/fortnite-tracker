"""Imports finished Epic tournaments from Cito, to train the estimator.

What the API actually gives, confirmed by probing: for an Epic event,
`/tournaments/{id}/placements` returns each player's `rank`, `points`,
`kills` and `matchesPlayed`, and the envelope announces the `total` row
count — which gives the **field size**, the piece of information that was
missing the most.

Three precautions, learned from that probing:

* **Tier-3 cups are dropped.** A Victus Fast Cup has no `epic_event_id`
  and its placements carry neither points nor games played.
* **Rows are per player, not per team.** In duos each result appears
  twice; group on `teamName`, which is the concatenation of the team's
  accounts.
* **Some events have no points at all.** Check before saving, and refuse
  rather than pollute the history.

    python import_history.py --list      catalog only, ~6 requests
    python import_history.py             catalog then import
    python import_history.py --budget 30  cap the number of requests
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
import calibration
import cito
import db


def calibration_reset(conn) -> None:
    calibration.invalidate(conn)

# Cito spells out region names in full. NA West and NA East no longer exist
# competitively since their merger: events still labeled that way are NA
# Central tournaments.
REGION_MAP = {
    "europe": "EU", "eu": "EU",
    "asia": "ASIA", "asie": "ASIA",
    "na central": "NAC", "nac": "NAC", "na west": "NAC", "naw": "NAC",
    "na east": "NAC", "nae": "NAC", "north america": "NAC",
    "brazil": "BR", "br": "BR",
    "oceania": "OCE", "oce": "OCE",
    "middle east": "ME", "me": "ME",
}
# Cito reports the region as "Unknown" for every NA Central tournament —
# it's the id's suffix that tells the truth, and it's never wrong.
ID_SUFFIX = re.compile(r"-(eu|europe|nac|nae|naw|asia|br|me|oce)$")

# Families we track. A "Ranked" cup isn't a tournament with stakes: excluded.
FAMILIES = {
    "fncs": r"\bfncs\b",
    "reload": r"\breload\b",
    "cash cup": r"\bcash\s*cup\b",
}
EXCLUDE = r"\branked\s*cup\b|\bmobile\b|\bicon\b|\btest\b"

MIN_TEAMS = 60           # below this, the standings don't teach us anything
MIN_SCORED = 0.7         # share of teams that must have nonzero points
PAGE = 100               # rows per catalog page
BOARD = 200              # standings rows requested per tournament


def logger(conn):
    return lambda endpoint, status: db.log_api_call(conn, endpoint, status)


def wanted(name: str, families: list[str], pattern: str = "") -> bool:
    low = (name or "").lower()
    if pattern:                     # targeted search: the pattern is the only rule that counts
        return pattern.lower() in low
    if re.search(EXCLUDE, low):
        return False
    return any(re.search(FAMILIES[f], low) for f in families if f in FAMILIES)


def region_of(row: dict) -> str:
    """Tournament region, read from the id first."""
    found = ID_SUFFIX.search(row.get("id") or "")
    if found:
        return REGION_MAP.get(found.group(1), "")
    return REGION_MAP.get((row.get("region") or "").strip().lower(), "")


def team_mode_of(name: str, per_team: float) -> str:
    low = (name or "").lower()
    for word, mode in (("solo", "Solo"), ("duo", "Duo"), ("trio", "Trio"),
                       ("squad", "Squad")):
        if word in low:
            return mode
    return {1: "Solo", 2: "Duo", 3: "Trio", 4: "Squad"}.get(round(per_team), "Duo")


def game_mode_of(name: str) -> str:
    low = (name or "").lower()
    if "reload" in low:
        return "Reload Zero Build" if "zero build" in low or " zb" in low else "Reload"
    if "zero build" in low or " zb" in low:
        return "Zero Build"
    return "Battle Royale"


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def catalog(conn, regions: list[str], families: list[str], year: int,
            pages: int, spent: list[int], pattern: str = "") -> list[dict]:
    """The Epic tournaments matching the wanted families and regions.

    The API's `region` filter has no effect: a page mixes every region
    together. So we only paginate once and sort here — one request per page
    instead of one per page per region.
    """
    keep = []
    for page in range(pages):
        path = f"/tournaments?year={year}&limit={PAGE}&offset={page * PAGE}"
        try:
            payload = cito.call(path, log=logger(conn))
        except cito.CitoError as exc:
            print(f"   page {page + 1}: {exc}")
            break
        spent[0] += 1
        rows = payload.get("data") or []
        total = (payload.get("pagination") or {}).get("total")
        hits = []
        for row in rows:
            if not (row.get("identifiers") or {}).get("epic_event_id"):
                continue
            if not wanted(row.get("name"), families, pattern):
                continue
            if not (row.get("availableSubresources") or {}).get("placements"):
                continue
            region = region_of(row)
            if region not in regions:
                continue
            row["_region"] = region
            hits.append(row)
        keep.extend(hits)
        print(f"   page {page + 1}: {len(rows)} read, {len(hits)} kept "
              f"(catalog {total})")
        if len(rows) < PAGE:
            break
    unique = {r["id"]: r for r in keep}
    return sorted(unique.values(), key=lambda r: r.get("startDate") or "", reverse=True)


# --------------------------------------------------------------------------- #
# Tournament standings
# --------------------------------------------------------------------------- #
def read_board(conn, ident: str, spent: list[int]) -> dict | None:
    """One request: the top of the standings, plus the field size."""
    try:
        payload = cito.call(f"/tournaments/{ident}/placements?limit={BOARD}",
                            log=logger(conn))
    except cito.CitoError as exc:
        return {"ok": False, "reason": str(exc)}
    spent[0] += 1
    rows = payload.get("data") or []
    total_rows = (payload.get("pagination") or {}).get("total") or len(rows)
    if not rows:
        return {"ok": False, "reason": "empty standings"}

    teams = {}
    for row in rows:
        key = row.get("teamName") or row.get("accountId")
        if key in teams:
            continue
        try:
            teams[key] = {"rank": int(row["rank"]),
                          "points": float(row.get("points") or 0),
                          "kills": int(row.get("kills") or 0),
                          "games": int(row.get("matchesPlayed") or 0)}
        except (TypeError, ValueError):
            continue
    if not teams:
        return {"ok": False, "reason": "no readable team"}

    per_team = len(rows) / len(teams)              # 2 rows per team in duos
    field = int(round(total_rows / per_team))
    values = sorted(teams.values(), key=lambda t: t["rank"])
    scored = sum(1 for t in values if t["points"] > 0) / len(values)
    if scored < MIN_SCORED:
        return {"ok": False, "reason": f"points missing (only {round(100 * scored)}% filled in)"}
    if field < MIN_TEAMS:
        return {"ok": False, "reason": f"field too small ({field} teams)"}

    # Probing turned up isolated, inconsistent `rank` values — a team with 2
    # points announced as 25th in the middle of teams sitting at 380. Since
    # the request starts from the top of the standings, the real order can be
    # rebuilt safely: sort by points and let rank become position.
    ordered = sorted((t for t in values if t["points"] > 0),
                     key=lambda t: -t["points"])
    # The very last rows of the batch are the edge of the requested window:
    # that's where outliers and truncation land. We don't read a threshold
    # off them, and we stop as soon as a value drops off from the previous
    # one — a real curve doesn't collapse between two neighboring ranks.
    usable = ordered[:max(0, len(ordered) - 2)]
    thresholds, previous = {}, None
    for target in (1, 3, 5, 10, 20, 25, 50, 100):
        if target > len(usable):
            break
        value = round(usable[target - 1]["points"], 1)
        if previous is not None and value < 0.4 * previous:
            break
        thresholds[target] = value
        previous = value
    anomalies = sum(1 for a, b in zip(values, values[1:]) if a["points"] < b["points"])

    games = [t["games"] for t in ordered if t["games"]]
    top_games = statistics.median([t["games"] for t in ordered[:20] if t["games"]] or [0])
    return {"ok": True, "field": field, "teams": len(teams), "per_team": per_team,
            "thresholds": thresholds, "max_games": max(games) if games else 0,
            "median_games": statistics.median(games) if games else 0,
            "top_games": top_games,
            # what a leaderboard leader scores per game: this is the scale
            # the scoring table will need to explain
            "top_per_game": round(thresholds[1] / top_games, 1)
                            if thresholds.get(1) and top_games else None,
            "anomalies": anomalies}


# --------------------------------------------------------------------------- #
# Second pass: deep ranks
# --------------------------------------------------------------------------- #
PER_TEAM = {"Solo": 1, "Duo": 2, "Trio": 3, "Squad": 4}


def deep_window(conn, ident: str, target: int, per_team: int,
                spent: list[int], budget: int) -> tuple[int, float] | None:
    """The threshold at a given rank, read from a window of the standings.

    First version: the position to request was the rank times the number of
    players per team. Wrong — some teams have only one row (a teammate
    missing from the standings), the offset drifts, and by rank 500 you land
    somewhere else entirely. Result: absurd thresholds, 6 points at rank 771.

    Reliable version: stop trusting the arithmetic and read the `rank` field
    the API returns on every row instead. If the window misses, it says where
    it landed, and a second attempt corrects course.
    """
    guess = max(0, (target - 60)) * per_team
    for attempt in range(2):
        if spent[0] >= budget:
            return None
        try:
            payload = cito.call(
                f"/tournaments/{ident}/placements?limit=240&offset={int(guess)}",
                log=logger(conn))
        except cito.CitoError:
            return None
        spent[0] += 1
        rows = payload.get("data") or []
        if not rows:
            return None

        seen, pairs = set(), []
        for row in rows:
            key = row.get("teamName") or row.get("accountId")
            if key in seen:
                continue
            seen.add(key)
            try:
                rank, pts = int(row["rank"]), float(row.get("points") or 0)
            except (TypeError, ValueError):
                continue
            if pts > 0:
                pairs.append((rank, pts))
        if len(pairs) < 20:
            return None

        ranks = sorted(r for r, _ in pairs)
        low, high = ranks[0], ranks[-1]
        if low <= target <= high:
            near = [p for r, p in pairs if abs(r - target) <= 3]
            if not near:
                near = [p for r, p in pairs
                        if abs(r - target) <= max(5, (high - low) // 20)]
            if not near:
                return None
            return target, round(statistics.median(near), 1)

        # the window landed elsewhere: recalibrate with what it just taught us
        median_rank = ranks[len(ranks) // 2]
        if median_rank <= 1:
            return None
        guess = max(0, guess * target / median_rank - 60 * per_team)
    return None


def deepen(conn, budget: int, spent: list[int]) -> int:
    """Backfills already-imported tournaments with deeper thresholds."""
    rows = list(conn.execute(
        "SELECT id, name, family, region, team_mode, field_size, event_id "
        "FROM competition WHERE source = 'import' AND event_id != '' "
        "ORDER BY field_size DESC"))
    done = 0
    for comp in rows:
        if spent[0] >= budget:
            print(f"\nBudget reached ({budget} requests).")
            break
        field = comp["field_size"] or 0
        per_team = PER_TEAM.get(comp["team_mode"], 2)
        # three depths in the zone where qualification cutoffs happen,
        # expressed as a share of the field
        targets = sorted({int(field * q) for q in (0.08, 0.18, 0.35)} - {0})
        targets = [r for r in targets if 60 <= r <= field - 60]
        if not targets:
            continue
        # `set_finals` merges, it doesn't replace: without this wipe, a wrong
        # deep threshold from an earlier pass would survive its own
        # correction. So clear it out before reading it back.
        conn.execute("DELETE FROM final_result WHERE competition_id = ? AND rank > 50",
                     (comp["id"],))
        known = {int(r["rank"]): r["points"] for r in conn.execute(
            "SELECT rank, points FROM final_result WHERE competition_id = ? AND rank <= 50",
            (comp["id"],))}
        added, misses = {}, 0
        for target in targets:
            if spent[0] >= budget or misses >= 2:
                break                      # this standings page is resisting: don't push it
            found = deep_window(conn, comp["event_id"], target, per_team, spent, budget)
            if found:
                added[found[0]] = found[1]
            else:
                misses += 1
        # Final check: the completed curve has to stay decreasing, with no
        # collapse between two neighboring ranks. A threshold that fails this
        # is dropped rather than fixed — a gap beats a made-up value.
        merged, last, rejected = {}, None, 0
        for rank in sorted({**known, **added}):
            value = {**known, **added}[rank]
            if last is None or (value <= last * 1.02 and value >= last * 0.35):
                merged[rank] = value
                last = value
            else:
                rejected += 1
        added = {r: v for r, v in merged.items() if r in added}
        known = {r: v for r, v in merged.items() if r in known}
        if added:
            db.set_finals(conn, comp["id"], {**known, **added})
            done += 1
            print(f"   + {comp['family'][:26]:<28} {comp['region']:<5} "
                  f"field {field:>5}: " + " · ".join(f"top {r} = {v:g}"
                                                      for r, v in sorted(added.items())))
    return done


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Import finished Epic tournaments")
    parser.add_argument("--regions", default="EU,ASIA,NAC")
    parser.add_argument("--families", default="fncs,reload,cash cup")
    parser.add_argument("--name", default="",
                        help="only keep tournaments whose name contains this text")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--pages", type=int, default=4, help="catalog pages to read")
    parser.add_argument("--budget", type=int, default=60, help="maximum requests")
    parser.add_argument("--list", action="store_true", help="catalog only, no import")
    parser.add_argument("--deep", action="store_true",
                        help="backfill already-imported tournaments with deeper ranks")
    parser.add_argument("--verify", action="store_true",
                        help="check the consistency of stored thresholds, no request made")
    args = parser.parse_args()

    db.init_db()

    if args.verify:
        with db.session() as conn:
            total, dropped = 0, 0
            for comp in conn.execute(
                    "SELECT id, family, region FROM competition WHERE source = 'import'"):
                fin = sorted((int(r["rank"]), r["points"]) for r in conn.execute(
                    "SELECT rank, points FROM final_result WHERE competition_id = ?",
                    (comp["id"],)))
                last, bad = None, []
                for rank, value in fin:
                    if last is None or (value <= last * 1.02 and value >= last * 0.35):
                        last = value
                    else:
                        bad.append(rank)
                total += len(fin)
                for rank in bad:
                    conn.execute("DELETE FROM final_result WHERE competition_id = ? "
                                 "AND rank = ?", (comp["id"], rank))
                dropped += len(bad)
                if bad:
                    print(f"   {comp['family'][:26]:<28} {comp['region']:<5} "
                          f"inconsistent ranks removed: {bad}")
            calibration_reset(conn)
        print(f"\n{dropped} inconsistent threshold(s) removed out of {total}. "
              f"No request spent.\n")
        return 0

    if not cito.read_key():
        print("No Cito key on file.")
        return 1

    if args.deep:
        with db.session() as conn:
            before = db.quota_view(conn, per_tournament=18, limit=cito.FREE_QUOTA)
            print(f"\nQuota: {before['used']}/{before['limit']}. "
                  f"Budget: {args.budget} requests.\n")
            spent = [0]
            done = deepen(conn, args.budget, spent)
            after = db.quota_view(conn, per_tournament=18, limit=cito.FREE_QUOTA)
        print(f"\n{done} tournament(s) backfilled. "
              f"Quota: {after['used']}/{after['limit']} (+{spent[0]}).\n")
        return 0

    regions = [r.strip().upper() for r in args.regions.split(",") if r.strip()]
    families = [f.strip().lower() for f in args.families.split(",") if f.strip()]
    spent = [0]

    with db.session() as conn:
        before = db.quota_view(conn, per_tournament=18, limit=cito.FREE_QUOTA)
        print(f"\nQuota: {before['used']}/{before['limit']}. "
              f"Budget for this run: {args.budget} requests.\n")
        print(f"Catalog {args.year} · regions {', '.join(regions)} · "
              + (f"name containing \"{args.name}\"" if args.name
                 else f"families {', '.join(families)}"))
        found = catalog(conn, regions, families, args.year, args.pages, spent,
                        pattern=args.name)

        print(f"\n{len(found)} tournament(s) kept:")
        for row in found[:40]:
            family, stage = db.split_name(row.get("name") or "")
            print(f"   {(row.get('startDate') or '')[:10]}  {row['_region']:<5} "
                  f"{family[:34]:<34} {stage or '—':<14} {row['id'][-40:]}")
        if len(found) > 40:
            print(f"   … and {len(found) - 40} more")

        if args.list:
            print(f"\n(list mode) {spent[0]} request(s) spent. "
                  f"Importing would cost {len(found)} more request(s).\n")
            return 0

        print()
        created, refused = 0, []
        for row in found:
            if spent[0] >= args.budget:
                print(f"\nBudget reached ({args.budget} requests).")
                break
            ident = row["id"]
            board = read_board(conn, ident, spent)
            name = row.get("name") or ident
            if not board or not board.get("ok"):
                refused.append((name, board.get("reason") if board else "error"))
                print(f"   ✗ {name[:44]:<44} {board.get('reason') if board else ''}")
                continue

            family, stage = db.split_name(name)
            date = (row.get("startDate") or "")[:10] or None
            comp_id = db.create_history_entry(
                conn, kind=family, region=row["_region"], date=date,
                values=board["thresholds"],
                max_games=board["max_games"] or db.DEFAULT_MAX_GAMES,
                scoring=None, stage=stage,
                team_mode=team_mode_of(name, board["per_team"]),
                game_mode=game_mode_of(name),
                label=(row.get("startDate") or "")[:10])
            db.update_competition(conn, comp_id, source="import",
                                  field_size=board["field"], event_id=ident)
            created += 1
            print(f"   ✓ {name[:40]:<40} {board['field']:>6} teams · "
                  f"top1 {board['thresholds'].get(1, '—')} · "
                  f"top100 {board['thresholds'].get(100, '—')} · "
                  f"{board['max_games']} games · "
                  f"{board['top_per_game'] or '—'} pts/game at the top")

        after = db.quota_view(conn, per_tournament=18, limit=cito.FREE_QUOTA)

    print(f"\n{created} tournament(s) imported, {len(refused)} refused.")
    print(f"Quota: {after['used']}/{after['limit']} (+{spent[0]} this run).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
