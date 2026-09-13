#!/usr/bin/env bash
# Seed /opt/oracle1001/ais_data from current Docker volume (Korolev), then
# recreate stack with bind-mount + SENTINEL_AIS_MODE=off (edge on London).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

APP=/opt/oracle1001/sentinel
AIS=/opt/oracle1001/ais_data
mkdir -p "${AIS}" "${APP}"

echo "==> seed AIS DB from docker volume if present"
VOL=$(docker volume inspect sentinel_data_sqlite -f '{{.Mountpoint}}' 2>/dev/null || true)
if [[ -n "${VOL}" && -f "${VOL}/sentinel_ais.db" ]]; then
  cp -a "${VOL}/sentinel_ais.db" "${AIS}/sentinel_ais.db"
  cp -a "${VOL}/sentinel_ais.db-wal" "${AIS}/" 2>/dev/null || true
  cp -a "${VOL}/sentinel_ais.db-shm" "${AIS}/" 2>/dev/null || true
  echo "seeded from ${VOL}"
elif [[ -f /opt/oracle1001/analytical_engine/история1/sentinel_ais.db ]]; then
  cp -a /opt/oracle1001/analytical_engine/история1/sentinel_ais.db "${AIS}/sentinel_ais.db"
  echo "seeded from analytical_engine"
else
  # create empty placeholder — London sync will fill
  touch "${AIS}/sentinel_ais.db"
  echo "WARN: empty placeholder DB"
fi
ls -lh "${AIS}"

cd "${APP}"
# Ensure AIS off on Korolev (London is single WS owner — G3 connection cap)
mkdir -p /opt/oracle1001/logs
date -u +"%Y-%m-%dT%H:%M:%SZ" > /opt/oracle1001/logs/failover_cutover.ts
grep -q '^SENTINEL_AIS_MODE=' .env 2>/dev/null \
  && sed -i 's/^SENTINEL_AIS_MODE=.*/SENTINEL_AIS_MODE=off/' .env \
  || echo 'SENTINEL_AIS_MODE=off' >> .env

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --remove-orphans
sleep 6
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
curl -fsS http://127.0.0.1:8765/output/api/v1/health | head -c 400 || true
echo
echo "KOROLEV_ANALYTICS_MODE_OK"
