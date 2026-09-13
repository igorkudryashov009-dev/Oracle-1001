#!/usr/bin/env bash
# Deploy / refresh Sentinel stack on Korolev (Node A) — FULL IMAGE BAKE.
# Guarantees hotfixes live in the image layers, not docker-cp memory.
# Run ON the server from /opt/oracle1001/sentinel
set -euo pipefail
APP_ROOT="${APP_ROOT:-/opt/oracle1001/sentinel}"
NO_CACHE="${NO_CACHE:-1}"
FORCE_RECREATE="${FORCE_RECREATE:-1}"
cd "${APP_ROOT}"

echo "==> free RAM before start"
free -h

# Conflicting hybrid monitor (Streamlit ~130MB RSS) — stop to protect 1GB budget
if docker ps --format '{{.Names}}' | grep -qx 'eoil-monitor'; then
  echo "==> stopping eoil-monitor (RAM conflict with Sentinel on 1GB VPS)"
  docker update --restart=no eoil-monitor || true
  docker stop eoil-monitor || true
fi

# Optional legacy receiver
if pgrep -f '/opt/sentinel/receiver.py' >/dev/null 2>&1; then
  echo "==> stopping legacy /opt/sentinel/receiver.py"
  pkill -f '/opt/sentinel/receiver.py' || true
fi

mkdir -p data/archive output/archive output/models output/assets/3d_models assets/7000 logs
if [[ ! -f output/fleet_database.csv ]]; then
  echo "ERROR: output/fleet_database.csv missing — sync from workstation first" >&2
  exit 1
fi
if [[ ! -f output/fleet_oil_tankers.csv ]]; then
  echo 'imo,mmsi,vessel_name,vessel_type,dwt_tons,draft_m,flag,vessel_category' > output/fleet_oil_tankers.csv
fi
if [[ ! -f output/fleet_database_full.csv ]]; then
  cp -a output/fleet_database.csv output/fleet_database_full.csv
fi

if [[ ! -f .env ]]; then
  echo "WARN: .env missing — creating stub (set AISSTREAM_API_KEY manually)" >&2
  touch .env
fi

echo "==> IMAGE BAKE (no-cache=${NO_CACHE} force-recreate=${FORCE_RECREATE})"
if [[ "${NO_CACHE}" == "1" ]]; then
  docker compose -f docker-compose.yml -f docker-compose.prod.yml build --no-cache --pull
else
  docker compose -f docker-compose.yml -f docker-compose.prod.yml build
fi

if [[ "${FORCE_RECREATE}" == "1" ]]; then
  echo "==> full destroy + recreate from baked image (not restart)"
  docker compose -f docker-compose.yml -f docker-compose.prod.yml down --remove-orphans
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate --remove-orphans
else
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --remove-orphans
fi

echo "==> wait health"
for i in $(seq 1 48); do
  if curl -fsS --max-time 5 "http://127.0.0.1:8765/output/api/v1/health" >/tmp/sentinel_health.json 2>/dev/null; then
    python3 - <<'PY'
import json
h=json.load(open("/tmp/sentinel_health.json"))
print("pipeline=", h.get("pipeline_health_status"),
      "fleet=", h.get("fleet_sample_status"),
      "coverage=", h.get("top500_live_coverage"),
      "disk_free=", h.get("disk_free_pct"),
      "active_node=", h.get("active_node"),
      "mode=", h.get("source_mode"))
PY
    break
  fi
  sleep 5
done

docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker exec sentinel-web python - <<'PY'
from services.dual_gate import DISK_FREE_MIN_PCT, probe_disk_usage
from services import log_retention, quant_risk_service
src = open("/app/services/archive_service.py", encoding="utf-8", errors="ignore").read()
assert DISK_FREE_MIN_PCT == 20.0
assert "PREMIUM SATELLITE" not in src
assert hasattr(quant_risk_service, "compute_quant_risk_payload")
assert hasattr(log_retention, "run_retention")
print("BAKE_OK disk_min=", DISK_FREE_MIN_PCT, "disk=", probe_disk_usage().get("disk_free_pct"))
PY

free -h
echo "==> deploy_korolev_sentinel done (baked)"
