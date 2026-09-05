#!/bin/bash
set -euo pipefail
tmp=/tmp/oracle_cron.$$
crontab -l 2>/dev/null | grep -v sync_ais_db_to_korolev | grep -v sqlite_retention >"$tmp" || true
echo '*/5 * * * * /opt/oracle1001/deploy/sentinel/sync_ais_db_to_korolev.sh >> /opt/oracle1001/logs/db_sync.log 2>&1' >>"$tmp"
echo '15 3 * * * cd /opt/oracle1001/ais_ingest && /opt/oracle1001/ais_ingest/venv/bin/python -m services.sqlite_retention >> /opt/oracle1001/logs/retention.log 2>&1' >>"$tmp"
crontab "$tmp"
rm -f "$tmp"
echo CRON_OK
crontab -l
/opt/oracle1001/deploy/sentinel/aisstream_healthcheck.sh
echo HEALTH_EXIT:$?
systemctl is-active aisstream-connector.service
systemctl is-active aisstream-connector-healthcheck.timer
