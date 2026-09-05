#!/usr/bin/env bash
# AISStream connector healthcheck — London LD8
# Fails if service inactive OR MPS stayed ~0 for >3 minutes (via telemetry table / log).
set -euo pipefail

LOG_DIR="/opt/oracle1001/logs"
ALERT_LOG="${LOG_DIR}/alerts.log"
DB_CANDIDATES=(
  "/opt/oracle1001/ais_ingest/история1/sentinel_ais.db"
  "/opt/oracle1001/ais_ingest/sentinel_ais.db"
)
APP_LOG="/opt/oracle1001/ais_ingest/logs/aisstream_connector.log"
HEALTH_JSON="/opt/oracle1001/ais_ingest/output/health.json"
MPS_ZERO_LIMIT_SEC=180

mkdir -p "${LOG_DIR}" /opt/oracle1001/ais_ingest/output
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

UNIT_STATE="$(systemctl is-active aisstream-connector.service 2>/dev/null || echo inactive)"
MPS="0"
MATCHED="0"
RECONNECTS="0"
OK=1
REASON=""

if [[ "${UNIT_STATE}" != "active" ]]; then
  OK=0
  REASON="systemd_inactive"
fi

# Latest MPS from app log
if [[ -f "${APP_LOG}" ]]; then
  LINE="$(grep 'telemetry mps=' "${APP_LOG}" | tail -n 1 || true)"
  if [[ -n "${LINE}" ]]; then
    MPS="$(echo "${LINE}" | sed -n 's/.*mps=\([0-9.]*\).*/\1/p')"
    MATCHED="$(echo "${LINE}" | sed -n 's/.*matched=\([0-9]*\).*/\1/p')"
    RECONNECTS="$(echo "${LINE}" | sed -n 's/.*reconnects=\([0-9]*\).*/\1/p')"
  fi
fi

# Track consecutive zero-MPS window via stamp file
STAMP="${LOG_DIR}/.mps_zero_since"
MPS_INT="$(printf '%.0f' "${MPS:-0}" 2>/dev/null || echo 0)"
NOW_EPOCH="$(date +%s)"
if [[ "${UNIT_STATE}" == "active" ]]; then
  if [[ "${MPS_INT}" -eq 0 ]]; then
    if [[ ! -f "${STAMP}" ]]; then
      echo "${NOW_EPOCH}" > "${STAMP}"
    fi
    ZERO_SINCE="$(cat "${STAMP}")"
    ELAPSED=$((NOW_EPOCH - ZERO_SINCE))
    if [[ "${ELAPSED}" -ge "${MPS_ZERO_LIMIT_SEC}" ]]; then
      OK=0
      REASON="mps_zero_gt_${MPS_ZERO_LIMIT_SEC}s"
    fi
  else
    rm -f "${STAMP}"
  fi
fi

cat > "${HEALTH_JSON}" <<EOF
{
  "service": "aisstream-connector",
  "status": $([ "${OK}" -eq 1 ] && echo '"healthy"' || echo '"unhealthy"'),
  "systemd": "${UNIT_STATE}",
  "mps": ${MPS:-0},
  "matched": ${MATCHED:-0},
  "reconnects": ${RECONNECTS:-0},
  "reason": "${REASON}",
  "checked_at_utc": "$(ts)"
}
EOF

# Symlink for /api/v1/health static serve (run_server / nginx)
mkdir -p /opt/oracle1001/ais_ingest/output/api/v1
ln -sfn "${HEALTH_JSON}" /opt/oracle1001/ais_ingest/output/api/v1/health
# Also JSON at exact health path without .json for static servers that map the file
cp -f "${HEALTH_JSON}" /opt/oracle1001/ais_ingest/output/api/v1/health.json

if [[ "${OK}" -ne 1 ]]; then
  echo "[$(ts)] CRITICAL aisstream-connector reason=${REASON} mps=${MPS} systemd=${UNIT_STATE}" >> "${ALERT_LOG}"
  exit 1
fi
echo "[$(ts)] OK mps=${MPS} matched=${MATCHED} reconnects=${RECONNECTS}" >> "${LOG_DIR}/healthcheck.log"
exit 0
