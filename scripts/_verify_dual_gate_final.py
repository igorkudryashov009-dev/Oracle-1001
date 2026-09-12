"""One-shot dual-gate final verification (Prompt dual-gate step 6–7)."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from services.satellite_ais_adapter import SatelliteAISAdapter
    from services.ais_source_adapter import AISSourceAdapter, AISStreamTerrestrialAdapter

    async def stub_check() -> None:
        a = SatelliteAISAdapter()
        try:
            await a.connect()
            raise AssertionError("SatelliteAISAdapter.connect must raise")
        except NotImplementedError as e:
            assert "Do NOT fabricate" in str(e) or "paid provider" in str(e)
            print("satellite_connect_stub=OK")
        assert a.coverage_report()["status"] == "not_activated"
        print("satellite_coverage_report=not_activated")

    asyncio.run(stub_check())
    print("AISSourceAdapter=", AISSourceAdapter.__name__)
    print("terrestrial_facade=", AISStreamTerrestrialAdapter.name)

    h = json.loads((ROOT / "output/api/v1/health.json").read_text(encoding="utf-8"))
    print("pipeline_health_status=", h.get("pipeline_health_status"))
    print("fleet_sample_status=", h.get("fleet_sample_status"))
    print("top500_live_coverage=", h.get("top500_live_coverage"))
    print("sample_size_caveat=", (h.get("sample_size_caveat") or "")[:120])
    qp = h.get("quant_pipeline") or {}
    print("model_cv_accuracy_pct=", qp.get("model_cv_accuracy_pct"))
    print("live_inference_confidence=", qp.get("live_inference_confidence"))

    html = (ROOT / "output/sentinel_dashboard.html").read_text(encoding="utf-8", errors="ignore")
    for needle in (
        "fleetSampleBanner",
        "fleet-sample-banner",
        "top10-grid",
        "photogrammetric",
        "bound_kn",
        "bal-slider-blockage",
        "sample_size_caveat",
        "live_inference_confidence",
        "insufficient_sample",
    ):
        print(f"html:{needle}={needle in html}")

    ap = ROOT / "output/alerts_hmm_state.json"
    if ap.exists():
        alerts = json.loads(ap.read_text(encoding="utf-8"))
        print(
            "alerts_gate=",
            {
                "production_actionable": alerts.get("production_actionable"),
                "signal_status": alerts.get("signal_status"),
                "fleet_sample_status": alerts.get("fleet_sample_status"),
            },
        )

    # Parse embedded payload for balance metric flags + banner fields
    m = re.search(r"window\.__SENTINEL_PAYLOAD__\s*=\s*(\{.*?\});\s*\n", html, re.S)
    if not m:
        print("embedded_payload=MISSING")
        return 1
    payload = json.loads(m.group(1))
    print("payload.pipeline_health_status=", payload.get("pipeline_health_status"))
    print("payload.fleet_sample_status=", payload.get("fleet_sample_status"))
    print("payload.sample_size_caveat=", (payload.get("sample_size_caveat") or "")[:100])
    bal = payload.get("balance") or {}
    pil = bal.get("pil") or payload.get("pil") or {}
    print("pil.status=", pil.get("status"), "display=", pil.get("status_display"))
    print("pil.sample_size_caveat=", (pil.get("sample_size_caveat") or "")[:100])
    for metric in ("lssi", "dar", "dfs", "LSSI", "DAR", "DFS"):
        node = bal.get(metric) or (bal.get("metrics") or {}).get(metric)
        if isinstance(node, dict):
            print(
                f"metric_{metric}=",
                {
                    "signal_status": node.get("signal_status"),
                    "value": node.get("value") or node.get("score"),
                },
            )
    alerts = payload.get("alerts") or {}
    if isinstance(alerts, dict):
        print(
            "payload.alerts_gate=",
            {
                "production_actionable": alerts.get("production_actionable"),
                "signal_status": alerts.get("signal_status"),
                "fleet_sample_status": alerts.get("fleet_sample_status"),
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
