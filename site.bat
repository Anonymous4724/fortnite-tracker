@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
rem  The code lives in src\ (or beside this file, in a copy laid out flat).
set "SRC=%~dp0src"
if not exist "%SRC%\refresh.py" set "SRC=%~dp0."
rem  Python writes its output as UTF-8 whatever the console is set to, so a
rem  run sent to a file does not stop on an accent it cannot spell.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem  --quiet is this file's own: it means "nobody is watching". The run goes
rem  to data\refresh.log and the window does not wait for a key at the end,
rem  which is what a scheduled task needs. Everything else goes to refresh.py.
set "QUIET="
set "ARGS="
for %%A in (%*) do (
  if /I "%%~A"=="--quiet" (set "QUIET=1") else (set "ARGS=!ARGS! %%~A")
)
rem  One launcher, chosen once: running the whole thing twice because the
rem  first attempt failed would put half a run on top of the other.
set "PY=py"
where py >nul 2>nul || set "PY=python"
if defined QUIET (
  if not exist "data" mkdir "data"
  echo.>> "data\refresh.log"
  echo ===== %DATE% %TIME% site ===== >> "data\refresh.log"
  %PY% "%SRC%\refresh.py" --page --publish!ARGS! >> "data\refresh.log" 2>&1
  exit /b !ERRORLEVEL!
)
echo ===============================================================
echo  Site: the page and the week's calendar, put online.
echo ===============================================================
echo.
echo   site.bat            rebuild the page, refresh the calendar and push -
echo                       seconds, not minutes. The model stays as it is;
echo                       update.bat is what renews that.
echo   site.bat --quiet    no pause: for a scheduled task, written to
echo                       data\refresh.log
echo.
%PY% "%SRC%\refresh.py" --page --publish!ARGS!
pause
