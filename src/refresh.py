"""One command: take in what the harvest downloaded, and put it on the site.

The harvest runs for hours and can be left alone. This is what runs after it —
or beside it, since every phase picks up where it left off — and it is the only
command the day-to-day needs:

    harvest --build     derive competitions from the pages on disk — new ones only,
                        or all of them once when the naming rules have changed
    pull_live           the live feed's readings of the cups it watched, filed
                        against those tournaments by Epic's ids
    analysis.live       the pace tables the live forecast reads, replayed from
                        the boards - when the last ones are PACE_MAX_AGE_DAYS old
    export_model        write model.json, refused unless it reproduces the model
    calendar_snapshot   what is on this week, for the predictor's opening list
    build               index.html + model.js + calendar.js, and standalone.html
    git (--publish)     commit the predictor and push, so the site updates

    python src/refresh.py                 everything above, no push
    python src/refresh.py --publish       and push the predictor repository
    python src/refresh.py --page --publish
                                      the page and the week's calendar: rebuild
                                      the page from its sources, refresh the
                                      calendar (seconds), and push - for a change
                                      to the page, the READMEs or the settings
                                      files, when the model can stay as it is
                                      (the predictor's own publish.py sets that
                                       up the first time, in one command)
    python src/refresh.py --fetch         a shallow harvest pass first (new windows)
    python src/refresh.py --dry-run       say what would run, run nothing

`--fetch` is the "catch up" mode: it runs the harvester's first pass — three
pages per window, new windows only — which is minutes once the calendar has been
covered and is what keeps this week's tournaments flowing into the model. Deeper
passes are the harvester's own business and take hours; leave them to
`harvest.bat` overnight.

Scheduled on Windows, this is the whole pipeline running itself. From an
administrator prompt, every day at 06:00:

    schtasks /Create /SC DAILY /ST 06:00 /TN "Fortnite refresh" /RU "%USERNAME%" /IT ^
        /TR "\\"C:\\path\\to\\fortnite-tracker\\refresh.bat\\" --fetch --publish --quiet"

`--quiet` belongs to the .bat rather than to this script: no window waiting on
a key at the end, the whole run appended to `data/refresh.log`.

Once a week is the floor, whatever else is skipped: the calendar the site ships
covers seven days, and it is what the live feed reads to know which cups are
under way. A calendar older than that and the feed follows nothing - and a cup
Epic announces between two runs is a cup the feed never followed. The
predictor repository's own workflow (.github/workflows/calendar.yml) refreshes
the calendar every three hours from GitHub for that reason, and this run takes
its commits in before writing anything, so the two never disagree on a file
both of them write.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The code lives in `src/`, the database and the launchers one level up; a
# copy laid out flat still works. Everything below runs from ROOT.
ROOT = HERE.parent if HERE.name == "src" else HERE
CANDIDATES = ["../threshold-ladder", "../predictor"]     # beside the tracker's folder


def find_predictor(given: str | None) -> Path | None:
    for candidate in ([given] if given else CANDIDATES):
        path = (ROOT / candidate).resolve()
        if (path / "build.py").exists() and (path / "src" / "app.html").exists():
            return path
    return None


def run(label: str, *command: str, cwd: Path | None = None, dry: bool = False) -> bool:
    print(f"\n{label}")
    print("  " + " ".join(command))
    if dry:
        return True
    result = subprocess.run(list(command), cwd=str(cwd or ROOT))
    if result.returncode:
        print(f"\n  {label.split('.')[-1].strip()} failed (exit {result.returncode}). "
              f"Stopping here: nothing after this step ran.")
    return result.returncode == 0


# The files a run writes from scratch: when GitHub's workflow wrote them too
# since the last pull, this run's copy is the newer one and wins the rebase.
GENERATED = {"calendar.js", "index.html", "standalone.html", "model.js", "model.json"}


def settle_generated(repo: Path) -> bool:
    """Finish a rebase that stopped on the generated files alone, keeping
    this run's copy of each; False when anything else is in conflict."""
    listed = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=str(repo),
                            capture_output=True, text=True)
    conflicted = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not conflicted or any(name not in GENERATED for name in conflicted):
        return False
    # In a rebase "theirs" is the commit being replayed: this folder's own.
    for name in conflicted:
        if subprocess.run(["git", "checkout", "--theirs", "--", name], cwd=str(repo),
                          capture_output=True).returncode:
            return False
        subprocess.run(["git", "add", "--", name], cwd=str(repo), capture_output=True)
    done = subprocess.run(["git", "-c", "core.editor=true", "rebase", "--continue"], cwd=str(repo),
                          capture_output=True, text=True)
    return done.returncode == 0


# How old the pace tables may get before a run replays them. Three days: the
# feed's evenings of a cup's week reach its own pace row before its next week.
PACE_MAX_AGE_DAYS = 3
PACE_PATH = ROOT / "analysis" / "pace.json"


def pace_stale() -> bool:
    """Are the pace tables missing, or older than PACE_MAX_AGE_DAYS?"""
    if not (ROOT / "analysis" / "live.py").exists():
        return False
    if not PACE_PATH.exists():
        return True
    try:
        generated = json.loads(PACE_PATH.read_text(encoding="utf-8")).get("generated") or ""
        age = (date.today() - date.fromisoformat(str(generated)[:10])).days
    except (OSError, ValueError):
        return True
    return age >= PACE_MAX_AGE_DAYS


def untrack_ignored(repo: Path, dry: bool = False) -> None:
    """Take out of the repository what `.gitignore` now says stays home.

    An ignore rule only ever applies to files git is not already following, so
    a rule added after a file was committed leaves it in the repository — and
    on the site — for good. The files stay on disk; they stop being published.
    """
    listed = subprocess.run(["git", "ls-files", "-i", "-c", "--exclude-standard"],
                            cwd=str(repo), capture_output=True, text=True)
    files = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if listed.returncode or not files:
        return
    print(f"\n  {len(files)} file(s) now ignored are still in the repository — taking "
          "them out:\n    " + "\n    ".join(files))
    if not dry:
        subprocess.run(["git", "rm", "--cached", "-q", "--", *files], cwd=str(repo))


def summarise(model_path: Path) -> str:
    if not model_path.exists():
        return "no model yet"
    try:
        model = json.loads(model_path.read_text(encoding="utf-8"))
        source = model.get("source") or {}
        return (f"{source.get('tournaments', '?')} tournaments, "
                f"{len(model.get('categories') or [])} categories, "
                f"exported {model.get('generated', '?')}")
    except (ValueError, OSError):
        return "unreadable"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", help="the predictor repository, if not beside this one")
    parser.add_argument("--fetch", action="store_true",
                        help="run a shallow harvest pass first, for new windows")
    parser.add_argument("--publish", action="store_true",
                        help="commit and push the predictor repository afterwards")
    parser.add_argument("--page", action="store_true",
                        help="the page and the calendar alone: no harvest, no model, no feed")
    parser.add_argument("--pace", action="store_true",
                        help="replay the pace tables now, whatever their age")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands and run none of them")
    args = parser.parse_args()
    py = sys.executable

    predictor = find_predictor(args.to)
    if not predictor:
        print("Cannot find the predictor repository (build.py + src/app.html).")
        print("Looked beside this folder; point at it with --to ../threshold-ladder")
        return 1
    print(f"tracker   : {ROOT}")
    print(f"predictor : {predictor}")
    print(f"before    : {summarise(predictor / 'model.json')}")

    step = 0
    def label(text):
        nonlocal step
        step += 1
        return f"{step}. {text}"

    if args.page and args.fetch:
        print("--page rebuilds the page alone; --fetch is a harvest pass. Pick one.")
        return 1
    # What GitHub wrote since the last run - the workflow's calendar refreshes
    # - comes in first, before this run rewrites the same files on top of it.
    # A pull that fails (no network, a conflict) is not a reason to stop: the
    # publish step pulls again, and settles the generated files by itself.
    if args.publish and (predictor / ".git").exists():
        if not run(label("Take in what GitHub wrote since the last run"),
                   "git", "pull", "--rebase", "--quiet", cwd=predictor, dry=args.dry_run):
            subprocess.run(["git", "rebase", "--abort"], cwd=str(predictor), capture_output=True)
            print("  (could not pull now - the publish step tries again)")
    if args.page:
        # Everything the page is built from is already on disk - the model of
        # the last full run, the sources, the settings files - except the
        # week's calendar, which is a few requests and goes stale by the day.
        if not run(label("The week ahead, for the predictor's opening list"),
                   py, str(HERE / "calendar_snapshot.py"), "--out", str(predictor / "calendar.js"),
                   dry=args.dry_run):
            print("  (the calendar could not be refreshed — the site keeps the previous one)")
        if not run(label("Build the site and the standalone file"),
                   py, str(predictor / "build.py"), cwd=predictor, dry=args.dry_run):
            return 1
    if not args.page and args.fetch:
        if not run(label("Harvest — a shallow pass over new windows"),
                   py, str(HERE / "harvest_osirion.py"), "--catalogue", "--fetch", "--passes", "3",
                   dry=args.dry_run):
            return 1
    if not args.page and not run(label("Harvest — derive competitions from what is on disk"),
                                 py, str(HERE / "harvest_osirion.py"), "--build", dry=args.dry_run):
        return 1
    # The live feed's readings, filed against the tournaments just built. A
    # feed that cannot be reached is not a reason to stop: the readings wait
    # there for a month.
    if not args.page:
        if not run(label("The live feed's readings, into the database"),
                   py, str(HERE / "pull_live.py"), dry=args.dry_run):
            print("  (the feed could not be pulled — its readings keep for a month, next run)")
        # The pace tables: every board replayed game by game, minutes of
        # work, so every few days rather than every run - the feed's evenings
        # of the week join the cups' own paces then. A failed replay is not a
        # reason to stop: the export carries the tables of the last one.
        if args.pace or pace_stale():
            if not run(label("The pace tables, replayed from the boards (a few minutes)"),
                       py, "-m", "analysis.live", cwd=ROOT, dry=args.dry_run):
                print("  (the pace tables could not be replayed — the export keeps the last ones)")
        if not run(label("Export the model (refused unless it reproduces the Python model)"),
                   py, str(HERE / "export_model.py"), dry=args.dry_run):
            return 1
        if not args.dry_run:
            shutil.copy(ROOT / "model.json", predictor / "model.json")
        if not run(label("The week ahead, for the predictor's opening list"),
                   py, str(HERE / "calendar_snapshot.py"), "--out", str(predictor / "calendar.js"),
                   dry=args.dry_run):
            # A calendar that could not be fetched is not a reason to ship a stale
            # model: the site works without the panel. Say so and carry on.
            print("  (the calendar could not be refreshed — the site keeps the previous one)")
        if not run(label("Build the site and the standalone file"),
                   py, str(predictor / "build.py"), cwd=predictor, dry=args.dry_run):
            return 1

    if args.publish and not (predictor / ".git").exists():
        # A first push needs a repository and a remote, which is a different
        # job with different failure modes; publish.py does it and says what it
        # is doing. Failing here with "not a git repository" would say nothing.
        print("\nThe predictor has never been published: there is no repository yet.")
        print(f"Run this once, and the site is live and stays live:\n"
              f"    cd {predictor}\n    python publish.py")
        return 1
    if args.publish:
        stamp = date.today().isoformat()
        message = f"Site update {stamp}" if args.page else f"Model refresh {stamp}"
        untrack_ignored(predictor, dry=args.dry_run)
        # The pull takes in what was done on GitHub itself - a CNAME file
        # written from the settings page, a file edited on the site - which
        # would otherwise have the push refused as "non-fast-forward". Rebasing
        # keeps this folder's commits on top of them, in one straight line.
        for command in (["git", "add", "-A"],
                        ["git", "commit", "-m", message],
                        ["git", "pull", "--rebase", "--quiet"],
                        ["git", "push"]):
            if not run(label("Publish: " + " ".join(command[:2])), *command,
                       cwd=predictor, dry=args.dry_run):
                # "nothing to commit" exits 1; that is not a failure worth
                # stopping on, and an earlier push may still be owed.
                if command[1] == "commit":
                    print("  (nothing changed since the last publish)")
                    continue
                if command[1] == "pull":
                    if settle_generated(predictor):
                        print("  (the workflow had refreshed the calendar meanwhile: this run's "
                              "copy of the generated files kept, the rest taken in)")
                        continue
                    subprocess.run(["git", "rebase", "--abort"], cwd=str(predictor),
                                   capture_output=True)
                    print("\n  GitHub has changes this folder could not take in by itself.")
                    print("  Run `git status` in the predictor folder and settle them, then run this again.")
                return 1

    print(f"\nafter     : {summarise(predictor / 'model.json')}")
    if not args.publish:
        print("Not pushed. Add --publish to put it on the site, or commit the predictor yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
