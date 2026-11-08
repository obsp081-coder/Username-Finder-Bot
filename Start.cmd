@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Project environment is missing. Read README.md for setup.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m username_finder
