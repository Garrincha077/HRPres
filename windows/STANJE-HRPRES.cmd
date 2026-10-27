@echo off
chcp 65001 >nul
"%~dp0.venv\Scripts\python.exe" -X utf8 -m hrpres status --db "%~dp0data\state.sqlite3"
pause
