@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  TencentDB-Agent-Memory one-click setup
echo  (download ~tens of MB, npm build takes minutes)
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_memorycore.ps1"
echo.
echo ==== For daily use, double-click start_tencentdb_gateway.bat ====
echo.
pause
