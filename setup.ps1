# VoiceFlock one-time setup for Windows. Right-click -> "Run with PowerShell", or in a terminal:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
# Installs Python 3.11 and ffmpeg if missing (via winget), creates a virtual environment,
# installs VoiceFlock, writes .env, and optionally registers VoiceFlock to start at login.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
Write-Host "`n== VoiceFlock setup ==`n" -ForegroundColor Cyan

function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Winget-Install($id) {
    if (-not (Have "winget")) { throw "winget is not available. Install '$id' manually, then re-run setup." }
    winget install -e --id $id --accept-package-agreements --accept-source-agreements --silent
}

# --- Python 3.10-3.12 ----------------------------------------------------------
$py = $null
foreach ($v in "3.12", "3.11", "3.10") {
    if (Have "py") { & py -$v -c "pass" 2>$null; if ($LASTEXITCODE -eq 0) { $py = "py -$v"; break } }
}
if (-not $py -and (Have "python")) {
    $ver = & python -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
    if ($ver -in "3.10", "3.11", "3.12") { $py = "python" }
}
if (-not $py) {
    Write-Host "Installing Python 3.11 ..." -ForegroundColor Yellow
    Winget-Install "Python.Python.3.11"
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $py = "py -3.11"
}
Write-Host "Python: $py"

# --- ffmpeg --------------------------------------------------------------------
if (-not (Have "ffmpeg")) {
    $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*" -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $found) {
        Write-Host "Installing ffmpeg ..." -ForegroundColor Yellow
        Winget-Install "Gyan.FFmpeg"
    }
}
Write-Host "ffmpeg: ok (VoiceFlock also finds the winget install even if PATH is not refreshed yet)"

# --- virtual environment + packages --------------------------------------------
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment ..." -ForegroundColor Yellow
    Invoke-Expression "$py -m venv .venv"
}
$pip = ".\.venv\Scripts\python.exe -m pip"
Invoke-Expression "$pip install --upgrade pip wheel" | Out-Null
$gpu = (Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match "NVIDIA" })
if ($gpu) {
    Write-Host "NVIDIA GPU detected ($($gpu.Name)): installing CUDA PyTorch (large download) ..." -ForegroundColor Yellow
    Invoke-Expression "$pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121"
} else {
    Write-Host "Installing CPU PyTorch ..." -ForegroundColor Yellow
    Invoke-Expression "$pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu"
}
Write-Host "Installing VoiceFlock ..." -ForegroundColor Yellow
Invoke-Expression "$pip install -e ."

# --- .env ----------------------------------------------------------------------
if (-not (Test-Path ".env")) {
    Write-Host "`nHuggingFace token (starts with hf_; from https://huggingface.co/settings/tokens)." -ForegroundColor Cyan
    Write-Host "Accept the model terms first: https://huggingface.co/pyannote/speaker-diarization-community-1 and https://huggingface.co/pyannote/segmentation-3.0"
    $tok = Read-Host "HF_TOKEN (leave empty to set later in Settings)"
    $pw  = Read-Host "Password for the web UI (leave empty for none)"
    @("HF_TOKEN=$tok", "VOICEFLOCK_PASSWORD=$pw", "VOICEFLOCK_DATA_DIR=./data", "VOICEFLOCK_DEVICE=auto") | Set-Content ".env" -Encoding ASCII
    Write-Host ".env written."
} else { Write-Host ".env already exists, leaving it alone." }

# --- autostart -----------------------------------------------------------------
$ans = Read-Host "`nStart VoiceFlock automatically when you log in? (y/n)"
if ($ans -match "^[Yy]") {
    $startup = [Environment]::GetFolderPath("Startup")
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut("$startup\VoiceFlock.lnk")
    $lnk.TargetPath = "$PSScriptRoot\VoiceFlock.bat"
    $lnk.Arguments = "--quiet"
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.WindowStyle = 7   # minimised
    $lnk.Save()
    Write-Host "Shortcut added to $startup"
}

Write-Host "`nDone. Double-click VoiceFlock.bat to start (it opens http://localhost:8787)." -ForegroundColor Green
Write-Host "Then go to Settings -> Run self-test. The first run downloads the models (a few minutes)."
