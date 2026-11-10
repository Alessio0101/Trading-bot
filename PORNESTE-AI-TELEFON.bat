@echo off
rem Double-click: starts the AI trading system (paper only) and shows the link for the iPhone.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai_team\system.ps1" telefon
echo.
echo Poti inchide aceasta fereastra: sistemul ruleaza in fundal si se reporneste singur daca o componenta se opreste.
echo Pentru oprire: dublu-click pe OPRESTE-AI.bat
pause
