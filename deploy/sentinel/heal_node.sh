#!/usr/bin/env bash
# Idempotent self-heal for Sentinel Node A (Korolev) / Node B (London).
# - Non-interactive APT
# - DPKG lock heal
# - Docker daemon up
# - Compose stack (Korolev) or standby check (London)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE="${NEEDRESTART_MODE:-a}"

ROLE="${1:-korolev}"
if [[ "${ROLE}" == "--role" ]]; then ROLE="${2:-korolev}"; fi

log() { echo "[heal $(date -u +%H:%M:%SZ)] $*"; }

heal_dpkg() {
  log "dpkg/apt heal"
  # Clear stale locks if no apt process
  if ! pgrep -x apt-get >/dev/null 2>&1 && ! pgrep -x dpkg >/dev/null 2>&1; then
    rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock 2>/dev/null || true
  fi
  dpkg --configure -a || true
  apt-get -y -f install || true
}

ensure_swap() {
  if [[ ! -f /swapfile ]]; then
    log "creating 2G /swapfile"
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile 2>/dev/null || true
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
}

ensure_ufw() {
  ufw allow 22/tcp comment 'sentinel-ssh' || true
  ufw allow 80/tcp || true
  ufw allow 443/tcp || true
  ufw allow 8765/tcp comment 'sentinel-hud' || true
  ufw --force enable || true
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    log "install docker"
    curl -fsSL https://get.docker.com | sh
  fi
  systemctl enable docker >/dev/null 2>&1 || true
  if ! systemctl is-active --quiet docker; then
    log "starting docker"
    systemctl start docker
  fi
  # wait socket
  for i in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then
      log "docker OK"
      return 0
    fi
    sleep 2
  done
  log "ERROR: docker not ready"
  systemctl status docker --no-pager | head -40 || true
  return 1
}

heal_korolev() {
  # shellcheck source=/dev/null
  source /opt/oracle1001/deploy/sentinel/resolve_compose_dir.sh 2>/dev/null \
    || source "$(dirname "$0")/resolve_compose_dir.sh" 2>/dev/null \
    || true
  local APP
  if declare -F resolve_sentinel_app_dir >/dev/null 2>&1; then
    APP=$(resolve_sentinel_app_dir) || APP=""
  else
    APP=/opt/oracle1001/sentinel
  fi
  if [[ -z "${APP}" || ! -f "${APP}/docker-compose.yml" ]]; then
    log "[FAIL] docker-compose.yml not found under /opt/oracle1001"
    return 1
  fi
  log "[OK] compose dir=${APP}"
  ensure_swap
  ensure_ufw
  ensure_docker
  mkdir -p /opt/oracle1001/ais_data
  cd "${APP}"
  # Prefer AIS on for SoT unless explicitly off
  if [[ ! -f .env ]]; then touch .env; fi
  if ! grep -q '^SENTINEL_AIS_MODE=' .env; then
    echo 'SENTINEL_AIS_MODE=on' >> .env
  fi
  # Stop RAM hog if present
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx eoil-monitor; then
    log "stopping eoil-monitor (1GB budget)"
    docker update --restart=no eoil-monitor || true
    docker stop eoil-monitor || true
  fi
  log "compose up"
  if [[ -f docker-compose.prod.yml ]]; then
    docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --remove-orphans
  else
    docker compose -f docker-compose.yml up -d --remove-orphans
  fi
  # Ensure AIS-mode-aware supervisor if present on host
  if [[ -f scripts/run_sentinel_core.py ]]; then
    docker cp scripts/run_sentinel_core.py sentinel-core:/app/scripts/run_sentinel_core.py 2>/dev/null || true
  fi
  # Soft integrity probe (named volume via container) — trigger hard recover if malformed
  local chk=""
  chk=$(docker exec sentinel-core sqlite3 /app/история1/sentinel_ais.db 'PRAGMA integrity_check;' 2>/dev/null | head -1 || true)
  if [[ -n "${chk}" && "${chk}" != "ok" ]]; then
    log "DB integrity=${chk} — running hard_recover_ais_db.sh"
    if [[ -x /opt/oracle1001/deploy/sentinel/hard_recover_ais_db.sh ]]; then
      bash /opt/oracle1001/deploy/sentinel/hard_recover_ais_db.sh || true
    fi
  fi
  # Health wait
  for i in $(seq 1 36); do
    if curl -fsS --max-time 5 http://127.0.0.1:8765/output/api/v1/health >/tmp/heal_health.json 2>/dev/null; then
      python3 - <<'PY' || true
import json
h=json.load(open("/tmp/heal_health.json"))
print("[OK] pipeline=", h.get("pipeline_health_status"), "mode=", h.get("source_mode"), "replica=", (h.get("replica") or {}).get("status"))
PY
      log "[OK] HEAL_KOROLEV_OK"
      return 0
    fi
    sleep 5
  done
  log "[WARN] health not ready yet"
  docker compose ps || true
  return 1
}

heal_london() {
  ensure_swap
  ensure_ufw
  # Do NOT auto-start AIS connector (standby — Korolev is SoT)
  systemctl stop aisstream-connector.service 2>/dev/null || true
  systemctl disable aisstream-connector.service 2>/dev/null || true
  # Hop check
  if ssh -o BatchMode=yes -o ConnectTimeout=12 root@45.8.230.214 'echo hop_ok' 2>/dev/null; then
    log "London→Korolev SSH OK"
  else
    log "WARN: London→Korolev SSH failed"
  fi
  log "HEAL_LONDON_STANDBY_OK"
}

heal_dpkg || true
case "${ROLE}" in
  korolev|a|nodea) heal_korolev ;;
  london|b|nodeb) heal_london ;;
  *) log "usage: $0 --role korolev|london"; exit 2 ;;
esac
