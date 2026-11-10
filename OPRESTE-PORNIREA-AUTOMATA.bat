@echo off
rem Double-click: turns off automatic start with Windows (does not stop the running system).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai_team\system.ps1" fara-autostart
pause
