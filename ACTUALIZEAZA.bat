@echo off
rem Double-click: downloads the newest version, keeps the paper account, history and keys, and restarts.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai_team\system.ps1" actualizeaza
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo.
echo EROARE: actualizarea nu s-a terminat. Botul ruleaza in continuare versiunea veche.
echo Citeste mesajul de mai sus si trimite o poza.
pause
exit /b 1
