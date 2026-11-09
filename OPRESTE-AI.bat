@echo off
rem Double-click: stops the AI trading system.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai_team\system.ps1" stop
pause
