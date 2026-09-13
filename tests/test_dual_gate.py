"""Unit tests for dual Deploy Gate semantics."""

from __future__ import annotations

from services.dual_gate import (
    FLEET_SAMPLE_LIMITED_MIN,
    apply_fleet_sample_to_metric,
    compute_fleet_sample_status,
    compute_pipeline_health_status,
    live_inference_confidence,
)
from services.satellite_ais_adapter import SatelliteAISAdapter


def test_fleet_sample_thresholds_from_prompt7_soak():
    assert FLEET_SAMPLE_LIMITED_MIN == 5
    assert compute_fleet_sample_status(100)["fleet_sample_status"] == "FULL"
    assert compute_fleet_sample_status(5)["fleet_sample_status"] == "LIMITED"
    assert compute_fleet_sample_status(7)["fleet_sample_status"] == "LIMITED"
    assert compute_fleet_sample_status(4)["fleet_sample_status"] == "INSUFFICIENT"
    assert compute_fleet_sample_status(0)["fleet_sample_status"] == "INSUFFICIENT"


def test_pipeline_nominal_independent_of_coverage():
    pipe = compute_pipeline_health_status(
        freshness={"age_sec": 12.0, "live_ok": True, "stale": False, "integrity_ok": True},
        connector={"reconnects": 0, "http_429_count": 0},
        port_ok=True,
        port_drift_8478=False,
    )
    assert pipe["pipeline_health_status"] == "NOMINAL"


def test_pipeline_critical_on_429():
    pipe = compute_pipeline_health_status(
        freshness={"age_sec": 10.0, "live_ok": True, "stale": False, "integrity_ok": True},
        connector={"reconnects": 0, "http_429_count": 2},
        port_ok=True,
    )
    assert pipe["pipeline_health_status"] == "CRITICAL"


def test_live_inference_confidence_drops_when_limited():
    out = live_inference_confidence(
        model_cv_accuracy_pct=80.0,
        fleet_sample_status="LIMITED",
        coverage=5,
    )
    assert out["model_cv_accuracy_pct"] == 80.0
    assert out["live_inference_confidence"] == "LOW"
    assert out["live_inference_confidence_pct"] < 80.0


def test_lssi_insufficient_sample_flag():
    sample = compute_fleet_sample_status(7)
    metric = apply_fleet_sample_to_metric(
        {"signal": "BULLISH", "lssi_index": 90},
        sample=sample,
        metric_name="LSSI",
    )
    assert metric["signal_status"] == "insufficient_sample"
    assert metric["signal"] == "INSUFFICIENT_SAMPLE"
    assert metric["production_signal"] is False


def test_pipeline_degraded_on_low_disk():
    pipe = compute_pipeline_health_status(
        freshness={"age_sec": 12.0, "live_ok": True, "stale": False, "integrity_ok": True},
        connector={"reconnects": 0, "http_429_count": 0},
        port_ok=True,
        disk={"disk_free_pct": 18.0},
    )
    assert pipe["pipeline_health_status"] == "DEGRADED"
    assert any(r.startswith("disk_free_low") for r in pipe["reasons"])


def test_pipeline_critical_on_disk_near_full():
    pipe = compute_pipeline_health_status(
        freshness={"age_sec": 12.0, "live_ok": True, "stale": False, "integrity_ok": True},
        connector={"reconnects": 0, "http_429_count": 0},
        port_ok=True,
        disk={"disk_free_pct": 8.0},
    )
    assert pipe["pipeline_health_status"] == "CRITICAL"
    assert any(r.startswith("disk_free_critical") for r in pipe["reasons"])


def test_satellite_adapter_is_stub():
    import asyncio

    import pytest

    ad = SatelliteAISAdapter(provider=None, credentials=None)
    rep = ad.coverage_report()
    assert rep["status"] == "not_activated"
    with pytest.raises(NotImplementedError):
        asyncio.run(ad.connect())
