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
$HostAddress = if ($env:MPAU_BIND_HOST) { $env:MPAU_BIND_HOST } else { "0.0.0.0" }
$Port = if ($env:MPAU_PORT) { $env:MPAU_PORT } else { "8788" }
& $Python -m uvicorn webapp.api.main:app --host $HostAddress --port $Port --workers 1
exit $LASTEXITCODE
