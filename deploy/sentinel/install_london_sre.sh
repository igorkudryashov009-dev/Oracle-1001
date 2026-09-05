#!/usr/bin/env bash
# Install sync cron + healthcheck units on London LD8 (idempotent).
set -euo pipefail

BASE="/opt/oracle1001/deploy/sentinel"
mkdir -p "${BASE}" /opt/oracle1001/logs /var/lock
chmod +x "${BASE}/sync_ais_db_to_korolev.sh" "${BASE}/aisstream_healthcheck.sh" 2>/dev/null || true

cp -f "${BASE}/aisstream-connector.london.service" /etc/systemd/system/aisstream-connector.service
cp -f "${BASE}/aisstream-connector-healthcheck.service" /etc/systemd/system/
cp -f "${BASE}/aisstream-connector-healthcheck.timer" /etc/systemd/system/
cp -f "${BASE}/sentinel-alert@.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now aisstream-connector.service
systemctl enable --now aisstream-connector-healthcheck.timer

tmp=/tmp/oracle_cron.$$
crontab -l 2>/dev/null | grep -v 'sync_ais_db_to_korolev' | grep -v 'sqlite_retention' >"${tmp}" || true
echo '*/5 * * * * /opt/oracle1001/deploy/sentinel/sync_ais_db_to_korolev.sh >> /opt/oracle1001/logs/db_sync.log 2>&1' >>"${tmp}"
echo '15 3 * * * cd /opt/oracle1001/ais_ingest && /opt/oracle1001/ais_ingest/venv/bin/python -m services.sqlite_retention >> /opt/oracle1001/logs/retention.log 2>&1' >>"${tmp}"
crontab "${tmp}"
rm -f "${tmp}"

echo "=== crontab ==="
crontab -l
echo "=== timers ==="
systemctl list-timers 'aisstream*' --no-pager || true
echo "INSTALL_OK"
