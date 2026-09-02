@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Osirion harvest - free public API, no key needed.
echo.
echo First a three-call check, so you can see what it found before committing
echo to a long download.
echo.
py harvest_osirion.py --check 2>nul || python harvest_osirion.py --check
echo.
echo ---------------------------------------------------------------
echo Press a key to start the full harvest, or close this window now.
echo It can run for hours. Stopping it is safe: it picks up where it
echo left off next time.
echo ---------------------------------------------------------------
pause >nul
py harvest_osirion.py 2>nul || python harvest_osirion.py
pause
