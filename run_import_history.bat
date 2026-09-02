@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  Catalog of finished Epic tournaments (no import, ~6 requests).
echo.
python import_history.py --list
echo.
echo  To import afterwards: python import_history.py
echo.
pause
