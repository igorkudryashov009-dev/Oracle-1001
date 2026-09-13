#!/bin/bash
set -euo pipefail
cd /opt/oracle1001/sentinel
docker exec sentinel-web python - <<'PY'
from services.dual_gate import DISK_FREE_MIN_PCT, probe_disk_usage
from services import log_retention, quant_risk_service
src = open("/app/services/archive_service.py", encoding="utf-8", errors="ignore").read()
assert DISK_FREE_MIN_PCT == 20.0
assert "PREMIUM SATELLITE" not in src
assert hasattr(log_retention, "run_retention")
assert hasattr(quant_risk_service, "compute_quant_risk_payload")
print("BAKE_OK", DISK_FREE_MIN_PCT, probe_disk_usage().get("disk_free_pct"))
PY

echo "==> SECOND recreate proof (destroy containers, up from image only)"
docker compose -f docker-compose.yml -f docker-compose.prod.yml down --remove-orphans
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate --remove-orphans
sleep 22

docker exec sentinel-web python - <<'PY'
from services.dual_gate import DISK_FREE_MIN_PCT
from services import log_retention
src = open("/app/services/archive_service.py", encoding="utf-8", errors="ignore").read()
assert DISK_FREE_MIN_PCT == 20.0
assert "PREMIUM SATELLITE" not in src
assert hasattr(log_retention, "run_retention")
print("RECREATE_OK", DISK_FREE_MIN_PCT)
PY

curl -sS http://127.0.0.1:8765/output/api/v1/health -o /tmp/h2.json
curl -sS "http://127.0.0.1:8765/api/v1/quant/risk?horizon=7" -o /tmp/q2.json
curl -sS http://127.0.0.1:8765/output/archive/api_status.json -o /tmp/a2.json
python3 - <<'PY'
import json
h=json.load(open("/tmp/h2.json"))
q=json.load(open("/tmp/q2.json"))
a=json.load(open("/tmp/a2.json"))
print("HEALTH", h.get("pipeline_health_status"), h.get("fleet_sample_status"), h.get("disk_free_pct"), h.get("active_node"), h.get("top500_live_coverage"))
print("QUANT", q.get("is_synthetic"), q.get("production_actionable"), q.get("recommended_strategy_id"), q.get("blocked_reason"), q.get("model_last_retrained"))
print("ARCH", a.get("api_plan"), a.get("is_synthetic"))
assert "PREMIUM" not in str(a.get("api_plan") or "")
assert q.get("recommended_strategy_id") is None
assert h.get("disk_free_pct") is not None
print("REGRESSION_OK")
PY
