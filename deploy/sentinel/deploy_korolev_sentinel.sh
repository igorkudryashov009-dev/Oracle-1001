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

mkdir -p data/archive output/archive output/models output/assets/3d_models assets/7000 assets/arctic logs
if [[ ! -f output/fleet_database.csv ]]; then
  echo "ERROR: output/fleet_database.csv missing — sync from workstation first" >&2
  exit 1
fi
if [[ ! -d assets/arctic/videos ]]; then
  echo "WARN: assets/arctic/videos missing — ARCTIC sheet will 404 flight videos" >&2
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

# Named volume output_artifacts hides image-baked /app/output. Seed HUD artifacts
# from the host Sync-Tree (same discipline as never losing *.glb / arctic *.mp4).
echo "==> seed output_artifacts volume from host Sync-Tree"
VOL_OUT="$(docker volume inspect sentinel_output_artifacts -f '{{.Mountpoint}}' 2>/dev/null || true)"
if [[ -n "${VOL_OUT}" && -d "${VOL_OUT}" ]]; then
  mkdir -p "${VOL_OUT}/js" "${VOL_OUT}/css" "${VOL_OUT}/assets" "${VOL_OUT}/archive" "${VOL_OUT}/api/v1"
  if [[ -d output/js ]]; then
    rsync -a --delete output/js/ "${VOL_OUT}/js/"
  fi
  if [[ -d output/css ]]; then
    rsync -a output/css/ "${VOL_OUT}/css/"
  fi
  if [[ -f output/sentinel_dashboard.html ]]; then
    cp -a output/sentinel_dashboard.html "${VOL_OUT}/sentinel_dashboard.html"
  fi
  if [[ -d output/assets ]]; then
    rsync -a --exclude='_probe' --exclude='screenshots' output/assets/ "${VOL_OUT}/assets/"
  fi
  if [[ -f output/qflex_fleet_cargo.json ]]; then
    cp -a output/qflex_fleet_cargo.json "${VOL_OUT}/qflex_fleet_cargo.json"
  fi
  if [[ -f output/archive/api_status.json ]]; then
    mkdir -p "${VOL_OUT}/archive"
    cp -a output/archive/api_status.json "${VOL_OUT}/archive/api_status.json"
  fi
  chown -R 10001:10001 "${VOL_OUT}/js" "${VOL_OUT}/css" "${VOL_OUT}/assets" \
    "${VOL_OUT}/sentinel_dashboard.html" 2>/dev/null || chmod -R a+rX "${VOL_OUT}/js" "${VOL_OUT}/assets" || true
  echo "SEEDED_OUTPUT_VOLUME ok js=$(ls "${VOL_OUT}/js" | wc -l) arctic_vid=$(ls assets/arctic/videos 2>/dev/null | wc -l)"
else
  echo "WARN: sentinel_output_artifacts mountpoint not found — HUD may serve stale volume bytes" >&2
fi

docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker exec sentinel-web python - <<'PY'
from pathlib import Path
from services.dual_gate import DISK_FREE_MIN_PCT, probe_disk_usage
from services import log_retention, quant_risk_service
src = open("/app/services/archive_service.py", encoding="utf-8", errors="ignore").read()
assert DISK_FREE_MIN_PCT == 20.0
assert "PREMIUM SATELLITE" not in src
assert hasattr(quant_risk_service, "compute_quant_risk_payload")
assert hasattr(log_retention, "run_retention")
assert Path("/app/services/maptiles_proxy.py").is_file()
assert Path("/app/services/vesselfinder_client.py").is_file()
assert Path("/app/output/js/arctic_sheet.js").is_file(), "arctic_sheet.js missing in output volume"
arctic_vid = Path("/app/assets/arctic/videos")
assert arctic_vid.is_dir(), "assets/arctic bind-mount missing"
print(
    "BAKE_OK disk_min=", DISK_FREE_MIN_PCT,
    "disk=", probe_disk_usage().get("disk_free_pct"),
    "arctic_js=1",
    "arctic_mp4=", sum(1 for _ in arctic_vid.glob("*.mp4")),
)
PY

# Re-seed after BAKE_OK — core/web may rewrite top10_vessels_manifest.js during first boot.
echo "==> re-seed output_artifacts after first-boot writers settle"
sleep 5
if [[ -n "${VOL_OUT}" && -d "${VOL_OUT}" && -d output/js ]]; then
  rsync -a output/js/ "${VOL_OUT}/js/"
  [[ -f output/sentinel_dashboard.html ]] && cp -a output/sentinel_dashboard.html "${VOL_OUT}/sentinel_dashboard.html"
  chown -R 10001:10001 "${VOL_OUT}/js" 2>/dev/null || true
  echo "RESEED_OUTPUT_VOLUME ok"
fi

free -h
echo "==> deploy_korolev_sentinel done (baked)"
