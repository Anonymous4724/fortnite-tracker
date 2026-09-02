"""Research layer: refit, diagnose and cross-validate the model in `calibration.py`.

Kept out of the app on purpose. The app is stdlib-only so it runs anywhere; this
folder needs numpy/pandas/scipy/matplotlib and is allowed to. The dependency
arrow points one way — `analysis` imports `db`/`calibration`, never the reverse.
"""
from __future__ import annotations

import os
import sys

# Run as `python -m analysis.fit` from the repo root and this is redundant; run it
# from anywhere else and it is what makes `import db` resolve.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
