$ErrorActionPreference = "Stop"

# Keep machine-specific paths and hostnames outside version control.
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$LocalConfig = Join-Path $PSScriptRoot "mpau.local.ps1"
if (-not (Test-Path $LocalConfig)) {
    throw "Missing deploy\windows\mpau.local.ps1. Copy mpau.env.example.ps1 and edit it first."
}
. $LocalConfig

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Missing .venv. Install the project with: py -3.12 -m venv .venv"
}

Set-Location $ProjectRoot
& $Python -m uvicorn webapp.api.main:app --host 127.0.0.1 --port 8788 --workers 1
exit $LASTEXITCODE
