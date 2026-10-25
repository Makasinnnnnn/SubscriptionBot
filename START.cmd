@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\production.ps1" start
if errorlevel 1 (
    echo Startup failed. See the error above.
    pause
    exit /b 1
)
pause
