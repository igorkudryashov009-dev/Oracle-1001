#!/usr/bin/env bash
set -euo pipefail
ROOT="/opt/oracle1001/analytical_engine"
mkdir -p "${ROOT}/logs" "${ROOT}/output/js"
CRON_LINE='*/15 * * * * cd /opt/oracle1001/analytical_engine && /opt/oracle1001/analytical_engine/venv/bin/python build_sentinel_dashboard.py >> /opt/oracle1001/analytical_engine/logs/dashboard_cron.log 2>&1'
tmp="$(mktemp)"
crontab -l 2>/dev/null | grep -v 'build_sentinel_dashboard.py' >"${tmp}" || true
echo "${CRON_LINE}" >>"${tmp}"
crontab "${tmp}"
rm -f "${tmp}"
echo "=== CRONTAB ==="
crontab -l
echo "=== VERIFY PATHS ==="
ls -la "${ROOT}/venv/bin/python"
ls -la "${ROOT}/build_sentinel_dashboard.py"
ls -lh "${ROOT}/output/sentinel_dashboard.html"
ls -ld "${ROOT}/logs"
