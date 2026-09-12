"""Verify Dual Truth publish: health.json ↔ HTML payload sync."""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
h = json.loads((ROOT / "output/api/v1/health.json").read_text(encoding="utf-8"))
qp = h.get("quant_pipeline") or {}
html = (ROOT / "output/sentinel_dashboard.html").read_text(encoding="utf-8")

m = re.search(r'"ensemble_accuracy_pct"\s*:\s*([0-9.]+)', html)
m2 = re.search(r'"accuracy_basis"\s*:\s*"([^"]+)"', html)
m3 = re.search(r'"ensemble_confidence_proxy_pct"\s*:\s*([0-9.]+)', html)

html_ens = float(m.group(1)) if m else None
html_basis = m2.group(1) if m2 else None
html_proxy = float(m3.group(1)) if m3 else None

ui_match = (
    html_ens is not None
    and qp.get("ensemble_accuracy_pct") is not None
    and abs(html_ens - float(qp["ensemble_accuracy_pct"])) < 0.05
    and html_basis == qp.get("accuracy_basis")
)

report = {
    "coverage_gate": {
        "passed": False,
        "live_vessel_count": h.get("live_vessel_count"),
        "top500_live_coverage": h.get("top500_live_coverage"),
        "threshold": 100,
        "note": "Prompt 1 v2 incomplete — full TOP-500 recalibration SKIPPED",
    },
    "dual_truth_audit": {
        "formula_a": (
            "purged_cv_directional = sum(dir_acc_i * w_i) / sum(w_i) "
            "over CatBoost(h14 purged WF) + Markov/Spectral/Elliott holdout; "
            "w_i = adaptive_ensemble_weights()"
        ),
        "formula_b_legacy": (
            "unlabeled ensemble_accuracy_pct ≈ mean(ICE.confidence, "
            "abs(fleet_p_eu_avg-0.5)*2*0.7, hardcoded_72/100*0.9) * 100 "
            "— NOT directional holdout"
        ),
        "production_kpi": "purged_cv_directional",
        "diagnostic_field": "ensemble_confidence_proxy_pct",
    },
    "published": {
        "ensemble_accuracy_pct": qp.get("ensemble_accuracy_pct"),
        "accuracy_basis": qp.get("accuracy_basis"),
        "ensemble_confidence_proxy_pct": qp.get("ensemble_confidence_proxy_pct"),
        "component_accuracy_pct": qp.get("component_accuracy_pct"),
        "ensemble_weights": qp.get("ensemble_weights"),
        "weight_governance": qp.get("weight_governance"),
    },
    "before_after": {
        "health_before_pct": 42.9,
        "health_before_basis": "unlabeled_confidence_proxy",
        "health_after_pct": qp.get("ensemble_accuracy_pct"),
        "health_after_basis": qp.get("accuracy_basis"),
        "pre_governance_fixed_weights_pct": 70.4,
        "post_governance_adaptive_weights_pct": qp.get("ensemble_accuracy_pct"),
        "threshold_80_met": bool(
            qp.get("ensemble_accuracy_pct") is not None
            and float(qp["ensemble_accuracy_pct"]) > 80.0
        ),
    },
    "ui_payload": {
        "ensemble_accuracy_pct": html_ens,
        "accuracy_basis": html_basis,
        "ensemble_confidence_proxy_pct": html_proxy,
    },
    "ui_backend_sync": ui_match,
    "recalibration_on_full_top500": "SKIPPED_COVERAGE_LT_100",
    "freshness_note": {
        "source_mode": h.get("source_mode"),
        "ais_truth": (h.get("replica") or {}).get("ais_truth"),
        "age_sec": (h.get("replica") or {}).get("age_sec"),
    },
}

out = ROOT / "output" / "dual_truth_unification_report.json"
out.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
print("WROTE", out)

try:
    raw = urllib.request.urlopen("http://127.0.0.1:8765/output/api/v1/health.json", timeout=5).read()
    hh = json.loads(raw)
    q2 = hh.get("quant_pipeline") or {}
    print(
        "HTTP_OK",
        "ens=", q2.get("ensemble_accuracy_pct"),
        "basis=", q2.get("accuracy_basis"),
        "proxy=", q2.get("ensemble_confidence_proxy_pct"),
    )
except Exception as exc:  # noqa: BLE001
    print("HTTP_FAIL", exc)
