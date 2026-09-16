"""
Tests for api_server.py: get_quant_risk_payload and Dual Gate Consumer Contract.
Verifies:
  - is_synthetic is strictly mandatory and True for incomplete production evidence
  - synthetic_components lists 'returns_sharpe_cvar'
  - 100% deterministic outputs across sequential invocations (no unseeded np.random)
  - Real quantiles match ttf_ensemble_forecast.json
  - Real regimes match ttf_markov_regimes.json
  - production_actionable is strictly False when is_synthetic is True or fleet is not FULL
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest
from fastapi.testclient import TestClient

from api_server import app

OUTPUT_DIR = ROOT_DIR / "output"
FORECAST_JSON = OUTPUT_DIR / "ttf_ensemble_forecast.json"
MARKOV_JSON = OUTPUT_DIR / "ttf_markov_regimes.json"


@pytest.fixture
def client():
    return TestClient(app)


def test_quant_risk_is_synthetic_mandatory(client):
    response = client.get("/api/v1/quant/risk?horizon=7")
    assert response.status_code == 200
    data = response.json()

    # Step 1: Mandatory is_synthetic flag
    assert "is_synthetic" in data, "is_synthetic field must be present in response"
    assert isinstance(data["is_synthetic"], bool)
    assert data["is_synthetic"] is True, "Must be True while ledger returns are statistically unqualified"

    # Synthetic components list
    assert "synthetic_components" in data
    assert isinstance(data["synthetic_components"], list)
    assert "returns_sharpe_cvar" in data["synthetic_components"]


def test_quant_risk_deterministic_across_calls(client):
    """Step 5: Sequential calls must return identical bytes/values without random noise."""
    res1 = client.get("/api/v1/quant/risk?horizon=7").json()
    res2 = client.get("/api/v1/quant/risk?horizon=7").json()
    res3 = client.get("/api/v1/quant/risk?horizon=7").json()

    assert res1["sharpe_ratio"] == res2["sharpe_ratio"] == res3["sharpe_ratio"]
    assert res1["cvar_95_pct"] == res2["cvar_95_pct"] == res3["cvar_95_pct"]
    assert res1["p50"] == res2["p50"] == res3["p50"]
    assert res1["active_regime"] == res2["active_regime"] == res3["active_regime"]
    assert res1["regime_probabilities"] == res2["regime_probabilities"] == res3["regime_probabilities"]


def test_quant_risk_real_quantiles_integration(client):
    """Step 2: p10/p50/p90 must match real ttf_ensemble_forecast.json."""
    if not FORECAST_JSON.exists():
        pytest.skip("ttf_ensemble_forecast.json not found on disk")

    fc_data = json.loads(FORECAST_JSON.read_text(encoding="utf-8"))
    res7 = client.get("/api/v1/quant/risk?horizon=7").json()
    hz7 = fc_data.get("horizons", {}).get("7", {})

    expected_p10 = round(float(hz7.get("p10")), 2)
    expected_p50 = round(float(hz7.get("p50")), 2)
    expected_p90 = round(float(hz7.get("p90")), 2)

    assert res7["p10"] == expected_p10
    assert res7["p50"] == expected_p50
    assert res7["p90"] == expected_p90
    assert res7["quantiles_source"] == fc_data.get("model_id")
    assert res7["model_cv_accuracy_pct"] == 75.0
    assert res7["live_inference_confidence"] == "LOW"
    assert "model_last_retrained" in res7
    assert res7["model_last_retrained"] is not None
    assert "T" in str(res7["model_last_retrained"])  # UTC ISO-ish


def test_quant_risk_real_regime_integration(client):
    """Step 2: active_regime and probabilities must match real ttf_markov_regimes.json."""
    if not MARKOV_JSON.exists():
        pytest.skip("ttf_markov_regimes.json not found on disk")

    mr_data = json.loads(MARKOV_JSON.read_text(encoding="utf-8"))
    res = client.get("/api/v1/quant/risk?horizon=7").json()

    assert res["active_regime"] == mr_data.get("current_state_name")
    assert "MeanReverting_Transit" in res["regime_probabilities"]
    assert res["regime_source"] == mr_data.get("engine")


def test_quant_risk_consumer_contract_enforcement(client):
    """Step 4: production_actionable must be False when is_synthetic is True or fleet is not FULL."""
    res = client.get("/api/v1/quant/risk?horizon=7").json()

    assert res["production_actionable"] is False
    assert res["signal_status"] in {"insufficient_sample", "synthetic_components_present", "pipeline_degraded"}
    assert res["sample_size_caveat"] is not None
    assert len(res["sample_size_caveat"]) > 10
    assert "active_node" in res
    assert res["active_node"] in {"korolev", "london"}


def test_quant_risk_strategy_gated_when_not_actionable(client):
    """P0 Invariant: recommended_strategy_* must be strictly None when production_actionable is False."""
    res = client.get("/api/v1/quant/risk?horizon=7")
    assert res.status_code == 200
    data = res.json()

    assert data["production_actionable"] is False
    assert data["recommended_strategy_id"] is None, (
        "recommended_strategy_id must be None when production_actionable is False"
    )
    assert data["recommended_strategy_name"] is None, (
        "recommended_strategy_name must be None when production_actionable is False"
    )
    assert data["blocked_reason"] is not None
    assert isinstance(data["blocked_reason"], str)
    assert len(data["blocked_reason"]) > 3


def test_health_gate_status_contract(client):
    """Step 1 & 2: Health probe must return dual gate statuses and active_node."""
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    data = res.json()

    assert data["pipeline_health_status"] in {"NOMINAL", "DEGRADED", "CRITICAL"}
    assert data["fleet_sample_status"] in {"FULL", "LIMITED", "INSUFFICIENT"}
    assert "active_node" in data
    assert data["active_node"] in {"korolev", "london"}
    assert data["canonical_port"] == 8765


def test_quant_gate_ignores_stale_disk_health_json(monkeypatch, tmp_path):
    """
    Root-cause regression: quant must share live Dual Gate with health, not prefer
    a stale on-disk health.json snapshot (which can stay CRITICAL while live is NOMINAL).
    """
    from services import quant_risk_service as qrs

    out = tmp_path / "output"
    health_dir = out / "api" / "v1"
    health_dir.mkdir(parents=True)
    (health_dir / "health.json").write_text(
        json.dumps(
            {
                "pipeline_health_status": "CRITICAL",
                "fleet_sample_status": "INSUFFICIENT",
                "top500_live_coverage": 0,
                "replica": {"age_sec": 999999.0},
                "active_node": "korolev",
            }
        ),
        encoding="utf-8",
    )

    live_doc = {
        "pipeline_health_status": "NOMINAL",
        "fleet_sample_status": "LIMITED",
        "top500_live_coverage": 5,
        "replica": {"age_sec": 12.0, "live_ok": True},
        "pipeline_health": {"ais_lag_sec": 12.0, "pipeline_health_status": "NOMINAL"},
        "active_node": "korolev",
    }

    monkeypatch.setattr(qrs, "resolve_output_dir", lambda: out)
    monkeypatch.setattr(
        "services.ais_health.build_health_document",
        lambda **kwargs: live_doc,
    )

    payload = qrs.compute_quant_risk_payload(horizon=7)
    assert payload["pipeline_health_status"] == "NOMINAL"
    assert payload["fleet_sample_status"] == "LIMITED"
    assert payload["blocked_reason"] != "pipeline_status_critical"
    assert "insufficient_sample" in str(payload["blocked_reason"])


def test_quant_pipeline_matches_build_health_document():
    """Same request-cycle SoT: quant gate fields == live health builder."""
    from services.ais_health import build_health_document
    from services.quant_risk_service import compute_quant_risk_payload

    doc = build_health_document()
    payload = compute_quant_risk_payload(horizon=7)
    assert payload["pipeline_health_status"] == doc["pipeline_health_status"]
    assert payload["fleet_sample_status"] == doc["fleet_sample_status"]
    assert int(payload.get("horizon_days") or 7) == 7

