"""Verify spoofing / alerts / what-if / hud_state / healthcheck artifacts."""
import json
import os
import urllib.request
import sys

PORT = int(os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or "8765")
BASE = f"http://127.0.0.1:{PORT}"

def fetch(path):
    r = urllib.request.urlopen(BASE + path, timeout=8)
    return r.status, r.read().decode("utf-8", errors="replace")

st, html = fetch("/output/sentinel_dashboard.html?sheet=balance")
_, baljs = fetch("/output/js/balance_engine.js")
_, hudjs = fetch("/output/js/hud_state.js")
_, engjs = fetch("/output/js/sentinel_engine.js")
_, health_raw = fetch("/output/api/v1/health.json")
health = json.loads(health_raw)

# Extract embedded payload fragment checks
checks = [
    ("HTTP 200 balance", st == 200),
    ("What-If panel HTML", "bal-slider-blockage" in html and "bal-slider-temp" in html),
    ("What-If labels", "Strait Blockage Delay" in html and "Temperature Anomaly" in html),
    ("hud_state.js linked", "hud_state.js" in html),
    ("balance_engine what-if", "wireWhatIfSliders" in baljs and "applyScenario" in baljs),
    ("HUD localStorage key", "oracle1001.sentinel.hud.v1" in hudjs),
    ("switchSheet balance", 'name === "balance"' in engjs),
    ("ais_spoofing in HTML", "ais_spoofing" in html or "SPOOFED TRACK" in html),
    ("alerts payload embedded", '"alerts"' in html or "__ALERTS_PAYLOAD__" in html),
    ("health ais_spoofing", "ais_spoofing" in health),
    ("health reactive_alerts", "reactive_alerts" in health),
    ("health spoofed_excluded in balance", "spoofed_excluded" in (health.get("top500_balance_status") or {})),
]

# Unit-ish: spoof detector on synthetic jump
from services.sentinel_analytics import detect_ais_spoofing
vessels = [{"imo": "1", "mmsi": "111", "vessel_name": "TEST", "lat": 1.0, "lon": 103.0, "sog": 12}]
rows = [
    {"imo": "1", "mmsi": "111", "vessel_name": "TEST", "lat": 1.0, "lon": 103.0, "timestamp_utc": "2026-09-07T00:00:00Z"},
    {"imo": "1", "mmsi": "111", "vessel_name": "TEST", "lat": 5.0, "lon": 108.0, "timestamp_utc": "2026-09-07T00:30:00Z"},  # huge jump
]
rep = detect_ais_spoofing(rows, vessels)
checks.append(("spoof detector flags jump", vessels[0].get("is_spoofed") is True and rep["spoofed_count"] >= 1))
checks.append(("spoof tag text", vessels[0].get("spoof_tag") == "SPOOFED TRACK DETECTED"))

# Healthcheck script exists
from pathlib import Path
checks.append(("ais_ingest_healthcheck.sh exists", Path("scripts/ais_ingest_healthcheck.sh").exists()))
checks.append(("docker-compose uses healthcheck script", "ais_ingest_healthcheck.sh" in Path("docker-compose.yml").read_text(encoding="utf-8")))

passed = failed = 0
print(f"== Reactive Systems Gate :{PORT} ==")
for label, ok in checks:
    tag = "PASS" if ok else "FAIL"
    if ok: passed += 1
    else: failed += 1
    print(f"  {tag}  {label}")

print()
print(f"spoofed_count health={health.get('ais_spoofing')}")
print(f"alerts health={health.get('reactive_alerts')}")
print(f"RESULT: {passed} PASS / {failed} FAIL")
print(f"BALANCE {BASE}/output/sentinel_dashboard.html?sheet=balance")
sys.exit(0 if failed == 0 else 1)
