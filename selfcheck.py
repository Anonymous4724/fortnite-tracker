"""One-shot check of everything before the folder goes to GitHub."""
import io
import os
import re
import subprocess
import sys

OK, FAIL = [], []


def check(name, condition, detail=""):
    (OK if condition else FAIL).append(f"{name} {detail}".strip())
    print(f"  {'ok  ' if condition else 'FAIL'}  {name} {detail}")


def run(cmd, timeout=600):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


print("\n1. every module imports")
for mod in ("app", "db", "calibration", "predict", "cito", "tracking", "team_stats",
            "scoring_infer", "i18n", "backtest", "cleanup", "import_history",
            "explore_history", "recompute_scoring", "check_forms",
            "osirion", "harvest_osirion"):
    code, out = run(f'python3 -c "import {mod}"')
    check(mod, code == 0, out.strip().splitlines()[-1] if code else "")

print("\n2. every page answers, in both languages")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402
import db  # noqa: E402

client = app.app.test_client()
with db.session() as conn:
    ids = [c["id"] for c in db.list_competitions(conn)][:3]
pages = ["/", "/history", "/train", "/scoring", "/manual", "/series"]
pages += [f"/competition/{i}" for i in ids] + [f"/prepare/{i}" for i in ids]
FRENCH = re.compile(r"\b(barème|seuil|palier|manche|tournoi|équipe|aucun|relevé|"
                    r"entraîn|saisie|prédiction|réglage|fourchette)\w*", re.I)
for lang in ("en", "fr"):
    client.set_cookie("lang", lang)
    bad, leaked = [], []
    for url in pages:
        response = client.get(url)
        if response.status_code != 200:
            bad.append((url, response.status_code))
            continue
        if lang == "en":
            body = response.get_data(as_text=True).split("<main")[-1]
            # the owner's own notes are stored in French; only markup is checked
            body = re.sub(r'"note":\s*"[^"]*"', "", body)
            hits = set(FRENCH.findall(body)) - {"seuils"}
            if hits:
                leaked.append((url, sorted(hits)))
    check(f"{lang}: {len(pages)} pages", not bad, str(bad))
    if lang == "en":
        check("en: no French left in the markup", not leaked, str(leaked)[:120])

print("\n3. language switch")
r = client.get("/language/fr")
check("/language/fr redirects and sets the cookie",
      r.status_code == 302 and "lang=fr" in str(r.headers))

print("\n4. the checks that ship with the repo")
for name, cmd, needle in (
    ("check_forms.py", "python3 check_forms.py", "Nothing gets lost"),
    ("fuzz_api.py", "python3 fuzz_api.py", "server errors: 0"),
    ("i18n.py", "python3 i18n.py", "0 missing"),
    ("test_osirion.py", "python3 test_osirion.py", "answers are known"),
    ("backtest.py", "python3 backtest.py", "competition"),
):
    code, out = run(cmd)
    check(name, needle in out, "" if needle in out else out.strip()[-140:])

code, out = run("python3 cleanup.py --list")
# Caches are meant to be offered; anything else means a live file went missing
# from the reachability walk.
offered = [l.split("·")[1].split()[0] for l in out.splitlines() if l.strip().startswith("·")]
check("cleanup.py --list offers only caches",
      all(o.endswith("__pycache__") for o in offered), str(offered))

print("\n5. the research layer")
for mod in ("data", "fit", "diagnostics", "validate", "anchor"):
    code, out = run(f"python3 -m analysis.{mod}", timeout=900)
    check(f"analysis.{mod}", code == 0, out.strip()[-120:] if code else "")

print("\n6. no French left in the source")
leaks = []
for folder, names in (("", os.listdir(".")), ("templates/", os.listdir("templates")),
                      ("static/", os.listdir("static"))):
    for name in names:
        path = folder + name
        if not path.endswith((".py", ".html", ".js")) or path == "i18n.py":
            continue
        text = io.open(path, encoding="utf-8").read()
        for n, line in enumerate(text.splitlines(), 1):
            # db.py talks about French tournament names on purpose; that comment
            # says so in English and names the words it matches.
            if "the French for" in line or "French" in line:
                continue
            if line.lstrip().startswith(("#", "//", "*", "/*")) and FRENCH.search(line):
                leaks.append(f"{path}:{n}")
check("comments are English", not leaks, str(leaks[:5]))

print("\n7. nothing private, nothing secret")
private = re.compile(r"stopk|C:\\\\Users|@gmail\.", re.I)
found = []
for root, dirs, names in os.walk("."):
    dirs[:] = [d for d in dirs if d not in ("data", "__pycache__", ".git", "figures")]
    for name in names:
        if name.endswith((".py", ".html", ".js", ".md", ".bat", ".sh", ".txt")):
            path = os.path.join(root, name)
            if path.endswith("selfcheck.py"):
                continue                        # this file quotes the pattern itself
            if private.search(io.open(path, encoding="utf-8", errors="ignore").read()):
                found.append(path)
check("no personal paths or addresses", not found, str(found))
check("no key file in the tree",
      not any(os.path.exists(f) for f in ("cito_key.txt", "cle_cito.txt")))

print("\n" + "=" * 68)
print(f"  {len(OK)} checks passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"    - {f}")
print("=" * 68 + "\n")
sys.exit(1 if FAIL else 0)
