#!/usr/bin/env bash
# Hard recover: stop ALL writers, vacuum clean DB into ais_data, restart stack.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
AIS=/opt/oracle1001/ais_data
APP=/opt/oracle1001/sentinel
DB="$AIS/sentinel_ais.db"
TS=$(date -u +%Y%m%dT%H%M%SZ)

cd "$APP"
echo "==> stop core+web"
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop sentinel-core sentinel-web || true
sleep 3

mkdir -p "$AIS/corrupt_backup"
cp -a "$DB" "$AIS/corrupt_backup/sentinel_ais.db.$TS" 2>/dev/null || true
cp -a "${DB}-wal" "$AIS/corrupt_backup/" 2>/dev/null || true
cp -a "${DB}-shm" "$AIS/corrupt_backup/" 2>/dev/null || true

VOL=$(docker volume inspect sentinel_data_sqlite -f '{{.Mountpoint}}' 2>/dev/null || true)
SRC=""
for cand in \
  "$VOL/sentinel_ais.db" \
  /opt/oracle1001/analytical_engine/история1/sentinel_ais.db \
  "$AIS/corrupt_backup/sentinel_ais.db.$TS"
do
  [[ -n "$cand" && -f "$cand" ]] || continue
  chk=$(sqlite3 "$cand" 'PRAGMA integrity_check;' 2>/dev/null | head -1 || true)
  echo "cand=$cand check=$chk"
  if [[ "$chk" == "ok" ]]; then
    SRC="$cand"
    break
  fi
done

rm -f "$DB" "${DB}-wal" "${DB}-shm" "$AIS/sentinel_ais.clean"
CLEAN="$AIS/sentinel_ais.clean"

if [[ -n "$SRC" ]]; then
  echo "==> VACUUM INTO clean from $SRC"
  sqlite3 "$SRC" "VACUUM INTO '$CLEAN';"
else
  echo "==> no good source — empty placeholder"
  : > "$CLEAN"
fi

# Live path = named volume (prod overlay). Seed volume, keep host file as staging only.
VOL_MP=$(docker volume inspect sentinel_data_sqlite -f '{{.Mountpoint}}' 2>/dev/null || true)
if [[ -n "$VOL_MP" ]]; then
  echo "==> seed named volume $VOL_MP"
  rm -f "$VOL_MP/sentinel_ais.db" "$VOL_MP/sentinel_ais.db-wal" "$VOL_MP/sentinel_ais.db-shm"
  cp -a "$CLEAN" "$VOL_MP/sentinel_ais.db"
  chown -R 10001:10001 "$VOL_MP" 2>/dev/null || chmod -R a+rwX "$VOL_MP" || true
fi
mv -f "$CLEAN" "$DB"
rm -f "${DB}-wal" "${DB}-shm"
sqlite3 "$DB" 'PRAGMA integrity_check;' 2>&1 | head -3 || true
chmod 666 "$DB" 2>/dev/null || true
chown -R 10001:10001 "$AIS" 2>/dev/null || chmod -R a+rwX "$AIS" || true

echo "==> start stack (named volume live DB)"
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
if [[ -f "$APP/scripts/run_sentinel_core.py" ]]; then
  docker cp "$APP/scripts/run_sentinel_core.py" sentinel-core:/app/scripts/run_sentinel_core.py || true
fi
sleep 15
docker ps --format '{{.Names}} {{.Status}}'
docker logs sentinel-core --tail 15 2>&1 || true
docker exec sentinel-core sqlite3 /app/история1/sentinel_ais.db 'PRAGMA integrity_check;' 2>&1 | head -2 || true
curl -fsS http://127.0.0.1:8765/output/api/v1/health > /tmp/h.json || true
python3 - <<'PY'
import json
h=json.load(open('/tmp/h.json'))
print('pipeline=', h.get('pipeline_health_status'))
print('replica=', (h.get('replica') or {}).get('status'))
print('cov_err=', (h.get('top500_coverage') or {}).get('error'))
print('mode=', h.get('source_mode'))
print('N=', h.get('top500_live_coverage'))
PY
echo HARD_RECOVER_DONE
