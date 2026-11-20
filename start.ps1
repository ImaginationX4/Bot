$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    npm.cmd ci --prefer-offline --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    uv run --locked uvicorn server:app --host 127.0.0.1 --port 8000
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
