<#
.SYNOPSIS
    Build the self-contained Windows installer for the MPAU local agent.

.DESCRIPTION
    Build the desktop agent in an isolated virtual environment:
    1. Package local_agent as a one-directory executable with PyInstaller.
    2. By default, create MPAU-Agent-Setup.exe with Inno Setup 6.

.PARAMETER Python
    Python interpreter used to create the build virtual environment.
    Defaults to "py" (the Windows Python Launcher).

.PARAMETER InnoCompiler
    Full path to the Inno Setup ISCC.exe compiler. Common locations are searched by default.

.PARAMETER SkipInstaller
    Run PyInstaller only and skip Inno Setup. The generated dist/MPAU-Agent directory is distributable.

.PARAMETER ProjectRoot
    Project root path. By default it is inferred from the script location.

.EXAMPLE
    .\build-mpau-agent.ps1
    # Full build: PyInstaller plus an Inno Setup installer.

.EXAMPLE
    .\build-mpau-agent.ps1 -SkipInstaller
    # PyInstaller only, with output in dist/MPAU-Agent/.

.EXAMPLE
    .\build-mpau-agent.ps1 -Python "C:\Python312\python.exe" -InnoCompiler "D:\Tools\ISCC.exe"
    # Use explicit Python and Inno Setup paths.
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
Write-Host "Project root: $ProjectRoot" -ForegroundColor Cyan

$BuildVenv = Join-Path $ProjectRoot ".venv-agent-build"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$SpecFile = Join-Path $ProjectRoot "deploy\windows\mpau-agent.spec"

# Step 1: Create or reuse build virtual environment
Set-Location $ProjectRoot
if (-not (Test-Path $BuildPython)) {
    Write-Host "Creating build virtual environment..." -ForegroundColor Yellow
    & $Python -3.12 -m venv $BuildVenv
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the virtual environment. Verify that Python 3.12 is installed and $Python is available."
    }
    Write-Host "Virtual environment created: $BuildVenv" -ForegroundColor Green
} else {
    Write-Host "Reusing build virtual environment: $BuildVenv" -ForegroundColor Green
}

# Step 2: Install build dependencies
Write-Host "Installing desktop and build dependencies..." -ForegroundColor Yellow
& $BuildPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip."
}
& $BuildPython -m pip install -e ".[desktop,build]" --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install dependencies. Check the versions in pyproject.toml."
}
Write-Host "Dependencies installed." -ForegroundColor Green

# Fail before packaging if the desktop dependency set cannot import every module
# required by authenticated Tmall product inspection.
Write-Host "Validating desktop packaging modules..." -ForegroundColor Yellow
& $BuildPython -c "import local_agent.desktop; import webapp.ai_copy.contracts; import webapp.ai_copy.errors; import webapp.ai_copy.product_lookup.cache; import webapp.ai_copy.product_lookup.interfaces; import webapp.ai_copy.product_lookup.public_http; import webapp.ai_copy.product_lookup.tmall_client; import webapp.ai_copy.product_lookup.tmall_reader"
if ($LASTEXITCODE -ne 0) {
    throw "A desktop module import failed; installer generation has stopped."
}
Write-Host "Desktop packaging modules validated." -ForegroundColor Green

# Step 3: Run PyInstaller
Write-Host "Starting PyInstaller..." -ForegroundColor Yellow
& $BuildPython -m PyInstaller `
    --clean `
    --noconfirm `
    $SpecFile

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed. Review the preceding output for details."
}

$DistDir = Join-Path $ProjectRoot "dist\MPAU-Agent"
if (-not (Test-Path $DistDir)) {
    throw "PyInstaller output directory was not created: $DistDir"
}
$AgentExe = Join-Path $DistDir "MPAU-Agent.exe"
if (-not (Test-Path $AgentExe)) {
    throw "PyInstaller did not create the main executable: $AgentExe"
}

# Inspect the frozen archive as well as the source imports. This catches an
# accidental PyInstaller exclusion that would only fail on an installed PC.
$ArchiveListing = (& $BuildPython -m PyInstaller.utils.cliutils.archive_viewer `
    --recursive --brief $AgentExe 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the PyInstaller archive."
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
        throw "The PyInstaller archive is missing a required module: $Module"
    }
}
Write-Host "PyInstaller completed: $DistDir" -ForegroundColor Green

# Step 4: (Optional) Build Inno Setup installer
if ($SkipInstaller) {
    Write-Host "Skipped Inno Setup installer generation (-SkipInstaller)." -ForegroundColor Yellow
    Write-Host "Distributable program directory: $DistDir" -ForegroundColor Cyan
    return
}

Write-Host "Locating Inno Setup 6..." -ForegroundColor Yellow
if (-not $InnoCompiler) {
    $Candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
    )
    $InnoCompiler = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

if (-not $InnoCompiler -or -not (Test-Path $InnoCompiler)) {
    Write-Host "Inno Setup 6 was not found." -ForegroundColor Red
    Write-Host "Install it from https://jrsoftware.org/isinfo.php or pass the ISCC.exe path with -InnoCompiler." -ForegroundColor Yellow
    Write-Host "Alternatively, use -SkipInstaller to generate only the PyInstaller directory." -ForegroundColor Yellow
    Write-Host "Distributable program directory: $DistDir" -ForegroundColor Cyan
    throw "Inno Setup 6 is not installed."
}

Write-Host "Using Inno Setup: $InnoCompiler" -ForegroundColor Green
$IssFile = Join-Path $ProjectRoot "deploy\windows\mpau-agent-installer.iss"
Write-Host "Building installer..." -ForegroundColor Yellow
& $InnoCompiler $IssFile

if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed. Check mpau-agent-installer.iss."
}

$OutputDir = Join-Path $ProjectRoot "deploy\windows\output"
$SetupExe = Join-Path $OutputDir "MPAU-Agent-Setup.exe"
if (-not (Test-Path $SetupExe)) {
    throw "Installer was not created: $SetupExe"
}
$SetupHash = (Get-FileHash -Algorithm SHA256 $SetupExe).Hash.ToLowerInvariant()
$ChecksumFile = Join-Path $OutputDir "MPAU-Agent-Setup.exe.sha256"
"$SetupHash *MPAU-Agent-Setup.exe" | Set-Content -Path $ChecksumFile -Encoding ascii
Write-Host "Installer created: $SetupExe" -ForegroundColor Green
Write-Host "Installer size: $((Get-Item $SetupExe).Length) bytes" -ForegroundColor Cyan
Write-Host "SHA-256: $SetupHash" -ForegroundColor Cyan
