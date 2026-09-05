#!/usr/bin/env bash
# Idempotent install of weekly gas monitor on Rucloud (private runner).
# Safe to re-run: refreshes code/units without duplicating timers.
set -euo pipefail

ROOT="/opt/oracle1001/weekly_monitor"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_USER="${DEPLOY_USER:-root}"

echo "==> Installing into ${ROOT}"
mkdir -p "${ROOT}"/{bin,data,features/gas_carrier_weekly,logs,state,deploy}
mkdir -p /var/log/oracle1001

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip ca-certificates curl ufw

# Drop page cache pressure hint: stop unnecessary apt lists growth later is fine.

if [[ ! -d "${ROOT}/venv" ]]; then
  python3 -m venv "${ROOT}/venv"
fi
# shellcheck disable=SC1091
source "${ROOT}/venv/bin/activate"
pip install --upgrade pip wheel
pip install -r "${SRC_DIR}/requirements-monitor.txt"

# Application payload (copied next to this script by packager)
install -m 0644 "${SRC_DIR}/weekly_gas_carrier_monitor.py" "${ROOT}/weekly_gas_carrier_monitor.py"
install -m 0755 "${SRC_DIR}/run_gas_monitor.sh" "${ROOT}/bin/run_gas_monitor.sh"
install -m 0755 "${SRC_DIR}/setup_ufw.sh" "${ROOT}/bin/setup_ufw.sh"
install -m 0755 "${SRC_DIR}/export_gas_artifacts.sh" "${ROOT}/bin/export_gas_artifacts.sh"
install -m 0644 "${SRC_DIR}/requirements-monitor.txt" "${ROOT}/requirements-monitor.txt"
install -m 0644 "${SRC_DIR}/README_VPS_GAS_MONITOR.md" "${ROOT}/README_VPS_GAS_MONITOR.md"

if [[ -f "${SRC_DIR}/fleet_database.csv" ]]; then
  install -m 0644 "${SRC_DIR}/fleet_database.csv" "${ROOT}/data/fleet_database.csv"
fi

# .env: create from template only if missing (never overwrite secrets on re-run)
if [[ ! -f "${ROOT}/.env" ]]; then
  # Strip CR so Windows-edited templates do not break `source`
  tr -d '\r' < "${SRC_DIR}/env.template" > "${ROOT}/.env"
  chmod 600 "${ROOT}/.env"
  echo "Created ${ROOT}/.env from template (mode 600). Edit PROVIDER_API_KEY before production."
else
  # Heal accidental CRLF without touching values
  sed -i 's/\r$//' "${ROOT}/.env" || true
  chmod 600 "${ROOT}/.env"
  echo "Keeping existing ${ROOT}/.env (idempotent)."
fi

# systemd units (overwrite content is OK; enable is idempotent)
install -m 0644 "${SRC_DIR}/gas-monitor.service" /etc/systemd/system/gas-monitor.service
install -m 0644 "${SRC_DIR}/gas-monitor.timer" /etc/systemd/system/gas-monitor.timer
install -m 0644 "${SRC_DIR}/gas-monitor-export.service" /etc/systemd/system/gas-monitor-export.service
systemctl daemon-reload
systemctl enable gas-monitor.timer
systemctl start gas-monitor.timer

# Firewall
SSH_ALLOW_CIDRS="${SSH_ALLOW_CIDRS:-}" SSH_PORT="${SSH_PORT:-22}" \
  bash "${ROOT}/bin/setup_ufw.sh"

echo "==> Done"
systemctl status gas-monitor.timer --no-pager || true
systemctl list-timers 'gas-monitor.*' --no-pager || true
