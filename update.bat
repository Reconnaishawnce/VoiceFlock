@echo off
rem Updates VoiceFlock (and yt-dlp, which YouTube breaks regularly). Close VoiceFlock first, then start VoiceFlock.bat again.
rem Tip: with VoiceFlock running you can instead use Settings -> Update and restart in the web UI.
cd /d "%~dp0"
rem keep this window open whatever happens, so errors stay readable (relaunch inside a persistent cmd)
if defined VF_KEEP_OPEN goto :run
set "VF_KEEP_OPEN=1"
cmd /k call "%~f0" %*
exit /b
:run
if exist ".env" for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if not "%%b"=="" set "%%a=%%b"
if "%VOICEFLOCK_PORT%"=="" set VOICEFLOCK_PORT=8787
rem stop any VoiceFlock already holding the port (a stale instance from before an update, or one started at login)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%VOICEFLOCK_PORT% .*LISTENING"') do (
  echo Stopping the VoiceFlock already running, PID %%p ...
  taskkill /F /PID %%p >nul 2>&1
)
set "HERE=%CD:\=/%"
git config --global --add safe.directory "%HERE%" >nul 2>&1
git pull || (echo git pull failed & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install -e . --quiet --upgrade-strategy only-if-needed
".venv\Scripts\python.exe" -m pip install -U yt-dlp --quiet
".venv\Scripts\python.exe" -c "import torch;print('GPU acceleration:', 'ON' if torch.cuda.is_available() else 'off (run gpu.bat if you have an NVIDIA GPU)')"
echo.
echo Updated and the old server was stopped. Start VoiceFlock.bat again.
pause
