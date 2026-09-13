#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
AIS=/opt/oracle1001/ais_data
DB="$AIS/sentinel_ais.db"
APP=/opt/oracle1001/sentinel
TS=$(date -u +%Y%m%dT%H%M%SZ)

echo "==> stop core writer"
cd "$APP"
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop sentinel-core || true
sleep 2

mkdir -p "$AIS/corrupt_backup"
cp -a "$DB" "$AIS/corrupt_backup/sentinel_ais.db.$TS" 2>/dev/null || true
cp -a "${DB}-wal" "$AIS/corrupt_backup/" 2>/dev/null || true
cp -a "${DB}-shm" "$AIS/corrupt_backup/" 2>/dev/null || true

echo "==> integrity current"
sqlite3 "$DB" 'PRAGMA integrity_check;' 2>&1 | head -5 || true

VOL=$(docker volume inspect sentinel_data_sqlite -f '{{.Mountpoint}}' 2>/dev/null || true)
LEG=/opt/oracle1001/analytical_engine/история1/sentinel_ais.db
RESTORED=0

try_restore() {
  local src="$1"
  local label="$2"
  [[ -f "$src" ]] || return 1
  local check
  check=$(sqlite3 "$src" 'PRAGMA integrity_check;' 2>/dev/null | head -1 || true)
  echo "==> check $label => $check"
  if [[ "$check" == "ok" ]]; then
    rm -f "$DB" "${DB}-wal" "${DB}-shm"
    cp -a "$src" "$DB"
    # optional wal/shm
    [[ -f "${src}-wal" ]] && cp -a "${src}-wal" "${DB}-wal" || true
    [[ -f "${src}-shm" ]] && cp -a "${src}-shm" "${DB}-shm" || true
    RESTORED=1
    echo "RESTORED_FROM=$label"
    return 0
  fi
  return 1
}

if [[ -n "$VOL" ]]; then
  try_restore "$VOL/sentinel_ais.db" "docker_volume" || true
fi
if [[ "$RESTORED" -eq 0 ]]; then
  try_restore "$LEG" "analytical_engine" || true
fi
if [[ "$RESTORED" -eq 0 ]]; then
  echo "==> attempt .recover"
  rm -f "$DB.recovered"
  if sqlite3 "$DB" '.recover' 2>/tmp/recover.err | sqlite3 "$DB.recovered"; then
    check=$(sqlite3 "$DB.recovered" 'PRAGMA integrity_check;' 2>/dev/null | head -1 || true)
    echo "recover_check=$check"
    if [[ "$check" == "ok" ]]; then
      rm -f "$DB" "${DB}-wal" "${DB}-shm"
      mv "$DB.recovered" "$DB"
      RESTORED=1
    fi
  else
    head -20 /tmp/recover.err || true
  fi
fi
if [[ "$RESTORED" -eq 0 ]]; then
  echo "==> fresh DB (schema will be created by connector)"
  rm -f "$DB" "${DB}-wal" "${DB}-shm"
fi

echo "==> final integrity"
sqlite3 "$DB" 'PRAGMA integrity_check;' 2>&1 | head -3 || echo "(empty/new db)"
chown -R 10001:10001 "$AIS" 2>/dev/null || chmod -R a+rwX "$AIS" || true

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
# re-inject supervisor
if [[ -f "$APP/scripts/run_sentinel_core.py" ]]; then
  docker cp "$APP/scripts/run_sentinel_core.py" sentinel-core:/app/scripts/run_sentinel_core.py || true
fi
sleep 12
docker logs sentinel-core --tail 25 2>&1 || true
curl -fsS http://127.0.0.1:8765/output/api/v1/health > /tmp/h.json || true
python3 - <<'PY'
import json
try:
  h=json.load(open('/tmp/h.json'))
  print('pipeline=', h.get('pipeline_health_status'))
  print('replica=', (h.get('replica') or {}).get('status'))
  print('cov_err=', (h.get('top500_coverage') or {}).get('error'))
  print('mode=', h.get('source_mode'))
except Exception as e:
  print('health_parse_err', e)
PY
echo DB_RECOVER_DONE
