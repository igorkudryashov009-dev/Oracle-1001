#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import json
s=json.load(open("/opt/oracle1001/weekly_monitor/features/gas_carrier_weekly/summary.json", encoding="utf-8"))
print(
    s["generated_at_utc"],
    "dry_run", s.get("dry_run"),
    "coverage", s.get("coverage_of_top_n_target_pct"),
    "ok", s.get("success_count"), "/", s.get("selected_for_poll"),
)
PY
systemctl is-active gas-monitor.timer
systemctl list-timers gas-monitor.timer --no-pager
stat -c '%a %n' /opt/oracle1001/weekly_monitor/.env
ufw status | head -n 15
bash -n /opt/oracle1001/weekly_monitor/bin/run_gas_monitor.sh
bash -n /opt/oracle1001/weekly_monitor/bin/setup_ufw.sh
echo "bash_n_ok"
tail -n 3 /opt/oracle1001/weekly_monitor/logs/alerts.log 2>/dev/null || echo "alerts: none"
