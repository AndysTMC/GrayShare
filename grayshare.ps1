# GrayShare one-command launcher (Windows PowerShell).
#
#   .\grayshare.ps1              desktop app
#   .\grayshare.ps1 -Headless    headless server on the LAN
#   .\grayshare.ps1 -Port 4567   pick a port
#
# First run creates .venv and installs dependencies automatically.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Py = ".\.venv\Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Host "==> First run: setting up GrayShare (one time)..."
    python -m venv .venv
    & $Py -m pip install --quiet --upgrade pip
    & $Py -m pip install --quiet -r requirements.txt
    Write-Host "==> Setup complete."
}

$Headless = $false
$args2 = @()
foreach ($a in $args) {
    if ($a -in @("--headless", "-H")) { $Headless = $true }
    elseif ($a -eq "-Headless") { $Headless = $true }
    else { $args2 += $a }
}

if ($Headless) {
    & $Py desktop_app.py --server-only @args2
} else {
    & $Py desktop_app.py @args2
}
