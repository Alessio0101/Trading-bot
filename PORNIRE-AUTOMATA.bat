@echo off
rem Double-click: the AI trading system starts by itself every time you log in to Windows (paper only).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai_team\system.ps1" autostart
echo.
echo Gata. De acum botul porneste singur la fiecare pornire a Windows-ului.
echo Ca sa opresti pornirea automata: dublu-click pe OPRESTE-PORNIREA-AUTOMATA.bat
pause
