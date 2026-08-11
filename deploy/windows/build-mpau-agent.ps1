<#
.SYNOPSIS
    构建 MPAU 本地执行助手的自包含 Windows 安装包。

.DESCRIPTION
    本脚本在隔离的虚拟环境中构建桌面代理程序：
    1. 使用 PyInstaller 将 local_agent 打包为单个目录 EXE
    2. （默认）使用 Inno Setup 6 生成 MPAU-Agent-Setup.exe 安装程序

.PARAMETER Python
    Python 解释器路径或名称，用于创建构建虚拟环境。默认使用 "py"（Windows Python Launcher）。

.PARAMETER InnoCompiler
    Inno Setup ISCC.exe 的完整路径。默认自动搜索常见安装位置。

.PARAMETER SkipInstaller
    仅运行 PyInstaller 打包，跳过 Inno Setup 安装程序生成。生成的 dist/MPAU-Agent/ 目录可直接分发。

.PARAMETER ProjectRoot
    项目根目录路径。默认自动从脚本位置推断。

.EXAMPLE
    .\build-mpau-agent.ps1
    # 完整构建：PyInstaller + Inno Setup 安装程序

.EXAMPLE
    .\build-mpau-agent.ps1 -SkipInstaller
    # 仅运行 PyInstaller，输出到 dist/MPAU-Agent/

.EXAMPLE
    .\build-mpau-agent.ps1 -Python "C:\Python312\python.exe" -InnoCompiler "D:\Tools\ISCC.exe"
    # 指定 Python 和 Inno Setup 路径
#>

param(
    [string]$Python = "py",
    [string]$InnoCompiler = "",
    [switch]$SkipInstaller,
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"

# Determine project root
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
Write-Host "项目目录：$ProjectRoot" -ForegroundColor Cyan

$BuildVenv = Join-Path $ProjectRoot ".venv-agent-build"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$SpecFile = Join-Path $ProjectRoot "deploy\windows\mpau-agent.spec"

# Step 1: Create or reuse build virtual environment
Set-Location $ProjectRoot
if (-not (Test-Path $BuildPython)) {
    Write-Host "创建构建虚拟环境..." -ForegroundColor Yellow
    & $Python -3.12 -m venv $BuildVenv
    if ($LASTEXITCODE -ne 0) {
        throw "创建虚拟环境失败，请确认 Python 3.12 已安装且 $Python 可用"
    }
    Write-Host "虚拟环境已创建：$BuildVenv" -ForegroundColor Green
} else {
    Write-Host "复用已有构建虚拟环境：$BuildVenv" -ForegroundColor Green
}

# Step 2: Install build dependencies
Write-Host "安装构建依赖（desktop + build extras）..." -ForegroundColor Yellow
& $BuildPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) {
    throw "pip 升级失败"
}
& $BuildPython -m pip install -e ".[desktop,build]" --quiet
if ($LASTEXITCODE -ne 0) {
    throw "依赖安装失败，请检查 pyproject.toml 中的依赖版本"
}
Write-Host "依赖安装完成" -ForegroundColor Green

# Fail before packaging if the desktop dependency set cannot import every module
# required by authenticated Tmall product inspection.
Write-Host "校验桌面端打包模块..." -ForegroundColor Yellow
& $BuildPython -c "import local_agent.desktop; import webapp.ai_copy.contracts; import webapp.ai_copy.errors; import webapp.ai_copy.product_lookup.cache; import webapp.ai_copy.product_lookup.interfaces; import webapp.ai_copy.product_lookup.public_http; import webapp.ai_copy.product_lookup.tmall_client; import webapp.ai_copy.product_lookup.tmall_reader"
if ($LASTEXITCODE -ne 0) {
    throw "桌面端模块导入失败，已停止生成安装包"
}
Write-Host "桌面端打包模块校验完成" -ForegroundColor Green

# Step 3: Run PyInstaller
Write-Host "启动 PyInstaller 打包..." -ForegroundColor Yellow
& $BuildPython -m PyInstaller `
    --clean `
    --noconfirm `
    $SpecFile

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败，请查看上方输出排查错误"
}

$DistDir = Join-Path $ProjectRoot "dist\MPAU-Agent"
if (-not (Test-Path $DistDir)) {
    throw "PyInstaller 输出目录未生成：$DistDir"
}
$AgentExe = Join-Path $DistDir "MPAU-Agent.exe"
if (-not (Test-Path $AgentExe)) {
    throw "PyInstaller 主程序未生成：$AgentExe"
}

# Inspect the frozen archive as well as the source imports. This catches an
# accidental PyInstaller exclusion that would only fail on an installed PC.
$ArchiveListing = (& $BuildPython -m PyInstaller.utils.cliutils.archive_viewer `
    --recursive --brief $AgentExe 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "无法读取 PyInstaller 归档内容"
}
$RequiredFrozenModules = @(
    "webapp.ai_copy.contracts",
    "webapp.ai_copy.errors",
    "webapp.ai_copy.product_lookup.cache",
    "webapp.ai_copy.product_lookup.interfaces",
    "webapp.ai_copy.product_lookup.public_http",
    "webapp.ai_copy.product_lookup.tmall_client",
    "webapp.ai_copy.product_lookup.tmall_reader"
)
foreach ($Module in $RequiredFrozenModules) {
    $Pattern = "(?m)^\s*" + [regex]::Escape($Module) + "\s*$"
    if ($ArchiveListing -notmatch $Pattern) {
        throw "PyInstaller 归档缺少必需模块：$Module"
    }
}
Write-Host "PyInstaller 打包完成 -> $DistDir" -ForegroundColor Green

# Step 4: (Optional) Build Inno Setup installer
if ($SkipInstaller) {
    Write-Host "已跳过 Inno Setup 安装程序生成（-SkipInstaller）" -ForegroundColor Yellow
    Write-Host "可分发的程序目录：$DistDir" -ForegroundColor Cyan
    return
}

Write-Host "查找 Inno Setup 6..." -ForegroundColor Yellow
if (-not $InnoCompiler) {
    $Candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
    )
    $InnoCompiler = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

if (-not $InnoCompiler -or -not (Test-Path $InnoCompiler)) {
    Write-Host "未找到 Inno Setup 6。" -ForegroundColor Red
    Write-Host "请从 https://jrsoftware.org/isinfo.php 下载安装，或使用 -InnoCompiler 指定 ISCC.exe 路径。" -ForegroundColor Yellow
    Write-Host "也可使用 -SkipInstaller 仅生成 PyInstaller 目录。" -ForegroundColor Yellow
    Write-Host "可分发的程序目录：$DistDir" -ForegroundColor Cyan
    throw "Inno Setup 6 未安装"
}

Write-Host "使用 Inno Setup：$InnoCompiler" -ForegroundColor Green
$IssFile = Join-Path $ProjectRoot "deploy\windows\mpau-agent-installer.iss"
Write-Host "生成安装包..." -ForegroundColor Yellow
& $InnoCompiler $IssFile

if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup 打包失败，请检查 mpau-agent-installer.iss 配置"
}

$OutputDir = Join-Path $ProjectRoot "deploy\windows\output"
$SetupExe = Join-Path $OutputDir "MPAU-Agent-Setup.exe"
if (-not (Test-Path $SetupExe)) {
    throw "安装包未生成：$SetupExe"
}
$SetupHash = (Get-FileHash -Algorithm SHA256 $SetupExe).Hash.ToLowerInvariant()
$ChecksumFile = Join-Path $OutputDir "MPAU-Agent-Setup.exe.sha256"
"$SetupHash *MPAU-Agent-Setup.exe" | Set-Content -Path $ChecksumFile -Encoding ascii
Write-Host "安装包已生成：$SetupExe" -ForegroundColor Green
Write-Host "安装包大小：$((Get-Item $SetupExe).Length) bytes" -ForegroundColor Cyan
Write-Host "SHA-256：$SetupHash" -ForegroundColor Cyan
