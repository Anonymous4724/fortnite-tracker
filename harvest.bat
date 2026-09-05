@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "SRC=%~dp0src"
if not exist "%SRC%\harvest_osirion.py" set "SRC=%~dp0."
set "PYTHONUTF8=1"
set "PY=py"
where py >nul 2>nul || set "PY=python"
echo ===============================================================
echo  Osirion harvest - free public API, no key, no credits.
echo ===============================================================
echo.
echo It holds sleep off by itself while it runs, and lets the
echo machine sleep normally again the moment it stops. The screen
echo can still go dark - only the machine is kept awake.
echo.
echo Progress is copied to data\osirion\harvest.log, so you can
echo close this window and still read what happened.
echo.
echo Newest sessions first, wide before deep: 3 pages of every
echo session, then 10, then 30, then the API's maximum of 101.
echo Each page is 100 teams, so 3 pages is the top 300.
echo.
echo Safe to interrupt with Ctrl+C. Starting again resumes.
echo.
echo Narrower runs, if you would rather:
echo   %PY% src\harvest_osirion.py --catalogue --fetch --from-year 2026
echo   %PY% src\harvest_osirion.py --catalogue --fetch --only fncs
echo.
%PY% "%SRC%\harvest_osirion.py" --catalogue --fetch --rate 58
echo.
echo Fetching finished or interrupted. Nothing has touched your database.
echo Turn what was downloaded into tournaments, and put them on the site:
echo   update.bat
pause
