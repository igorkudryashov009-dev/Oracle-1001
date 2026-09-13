#!/usr/bin/env bash
# Idempotent VPS provisioning for Sentinel (1 GB Ubuntu nodes).
# - 2 GB swapfile
# - UFW: 22, 80, 443, 8765
# - Docker Engine + Compose plugin
# Usage: bash provision_vps.sh [--role korolev|london]
set -euo pipefail

ROLE="${1:-korolev}"
if [[ "${ROLE}" == "--role" ]]; then
  ROLE="${2:-korolev}"
fi

export DEBIAN_FRONTEND=noninteractive

echo "==> [${ROLE}] apt update + base packages"
apt-get update -y
apt-get install -y --no-install-recommends \
  git curl ca-certificates ufw htop fail2ban \
  apt-transport-https gnupg lsb-release jq rsync

echo "==> [${ROLE}] ensure 2G /swapfile"
if [[ ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=progress
  chmod 600 /swapfile
  mkswap /swapfile
fi
if ! swapon --show | grep -q '/swapfile'; then
  swapon /swapfile || true
fi
if ! grep -q '^/swapfile ' /etc/fstab 2>/dev/null; then
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
# Prefer swappiness suitable for tiny RAM + Docker
sysctl -w vm.swappiness=30 >/dev/null || true
if ! grep -q '^vm.swappiness=' /etc/sysctl.conf 2>/dev/null; then
  echo 'vm.swappiness=30' >> /etc/sysctl.conf
fi
free -h
swapon --show || true

echo "==> [${ROLE}] UFW harden (22/80/443/8765)"
ufw allow 22/tcp comment 'sentinel-ssh' || true
ufw allow 80/tcp comment 'sentinel-http' || true
ufw allow 443/tcp comment 'sentinel-https' || true
ufw allow 8765/tcp comment 'sentinel-hud' || true
ufw default deny incoming || true
ufw default allow outgoing || true
ufw --force enable || true
ufw status numbered || true

echo "==> [${ROLE}] Docker Engine"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker
docker --version
docker compose version || true

echo "==> [${ROLE}] fail2ban"
systemctl enable --now fail2ban || true

mkdir -p /opt/oracle1001/sentinel /var/log/oracle1001
echo "==> [${ROLE}] provision complete"
