@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  Oracle-1001 pipeline runner
echo  Root: %CD%
echo ============================================================

if not exist "venv\Scripts\python.exe" (
  echo ERROR: venv\Scripts\python.exe not found. Create venv first.
  exit /b 1
)

echo.
echo [1/4] Running run_all.py ...
".\venv\Scripts\python.exe" -X utf8 run_all.py
if errorlevel 1 (
  echo ERROR: run_all.py failed
  exit /b 1
)

echo.
echo [2/4] Verifying output artifacts ...
set MISSING=0
for %%F in (fleet_database.csv osint_layers.html dashboard.html) do (
  if not exist "output\%%F" (
    echo   MISSING: output\%%F
    set MISSING=1
  ) else (
    echo   OK: output\%%F
  )
)
if "%MISSING%"=="1" (
  echo ERROR: required output files missing
  exit /b 2
)

echo.
echo [3/4] Fleet metrics snapshot ...
".\venv\Scripts\python.exe" -X utf8 print_fleet_report.py
if errorlevel 1 (
  echo ERROR: print_fleet_report.py failed
  exit /b 3
)

echo.
echo [4/4] Restarting local server on :8765 ...
REM Kill stale listeners on 8765 (ignore errors)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765" ^| findstr LISTENING') do (
  echo   Killing PID %%P on :8765
  taskkill /PID %%P /F >nul 2>&1
)
REM Non-interactive sleep (timeout fails under redirected CI shells)
ping -n 2 127.0.0.1 >nul
start "Oracle-1001 HTTP :8765" /MIN ".\venv\Scripts\python.exe" -X utf8 run_server.py --port 8765 --force
ping -n 2 127.0.0.1 >nul

echo.
echo ============================================================
echo  DONE
echo  Open: http://127.0.0.1:8765/output/osint_layers.html
echo  Fleet: http://127.0.0.1:8765/output/dashboard.html
echo ============================================================
exit /b 0
