@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  Check: does everything a form submits actually reach the database?
echo  (throwaway test database, your data is never touched)
echo.
python check_forms.py
echo.
pause
