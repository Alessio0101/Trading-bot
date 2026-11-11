@echo off
rem Double-click once: saves the optional Anthropic (Claude news) and FRED (economic data) keys.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai_team\system.ps1" chei
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo.
echo EROARE: cheile nu au fost salvate. Citeste mesajul de mai sus si trimite o poza.
pause
exit /b 1
