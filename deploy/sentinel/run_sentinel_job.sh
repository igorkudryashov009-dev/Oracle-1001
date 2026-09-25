#!/usr/bin/env bash
# Host-side Sentinel job runner (systemd oneshot).
# 1) docker exec job into sentinel-web (secrets + SENTINEL_DB_PATH)
# 2) honour watchdog restart queue written under data/archive/ (bind mount)
set -euo pipefail

JOB="${1:-}"
if [[ -z "${JOB}" ]]; then
  echo "usage: run_sentinel_job.sh <job_name>" >&2
  exit 2
fi

APP_ROOT="${APP_ROOT:-/opt/oracle1001/sentinel}"
REQ="${APP_ROOT}/data/archive/watchdog_restart_request"
FORBIDDEN_RESTART="sentinel-web sentinel_api_edge"

if ! docker ps --format '{{.Names}}' | grep -qx sentinel-web; then
  echo "ERROR: sentinel-web not running" >&2
  exit 3
fi

echo "==> job=${JOB} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker exec sentinel-web python -m services.scheduler --job "${JOB}"
rc=$?

if [[ -f "${REQ}" ]]; then
  target="$(tr -d '[:space:]' < "${REQ}" || true)"
  rm -f "${REQ}"
  if [[ -n "${target}" ]]; then
    if echo " ${FORBIDDEN_RESTART} " | grep -q " ${target} "; then
      echo "REFUSED restart of edge-gateway: ${target}" >&2
    else
      echo "==> watchdog queued restart: ${target}"
      docker restart "${target}" || true
    fi
  fi
fi

exit "${rc}"
