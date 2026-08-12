@echo off
REM Daily snapshot wrapper for Windows Task Scheduler (00:05 UTC).
REM Uses the venv python.exe by FULL PATH (not "python" via PATH/activate.bat)
REM -- Task Scheduler runs non-interactively and PATH/activate can silently
REM resolve to a system Python without our dependencies (pandas, etc.),
REM which then fails with ModuleNotFoundError and looks like "the task never runs".
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [FATAL] venv python not found at %PY% — run: python -m venv venv ; venv\Scripts\pip install -r requirements.txt >> logs\snapshot_log.txt
  exit /b 1
)

"%PY%" daily_snapshot.py
"%PY%" forecast.py
"%PY%" build_history_dashboard.py
endlocal
