@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  Recomputing scoring tables from data already in the database.
echo  No API request is spent.
echo.
python recompute_scoring.py
echo.
pause
