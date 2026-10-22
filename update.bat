@echo off
rem Updates VoiceFlock (and yt-dlp, which YouTube breaks regularly). Close VoiceFlock first, then start VoiceFlock.bat again.
cd /d "%~dp0"
git pull || (echo git pull failed & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install -e . --quiet --upgrade-strategy only-if-needed
".venv\Scripts\python.exe" -m pip install -U yt-dlp --quiet
".venv\Scripts\python.exe" -c "import torch;print('GPU acceleration:', 'ON' if torch.cuda.is_available() else 'off (run gpu.bat if you have an NVIDIA GPU)')"
echo.
echo Updated. Start VoiceFlock.bat again.
pause
