param(
    [string]$OutputDirectory = "backups",
    [int]$RetentionCount = $(if ($env:BACKUP_RETENTION_COUNT) { [int]$env:BACKUP_RETENTION_COUNT } else { 14 })
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backupDir = [System.IO.Path]::GetFullPath((Join-Path $root $OutputDirectory))
if (-not $backupDir.StartsWith($root + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup directory must be inside the project directory."
}
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

$compose = Join-Path $PSScriptRoot "docker_compose.cmd"
$composeArgs = @("--project-directory", $root, "-f", (Join-Path $root "docker-compose.yml"))
if (Test-Path -LiteralPath (Join-Path $root ".env.production")) {
    $composeArgs += @("--env-file", (Join-Path $root ".env.production"), "-f", (Join-Path $root "docker-compose.production.yml"))
}
$timestamp = [DateTime]::UtcNow.ToString("yyyyMMdd_HHmmss")
$name = "subscription_$timestamp.dump"
$containerPath = "/tmp/$name"

& $compose @composeArgs exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL container is not ready." }
& $compose @composeArgs exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$1"' -- $containerPath
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed." }

$container = (& $compose @composeArgs ps -q postgres).Trim()
if (-not $container) { throw "PostgreSQL container was not found." }
$docker = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"
$destination = Join-Path $backupDir $name
& $docker cp "${container}:$containerPath" $destination
if ($LASTEXITCODE -ne 0) { throw "Copying backup failed." }
& $compose @composeArgs exec -T postgres pg_restore -l $containerPath | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Backup integrity check failed." }
& $compose @composeArgs exec -T postgres rm -f $containerPath

if ($RetentionCount -gt 0) {
    Get-ChildItem -LiteralPath $backupDir -File -Filter "subscription_*.dump" |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -Skip $RetentionCount |
        Remove-Item -Force
}
Write-Host "Backup created and verified: $destination"
