"""SRE integrity gates for TTF market + UI payload."""

from __future__ import annotations

import pytest

from services.ttf_forecast.integrity import (
    SREBuildError,
    assert_spot_in_band,
    assert_ttf_ui_payload,
    ensure_granger_nonempty,
    run_pre_build_gates,
)


def test_spot_band_hard_fail():
    with pytest.raises(SREBuildError):
        assert_spot_in_band(26.0)
    with pytest.raises(SREBuildError):
        assert_spot_in_band(120.0)
    assert_spot_in_band(71.952)


def test_granger_placeholder_nonempty():
    g = ensure_granger_nonempty({"labels": [], "neg_log10_p": []})
    assert len(g["labels"]) >= 1
    assert len(g["neg_log10_p"]) >= 1
    assert g.get("integrity_degraded") is True


def test_pre_build_gates_live():
    # Requires local DB + forecast artifacts from prior pipeline
    try:
        out = run_pre_build_gates()
    except SREBuildError as exc:
        pytest.skip(f"artifacts not ready: {exc}")
    assert out["ok"] is True
    assert out["market"]["rows"] >= 30


def test_ui_payload_rejects_empty():
    with pytest.raises(SREBuildError):
        assert_ttf_ui_payload({})
    with pytest.raises(SREBuildError):
        assert_ttf_ui_payload({"error": "x", "spot_eur_mwh": 71.9})
