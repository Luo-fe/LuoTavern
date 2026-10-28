@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem already running? just open the page
powershell -NoProfile -Command "try{Invoke-WebRequest 'http://127.0.0.1:8008' -UseBasicParsing -TimeoutSec 1|Out-Null; exit 0}catch{exit 1}" >nul 2>nul
if not errorlevel 1 goto up

where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python not found.
    pause
    exit /b 1
  )
  set PYCMD=py -3
) else (
  set PYCMD=python
)

echo Starting memory UI ...
start "MemoryUI" cmd /k "cd /d %~dp0 && %PYCMD% -u memory_ui.py"

rem wait for server then open browser
for /l %%i in (1,1,8) do (
  timeout /t 1 /nobreak >nul
  powershell -NoProfile -Command "try{Invoke-WebRequest 'http://127.0.0.1:8008' -UseBasicParsing -TimeoutSec 1|Out-Null; exit 0}catch{exit 1}" >nul 2>nul
  if not errorlevel 1 goto up
)
echo [WARN] Server not ready after 16s. Check the MemoryUI window for errors.
pause
exit /b 1

:up
start "" http://127.0.0.1:8008
exit /b 0
