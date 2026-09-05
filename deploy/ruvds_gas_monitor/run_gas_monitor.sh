#!/usr/bin/env bash
# Idempotent wrapper: run weekly monitor + consecutive-failure alerts.
set -euo pipefail

ROOT="/opt/oracle1001/weekly_monitor"
ENV_FILE="${ROOT}/.env"
PY="${ROOT}/venv/bin/python"
APP="${ROOT}/weekly_gas_carrier_monitor.py"
STATE_DIR="${ROOT}/state"
LOG_DIR="${ROOT}/logs"
ALERTS="${LOG_DIR}/alerts.log"
STREAK_FILE="${STATE_DIR}/fail_streak"
LOCK_DIR="${STATE_DIR}/run.lock"

mkdir -p "${STATE_DIR}" "${LOG_DIR}" "${ROOT}/features/gas_carrier_weekly"

if [[ ! -x "${PY}" ]]; then
  echo "ERROR: venv python missing: ${PY}" >&2
  exit 1
fi
if [[ ! -f "${APP}" ]]; then
  echo "ERROR: monitor script missing: ${APP}" >&2
  exit 1
fi

# Prevent overlap on tiny RAM hosts
if mkdir "${LOCK_DIR}" 2>/dev/null; then
  trap 'rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT
else
  echo "ERROR: another gas-monitor run holds ${LOCK_DIR}" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

EXTRA_ARGS=()
KEY="${PROVIDER_API_KEY:-}"
ALLOW_DRY="${GAS_MONITOR_ALLOW_DRY_RUN:-0}"

if [[ -z "${KEY}" || "${KEY}" == YOUR_* || "${KEY}" == "YOUR_PROVIDER_API_KEY_HERE" ]]; then
  if [[ "${ALLOW_DRY}" == "1" ]]; then
    echo "WARN: PROVIDER_API_KEY unset/placeholder — running --dry-run (no paid calls)"
    EXTRA_ARGS+=(--dry-run)
    # 1GB RAM: do not materialize the entire gas list on bootstrap dry-run
    LIMIT_DRY="${GAS_MONITOR_DRY_RUN_LIMIT:-30}"
    EXTRA_ARGS+=(--limit "${LIMIT_DRY}")
  else
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "[${ts}] ALERT gas-monitor: PROVIDER_API_KEY missing and GAS_MONITOR_ALLOW_DRY_RUN!=1 — refusing run" >> "${ALERTS}"
    exit 2
  fi
fi

# Optional explicit limit (also used in production if set)
if [[ -n "${GAS_MONITOR_LIMIT:-}" ]]; then
  EXTRA_ARGS+=(--limit "${GAS_MONITOR_LIMIT}")
fi

# Honour optional fleet/out paths from .env (monitor also reads them)
if [[ -n "${FLEET_CSV:-}" ]]; then
  EXTRA_ARGS+=(--fleet-csv "${FLEET_CSV}")
fi
if [[ -n "${GAS_MONITOR_OUT_DIR:-}" ]]; then
  EXTRA_ARGS+=(--out-dir "${GAS_MONITOR_OUT_DIR}")
fi

set +e
"${PY}" "${APP}" "${EXTRA_ARGS[@]}"
rc=$?
set -e

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ "${rc}" -eq 0 ]]; then
  echo 0 > "${STREAK_FILE}"
  echo "[${ts}] gas-monitor OK"
  exit 0
fi

prev=0
if [[ -f "${STREAK_FILE}" ]]; then
  prev="$(tr -cd '0-9' < "${STREAK_FILE}" || true)"
  prev="${prev:-0}"
fi
streak=$((prev + 1))
echo "${streak}" > "${STREAK_FILE}"
echo "[${ts}] gas-monitor FAIL rc=${rc} streak=${streak}" | tee -a "${LOG_DIR}/runner.log" >&2

if [[ "${streak}" -ge 2 ]]; then
  {
    echo "[${ts}] ALERT gas-monitor: ${streak} consecutive failures (last rc=${rc})"
    echo "  Check: journalctl -u gas-monitor.service -n 100 --no-pager"
    echo "  Summary dir: ${GAS_MONITOR_OUT_DIR:-${ROOT}/features/gas_carrier_weekly}"
  } >> "${ALERTS}"
fi

exit "${rc}"
