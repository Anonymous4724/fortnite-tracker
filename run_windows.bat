@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Installing dependencies (first run only)...
py -m pip install -r requirements.txt --quiet 2>nul || python -m pip install -r requirements.txt --quiet
echo.
echo Starting Fortnite Comp Tracker...
py app.py 2>nul || python app.py
pause
