param(
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [switch]$Force,
    [switch]$SkipSafetyBackup
)

$ErrorActionPreference = "Stop"
$resolved = (Resolve-Path -LiteralPath $BackupPath -ErrorAction Stop).Path
if ([System.IO.Path]::GetExtension($resolved) -notin @(".dump", ".backup")) {
    throw "Only .dump or .backup files are accepted."
}
if (-not $Force) {
    $answer = Read-Host "Restore will replace database objects. Type RESTORE to continue"
    if ($answer -cne "RESTORE") { throw "Restore cancelled." }
}
if (-not $SkipSafetyBackup) {
    & (Join-Path $PSScriptRoot "backup_db.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Safety backup failed; restore cancelled." }
}

$compose = Join-Path $PSScriptRoot "docker_compose.cmd"
& $compose exec -T postgres pg_isready -U subscription -d subscription | Out-Null
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL container is not ready." }
$container = (& $compose ps -q postgres).Trim()
$docker = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"
$containerPath = "/tmp/restore.dump"
& $docker cp $resolved "${container}:$containerPath"
& $compose exec -T postgres pg_restore -l $containerPath | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Backup integrity check failed." }
& $compose exec -T postgres pg_restore -U subscription -d subscription --clean --if-exists --no-owner $containerPath
if ($LASTEXITCODE -ne 0) { throw "pg_restore failed." }
& $compose exec -T postgres rm -f $containerPath
Write-Host "Database restored from: $resolved"
