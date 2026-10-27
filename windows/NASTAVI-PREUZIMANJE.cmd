@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -X utf8 -m hrpres download --db "%~dp0data\state.sqlite3" --max-new 1000
pause
