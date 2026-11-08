@echo off
setlocal
cd /d "%~dp0"
if not exist "PikminRoute.exe" (
    echo PikminRoute.exe was not found. Keep it next to Start.bat.
    pause
    exit /b 1
)
start "" "%~dp0PikminRoute.exe"
