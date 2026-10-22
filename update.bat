@echo off
rem Updates VoiceFlock to the latest version. Close VoiceFlock first, run this, then start VoiceFlock.bat again.
cd /d "%~dp0"
git pull || (echo git pull failed & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install -e . --quiet --upgrade-strategy only-if-needed
echo.
echo Updated. Start VoiceFlock.bat again.
pause
