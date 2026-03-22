param(
  [switch]$SkipInstall = $false,
  [switch]$SkipNsis = $false
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  $py = "python"
}

$env:PYTHONNOUSERSITE = "1"

if (-not $SkipInstall) {
  & $py -m pip install --upgrade pip
  if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
  & $py -m pip install -r requirements.txt pyinstaller
  if ($LASTEXITCODE -ne 0) { throw "Dependency install failed." }
}

$workPath = ".\build"
$distPath = ".\dist"

if (Test-Path $workPath) { Remove-Item $workPath -Recurse -Force }
if (Test-Path $distPath) {
  try {
    Remove-Item $distPath -Recurse -Force
  }
  catch {
    $distPath = ".\dist_build"
    if (Test-Path $distPath) { Remove-Item $distPath -Recurse -Force }
  }
}

& $py -m PyInstaller --clean --noconfirm --workpath $workPath --distpath $distPath ".\grayshare.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

if (-not (Test-Path "$distPath\GrayShare.exe")) {
  throw "Portable build failed: $distPath\GrayShare.exe not found."
}

if ($distPath -ne ".\dist") {
  Write-Host "dist folder is in use; build output is in $distPath"
}

if (-not $SkipNsis) {
  $makensis = (Get-Command makensis -ErrorAction SilentlyContinue)
  if (-not $makensis) {
    $fallbackNsis = "C:\Program Files (x86)\NSIS\makensis.exe"
    if (Test-Path $fallbackNsis) {
      $makensis = Get-Item $fallbackNsis
    }
  }
  if ($makensis) {
    $nsisDistDir = Split-Path -Path $distPath -Leaf
    & $makensis.FullName "/DSOURCE_DIST_PATH=$nsisDistDir" "/DOUTPUT_DIST_PATH=$nsisDistDir" ".\installer.nsi"
    if ($LASTEXITCODE -ne 0) { throw "NSIS build failed." }
  }
  else {
    Write-Host "makensis not found in PATH. Portable EXE built, installer skipped."
  }
}

Write-Host "Build complete."
