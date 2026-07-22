@echo off
cd /d "%~dp0.."
set "PATH=%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin;%PATH%"
"%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe" run --rm -it cloudflare/cloudflared:latest tunnel --url http://host.docker.internal:8000
