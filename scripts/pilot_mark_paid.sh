#!/usr/bin/env bash
# Record the first payment for one pilot. Append-only. Git Bash.
set -euo pipefail
if [[ $# -ne 1 ]]; then
  echo "usage: scripts/pilot_mark_paid.sh <email>" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/venv/Scripts/python.exe"
if [[ ! -f "$PY" ]]; then
  PY="python"
fi
exec "$PY" -u -m services.pilot_outreach --paid "$1"
