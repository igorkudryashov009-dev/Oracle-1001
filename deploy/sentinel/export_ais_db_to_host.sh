#!/usr/bin/env bash
# Checkpoint WAL inside named volume, then copy a consistent file to host staging
# for London sync / offline backup. Host path is NOT the live writer.
set -euo pipefail
AIS=/opt/oracle1001/ais_data
STAGING="${AIS}/sentinel_ais.db"
mkdir -p "${AIS}"

if docker ps --format '{{.Names}}' | grep -qx sentinel-core; then
  docker exec sentinel-core sqlite3 /app/история1/sentinel_ais.db 'PRAGMA wal_checkpoint(TRUNCATE);' >/dev/null 2>&1 || true
  docker cp sentinel-core:/app/история1/sentinel_ais.db "${STAGING}.tmp"
else
  VOL=$(docker volume inspect sentinel_data_sqlite -f '{{.Mountpoint}}' 2>/dev/null || true)
  [[ -n "${VOL}" && -f "${VOL}/sentinel_ais.db" ]] || { echo "no source"; exit 1; }
  sqlite3 "${VOL}/sentinel_ais.db" 'PRAGMA wal_checkpoint(TRUNCATE);' >/dev/null 2>&1 || true
  cp -a "${VOL}/sentinel_ais.db" "${STAGING}.tmp"
fi

chk=$(sqlite3 "${STAGING}.tmp" 'PRAGMA integrity_check;' 2>/dev/null | head -1 || true)
if [[ "${chk}" != "ok" ]]; then
  echo "EXPORT_ABORT integrity=${chk}"
  rm -f "${STAGING}.tmp"
  exit 2
fi
mv -f "${STAGING}.tmp" "${STAGING}"
rm -f "${STAGING}-wal" "${STAGING}-shm" 2>/dev/null || true
date -u +"%Y-%m-%dT%H:%M:%SZ" > "${AIS}/.last_export_utc"
ls -lh "${STAGING}"
echo EXPORT_OK
