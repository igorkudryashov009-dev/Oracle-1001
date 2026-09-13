#!/bin/bash
set -euo pipefail
SRC=/tmp/sentinel_final_sync
mkdir -p /opt/oracle1001/sentinel/services/ttf_forecast /opt/oracle1001/sentinel/output/archive /opt/oracle1001/sentinel/output/models

cp -f "$SRC/archive_service.py" /opt/oracle1001/sentinel/services/
cp -f "$SRC/quant_risk_service.py" /opt/oracle1001/sentinel/services/
cp -f "$SRC/catboost_model.py" /opt/oracle1001/sentinel/services/ttf_forecast/
cp -f "$SRC/api_status.json" /opt/oracle1001/sentinel/output/archive/api_status.json
cp -f "$SRC/ttf_catboost_meta.json" /opt/oracle1001/sentinel/output/models/ttf_catboost_meta.json

for c in sentinel-web sentinel-core; do
  docker cp /opt/oracle1001/sentinel/services/archive_service.py "${c}:/app/services/archive_service.py"
  docker cp /opt/oracle1001/sentinel/services/quant_risk_service.py "${c}:/app/services/quant_risk_service.py"
  docker cp /opt/oracle1001/sentinel/services/ttf_forecast/catboost_model.py "${c}:/app/services/ttf_forecast/catboost_model.py"
  docker cp /opt/oracle1001/sentinel/output/archive/api_status.json "${c}:/app/output/archive/api_status.json"
  docker cp /opt/oracle1001/sentinel/output/models/ttf_catboost_meta.json "${c}:/app/output/models/ttf_catboost_meta.json"
done

docker restart sentinel-web sentinel-core
sleep 14

docker exec sentinel-core python -c "from services.archive_service import write_api_status, load_rotation_state; st=load_rotation_state(); st['ingest_mode']='hybrid_local'; st['auth_mode']='hybrid_local'; p=write_api_status(st); print('api_plan', p.get('api_plan')); print('is_synthetic', p.get('is_synthetic'))"

curl -sS http://127.0.0.1:8765/output/archive/api_status.json -o /tmp/arch.json
curl -sS "http://127.0.0.1:8765/api/v1/quant/risk?horizon=7" -o /tmp/q.json
curl -sS http://127.0.0.1:8765/output/api/v1/health -o /tmp/h.json
python3 -c "import json;a=json.load(open('/tmp/arch.json'));q=json.load(open('/tmp/q.json'));h=json.load(open('/tmp/h.json'));print('ARCHIVE',a.get('api_plan'),a.get('is_synthetic'),a.get('registry_source'));print('QUANT',q.get('model_cv_accuracy_pct'),q.get('model_last_retrained'),q.get('is_synthetic'),q.get('production_actionable'));print('HEALTH',h.get('pipeline_health_status'),h.get('fleet_sample_status'),h.get('disk_free_pct'),h.get('top500_live_coverage'));assert 'PREMIUM' not in str(a.get('api_plan'));assert a.get('is_synthetic') is True;assert q.get('model_last_retrained');assert h.get('disk_free_pct') is not None;print('FINAL_SYNC_OK')"
