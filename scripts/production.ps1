param(
    [ValidateSet("prepare", "start", "stop", "status", "logs")]
    [string]$Action = "start"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envPath = Join-Path $projectRoot ".env.production"
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Create .env.production from config/production.example.txt and fill in the settings."
}
$docker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $docker) {
    $docker = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"
}
if (-not (Test-Path -LiteralPath $docker)) { throw "Docker Desktop is not installed." }

# Keep interpolation and container env_file pointed at the same file.
$env:APP_ENV_FILE = $envPath
$composeArgs = @(
    "compose", "--project-directory", $projectRoot,
    "--env-file", $envPath,
    "-f", (Join-Path $projectRoot "docker-compose.yml"),
    "-f", (Join-Path $projectRoot "docker-compose.production.yml")
)
function Invoke-Compose {
    & $docker @composeArgs @args
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed (exit $LASTEXITCODE)." }
}

Invoke-Compose config --quiet
switch ($Action) {
    "stop" { Invoke-Compose stop; break }
    "status" { Invoke-Compose ps -a; break }
    "logs" { Invoke-Compose logs --tail 100 app bot scheduler ops migrations; break }
    default {
        Invoke-Compose build
        # Validate inside the actual image without connecting to Telegram or the DB.
        Invoke-Compose run --rm --no-deps app python -c "from app.core.config import get_settings; get_settings(); print('Configuration OK')"
        if ($Action -eq "prepare") {
            Write-Host "Ready. Run START.cmd to start the services."
            break
        }
        Invoke-Compose up -d --wait postgres
        & (Join-Path $PSScriptRoot "backup_db.ps1")
        if (-not $?) { throw "Backup failed; migration was not started." }
        # Stop writers before schema changes; a failed migration leaves them stopped.
        Invoke-Compose stop app bot scheduler ops
        Invoke-Compose run --rm --no-deps migrations
        Invoke-Compose up -d --no-build --no-deps --wait app bot scheduler ops
        Invoke-Compose ps
        Write-Host "Started. Local status: http://127.0.0.1:8000/health/db"
    }
}
