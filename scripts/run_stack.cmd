@echo off
cd /d "%~dp0.."
set "PATH=%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin;%PATH%"
"%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe" compose up --build
