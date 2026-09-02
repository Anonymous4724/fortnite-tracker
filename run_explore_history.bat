@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  Probing Cito's past-tournament endpoints (~8 requests).
echo.
python explore_history.py
echo.
pause
