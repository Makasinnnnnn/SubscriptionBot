@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m ruff check app tests main.py
if errorlevel 1 exit /b %errorlevel%
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 exit /b %errorlevel%
".venv\Scripts\python.exe" -m compileall -q app main.py tests
if errorlevel 1 exit /b %errorlevel%
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 exit /b %errorlevel%
