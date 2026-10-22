# JARVIS dependency installer. Safe to re-run: installed packages are skipped,
# failed ones are retried, and a summary of what failed is printed at the end.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$py   = Join-Path $venv 'Scripts\python.exe'

function Step($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

Step 'Checking Python'
$sysPy = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $sysPy) { Write-Host 'Python 3.12 not found on PATH. Install it from https://www.python.org/downloads/ and re-run.' -ForegroundColor Red; exit 1 }
& $sysPy --version

Step 'Creating virtual environment (reuses already-installed packages, no re-download)'
if (-not (Test-Path $py)) { & $sysPy -m venv $venv --system-site-packages }
if (-not (Test-Path $py)) { Write-Host 'Could not create the virtual environment.' -ForegroundColor Red; exit 1 }

# Slow-network settings: long timeout, many retries.
$net = @('--default-timeout', '120', '--retries', '15', '--disable-pip-version-check')

Step 'Updating pip'
& $py -m pip install --upgrade pip @net

$groups = [ordered]@{
    'core (window, tray, Windows APIs)'     = @('pywebview', 'pystray', 'pywin32', 'keyring')
    'automation (apps, volume, screen, UI)' = @('comtypes', 'pycaw', 'uiautomation', 'pyautogui', 'mss', 'screen-brightness-control')
    'reminders and notifications'           = @('apscheduler', 'winotify')
    'memory and math'                       = @('numpy')
    'voice (free, local)'                   = @('sounddevice', 'webrtcvad-wheels', 'onnxruntime', 'openwakeword', 'faster-whisper')
    'packaging'                             = @('pyinstaller')
}

$failed = @()
foreach ($g in $groups.Keys) {
    Step $g
    foreach ($pkg in $groups[$g]) {
        $ok = $false
        for ($i = 1; $i -le 5 -and -not $ok; $i++) {
            Write-Host "Installing $pkg (attempt $i of 5)"
            & $py -m pip install $pkg @net
            if ($LASTEXITCODE -eq 0) { $ok = $true }
        }
        if (-not $ok) { $failed += $pkg }
    }
}

Step 'Downloading free voice models (Whisper base, wake-word models)'
& $py -c "from huggingface_hub import snapshot_download; p=snapshot_download('Systran/faster-whisper-base'); print('Whisper base ->', p)"
if ($LASTEXITCODE -ne 0) { $failed += 'model: faster-whisper-base' }
& $py -c "import openwakeword; from openwakeword.utils import download_models; download_models(); print('wake-word models ready')"
if ($LASTEXITCODE -ne 0) { $failed += 'model: openwakeword' }

Step 'Summary'
if ($failed.Count -eq 0) {
    Write-Host 'All dependencies installed.' -ForegroundColor Green
} else {
    Write-Host 'These failed. Re-run this script to retry them:' -ForegroundColor Yellow
    $failed | ForEach-Object { Write-Host " - $_" }
}
