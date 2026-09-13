#!/usr/bin/env bash
# =============================================================================
# Oracle-1001 / Sentinel — Continuous AIS DB replication
# London LD8 (edge)  →  Korolev RuVDS (analytical core)
#
# Idempotent. Atomic replace (.tmp → rename). Bandwidth-limited.
# WAL: PRAGMA wal_checkpoint(PASSIVE) before rsync (non-blocking for writers).
# =============================================================================
set -euo pipefail

SRC_CANDIDATES=(
  "/opt/oracle1001/ais_ingest/история1/sentinel_ais.db"
  "/opt/oracle1001/ais_ingest/sentinel_ais.db"
  "/opt/oracle1001/sentinel_ais.db"
)
REMOTE_HOST="${KOROLEV_HOST:-45.8.230.214}"
REMOTE_USER="${KOROLEV_USER:-root}"
# Canonical analytical layout (Cyrillic) + ASCII root alias on Korolev
REMOTE_DIR="${KOROLEV_AIS_DIR:-/opt/oracle1001/ais_data}"
REMOTE_FILE="sentinel_ais.db"
REMOTE_ASCII_ALIAS="/opt/oracle1001/sentinel_ais.db"
# Also refresh legacy analytical_engine path if present (non-fatal)
REMOTE_LEGACY_DIR="/opt/oracle1001/analytical_engine/история1"
BW_LIMIT_KB="${SYNC_BW_LIMIT_KB:-2048}"
LOG_DIR="/opt/oracle1001/logs"
LOCK_FILE="/var/lock/oracle1001_ais_db_sync.lock"
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"

mkdir -p "${LOG_DIR}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*"; }

# ── lock (prevent overlapping cron runs) ─────────────────────────────────────
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "SKIP: another sync already running"
  exit 0
fi

T0=$(date +%s)

# ── resolve source DB ────────────────────────────────────────────────────────
SRC=""
for cand in "${SRC_CANDIDATES[@]}"; do
  if [[ -f "${cand}" ]]; then
    SRC="${cand}"
    break
  fi
done
if [[ -z "${SRC}" ]]; then
  log "ERROR: sentinel_ais.db not found in known paths"
  exit 1
fi
SIZE_BEFORE="$(stat -c '%s' "${SRC}" 2>/dev/null || echo 0)"
PREV_META="/opt/oracle1001/logs/.last_sync_src_size"
PREV_SIZE=0
[[ -f "${PREV_META}" ]] && PREV_SIZE="$(cat "${PREV_META}" 2>/dev/null || echo 0)"
log "SOURCE=${SRC} size=${SIZE_BEFORE} prev=${PREV_SIZE}"

# ── WAL checkpoint (PASSIVE — non-blocking for live writers) ─────────────────
CKPT_STATUS="ok"
if command -v sqlite3 >/dev/null 2>&1; then
  if ! sqlite3 "${SRC}" "PRAGMA wal_checkpoint(PASSIVE);" >/dev/null; then
    CKPT_STATUS="warn"
    log "WARN: wal_checkpoint returned non-zero (continuing)"
  else
    log "WAL checkpoint PASSIVE: ok"
  fi
else
  CKPT_STATUS="skipped"
  log "WARN: sqlite3 CLI missing — skipping WAL checkpoint"
fi

# ── Consistent online snapshot via sqlite3 .backup (avoids torn pages) ───────
SNAP="/tmp/sentinel_ais.db.snap.$$"
if command -v sqlite3 >/dev/null 2>&1; then
  rm -f "${SNAP}"
  if sqlite3 "${SRC}" ".backup '${SNAP}'"; then
    XFER_SRC="${SNAP}"
    log "SNAPSHOT ok path=${SNAP} size=$(stat -c '%s' "${SNAP}")"
  else
    log "WARN: .backup failed — falling back to raw DB file (integrity risk)"
    XFER_SRC="${SRC}"
  fi
else
  XFER_SRC="${SRC}"
fi

# ── prepare remote directory ─────────────────────────────────────────────────
ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}" \
  "mkdir -p '${REMOTE_DIR}' /opt/oracle1001/analytical_engine /tmp"

TMP_NAME="${REMOTE_FILE}.tmp.$$"
REMOTE_STAGE="/tmp/${TMP_NAME}"
REMOTE_FINAL="${REMOTE_DIR}/${REMOTE_FILE}"

# ── rsync snapshot to temporary remote file ──────────────────────────────────
log "SYNC start → ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_STAGE} (bw=${BW_LIMIT_KB}KB/s)"
rsync -az --partial --inplace \
  --bwlimit="${BW_LIMIT_KB}" \
  -e "ssh ${SSH_OPTS}" \
  "${XFER_SRC}" \
  "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_STAGE}"

# Cleanup local snapshot
[[ "${XFER_SRC}" == "${SNAP}" ]] && rm -f "${SNAP}" "${SNAP}-wal" "${SNAP}-shm" 2>/dev/null || true

# ── atomic rename + dual ASCII aliases ───────────────────────────────────────
ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}" bash -s <<EOF
set -euo pipefail
mkdir -p '${REMOTE_DIR}' /opt/oracle1001/analytical_engine '${REMOTE_LEGACY_DIR}'
mv -f '${REMOTE_STAGE}' '${REMOTE_FINAL}'
rm -f '${REMOTE_STAGE}-wal' '${REMOTE_STAGE}-shm' 2>/dev/null || true
ln -sfn '${REMOTE_FINAL}' /opt/oracle1001/analytical_engine/sentinel_ais.db
ln -sfn '${REMOTE_FINAL}' '${REMOTE_ASCII_ALIAS}'
# Best-effort legacy mirror (hardlink if same FS, else copy)
ln -f '${REMOTE_FINAL}' '${REMOTE_LEGACY_DIR}/${REMOTE_FILE}' 2>/dev/null \
  || cp -a '${REMOTE_FINAL}' '${REMOTE_LEGACY_DIR}/${REMOTE_FILE}'
date -u +"%Y-%m-%dT%H:%M:%SZ" > '${REMOTE_DIR}/.last_sync_utc'
mkdir -p /opt/oracle1001/logs
date -u +"%Y-%m-%dT%H:%M:%SZ" > /opt/oracle1001/logs/last_london_sync.ts
stat -c '%Y %s' '${REMOTE_FINAL}' > '${REMOTE_DIR}/.last_sync_meta'
ls -lh '${REMOTE_FINAL}'
EOF

SIZE_AFTER="$(stat -c '%s' "${SRC}" 2>/dev/null || echo 0)"
echo "${SIZE_AFTER}" > "${PREV_META}"
DELTA=$(( SIZE_AFTER - PREV_SIZE ))
T1=$(date +%s)
DUR=$(( T1 - T0 ))
log "SYNC OK size=${SIZE_AFTER} bytes delta=${DELTA} duration_sec=${DUR} wal_checkpoint=${CKPT_STATUS} → ${REMOTE_FINAL} (+ alias ${REMOTE_ASCII_ALIAS})"
exit 0
