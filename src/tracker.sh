#!/usr/bin/env bash
# The local tracker app, on Linux or macOS. Windows: tracker.bat at the root.
cd "$(dirname "$0")"
python3 -m pip install -r ../requirements.txt --quiet 2>/dev/null || python3 -m pip install -r requirements.txt --quiet
python3 app.py
