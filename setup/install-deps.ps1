# JARVIS dependency installer: makes the app's Python environment (.venv) and installs everything in
# requirements.txt and the window's packages. Safe to re-run: what is installed is skipped, and a package that
# fails (slow network) is retried. A summary of anything that still failed is printed at the end.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$py   = Join-Path $venv 'Scripts\python.exe'
$failed = @()

function Step($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

Step 'Checking Python, Node and Rust'
$sysPy = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $sysPy) { Write-Host 'Python 3.12 was not found. Install it (see README, step 1) and run this again.' -ForegroundColor Red; exit 1 }
& $sysPy --version
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Write-Host 'Node.js was not found. Install it (see README, step 1) and run this again.' -ForegroundColor Red; exit 1 }
Write-Host "Node $(node --version)"
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { Write-Host 'Rust was not found. Install it (see README, step 1) and run this again.' -ForegroundColor Red; exit 1 }
cargo --version

Step 'Creating the Python environment (.venv)'
if (-not (Test-Path $py)) { & $sysPy -m venv $venv }
if (-not (Test-Path $py)) { Write-Host 'Could not create the virtual environment.' -ForegroundColor Red; exit 1 }

# Slow-network settings: long timeout, many retries.
$net = @('--default-timeout', '120', '--retries', '15', '--disable-pip-version-check')

Step 'Updating pip'
& $py -m pip install --upgrade pip @net

Step 'Installing Python packages (requirements.txt)'
$ok = $false
for ($i = 1; $i -le 3 -and -not $ok; $i++) {
    Write-Host "Attempt $i of 3"
    & $py -m pip install -r (Join-Path $root 'requirements.txt') @net
    if ($LASTEXITCODE -eq 0) { $ok = $true }
}
if (-not $ok) { $failed += 'Python packages (requirements.txt)' }

Step 'Installing the window''s packages (npm install)'
Push-Location $root
$ok = $false
for ($i = 1; $i -le 3 -and -not $ok; $i++) {
    Write-Host "Attempt $i of 3"
    npm install
    if ($LASTEXITCODE -eq 0) { $ok = $true }
}
Pop-Location
if (-not $ok) { $failed += 'npm packages' }

Step 'Summary'
if ($failed.Count -eq 0) {
    Write-Host 'All dependencies installed. Start JARVIS with:  npx tauri dev' -ForegroundColor Green
} else {
    Write-Host 'These failed. Check your internet connection and run this script again:' -ForegroundColor Yellow
    $failed | ForEach-Object { Write-Host " - $_" }
}
