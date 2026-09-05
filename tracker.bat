@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "SRC=%~dp0src"
if not exist "%SRC%\app.py" set "SRC=%~dp0."
set "PYTHONUTF8=1"
set "PY=py"
where py >nul 2>nul || set "PY=python"
echo Installing dependencies (first run only)...
%PY% -m pip install -r requirements.txt --quiet
echo.
echo Starting Fortnite Comp Tracker...
%PY% "%SRC%\app.py"
pause
