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
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$composeArgs = @("--project-directory", $root, "-f", (Join-Path $root "docker-compose.yml"))
if (Test-Path -LiteralPath (Join-Path $root ".env.production")) {
    $composeArgs += @("--env-file", (Join-Path $root ".env.production"), "-f", (Join-Path $root "docker-compose.production.yml"))
}
& $compose @composeArgs exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL container is not ready." }
$container = (& $compose @composeArgs ps -q postgres).Trim()
if ($LASTEXITCODE -ne 0 -or -not $container) { throw "PostgreSQL container was not found." }
$docker = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"
$containerPath = "/tmp/restore.dump"
& $docker cp $resolved "${container}:$containerPath"
if ($LASTEXITCODE -ne 0) { throw "Copying backup failed." }
& $compose @composeArgs exec -T postgres pg_restore -l $containerPath | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Backup integrity check failed." }
& $compose @composeArgs stop app bot ops scheduler
if ($LASTEXITCODE -ne 0) { throw "Stopping database writers failed." }
try {
    & $compose @composeArgs exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --single-transaction --exit-on-error /tmp/restore.dump'
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed; services remain stopped." }
} finally {
    & $compose @composeArgs exec -T postgres rm -f $containerPath
}
& $compose @composeArgs start app bot ops scheduler
if ($LASTEXITCODE -ne 0) { throw "Database restored, but restarting services failed." }
Write-Host "Database restored from: $resolved"
