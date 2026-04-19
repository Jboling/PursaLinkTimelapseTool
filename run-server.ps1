# Start the Prusa Snapshot Companion (reads HOST/PORT and secrets from .env).
#
# Run from any directory, for example:
#   powershell -ExecutionPolicy Bypass -File "C:\Coding\PrusaLinkConnector\run-server.ps1"
# Or add that path to your PATH and run: run-server.ps1

$ErrorActionPreference = "Stop"

# Project root = folder this script lives in (works no matter what the current directory is).
$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) {
    $ProjectRoot = "C:\Coding\PrusaLinkConnector"
}
Set-Location -LiteralPath $ProjectRoot

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Virtual environment not found under $ProjectRoot" -ForegroundColor Yellow
    Write-Host "Create it and install deps:" -ForegroundColor Yellow
    Write-Host "  cd `"$ProjectRoot`"" -ForegroundColor Cyan
    Write-Host "  python -m venv .venv" -ForegroundColor Cyan
    Write-Host "  .\.venv\Scripts\pip install -r requirements.txt" -ForegroundColor Cyan
    exit 1
}

Write-Host "Starting server from $ProjectRoot (HOST/PORT from .env)..." -ForegroundColor Green
& $venvPython -m app.main
