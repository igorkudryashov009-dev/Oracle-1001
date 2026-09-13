#!/usr/bin/env bash
# Verify Q-Flex MP4 MIME + presence; restart sentinel-web via compose resolver.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
DEPLOY=/opt/oracle1001/deploy/sentinel
# shellcheck source=/dev/null
source "${DEPLOY}/resolve_compose_dir.sh" 2>/dev/null || source "$(dirname "$0")/resolve_compose_dir.sh"
APP=$(resolve_sentinel_app_dir) || { echo '[FAIL] compose dir'; exit 1; }
echo "[OK] compose dir=${APP}"
VID="${APP}/assets/1-10"
n=$(ls -1 "${VID}"/*.mp4 2>/dev/null | wc -l | tr -d ' ')
echo "[OK] mp4_count=${n} in ${VID}"
if [[ "${n}" -lt 1 ]]; then
  echo '[FAIL] no mp4 under assets/1-10 — upload flight clips first'
  exit 2
fi
sample=$(ls -1 "${VID}"/*.mp4 | head -n 1)
name=$(basename "${sample}")
hdr=$(curl -sI "http://127.0.0.1:8765/assets/1-10/${name}" | tr -d '\r')
echo "${hdr}" | head -n 8
echo "${hdr}" | grep -qi 'Content-Type: video/mp4' && echo '[OK] mime=video/mp4' || echo '[WARN] mime not video/mp4'
bash "${DEPLOY}/restart_sentinel_web.sh"
sleep 3
curl -fsS http://127.0.0.1:8765/output/api/v1/health > /tmp/h.json
python3 -c "import json;h=json.load(open('/tmp/h.json'));print('[OK]',h.get('pipeline_health_status'),h.get('source_mode'),(h.get('replica') or {}).get('status'))"
echo '[OK] video_mime_fix_done'
