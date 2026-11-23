# Developer build only. End users should run the supplied portable exe.
# Run from a Python 3.12 virtual environment after installing requirements-build.txt.
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$buildStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$releaseRoot = Join-Path $projectRoot ('dist-' + $buildStamp)
$scratchRoot = Join-Path $projectRoot ('build-' + $buildStamp)
python -m PyInstaller --onedir --windowed --name WeChatTracker `
  --distpath $releaseRoot --workpath $scratchRoot --specpath $scratchRoot `
  --hidden-import playwright.sync_api --hidden-import openpyxl `
  --exclude-module numpy --exclude-module pandas --exclude-module matplotlib `
  (Join-Path $projectRoot 'main.py')
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Copy-Item -LiteralPath (Join-Path $projectRoot '使用说明.txt') -Destination (Join-Path $releaseRoot 'WeChatTracker')
Write-Output $releaseRoot

