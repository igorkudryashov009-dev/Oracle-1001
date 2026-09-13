#!/usr/bin/env bash
# Install lean AIS edge relay on London LD8 (systemd + sync cron → Korolev).
# Non-interactive. Idempotent. Designed for 1 GB RAM (venv, not full Docker).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

APP="/opt/oracle1001/ais_ingest"
DEPLOY="/opt/oracle1001/deploy/sentinel"
LOG="/opt/oracle1001/logs"
KOROLEV="${KOROLEV_HOST:-45.8.230.214}"

echo "==> [london] packages"
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip sqlite3 rsync curl ca-certificates

mkdir -p "${APP}"/{services,logs,история1,output} "${DEPLOY}" "${LOG}" /var/lock /var/log/oracle1001

echo "==> [london] sync deploy helpers"
# Expect caller to have uploaded tree into /tmp/sentinel_london_stage
STAGE="${STAGE_DIR:-/tmp/sentinel_london_stage}"
if [[ ! -d "${STAGE}/services" ]]; then
  echo "ERROR: STAGE_DIR=${STAGE} missing services/ — upload first" >&2
  exit 1
fi

rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "${STAGE}/services/" "${APP}/services/"
install -m 0644 "${STAGE}/config.yaml" "${APP}/config.yaml"
install -m 0644 "${STAGE}/deploy/sentinel/requirements-ais-relay.txt" "${APP}/requirements-ais-relay.txt"
install -m 0755 "${STAGE}/deploy/sentinel/sync_ais_db_to_korolev.sh" "${DEPLOY}/sync_ais_db_to_korolev.sh"
install -m 0755 "${STAGE}/deploy/sentinel/aisstream_healthcheck.sh" "${DEPLOY}/aisstream_healthcheck.sh" 2>/dev/null || true
install -m 0644 "${STAGE}/deploy/sentinel/aisstream-connector.london.service" /etc/systemd/system/aisstream-connector.service
install -m 0644 "${STAGE}/deploy/sentinel/aisstream-connector-healthcheck.service" /etc/systemd/system/ 2>/dev/null || true
install -m 0644 "${STAGE}/deploy/sentinel/aisstream-connector-healthcheck.timer" /etc/systemd/system/ 2>/dev/null || true
install -m 0644 "${STAGE}/deploy/sentinel/sentinel-alert@.service" /etc/systemd/system/ 2>/dev/null || true

# Fleet CSV for MMSI rotation
mkdir -p "${APP}/output"
if [[ -f "${STAGE}/output/fleet_database.csv" ]]; then
  install -m 0644 "${STAGE}/output/fleet_database.csv" "${APP}/output/fleet_database.csv"
fi

# .env (AISSTREAM_API_KEY) — prefer staged, else keep existing
if [[ -f "${STAGE}/.env" ]]; then
  install -m 0600 "${STAGE}/.env" "${APP}/.env"
elif [[ ! -f "${APP}/.env" ]]; then
  touch "${APP}/.env"
  chmod 600 "${APP}/.env"
  echo "WARN: ${APP}/.env empty — set AISSTREAM_API_KEY" >&2
fi

# Empty package marker for services/
touch "${APP}/services/__init__.py"

echo "==> [london] venv (lean)"
if [[ ! -d "${APP}/venv" ]]; then
  python3 -m venv "${APP}/venv"
fi
# shellcheck disable=SC1091
source "${APP}/venv/bin/activate"
pip install --upgrade pip wheel
pip install --no-cache-dir -r "${APP}/requirements-ais-relay.txt"

echo "==> [london] ensure SSH to Korolev (${KOROLEV})"
mkdir -p /root/.ssh
chmod 700 /root/.ssh
if [[ ! -f /root/.ssh/id_ed25519 ]]; then
  ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 -q
fi
# Copy pub key to Korolev authorized_keys (BatchMode from this host may already work)
ssh -o BatchMode=yes -o ConnectTimeout=15 "root@${KOROLEV}" \
  "mkdir -p /opt/oracle1001/ais_data && echo London edge OK" \
  || echo "WARN: London→Korolev SSH not ready — copy id_ed25519.pub manually"

echo "==> [london] systemd + cron"
systemctl daemon-reload
systemctl enable --now aisstream-connector.service
systemctl restart aisstream-connector.service || true

tmp=/tmp/oracle_cron.$$
crontab -l 2>/dev/null | grep -v 'sync_ais_db_to_korolev' >"${tmp}" || true
echo "*/5 * * * * ${DEPLOY}/sync_ais_db_to_korolev.sh >> ${LOG}/db_sync.log 2>&1" >>"${tmp}"
crontab "${tmp}"
rm -f "${tmp}"

# Immediate first sync (best-effort)
bash "${DEPLOY}/sync_ais_db_to_korolev.sh" || true

echo "==> [london] status"
systemctl --no-pager --full status aisstream-connector.service | head -20 || true
free -h
echo "INSTALL_LONDON_AIS_OK"
