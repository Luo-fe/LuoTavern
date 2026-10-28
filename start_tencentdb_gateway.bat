@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  TencentDB-Agent-Memory gateway (this window = logs)
echo  Close this window = stop gateway. The bot falls back to local memory.
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_memorycore.ps1"
echo.
pause
