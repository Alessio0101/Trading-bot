@echo off
rem Double-click: the AI trading system starts by itself every time you log in to Windows (paper only).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai_team\system.ps1" autostart
if errorlevel 1 goto failed
echo.
echo Gata. De acum botul porneste singur la fiecare pornire a Windows-ului.
echo Ca sa opresti pornirea automata: dublu-click pe OPRESTE-PORNIREA-AUTOMATA.bat
pause
exit /b 0
:failed
echo.
echo EROARE: pornirea automata NU a fost instalata. Citeste mesajul de mai sus si trimite o poza.
pause
exit /b 1
