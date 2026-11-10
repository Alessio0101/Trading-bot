@echo off
rem Double-click: starts the AI trading system (paper only) and shows the link for the iPhone.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai_team\system.ps1" telefon
if errorlevel 1 goto failed
echo.
echo Poti inchide aceasta fereastra: sistemul ruleaza in fundal si se reporneste singur daca o componenta se opreste.
echo Pentru oprire: dublu-click pe OPRESTE-AI.bat
pause
exit /b 0
:failed
echo.
echo EROARE: sistemul NU a pornit. Citeste mesajul de mai sus si trimite o poza.
pause
exit /b 1
