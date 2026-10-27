@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv-vectors\Scripts\python.exe" -X utf8 "%~dp0app\src\hrpres\onnx_pilot.py" interactive --bundle "%~dp0vectors\pilot-25-minilm-onnx"
if errorlevel 1 pause
