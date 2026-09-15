#!/usr/bin/env bash
# OSM tile env + web restart with dynamic compose dir resolution.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# shellcheck source=/dev/null
source /opt/oracle1001/deploy/sentinel/resolve_compose_dir.sh 2>/dev/null \
  || source "$(dirname "$0")/resolve_compose_dir.sh"

APP=$(resolve_sentinel_app_dir) || {
  echo '[FAIL] docker-compose.yml not found under /opt/oracle1001'
  exit 1
}
echo "[OK] compose dir=${APP}"
cd "${APP}"

touch .env
grep -vE '^(MAPBOX_KEY|TILE_SERVER)=' .env > .env.tmp || true
mv .env.tmp .env
echo 'MAPBOX_KEY=' >> .env
echo 'TILE_SERVER=https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}' >> .env
echo 'TILE_SUBDOMAINS=' >> .env
echo 'TILE_ATTR=Tiles &copy; Esri' >> .env
echo 'SENTINEL_ASSET_V=basemap-v3' >> .env
grep -E 'MAPBOX|TILE|ASSET_V' .env || true

# Restart web service (canonical name: sentinel-web)
if [[ -f docker-compose.prod.yml ]]; then
  docker compose -f docker-compose.yml -f docker-compose.prod.yml restart sentinel-web
else
  docker compose -f docker-compose.yml restart sentinel-web
fi
sleep 5
curl -fsS -o /dev/null -w 'tiles=%{http_code}\n' http://127.0.0.1:8765/output/js/map_tiles.js
curl -fsS http://127.0.0.1:8765/output/sentinel_dashboard.html | grep -E 'map_tiles|__SENTINEL_MAP__|openstreetmap' | head -n 5 || true
curl -fsS http://127.0.0.1:8765/output/api/v1/health > /tmp/h.json
python3 -c "import json;h=json.load(open('/tmp/h.json'));print('[OK]',h.get('pipeline_health_status'),h.get('source_mode'),(h.get('replica') or {}).get('status'))"
df -h / | tail -n 1
echo '[OK] map_env_restart_done'
