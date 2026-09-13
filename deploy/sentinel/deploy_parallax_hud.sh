#!/usr/bin/env bash
# Deploy parallax HUD assets + cache-bust + restart web (dynamic compose dir).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
DEPLOY=/opt/oracle1001/deploy/sentinel
# shellcheck source=/dev/null
source "${DEPLOY}/resolve_compose_dir.sh" 2>/dev/null || source "$(dirname "$0")/resolve_compose_dir.sh"
APP=$(resolve_sentinel_app_dir) || { echo '[FAIL] no compose'; exit 1; }
echo "[OK] compose dir=${APP}"

docker cp /tmp/top10_sheet.js sentinel-web:/app/output/js/top10_sheet.js
docker cp /tmp/sentinel_hud.css sentinel-web:/app/output/css/sentinel_hud.css
# legacy path used by some HTML builds
docker cp /tmp/sentinel_hud.css sentinel-web:/app/output/js/sentinel_hud.css 2>/dev/null || true
docker cp /tmp/top10_sheet.js sentinel-core:/app/output/js/top10_sheet.js 2>/dev/null || true
docker cp /tmp/sentinel_hud.css sentinel-core:/app/output/css/sentinel_hud.css 2>/dev/null || true

# Cache-bust module URL in live HTML
python3 - <<'PY'
from pathlib import Path
import subprocess
html_paths = []
# prefer volume file via docker
subprocess.run(
    ["docker", "cp", "sentinel-web:/app/output/sentinel_dashboard.html", "/tmp/sentinel_dashboard.html"],
    check=False,
)
p = Path("/tmp/sentinel_dashboard.html")
if not p.exists():
    raise SystemExit("[FAIL] dashboard html missing")
t = p.read_text(encoding="utf-8", errors="replace")
t2 = t
# bump top10_sheet cache buster
import re
t2 = re.sub(
    r'src="js/top10_sheet\.js[^"]*"',
    'src="js/top10_sheet.js?v=autoplay-mime-v5"',
    t2,
)
if "autoplay-mime-v5" not in t2 and "top10_sheet.js" in t2:
    t2 = t2.replace("js/top10_sheet.js", "js/top10_sheet.js?v=autoplay-mime-v5")
# ensure css link exists (css/ preferred)
if "css/sentinel_hud.css" not in t2 and "js/sentinel_hud.css" in t2:
    t2 = t2.replace("js/sentinel_hud.css", "css/sentinel_hud.css?v=parallax-hover-v2")
elif "css/sentinel_hud.css" in t2:
    t2 = re.sub(
        r'href="css/sentinel_hud\.css[^"]*"',
        'href="css/sentinel_hud.css?v=autoplay-mime-v5"',
        t2,
    )
p.write_text(t2, encoding="utf-8")
print("[OK] cache_bust patched")
PY
docker cp /tmp/sentinel_dashboard.html sentinel-web:/app/output/sentinel_dashboard.html
docker cp /tmp/sentinel_dashboard.html sentinel-core:/app/output/sentinel_dashboard.html 2>/dev/null || true

cd "${APP}"
if [[ -f docker-compose.prod.yml ]]; then
  docker compose -f docker-compose.yml -f docker-compose.prod.yml restart sentinel-web
else
  docker compose restart sentinel-web
fi
sleep 4
curl -fsS -o /dev/null -w 'js=%{http_code}\n' 'http://127.0.0.1:8765/output/js/top10_sheet.js?v=autoplay-mime-v5'
curl -fsS http://127.0.0.1:8765/output/js/top10_sheet.js | grep -c 'bindAutoplayUnlock' || true
curl -fsS http://127.0.0.1:8765/output/js/top10_sheet.js | grep -c 'defaultMuted' || true
curl -fsS http://127.0.0.1:8765/output/sentinel_dashboard.html | grep -o 'top10_sheet.js[^"]*' | head -n 2
curl -fsS http://127.0.0.1:8765/output/api/v1/health > /tmp/h.json
python3 -c "import json;h=json.load(open('/tmp/h.json'));print('[OK]',h.get('pipeline_health_status'),h.get('source_mode'),(h.get('replica') or {}).get('status'))"
echo '[OK] hud_cachebust_restart_done'
