@echo off
cd /d "%~dp0.."
set APP_MODE=bot
".venv\Scripts\python.exe" main.py
