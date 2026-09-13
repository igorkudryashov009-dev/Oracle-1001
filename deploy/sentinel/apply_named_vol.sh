#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
APP=/opt/oracle1001/sentinel
AIS=/opt/oracle1001/ais_data
DEPLOY=/opt/oracle1001/deploy/sentinel
mkdir -p "$DEPLOY" /opt/oracle1001/logs "$AIS"

for f in heal_node.sh hard_recover_ais_db.sh export_ais_db_to_host.sh recover_ais_db.sh; do
  if [[ -f /tmp/$f ]]; then
    sed -i 's/\r$//' /tmp/$f
    install -m 0755 /tmp/$f "$DEPLOY/$f"
  fi
done
if [[ -f /tmp/docker-compose.prod.yml ]]; then
  sed -i 's/\r$//' /tmp/docker-compose.prod.yml
  cp -a /tmp/docker-compose.prod.yml "$APP/docker-compose.prod.yml"
fi

cd "$APP"
grep -q '^SENTINEL_AIS_MODE=' .env 2>/dev/null && sed -i 's/^SENTINEL_AIS_MODE=.*/SENTINEL_AIS_MODE=on/' .env || echo 'SENTINEL_AIS_MODE=on' >> .env

echo '==> seed volume'
VOL=$(docker volume inspect sentinel_data_sqlite -f '{{.Mountpoint}}' 2>/dev/null || true)
SRC=""
for cand in "$AIS/sentinel_ais.db" "$VOL/sentinel_ais.db"; do
  [[ -n "$cand" && -f "$cand" ]] || continue
  chk=$(sqlite3 "$cand" 'PRAGMA integrity_check;' 2>/dev/null | head -1 || true)
  echo "cand=$cand check=$chk"
  if [[ "$chk" == "ok" ]]; then SRC="$cand"; break; fi
done
if [[ -z "$SRC" ]]; then echo 'ERROR: no good DB'; exit 1; fi
mkdir -p "$VOL"
rm -f "$VOL/sentinel_ais.db" "$VOL/sentinel_ais.db-wal" "$VOL/sentinel_ais.db-shm"
CLEAN="$VOL/sentinel_ais.clean"
sqlite3 "$SRC" "VACUUM INTO '$CLEAN';"
mv "$CLEAN" "$VOL/sentinel_ais.db"
chown -R 10001:10001 "$VOL" 2>/dev/null || chmod -R a+rwX "$VOL" || true
sqlite3 "$VOL/sentinel_ais.db" 'PRAGMA integrity_check;' | head -1

echo '==> recreate named volume'
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate --remove-orphans
sleep 15
docker inspect sentinel-core --format '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}}{{println}}{{end}}' | head -20
docker exec sentinel-core sqlite3 /app/история1/sentinel_ais.db 'PRAGMA integrity_check;' | head -1 || true
docker logs sentinel-core --tail 15 2>&1 || true

tmp=/tmp/cron.$$
crontab -l 2>/dev/null | grep -vE 'heal_node|export_ais_db|hard_recover' >"$tmp" || true
echo "*/5 * * * * $DEPLOY/heal_node.sh --role korolev >> /opt/oracle1001/logs/heal_korolev.log 2>&1" >>"$tmp"
echo "*/15 * * * * $DEPLOY/export_ais_db_to_host.sh >> /opt/oracle1001/logs/export_ais.log 2>&1" >>"$tmp"
crontab "$tmp"; rm -f "$tmp"
crontab -l | grep -E 'heal|export' || true

bash "$DEPLOY/export_ais_db_to_host.sh" || true
curl -fsS http://127.0.0.1:8765/output/api/v1/health > /tmp/h.json || true
python3 - <<'PY'
import json
try:
  h=json.load(open('/tmp/h.json'))
  print(h.get('pipeline_health_status'), h.get('source_mode'), (h.get('replica') or {}).get('status'), h.get('top500_live_coverage'))
except Exception as e:
  print('health_err', e)
PY
echo SWITCH_NAMED_VOL_OK
