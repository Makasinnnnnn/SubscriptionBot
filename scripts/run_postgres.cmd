@echo off
cd /d "%~dp0.."
"C:\postgresql-18.4-2-windows-x64-binaries\pgsql\bin\postgres.exe" -D ".pgdata" -h 127.0.0.1 -p 5432
