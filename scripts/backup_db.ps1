param(
    [string]$OutputDirectory = "backups",
    [int]$RetentionCount = $(if ($env:BACKUP_RETENTION_COUNT) { [int]$env:BACKUP_RETENTION_COUNT } else { 14 })
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backupDir = [System.IO.Path]::GetFullPath((Join-Path $root $OutputDirectory))
if (-not $backupDir.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup directory must be inside the project directory."
}
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

$compose = Join-Path $PSScriptRoot "docker_compose.cmd"
$timestamp = [DateTime]::UtcNow.ToString("yyyyMMdd_HHmmss")
$name = "subscription_$timestamp.dump"
$containerPath = "/tmp/$name"

& $compose exec -T postgres pg_isready -U subscription -d subscription | Out-Null
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL container is not ready." }
& $compose exec -T postgres pg_dump -U subscription -d subscription -Fc -f $containerPath
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed." }

$container = (& $compose ps -q postgres).Trim()
if (-not $container) { throw "PostgreSQL container was not found." }
$docker = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"
$destination = Join-Path $backupDir $name
& $docker cp "${container}:$containerPath" $destination
& $compose exec -T postgres pg_restore -l $containerPath | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Backup integrity check failed." }
& $compose exec -T postgres rm -f $containerPath

if ($RetentionCount -gt 0) {
    Get-ChildItem -LiteralPath $backupDir -File -Filter "subscription_*.dump" |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -Skip $RetentionCount |
        Remove-Item -Force
}
Write-Host "Backup created and verified: $destination"
