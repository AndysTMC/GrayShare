<#
.SYNOPSIS
    GrayShare installer for Windows.

.DESCRIPTION
    Clones/updates the app to %LOCALAPPDATA%\GrayShare\app, creates a private
    Python venv there, installs dependencies, and drops a `grayshare` command
    into the user's PATH via an executable shim in %LOCALAPPDATA%\Programs\GrayShare\bin.

.EXAMPLE
    # Review first, then run:
    irm https://raw.githubusercontent.com/AndysTMC/GrayShare/main/install.ps1 -OutFile install-grayshare.ps1
    .\install-grayshare.ps1

    # Run immediately (equivalent of curl | bash):
    irm https://raw.githubusercontent.com/AndysTMC/GrayShare/main/install.ps1 | iex

.NOTES
    After installing, open a NEW terminal and run:  grayshare --headless
#>
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$Repo   = "https://github.com/AndysTMC/GrayShare.git"
$Branch = "main"
$InstallRoot = if ($env:GRAYSHARE_HOME) { $env:GRAYSHARE_HOME } else { Join-Path $env:LOCALAPPDATA "GrayShare" }
$AppDir = Join-Path $InstallRoot "app"
$BinDir = Join-Path $env:LOCALAPPDATA "Programs\GrayShare\bin"

function Say($msg)  { Write-Host "==> $msg" }
function Die($msg)  { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

# --- prerequisites -----------------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Die "git is required. Install it first: winget install --id Git.Git"
}
# Prefer a real python3 launcher; Windows sometimes only ships the Store alias.
$PythonCmd = $null
foreach ($candidate in @("python3", "python")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { $PythonCmd = $candidate; break }
}
if (-not $PythonCmd) {
    Die "python is required. Install it first: winget install --id Python.Python.3.12"
}
# Guard against the Microsoft Store python alias that opens the store instead.
$pySource = (Get-Command $PythonCmd).Source
if ($pySource -like "*WindowsApps*") {
    Die "'$PythonCmd' points to the Microsoft Store alias. Install real Python: winget install --id Python.Python.3.12"
}

Say "Using $PythonCmd ($pySource)"

New-Item -ItemType Directory -Force -Path $InstallRoot, $BinDir | Out-Null

# --- get the code ------------------------------------------------------------
if (Test-Path (Join-Path $AppDir ".git")) {
    Say "Updating existing checkout in $AppDir"
    Push-Location $AppDir
    git fetch origin $Branch 2>$null | Out-Null
    git reset --hard "origin/$Branch" | Out-Null
    Pop-Location
} else {
    Say "Cloning GrayShare into $AppDir"
    if (Test-Path "$AppDir.tmp") { Remove-Item -Recurse -Force "$AppDir.tmp" }
    git clone --depth 1 --branch $Branch $Repo "$AppDir.tmp" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "clone failed — check your internet connection." }
    if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
    Move-Item "$AppDir.tmp" $AppDir
}

# --- python environment ------------------------------------------------------
Say "Setting up Python environment (first install takes a minute)"
Push-Location $AppDir
$VenvPy = Join-Path $AppDir ".venv\Scripts\python.exe"     # Windows venv layout
if (-not (Test-Path $VenvPy)) {
    $VenvPy = Join-Path $AppDir ".venv/bin/python"          # POSIX venv layout
}
if (-not (Test-Path $VenvPy)) {
    & $PythonCmd -m venv .venv
    if ($LASTEXITCODE -ne 0) { Die "failed to create venv." }
    $VenvPy = Join-Path $AppDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $VenvPy)) { $VenvPy = Join-Path $AppDir ".venv/bin/python" }
}
& $VenvPy -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Die "pip upgrade failed." }
& $VenvPy -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Die "dependency install failed." }
Pop-Location

# --- the grayshare command ---------------------------------------------------
# A small batch shim + a PowerShell function shim, so both cmd and pwsh find it.
$ShimBat = Join-Path $BinDir "grayshare.bat"
@"
@echo off
set "APP=$AppDir"
set "PY=$VenvPy"
if not exist "%PY%" (
    echo GrayShare installation is broken. Reinstall:
    echo   irm https://raw.githubusercontent.com/AndysTMC/GrayShare/main/install.ps1 ^| iex
    exit /b 1
)
cd /d "%APP%"
if "%~1"=="update" (
    git -C "%APP%" fetch origin $Branch >nul 2>&1
    git -C "%APP%" reset --hard "origin/$Branch" >nul
    "%PY%" -m pip install --quiet --upgrade pip
    "%PY%" -m pip install --quiet -r requirements.txt
    echo GrayShare updated.
    exit /b 0
)
set HEADLESS=0
set REST=
:parseargs
if "%~1"=="" goto run
if /I "%~1"=="--headless" ( set HEADLESS=1 ) else ( set "REST=%REST% %~1" )
shift
goto parseargs
:run
if "%HEADLESS%"=="1" ( "%PY%" desktop_app.py --server-only%REST% ) else ( "%PY%" desktop_app.py%REST% )
"@ | Set-Content -Encoding ASCII -Path $ShimBat

$ShimPs1 = Join-Path $BinDir "grayshare.ps1"
@"
# GrayShare launcher (installed by install.ps1)
`$App = "$AppDir"
`$Py  = "$VenvPy"
if (-not (Test-Path `$Py)) {
    Write-Host "GrayShare installation is broken. Reinstall:"
    Write-Host "  irm https://raw.githubusercontent.com/AndysTMC/GrayShare/main/install.ps1 | iex"
    exit 1
}
switch (`$args[0]) {
    'update' {
        Push-Location `$App
        git fetch origin $Branch 2>`$null | Out-Null
        git reset --hard "origin/$Branch" | Out-Null
        & `$Py -m pip install --quiet --upgrade pip
        & `$Py -m pip install --quiet -r requirements.txt
        Pop-Location
        Write-Host "GrayShare updated."
        exit 0
    }
    'version' { Set-Location `$App; git describe --tags --always; exit 0 }
    '--version' { Set-Location `$App; git describe --tags --always; exit 0 }
    '-v' { Set-Location `$App; git describe --tags --always; exit 0 }
}
`$rest = @()
`$headless = `$false
foreach (`$a in `$args) {
    if (`$a -in @('--headless','-H')) { `$headless = `$true } else { `$rest += `$a }
}
Set-Location `$App
if (`$headless) { & `$Py desktop_app.py --server-only @rest }
else { & `$Py desktop_app.py @rest }
"@ | Set-Content -Encoding UTF8 -Path $ShimPs1

# Add BinDir to the user PATH (idempotent).
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$BinDir", "User")
    $env:Path += ";$BinDir"
    Say "Added $BinDir to your user PATH."
}

Say "Installed!"
Write-Host ""
Write-Host "  Open a NEW terminal, then:"
Write-Host "    grayshare --headless        # headless LAN server (prints URL + key)"
Write-Host "    grayshare                   # desktop window"
Write-Host "    grayshare --port 4567       # pick a port"
Write-Host "    grayshare update            # update to the latest version"
