#!/bin/bash
set -euo pipefail
mkdir -p /opt/oracle1001/sentinel/services /opt/oracle1001/sentinel/scripts /tmp/sentinel_disk_gate
cp -f /tmp/sentinel_disk_gate/dual_gate.py /opt/oracle1001/sentinel/services/
cp -f /tmp/sentinel_disk_gate/ais_health.py /opt/oracle1001/sentinel/services/
cp -f /tmp/sentinel_disk_gate/log_retention.py /opt/oracle1001/sentinel/services/
cp -f /tmp/sentinel_disk_gate/run_sentinel_core.py /opt/oracle1001/sentinel/scripts/
cp -f /tmp/sentinel_disk_gate/append_health_snapshot.py /opt/oracle1001/sentinel/scripts/

for c in sentinel-web sentinel-core; do
  docker cp /opt/oracle1001/sentinel/services/dual_gate.py "${c}:/app/services/dual_gate.py"
  docker cp /opt/oracle1001/sentinel/services/ais_health.py "${c}:/app/services/ais_health.py"
  docker cp /opt/oracle1001/sentinel/services/log_retention.py "${c}:/app/services/log_retention.py"
done
docker cp /opt/oracle1001/sentinel/scripts/run_sentinel_core.py sentinel-core:/app/scripts/run_sentinel_core.py
docker cp /opt/oracle1001/sentinel/scripts/append_health_snapshot.py sentinel-core:/app/scripts/append_health_snapshot.py

docker restart sentinel-web sentinel-core
sleep 12
curl -sS http://127.0.0.1:8765/output/api/v1/health -o /tmp/health_disk_check.json
python3 -c 'import json; d=json.load(open("/tmp/health_disk_check.json")); print("pipeline", d.get("pipeline_health_status")); print("disk_free_pct", d.get("disk_free_pct")); print("disk_used_pct", d.get("disk_used_pct")); print("disk", d.get("disk")); print("reasons", (d.get("pipeline_health") or {}).get("reasons"))'
df -h /
