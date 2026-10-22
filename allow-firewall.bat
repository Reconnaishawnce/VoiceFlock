@echo off
rem Lets phones on your network reach VoiceFlock (opens TCP 8787 in Windows Firewall). Asks for admin rights.
net session >nul 2>&1 || (powershell -Command "Start-Process -Verb RunAs -FilePath '%~f0'" & exit /b)
netsh advfirewall firewall delete rule name="VoiceFlock" >nul 2>&1
netsh advfirewall firewall add rule name="VoiceFlock" dir=in action=allow protocol=TCP localport=8787 profile=any
echo.
echo Done. From your phone open http://^<this laptop's IP^>:8787  (the IP is shown on the Settings page).
pause
