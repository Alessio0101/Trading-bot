@echo off
rem Double-click once: lets your own phone open the dashboard from anywhere through Tailscale (private).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai_team\system.ps1" oriunde
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo.
echo Nu s-a terminat. Citeste mesajul de mai sus (sau trimite o poza).
pause
exit /b 1
