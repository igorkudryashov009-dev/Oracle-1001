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
mkdir -p /opt/oracle1001/deploy/sentinel
DEST_RUNNER=/opt/oracle1001/deploy/sentinel/run_sentinel_job.sh
RUNNER_SRC="$(dirname "$0")/run_sentinel_job.sh"
[[ -f "${RUNNER_SRC}" ]] || RUNNER_SRC="${DEPLOY_DIR}/run_sentinel_job.sh"
if [[ -f "${RUNNER_SRC}" ]]; then
  # Avoid cp self→self (fails with exit 1 on GNU cp).
  if [[ "${RUNNER_SRC}" != "${DEST_RUNNER}" ]]; then
    cp -f "${RUNNER_SRC}" "${DEST_RUNNER}"
  fi
fi
sed -i 's/\r$//' "${DEST_RUNNER}"
chmod +x "${DEST_RUNNER}"

install -m 0644 "${SRC_DIR}/sentinel-job@.service" "${UNIT_DIR}/sentinel-job@.service"
install -m 0644 "${SRC_DIR}/sentinel-job-failed@.service" "${UNIT_DIR}/sentinel-job-failed@.service"
install -m 0644 "${SRC_DIR}/sentinel-log-retention.service" "${UNIT_DIR}/sentinel-log-retention.service"
install -m 0644 "${SRC_DIR}/sentinel-sqlite-optimize.service" "${UNIT_DIR}/sentinel-sqlite-optimize.service"
for t in \
  sentinel-archive-snapshot.timer \
  sentinel-gfw-poll.timer \
  sentinel-vf-allocator.timer \
  sentinel-watchdog.timer \
  sentinel-budget-sync.timer \
  sentinel-acceptance.timer \
  sentinel-daily-brief.timer \
  sentinel-log-retention.timer \
  sentinel-sqlite-optimize.timer
do
  install -m 0644 "${SRC_DIR}/${t}" "${UNIT_DIR}/${t}"
done

systemctl daemon-reload
for t in \
  sentinel-archive-snapshot.timer \
  sentinel-gfw-poll.timer \
  sentinel-vf-allocator.timer \
  sentinel-watchdog.timer \
  sentinel-budget-sync.timer \
  sentinel-acceptance.timer \
  sentinel-daily-brief.timer \
  sentinel-log-retention.timer \
  sentinel-sqlite-optimize.timer
do
  systemctl enable --now "${t}"
done

echo "==> active sentinel timers"
systemctl list-timers --all | grep -i sentinel || true

RUNNER=/opt/oracle1001/deploy/sentinel/run_sentinel_job.sh
# Kick all jobs once so job_log + health.scheduler populate (idempotent skips OK)
if docker ps --format '{{.Names}}' | grep -qx sentinel-web; then
  echo "==> bootstrap scheduler jobs (idempotent)"
  for j in pipeline_watchdog budget_sync archive_snapshot gfw_poll vf_allocator acceptance_check daily_brief; do
    if [[ -x "${RUNNER}" ]]; then
      bash "${RUNNER}" "$j" || true
    else
      docker exec sentinel-web python -m services.scheduler --job "$j" || true
    fi
  done
  docker exec sentinel-web python -m services.scheduler --check-overdue || true
fi
echo "SENTINEL_AUTOMATION_ARMED"
