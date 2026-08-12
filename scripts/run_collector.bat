@echo off
REM Long-running AIS collector (keep PC awake / or run on VPS).
REM Uses the venv python.exe by FULL PATH -- see run_daily_snapshot.bat for why.
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [FATAL] venv python not found at %PY% — run: python -m venv venv ; venv\Scripts\pip install -r requirements.txt
  exit /b 1
)

"%PY%" collector.py
endlocal
