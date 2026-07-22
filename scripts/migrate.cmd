@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m alembic upgrade head
".venv\Scripts\python.exe" -m alembic current
