#!/usr/bin/env bash
# Prepare gas weekly artifacts for pull (NO secrets). Idempotent.
set -euo pipefail
ROOT="/opt/oracle1001/weekly_monitor"
SRC="${ROOT}/features/gas_carrier_weekly"
EXPORT="${ROOT}/export/gas_carrier_weekly"
mkdir -p "${EXPORT}"
for f in summary.json results.json errors.json mission_control_module.json; do
  if [[ -f "${SRC}/${f}" ]]; then
    install -m 0644 "${SRC}/${f}" "${EXPORT}/${f}"
  fi
done
# Refuse to export env/secrets if someone misconfigures a wildcard copy later
if [[ -f "${EXPORT}/.env" ]]; then
  rm -f "${EXPORT}/.env"
  echo "Removed accidental .env from export" >&2
fi
echo "Export ready: ${EXPORT}"
ls -la "${EXPORT}"
