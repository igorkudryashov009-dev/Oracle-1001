#!/usr/bin/env bash
# Manual bake driver (ASCII) — pack from workstation, bake on A, sync SoT to B.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NODE_A="${NODE_A:-45.8.230.214}"
NODE_B="${NODE_B:-185.39.19.75}"
PACK=/tmp/sentinel_deploy_bake.tgz
cd "$ROOT"

echo "==> packing"
tar -czf "$PACK" \
  --exclude=venv --exclude=.git --exclude=logs --exclude=__pycache__ \
  --exclude=output/_qa_fidelity_v31 --exclude=output/.publish_snapshot \
  --exclude=assets/7000/videos --exclude=assets/1-10 \
  --exclude='*.mp4' --exclude=node_modules \
  docker-compose.yml docker-compose.prod.yml Dockerfile .dockerignore \
  docker services scripts web config.yaml requirements.txt \
  run_release.py build_sentinel_dashboard.py api_server.py \
  AGENTS.md CHANGELOG.md data deploy/sentinel \
  output/fleet_database.csv output/fleet_oil_tankers.csv output/fleet_database_full.csv \
  output/sentinel_dashboard.html output/js output/css output/assets \
  output/archive/api_status.json output/models \
  assets/7000

echo "==> scp Node A"
scp -o BatchMode=yes "$PACK" "root@${NODE_A}:/tmp/sentinel_deploy.tgz"
scp -o BatchMode=yes "$ROOT/.env" "root@${NODE_A}:/opt/oracle1001/sentinel/.env" 2>/dev/null || true

echo "==> extract + bake Node A"
ssh -o BatchMode=yes "root@${NODE_A}" 'set -e
mkdir -p /opt/oracle1001/sentinel
tar -xzf /tmp/sentinel_deploy.tgz -C /opt/oracle1001/sentinel
sed -i "s/\r$//" /opt/oracle1001/sentinel/deploy/sentinel/deploy_korolev_sentinel.sh
NO_CACHE=1 FORCE_RECREATE=1 bash /opt/oracle1001/sentinel/deploy/sentinel/deploy_korolev_sentinel.sh
'

echo "==> sync Node B SoT"
scp -o BatchMode=yes "$PACK" "root@${NODE_B}:/tmp/sentinel_deploy.tgz"
ssh -o BatchMode=yes "root@${NODE_B}" 'set -e
rm -rf /tmp/sentinel_london_stage
mkdir -p /tmp/sentinel_london_stage
tar -xzf /tmp/sentinel_deploy.tgz -C /tmp/sentinel_london_stage
sed -i "s/\r$//" /tmp/sentinel_london_stage/deploy/sentinel/install_london_ais_relay.sh
STAGE_DIR=/tmp/sentinel_london_stage bash /tmp/sentinel_london_stage/deploy/sentinel/install_london_ais_relay.sh
grep -q DISK_FREE_MIN_PCT /opt/oracle1001/ais_ingest/services/dual_gate.py
grep -q "OSINT REGISTRY" /opt/oracle1001/ais_ingest/services/archive_service.py
if grep -q "PREMIUM SATELLITE" /opt/oracle1001/ais_ingest/services/archive_service.py; then echo NODE_B_PREMIUM_BAD; exit 1; fi
echo NODE_B_SOT_OK
'

echo "==> bake_driver done"
