#!/usr/bin/env bash
set -euo pipefail
echo "==> london swap+ufw"
if [[ ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
fi
swapon /swapfile || true
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl -w vm.swappiness=30 >/dev/null || true
ufw allow 22/tcp comment 'sentinel-ssh' || true
ufw allow 80/tcp || true
ufw allow 443/tcp || true
ufw allow 8765/tcp comment 'sentinel-hud-relay' || true
ufw --force enable || true
mkdir -p /opt/oracle1001/sentinel /var/log/oracle1001
free -h
swapon --show
ufw status numbered | head -30
echo DONE
