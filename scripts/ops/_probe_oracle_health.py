#!/usr/bin/env python3
"""Quick topology + health oracle_state probe (local + Korolev)."""
from __future__ import annotations

import json
import urllib.request

TARGETS = {
    "local": "http://127.0.0.1:8765/output/api/v1/health",
    "korolev": "http://45.8.230.214:8765/output/api/v1/health",
}

for name, url in TARGETS.items():
    try:
        with urllib.request.urlopen(url, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8"))
        os_ = d.get("oracle_state") if isinstance(d.get("oracle_state"), dict) else {}
        print(
            f"{name}: pipeline={d.get('pipeline_health_status')} "
            f"fleet={d.get('fleet_sample_status')} cov={d.get('top500_live_coverage')} "
            f"oracle_state={bool(os_)} contract_mode={os_.get('contract_mode')}"
        )
    except Exception as exc:
        print(f"{name}: FAIL {exc}")
