#!/usr/bin/env bash
# Install Sentinel zero-touch systemd timers on Korolev (Node A).
# Safe to re-run (idempotent).
set -euo pipefail
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/oracle1001/deploy/sentinel}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)/systemd"
if [[ ! -d "${SRC_DIR}" ]]; then
  SRC_DIR="${DEPLOY_DIR}/systemd"
fi
if [[ ! -d "${SRC_DIR}" ]]; then
  SRC_DIR="/opt/oracle1001/sentinel/deploy/sentinel/systemd"
fi
if [[ ! -d "${SRC_DIR}" ]]; then
  echo "ERROR: systemd unit source missing" >&2
  exit 2
fi

echo "==> install sentinel automation units from ${SRC_DIR}"
install -m 0755 "${DEPLOY_DIR}/run_sentinel_job.sh" /opt/oracle1001/deploy/sentinel/run_sentinel_job.sh 2>/dev/null \
  || install -m 0755 "$(dirname "$0")/run_sentinel_job.sh" /opt/oracle1001/deploy/sentinel/run_sentinel_job.sh
sed -i 's/\r$//' /opt/oracle1001/deploy/sentinel/run_sentinel_job.sh
chmod +x /opt/oracle1001/deploy/sentinel/run_sentinel_job.sh

install -m 0644 "${SRC_DIR}/sentinel-job@.service" "${UNIT_DIR}/sentinel-job@.service"
for t in \
  sentinel-archive-snapshot.timer \
  sentinel-gfw-poll.timer \
  sentinel-vf-allocator.timer \
  sentinel-watchdog.timer \
  sentinel-budget-sync.timer
do
  install -m 0644 "${SRC_DIR}/${t}" "${UNIT_DIR}/${t}"
done

systemctl daemon-reload
for t in \
  sentinel-archive-snapshot.timer \
  sentinel-gfw-poll.timer \
  sentinel-vf-allocator.timer \
  sentinel-watchdog.timer \
  sentinel-budget-sync.timer
do
  systemctl enable --now "${t}"
done

echo "==> active sentinel timers"
systemctl list-timers --all | grep -i sentinel || true

RUNNER=/opt/oracle1001/deploy/sentinel/run_sentinel_job.sh
# Kick all jobs once so job_log + health.scheduler populate (idempotent skips OK)
if docker ps --format '{{.Names}}' | grep -qx sentinel-web; then
  echo "==> bootstrap scheduler jobs (idempotent)"
  for j in pipeline_watchdog budget_sync archive_snapshot gfw_poll vf_allocator; do
    if [[ -x "${RUNNER}" ]]; then
      bash "${RUNNER}" "$j" || true
    else
      docker exec sentinel-web python -m services.scheduler --job "$j" || true
    fi
  done
  docker exec sentinel-web python -m services.scheduler --check-overdue || true
fi
echo "SENTINEL_AUTOMATION_ARMED"
