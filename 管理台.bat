@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and check Add to PATH.
    pause
    exit /b 1
  )
  py -3 memory_ui.py
) else (
  python memory_ui.py
)
echo.
pause
