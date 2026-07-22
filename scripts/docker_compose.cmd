@echo off
set "PATH=%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin;%PATH%"
"%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe" compose %*
