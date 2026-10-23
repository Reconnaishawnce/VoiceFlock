@echo off
rem Starts the VoiceFlock server and opens the UI. Close this window to stop the server.
rem Usage: VoiceFlock.bat [--quiet]   (--quiet = don't open the browser; used by the login shortcut)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo VoiceFlock is not set up yet. Right-click setup.ps1 and choose "Run with PowerShell".
  pause
  exit /b 1
)
rem load .env into this process
if exist ".env" for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if not "%%b"=="" set "%%a=%%b"
if "%VOICEFLOCK_PORT%"=="" set VOICEFLOCK_PORT=8787
if "%VOICEFLOCK_WORKERS%"=="" set VOICEFLOCK_WORKERS=1
rem stop any VoiceFlock already holding the port (a stale instance from before an update, or one started at login)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%VOICEFLOCK_PORT% .*LISTENING"') do (
  echo Stopping the VoiceFlock already running (PID %%p)...
  taskkill /F /PID %%p >nul 2>&1
)
set VOICEFLOCK_SUPERVISED=1
if not "%~1"=="--quiet" start "" /b cmd /c "timeout /t 4 >nul & start "" http://localhost:%VOICEFLOCK_PORT%"
:loop
title VoiceFlock - http://localhost:%VOICEFLOCK_PORT%  (close this window to stop)
".venv\Scripts\python.exe" -m voiceflock serve --host 0.0.0.0 --port %VOICEFLOCK_PORT% --workers %VOICEFLOCK_WORKERS%
if %errorlevel%==3 (
  echo.
  echo Restarting VoiceFlock...
  goto loop
)
pause
