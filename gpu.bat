@echo off
rem (Re)installs the CUDA build of PyTorch so VoiceFlock uses your NVIDIA GPU. Close VoiceFlock first.
cd /d "%~dp0"
nvidia-smi >nul 2>&1 || (echo No NVIDIA driver found. Install the latest NVIDIA driver first. & pause & exit /b 1)
echo Installing CUDA PyTorch (about 3 GB download)...
".venv\Scripts\python.exe" -m pip install --force-reinstall --no-deps torch torchaudio --index-url https://download.pytorch.org/whl/cu126
".venv\Scripts\python.exe" -m pip install -e . --quiet
".venv\Scripts\python.exe" -c "import torch;print('GPU acceleration:', 'ON - ' + torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'STILL OFF - update your NVIDIA driver and run this again')"
pause
