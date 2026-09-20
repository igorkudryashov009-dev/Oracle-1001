#!/usr/bin/env bash
# Oracle-1001 / Sentinel — container init (path normalize + WAL-safe dirs)
set -euo pipefail

APP_HOME="${APP_HOME:-/app}"
export APP_HOME
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PORT="${DASHBOARD_PORT:-${PORT:-8765}}"
export DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-$PORT}"

# Container-relative paths only — never Windows absolute paths
export ASSETS_7000_DIR="${ASSETS_7000_DIR:-${APP_HOME}/assets/7000}"
export SENTINEL_DB_PATH="${SENTINEL_DB_PATH:-${APP_HOME}/история1/sentinel_ais.db}"
export OUTPUT_DIR="${OUTPUT_DIR:-${APP_HOME}/output}"
export DATA_DIR="${DATA_DIR:-${APP_HOME}/data}"

# Strip accidental host Windows path injection from common env keys
_normalize_win_path() {
  local key="$1"
  local val="${!key:-}"
  if [[ -z "$val" ]]; then
    return 0
  fi
  # Match C:\... or C:/... or \\Users\\...
  if [[ "$val" =~ ^[A-Za-z]:[\\/] ]] || [[ "$val" =~ ^\\\\ ]]; then
    echo "WARN: clearing Windows absolute path from ${key}=${val}" >&2
    unset "$key" || true
  fi
}

_normalize_win_path ASSETS_7000_DIR
_normalize_win_path SENTINEL_DB_PATH
_normalize_win_path FLEET_SOURCE_XLSX
_normalize_win_path ORACLE_SOURCE_XLSX
_normalize_win_path OUTPUT_DIR
_normalize_win_path DATA_DIR

# Re-apply container defaults if cleared
export ASSETS_7000_DIR="${ASSETS_7000_DIR:-${APP_HOME}/assets/7000}"
export SENTINEL_DB_PATH="${SENTINEL_DB_PATH:-${APP_HOME}/история1/sentinel_ais.db}"
export OUTPUT_DIR="${OUTPUT_DIR:-${APP_HOME}/output}"
export DATA_DIR="${DATA_DIR:-${APP_HOME}/data}"

# WAL files (-wal/-shm) MUST live on the same filesystem as the .db
DB_DIR="$(dirname "${SENTINEL_DB_PATH}")"
mkdir -p \
  "${DB_DIR}" \
  "${OUTPUT_DIR}/api/v1" \
  "${OUTPUT_DIR}/js" \
  "${OUTPUT_DIR}/assets/top10" \
  "${ASSETS_7000_DIR}" \
  "${DATA_DIR}" \
  "${APP_HOME}/logs"

# Soft WAL hygiene on start (PASSIVE — non-blocking for live writers)
if command -v sqlite3 >/dev/null 2>&1 && [[ -f "${SENTINEL_DB_PATH}" ]]; then
  sqlite3 "${SENTINEL_DB_PATH}" "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=60000; PRAGMA wal_checkpoint(PASSIVE);" \
    >/dev/null 2>&1 || true
fi

# Named volume may hide image-baked output/ — seed minimal health for HEALTHCHECK
HEALTH_FILE="${OUTPUT_DIR}/api/v1/health"
if [[ ! -f "${HEALTH_FILE}" ]]; then
  cat > "${HEALTH_FILE}" <<'EOF'
{"status":"ok","source_mode":"bootstrap","service":"oracle1001-sentinel"}
EOF
fi

mkdir -p "${OUTPUT_DIR}/archive" "${DATA_DIR}/archive" "${DATA_DIR}/cache" "${OUTPUT_DIR}/cache"

# OOB zero-touch: dirs + .env validate + intel mocks (Contract 1.8.0)
if [[ -f "${APP_HOME}/scripts/bootstrap_oob.py" ]]; then
  python "${APP_HOME}/scripts/bootstrap_oob.py" \
    >>"${APP_HOME}/logs/bootstrap_oob.log" 2>&1 || true
fi

# Out-of-box Archive + 7d balance bootstrap (sentinel-core only; non-blocking)
_should_bootstrap() {
  local flag="${SENTINEL_BOOTSTRAP_ARCHIVE:-}"
  if [[ "${flag}" == "1" || "${flag}" == "true" || "${flag}" == "yes" ]]; then
    return 0
  fi
  # Detect core command line without requiring compose env
  local joined="$*"
  if [[ "${joined}" == *run_sentinel_core* ]]; then
    return 0
  fi
  return 1
}

if _should_bootstrap "$@"; then
  (
    set +e
    sleep "${SENTINEL_BOOTSTRAP_DELAY_SEC:-20}"
    echo "[entrypoint] bootstrap: archive_service --force-sync" >&2
    python -m services.archive_service --force-sync \
      >>"${APP_HOME}/logs/bootstrap_archive.log" 2>&1
    echo "[entrypoint] bootstrap: balance_engine --hours 168" >&2
    python -m services.balance_engine --hours 168 \
      >>"${APP_HOME}/logs/bootstrap_balance.log" 2>&1
    echo "[entrypoint] bootstrap done" >&2
  ) &
fi

cd "${APP_HOME}"
exec "$@"
