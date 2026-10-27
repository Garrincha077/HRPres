@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv-vectors\Scripts\python.exe" -X utf8 -m hrpres.continuous search --root "%~dp0."
if errorlevel 1 pause
