"""Research layer: refit, diagnose and cross-validate the model in `calibration.py`.

Kept out of the app on purpose. The app is stdlib-only so it runs anywhere; this
folder needs numpy/pandas/scipy/matplotlib and is allowed to. The dependency
arrow points one way — `analysis` imports `db`/`calibration`, never the reverse.
"""
from __future__ import annotations

import os
import sys

# The app's modules live in `src/` beside this folder (or flat beside it, in a
# copy laid out the old way): put that folder first, so `import db` resolves
# wherever `python -m analysis.fit` is run from.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CODE = os.path.join(_ROOT, "src") if os.path.exists(os.path.join(_ROOT, "src", "db.py")) else _ROOT
if _CODE not in sys.path:
    sys.path.insert(0, _CODE)


def _require(*packages: str) -> None:
    """Name the missing dependency and the line that installs it.

    The app needs none of these, so somebody who has only ever run the app
    arrives here with a bare ModuleNotFoundError and no reason to guess that this
    one folder has requirements of its own. Say so instead.
    """
    import importlib.util
    missing = [p for p in packages if importlib.util.find_spec(p) is None]
    if not missing:
        return
    where = os.path.join("analysis", "requirements.txt")
    raise SystemExit(
        f"\nanalysis/ needs {', '.join(missing)}, which {'is' if len(missing) == 1 else 'are'}"
        f" not installed.\n"
        f"The app itself does not — only this research layer does.\n\n"
        f"    pip install -r {where}\n\n"
        f"On Windows, if `pip` is not found:  python -m pip install -r {where}\n")


_require("numpy", "pandas", "scipy")
