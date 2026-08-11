param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "未找到本地代理 Python 环境：$Python"
}

Set-Location $ProjectRoot
& $Python -m local_agent.desktop
