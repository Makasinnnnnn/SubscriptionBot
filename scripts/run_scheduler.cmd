@echo off
cd /d "%~dp0.."
set APP_MODE=scheduler
".venv\Scripts\python.exe" main.py
