@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_bot.ps1" -Platform discord
echo.
pause
