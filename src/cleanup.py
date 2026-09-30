"""Cleans up the folder: deletes whatever no longer serves a purpose.

The inventory isn't hand-maintained — it's **rebuilt on every run** starting
from the entry points: follow the Python imports, then the templates reached
by `render_template`, their `extends`/`include`, and the `static` files they
reference. Anything this walk doesn't reach is an orphan, regardless of which
version of the app left it behind.

None of this touches the database, the Cito key, or the archived tournaments.

    python src/cleanup.py           propose and ask for confirmation
    python src/cleanup.py --list    show only, delete nothing
    python src/cleanup.py --yes     delete without asking
"""
from __future__ import annotations

import argparse
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
# The code lives here in `src/`; the data, the launchers and the notes one
# level up. A flat copy still works.
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "src" else HERE


def where(rel: str) -> str:
    """A path in the code folder, or in the repository for data/ and the launchers."""
    return os.path.join(ROOT if rel.startswith("data/") or rel.endswith((".bat", ".sh")) else HERE, rel)


# What actually gets launched directly — everything else has to be reachable from here.
ENTRY_POINTS = ["app.py", "cleanup.py", "backtest.py", "check_forms.py", "fuzz_api.py",
                "selfcheck.py", "harvest_osirion.py", "export_model.py",
                "test_osirion.py", "calendar_snapshot.py", "refresh.py",
                "import_session.py", "pull_live.py", "rescore.py"]

def always_keep() -> set[str]:
    """Launchers, notes, keys: kept even though no code imports them.

    Computed at runtime rather than hardcoded, so a new launcher or a new
    key file doesn't need to be declared here.
    """
    # Scripts nobody imports because they are run, not called. `reachable()`
    # follows imports from the app's entry points, so a standalone tool is
    # invisible to it and would be offered for deletion on every run.
    keep = {"requirements.txt", ".gitignore", "LICENSE", "model.json",
            "harvest_osirion.py", "calendar_snapshot.py", "refresh.py",
            "selfcheck.py", "check_forms.py", "fuzz_api.py", "test_osirion.py",
            "cleanup.py", "export_model.py", "backtest.py", "import_session.py",
            "pull_live.py", "rescore.py", "coldbench.py", "tidy.py"}
    for folder in {HERE, ROOT}:
        for name in os.listdir(folder):
            if os.path.isdir(os.path.join(folder, name)):
                continue
            low = name.lower()
            # "cle" catches cle_cito.txt, the key file name used before v8.
            if low.endswith((".bat", ".sh", ".cmd", ".md")) or "key" in low or "cle" in low:
                keep.add(name)
    return keep

# Remnants identified from earlier stages of the project, with why they're gone.
LEGACY = [
    ("api-fortnite exploration tools",
     "That provider was dropped: the app talks to Cito directly.",
     ["explore_api.py", "explorer_api.bat", "lancer_exploration_api.bat",
      "cle_api.exemple.txt", "data/api_exploration"]),
    ("api-fortnite leftovers",
     "Its calendar endpoint needs a paid plan (403). The key is dead weight here.",
     ["cle_api.txt"]),
    ("Demo data",
     "The --demo launch option was removed.",
     ["seed_demo.py"]),
    ("Cito history import",
     "The training set comes from the Osirion harvest now. These probed and "
     "imported Cito's past tournaments, a few hundred requests at a time; the "
     "harvest reads thousands for free. Cito stays for live standings only.",
     ["import_history.py", "explore_history.py", "recompute_scoring.py",
      "run_import_history.bat", "run_explore_history.bat",
      "run_recompute_scoring.bat"]),
    ("Superseded launchers",
     "refresh.py does export, calendar and build in one; selfcheck runs the "
     "form check.",
     ["update_predictor.py", "update_predictor.bat", "run_check_forms.bat"]),
    ("French file names, from before v8",
     "Renamed in English when the project was published. The new names sit "
     "beside them; these are the leftovers.",
     ["nettoyer.py", "verifier_saisies.py", "importer_historique.py",
      "explorer_historique.py", "recalculer_bareme.py", "templates/scorings.html",
      "lancer.sh", "lancer_windows.bat", "nettoyer.bat",
      "lancer_exploration_historique.bat", "lancer_import_historique.bat",
      "lancer_recalcul_bareme.bat", "lancer_verification.bat",
      "data/exploration_historique.json", "data/evenements_bruts.json"]),
    ("Python caches",
     "Regenerated on every run.",
     ["__pycache__", "templates/__pycache__"]),
]

PROTECTED = [
    ("data/tracker.db", "tournaments, readings, predictions, thresholds"),
    ("data/tournaments", "archived tournaments"),
    ("data/cle_cito.txt", "active API key"),
]


# --------------------------------------------------------------------------- #
# Live inventory
# --------------------------------------------------------------------------- #
def read(rel: str) -> str:
    try:
        with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def reachable() -> set[str]:
    """Everything the app actually uses, found by following references."""
    keep: set[str] = set()

    # 1. Python modules, following imports from the entry points
    todo = [e for e in ENTRY_POINTS if os.path.exists(os.path.join(HERE, e))]
    while todo:
        module = todo.pop()
        if module in keep:
            continue
        keep.add(module)
        source = read(module)
        for name in re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][\w]*)", source, re.M):
            candidate = f"{name}.py"
            if candidate not in keep and os.path.exists(os.path.join(HERE, candidate)):
                todo.append(candidate)

    # 2. templates rendered by those modules, then their extends/include
    pages = []
    for module in list(keep):
        pages += re.findall(r"render_template\(\s*[\"']([\w./-]+\.html)", read(module))
    todo = [f"templates/{p}" for p in pages]
    while todo:
        page = todo.pop()
        if page in keep or not os.path.exists(os.path.join(HERE, page)):
            continue
        keep.add(page)
        for ref in re.findall(r"{%-?\s*(?:extends|include)\s+[\"']([\w./-]+\.html)", read(page)):
            todo.append(f"templates/{ref}")

    # 3. static files referenced by those templates
    for page in [p for p in keep if p.startswith("templates/")]:
        for asset in re.findall(r"filename\s*=\s*[\"']([\w./-]+)[\"']", read(page)):
            keep.add(f"static/{asset}")

    return keep | always_keep()


def orphans(keep: set[str]) -> list[str]:
    """Code files that are present but nothing reaches anymore."""
    found = []
    for folder in ("", "templates", "static"):
        base = os.path.join(HERE, folder) if folder else HERE
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            rel = f"{folder}/{name}" if folder else name
            full = os.path.join(HERE, rel)
            if os.path.isdir(full) or rel in keep:
                continue
            if os.path.splitext(name)[1] in (".py", ".html", ".css", ".js"):
                found.append(rel)
    return found


# --------------------------------------------------------------------------- #
def size_of(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human(size: float) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"


def collect(paths):
    items, total = [], 0
    for rel in paths:
        full = where(rel)
        if os.path.exists(full):
            size = size_of(full)
            items.append((rel, full, size))
            total += size
    return items, total


def partial_downloads() -> list[str]:
    """Pages the harvester was writing when it was stopped.

    `harvest_osirion.save` writes each page to `<name>.part` and renames it
    into place, so a Ctrl-C lands here and nowhere else. The next fetch of the
    same page overwrites it; one that never comes leaves it behind.
    """
    root = os.path.join(ROOT, "data", "osirion")
    found = []
    for folder, _, files in os.walk(root):
        for name in files:
            if name.endswith(".part"):
                found.append(os.path.relpath(os.path.join(folder, name), ROOT).replace("\\", "/"))
    return sorted(found)


def scan():
    """Group what can go, without listing anything twice.

    A renamed file is both a known leftover and unreachable from the code, so
    it used to appear in two groups and the second removal failed on a file
    that was already gone.
    """
    groups, total, seen = [], 0, set()
    for title, why, paths in LEGACY:
        items, size = collect([p for p in paths if p not in seen])
        if items:
            seen.update(rel for rel, _, _ in items)
            groups.append((title, why, items))
            total += size
    parts, size = collect(partial_downloads())
    if parts:
        groups.append(("Interrupted downloads",
                       "Half-written API pages; the harvester never reads them.",
                       parts))
        total += size
    extra, size = collect([p for p in orphans(reachable()) if p not in seen])
    if extra:
        groups.append(("Files no longer linked to the app",
                       "No import, no render_template, no reference reaches them.",
                       extra))
        total += size
    return groups, total


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean up the folder")
    parser.add_argument("--list", action="store_true", help="show without deleting")
    parser.add_argument("--yes", action="store_true", help="delete without asking")
    args = parser.parse_args()

    keep = reachable()
    groups, total = scan()

    print("\n" + "=" * 68)
    print("  KEPT NO MATTER WHAT")
    print("=" * 68)
    for rel, why in PROTECTED:
        full = where(rel)
        state = human(size_of(full)) if os.path.exists(full) else "absent"
        print(f"  {rel:<24} {state:>9}   {why}")
    used = sorted(p for p in keep if os.path.exists(os.path.join(HERE, p)))
    print(f"\n  + {len(used)} file(s) used by the app:")
    print("    " + ", ".join(used))

    if not groups:
        print("\nNothing to clean up.\n")
        return 0

    print("\n" + "=" * 68)
    print("  PROPOSED FOR DELETION")
    print("=" * 68)
    for title, why, items in groups:
        print(f"\n  {title}")
        print(f"    {why}")
        for rel, full, size in items:
            kind = "folder" if os.path.isdir(full) else "file"
            print(f"      · {rel:<30} {kind:<8} {human(size):>9}")
    print(f"\n  Total: {human(total)}")

    if args.list:
        print("\n(list mode: nothing was deleted)\n")
        return 0

    if not args.yes:
        print()
        if input("  Delete all of this? [y/N] ").strip().lower() not in ("y", "yes"):
            print("\n  Cancelled.\n")
            return 0

    removed, failed = 0, []
    for _, _, items in groups:
        for rel, full, _ in items:
            try:
                shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
                removed += 1
            except OSError as exc:
                failed.append((rel, str(exc)))

    print(f"\n  {removed} item(s) deleted, {human(total)} freed.")
    for rel, why in failed:
        print(f"  Could not delete {rel}: {why}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
