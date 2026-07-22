@echo off
call "%~dp0docker_compose.cmd" logs -f --tail=200 scheduler
