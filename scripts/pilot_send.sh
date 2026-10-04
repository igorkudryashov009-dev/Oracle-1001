#!/usr/bin/env bash
# Send one pilot offer. Git Bash. Does not send unless MAILGUN_* is set and the client is registered.
set -euo pipefail
if [[ $# -ne 2 ]]; then
  echo "usage: scripts/pilot_send.sh <email> <lang>" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/venv/Scripts/python.exe"
if [[ ! -f "$PY" ]]; then
  PY="python"
fi
exec "$PY" -u -m services.pilot_outreach "$1" "$2"
