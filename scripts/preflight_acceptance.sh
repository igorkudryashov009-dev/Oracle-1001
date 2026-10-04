#!/usr/bin/env bash
# Preflight for the 02:00 UTC acceptance cut. Git Bash. No WSL.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/venv/Scripts/python.exe"
if [[ ! -x "$PY" && ! -f "$PY" ]]; then
  PY="python"
fi
exec "$PY" -u "${ROOT}/scripts/preflight_acceptance.py"
