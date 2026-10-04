#!/bin/bash
# Host OnFailure hook. Never prints secret material.
# Usage: emit_alert.sh <kind> <provider> <message...>
set -euo pipefail
kind="${1:-job_failed}"
provider="${2:-systemd}"
shift 2 || true
msg="${*:-(no message)}"
# Drop anything that looks like a credential before it reaches a log line.
if printf '%s' "$msg" | grep -Eq 'sk_sent_|sk-ant-|api_key=|token=|userkey='; then
  msg="redacted"
fi
export SENTINEL_ALERT_KIND="$kind"
export SENTINEL_ALERT_PROVIDER="$provider"
export SENTINEL_ALERT_MSG="$msg"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx sentinel-web; then
  docker exec \
    -e SENTINEL_ALERT_KIND \
    -e SENTINEL_ALERT_PROVIDER \
    -e SENTINEL_ALERT_MSG \
    sentinel-web \
    python -c 'import os; from services.alerts import emit_alert; emit_alert(os.environ["SENTINEL_ALERT_KIND"], os.environ["SENTINEL_ALERT_MSG"], detail={"provider": os.environ["SENTINEL_ALERT_PROVIDER"]})'
else
  mkdir -p /opt/oracle1001/logs
  printf '%s kind=%s provider=%s msg=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$kind" "$provider" "$msg" \
    >> /opt/oracle1001/logs/alerts_fallback.log
fi
