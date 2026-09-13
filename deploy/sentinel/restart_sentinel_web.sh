#!/usr/bin/env bash
# Restart sentinel-web using dynamic compose dir (ASCII-only).
# NEVER: /opt/sentinel, service name "web", find-only under deploy/sentinel.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
DEPLOY=/opt/oracle1001/deploy/sentinel
# shellcheck source=/dev/null
source "${DEPLOY}/resolve_compose_dir.sh" 2>/dev/null || source "$(dirname "$0")/resolve_compose_dir.sh"
APP=$(resolve_sentinel_app_dir) || { echo '[FAIL] compose dir not found'; exit 1; }
echo "[OK] compose dir=${APP}"
cd "${APP}"
if [[ -f docker-compose.prod.yml ]]; then
  docker compose -f docker-compose.yml -f docker-compose.prod.yml restart sentinel-web
else
  docker compose -f docker-compose.yml restart sentinel-web
fi
echo '[OK] sentinel-web restarted'
