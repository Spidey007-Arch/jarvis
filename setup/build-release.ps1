# Builds the JARVIS release: a Windows installer (.exe) and a portable .zip. Neither needs Python, Node or Rust on
# the PC it runs on. Building them here does: run setup\install-deps.ps1 once first.
#
#   powershell -ExecutionPolicy Bypass -File setup\build-release.ps1
#
# Output goes to release\: JARVIS_<version>_x64-setup.exe and JARVIS_<version>_portable.zip.
# -SkipPython reuses the backend and agent already packaged in .dist (e.g. after only the installer step failed).
param([switch]$SkipPython)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $root '.venv\Scripts\python.exe'
$dist = Join-Path $root '.dist'
$out  = Join-Path $root 'release'
function Step($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Fail($t) { Write-Host $t -ForegroundColor Red; exit 1 }

if (-not (Test-Path $py)) { Fail 'No .venv found: run setup\install-deps.ps1 first.' }
$version = (Get-Content (Join-Path $root 'src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json).version

if (-not $SkipPython) {
Step 'Build tools (PyInstaller, tzdata)'
& $py -m pip install --disable-pip-version-check -q pyinstaller tzdata
if ($LASTEXITCODE -ne 0) { Fail 'Could not install PyInstaller.' }

Step 'Packaging the backend and the agent (a few minutes)'
# A running packaged JARVIS, or antivirus scanning the fresh DLLs, can briefly lock the old build: retry clearing it.
for ($i = 0; (Test-Path $dist) -and $i -lt 15; $i++) {
    Remove-Item $dist -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $dist) { Start-Sleep -Seconds 2 }
}
if (Test-Path $dist) { Fail "Could not clear ${dist}: close any JARVIS started from it and try again." }
& $py -m PyInstaller (Join-Path $root 'packaging\jarvis.spec') --distpath $dist --workpath (Join-Path $root 'build\pyinstaller') --noconfirm --log-level WARN
if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller failed; see the messages above.' }

}

Step 'Checking the packaged parts'
$probe = Join-Path $env:TEMP 'jarvis-probe.py'
# backend: time zones (calendars). agent: numpy and the whole standard library, which the sandbox needs.
$checks = @{ backend = 'import zoneinfo; zoneinfo.ZoneInfo("Asia/Kolkata"); print("ok")';
             agent   = 'import numpy, zoneinfo, statistics, sqlite3, fractions; zoneinfo.ZoneInfo("Asia/Kolkata"); print("ok")' }
foreach ($part in 'backend', 'agent') {
    $exe = Join-Path $dist "jarvis-$part\jarvis-$part.exe"
    if (-not (Test-Path $exe)) { Fail "Missing $exe" }
    Set-Content $probe $checks[$part]
    $ErrorActionPreference = 'Continue'  # a native program writing to stderr must not end the script here
    $r = & $exe $probe 2>&1
    $ErrorActionPreference = 'Stop'
    if ("$r" -notmatch 'ok') { Fail "jarvis-$part.exe failed its check: $r" }
    Write-Host "jarvis-$part.exe ok"
}
Remove-Item $probe -ErrorAction SilentlyContinue

Step 'Building the app and the installer'
Push-Location $root
try {
    # The first build downloads the NSIS installer tools from GitHub; on a slow line that can time out, so retry.
    $built = $false
    for ($i = 1; $i -le 3 -and -not $built; $i++) {
        if ($i -gt 1) { Write-Host "Retrying the app build (attempt $i of 3)..." -ForegroundColor Yellow }
        $ErrorActionPreference = 'Continue'
        npx tauri build --config src-tauri/tauri.release.conf.json
        $built = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = 'Stop'
    }
    if (-not $built) { Fail 'The Tauri build failed; see the messages above.' }
} finally { Pop-Location }

Step 'Collecting the installer and making the portable zip'
New-Item -ItemType Directory -Force $out | Out-Null
$setup = Get-ChildItem (Join-Path $root 'src-tauri\target\release\bundle\nsis') -Filter '*-setup.exe' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $setup) { Fail 'The installer was not produced.' }
Copy-Item $setup.FullName (Join-Path $out $setup.Name) -Force

$stage = Join-Path $root 'build\portable\JARVIS'
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item (Join-Path $root 'src-tauri\target\release\jarvis.exe') (Join-Path $stage 'JARVIS.exe')
Copy-Item (Join-Path $dist 'jarvis-backend') $stage -Recurse
Copy-Item (Join-Path $dist 'jarvis-agent') $stage -Recurse
Set-Content (Join-Path $stage 'README.txt') @"
JARVIS $version (portable)

Unzip anywhere and run JARVIS.exe. Nothing is installed; your settings and memory are kept in
%LOCALAPPDATA%\JARVIS, the same place the installed version uses.
Needs Windows 10 or 11 with the Microsoft Edge WebView2 runtime (built into Windows 11).
"@
$zip = Join-Path $out "JARVIS_${version}_portable.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $stage -DestinationPath $zip -CompressionLevel Optimal

Step 'Done'
Get-ChildItem $out | ForEach-Object { '{0,-40} {1,8:N0} MB' -f $_.Name, ($_.Length / 1MB) }
