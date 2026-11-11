@echo off
rem Double-click once: connects the AI trading bot to your Telegram (paper trades + weekly report).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai_team\system.ps1" telegram
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo.
echo EROARE: Telegram nu a fost configurat. Citeste mesajul de mai sus si trimite o poza.
pause
exit /b 1
