#!/bin/bash
# Read-only. Does not spend GFW credits and does not write state.
set -eu
cd "$(dirname "$0")/.."
if [ -x "./venv/Scripts/python.exe" ]; then
  PY="./venv/Scripts/python.exe"
elif [ -x "./venv/bin/python" ]; then
  PY="./venv/bin/python"
else
  PY="python"
fi
exec "$PY" -u scripts/morning_sweep_check.py
