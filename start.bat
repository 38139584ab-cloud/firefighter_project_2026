@echo off
title Fire Equipment Detection System
cd /d "%~dp0"

echo ===================================================
echo     Fire Equipment Detection System
echo ===================================================
echo.

:: Only run git pull if this is a git project
if exist .git (
    echo [1/3] Checking for updates...
    git pull origin main
) else (
    echo [1/3] Not a git project, skipping update check.
)

echo.
echo [2/3] Installing required Python packages, please wait...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo [3/3] Starting the system...
echo The browser opens automatically when the system is ready (this can take
echo up to a minute the first time). Do not close this window; close it to quit.
echo ===================================================

:: Gradio opens the browser itself (inbrowser=True) once the server is up
python main.py

echo.
echo The system has stopped. If you see an error above, please send it to the developer.
pause
