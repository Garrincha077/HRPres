@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -X utf8 -m hrpres.windows_menu --root "%~dp0."
if errorlevel 1 pause
