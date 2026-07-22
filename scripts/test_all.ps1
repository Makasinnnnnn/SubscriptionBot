$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
& $python -m ruff check app tests main.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m compileall -q app main.py tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m pip check
exit $LASTEXITCODE
