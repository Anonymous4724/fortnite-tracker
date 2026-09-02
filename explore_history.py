"""Probes Cito's past tournaments, targeting Epic events. ~5 requests.

The first pass showed that `/tournaments` mostly returns tier-3 cups (Victus
Fast Cup and friends) whose placements carry only a rank and a payout — no
points, no eliminations, no games played: useless for training the estimator.
Filtering by year, though, surfaces real Epic events, recognizable by their
`epic_event_id`.

This pass goes after those events specifically and checks whether **their**
placements carry points. That answer decides what comes next.

This script calls each of them once, writes everything it gets back to
`data/history_exploration.json`, and prints a readable summary. The report
holds only public tournament data — never the key.

    python explore_history.py
"""
from __future__ import annotations

import json
import os
import sys

import cito
import db

REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "data", "history_exploration.json")
SAMPLE = 3            # rows kept per endpoint in the report


def logger(conn):
    def log(endpoint, status):
        db.log_api_call(conn, endpoint, status)
    return log


def probe(conn, path: str, note: str) -> dict:
    print(f"  → {path}")
    try:
        payload = cito.call(path, log=logger(conn))
    except cito.CitoError as exc:
        print(f"     failed: {exc}")
        return {"path": path, "note": note, "ok": False, "error": str(exc),
                "status": getattr(exc, "status", 0)}
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        rows = [rows]
    rows = rows if isinstance(rows, list) else []
    print(f"     {len(rows)} row(s)" + (f" · fields: {', '.join(sorted(rows[0]))[:110]}"
                                        if rows and isinstance(rows[0], dict) else ""))
    return {"path": path, "note": note, "ok": True, "n_rows": len(rows),
            "total": (payload.get("pagination") or {}).get("total")
                     if isinstance(payload, dict) else None,
            "fields": sorted(rows[0]) if rows and isinstance(rows[0], dict) else [],
            "envelope": sorted(payload) if isinstance(payload, dict) else [],
            "sample": rows[:SAMPLE],
            # catalog and placements feed the analysis: keep them in full
            "full": rows if ("year=" in path or "placements" in path) else []}


def first_id(step: dict) -> str | None:
    """An id usable for querying sub-resources."""
    for row in step.get("sample") or []:
        if not isinstance(row, dict):
            continue
        ids = row.get("identifiers") or {}
        for key in ("cito_tournament_id", "slug", "epic_event_id"):
            if isinstance(ids, dict) and ids.get(key):
                return str(ids[key])
        for key in ("id", "tournamentId", "slug", "eventId"):
            if row.get(key):
                return str(row[key])
    return None


def main() -> int:
    db.init_db()
    if not cito.read_key():
        print("No Cito key on file. Open the app and enter one first.")
        return 1

    steps = []
    with db.session() as conn:
        quota = db.quota_view(conn, per_tournament=18, limit=cito.FREE_QUOTA)
        print(f"\nQuota before: {quota['used']}/{quota['limit']} requests this month.\n")

        # The first pass showed the general listing is dominated by tier-3
        # cups (Victus Fast Cup...) whose placements carry neither points nor
        # games. So we target the official Epic events instead, recognizable
        # only by their `epic_event_id`.
        catalog = probe(conn, "/tournaments?year=2026&limit=100", "2026 catalog")
        steps.append(catalog)

        epics = [r for r in (catalog.get("full") or []) if isinstance(r, dict)
                 and ((r.get("identifiers") or {}).get("epic_event_id"))]
        print(f"\n  {len(epics)} Epic event(s) in the batch "
              f"(out of {catalog.get('n_rows', 0)} rows received, "
              f"announced total: {catalog.get('total')}).")
        for row in epics[:12]:
            print(f"     - {row.get('name')} | {row.get('region')} | {row.get('id')}")

        for row in epics[:2]:
            ident = row.get("id")
            print(f"\n  testing Epic event: {ident}\n")
            steps.append(probe(conn, f"/tournaments/{ident}", "Epic detail"))
            steps.append(probe(conn, f"/tournaments/{ident}/placements?limit=50",
                               "Epic placements"))
        if not epics:
            print("\n  No Epic event in this batch.\n")
        ident = epics[0].get("id") if epics else None

        after = db.quota_view(conn, per_tournament=18, limit=cito.FREE_QUOTA)

    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump({"tested_id": ident, "steps": steps}, fh,
                  ensure_ascii=False, indent=2)

    ok = [s for s in steps if s["ok"]]
    print("\n" + "=" * 68)
    print(f"  {len(ok)}/{len(steps)} endpoints responded.")
    for s in steps:
        state = f"{s['n_rows']} row(s)" if s["ok"] else f"FAILED {s.get('status')}"
        print(f"    {s['note']:<24} {state}")
    print(f"\n  Quota: {after['used']}/{after['limit']} "
          f"(+{after['used'] - quota['used']} from this exploration)")
    print(f"  Report written to data/history_exploration.json")
    print("=" * 68 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
