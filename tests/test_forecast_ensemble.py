"""Tests for forecast_ensemble.py — synthetic series, offline."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import forecast_ensemble as fe


def _synth_flow(path: Path, n: int = 90, period: float = 7.0) -> None:
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    t = np.arange(n)
    y = 1_000_000 + 80_000 * np.sin(2 * np.pi * t / period) + 200 * t
    rng = np.random.default_rng(0)
    y = y + rng.normal(0, 5_000, n)
    df = pd.DataFrame(
        {
            "date": dates,
            "cargo_class": "crude_oil",
            "laden_dwt_sum": y,
            "coverage_pct_vessels": 12.5,
            "ballast_dwt_sum": 0.0,
            "uncertain_dwt_sum": 0.0,
            "n_laden": 10,
            "n_ballast": 0,
            "n_uncertain": 0,
            "n_vessels_observed": 100,
            "n_vessels_tracked": 2711,
            "fleet_dwt_empirical_max": 1e9,
            "laden_dwt_vs_fleet_max_pct": 1.0,
            "observed_dwt_sum": y,
            "observed_dwt_vs_fleet_max_pct": 1.0,
            "n_low_confidence_draft": 0,
            "insufficient_data_flag": False,
            "insufficient_data_warning": None,
            "sufficiency_detail": "ok",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def test_min_n_gates_documented():
    assert fe.MIN_N_ARIMA == 40
    assert fe.MIN_N_GBM == 60
    assert fe.MIN_N_LSTM == 300
    assert fe._min_naive(7) == 8


def test_refuse_below_naive_minimum(tmp_path: Path):
    pq = tmp_path / "dwt_flow_timeseries.parquet"
    _synth_flow(pq, n=5)
    out = fe.run_forecast_ensemble(
        timeseries_path=pq,
        horizon=7,
        out_report=tmp_path / "forecast_ensemble_report.json",
        out_series=tmp_path / "forecast_ensemble_series.parquet",
        out_dashboard=tmp_path / "forecast_dashboard.html",
    )
    assert out["forecast"]["issued"] is False
    assert "MIN_N_NAIVE" in out["forecast"]["reason"]
    assert (tmp_path / "forecast_dashboard.html").exists()
    html = (tmp_path / "forecast_dashboard.html").read_text(encoding="utf-8")
    assert "ПРОГНОЗ НЕ ВЫДАН" in html or "N=" in html


def test_end_to_end_issues_forecast_with_ci_and_baseline(tmp_path: Path):
    pq = tmp_path / "dwt_flow_timeseries.parquet"
    _synth_flow(pq, n=100)
    # stub spectral with reliable weekly peak
    (tmp_path / "spectral_report.json").write_text(
        json.dumps(
            {
                "analyses": [
                    {
                        "spectrum": {
                            "top_frequencies": [
                                {"period_days": 7.0, "claim_reliable": True, "power": 1.0}
                            ]
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # point ensemble at tmp spectral via monkeypatch paths
    fe.SPECTRAL_PATH = tmp_path / "spectral_report.json"
    fe.MARKOV_PATH = tmp_path / "missing_markov.json"
    fe.ELLIOTT_PATH = tmp_path / "missing_elliott.json"
    fe.CAUSAL_PATH = tmp_path / "missing_causal.json"

    out = fe.run_forecast_ensemble(
        timeseries_path=pq,
        horizon=7,
        out_report=tmp_path / "forecast_ensemble_report.json",
        out_series=tmp_path / "forecast_ensemble_series.parquet",
        out_dashboard=tmp_path / "forecast_dashboard.html",
    )
    assert out["forecast"]["issued"] is True
    assert len(out["forecast"]["point"]) == 7
    assert len(out["forecast"]["interval"]["ci_low"]) == 7
    assert out["forecast"]["interval"]["distributional_assumption"].startswith("none")
    assert "naive" in out["walk_forward"]["models"]
    # every scored model reports beats_baseline key
    for name, sc in out["walk_forward"]["models"].items():
        assert "beats_baseline" in sc
        assert "mae" in sc
    assert out["training_sample"]["n_observations"] >= 90
    assert "coverage_pct_vessels_mean" in out["training_sample"]
    html = (tmp_path / "forecast_dashboard.html").read_text(encoding="utf-8")
    # Apple template renders via JS; banner source contains LIVE / N= / coverage keys
    assert "Model trained on" in html or "Модель обучена на N=" in html or "N=${nObs}" in html
    assert "coverage" in html.lower() or "покрытие" in html.lower() or "COVERAGE" in html
    assert "design_system.css" in html
    series = pd.read_parquet(tmp_path / "forecast_ensemble_series.parquet")
    assert {"y_hat", "ci_low", "ci_high"}.issubset(series.columns)


def test_lstm_refused_on_small_n():
    y = np.linspace(1, 50, 50)
    fc, reason = fe.forecast_lstm(y, horizon=7)
    assert fc is None
    assert reason is not None
    assert "MIN_N_LSTM" in reason or "torch" in reason


def test_bootstrap_ci_not_gaussian_shape():
    y = np.cumsum(np.random.default_rng(0).standard_t(3, size=80))
    point = np.array([y[-1]] * 5)
    fitted = np.r_[np.nan, y[:-1]]
    fitted[0] = y[0]
    iv = fe.residual_bootstrap_intervals(y, point, in_sample_fitted=fitted, B=200)
    assert iv["distributional_assumption"].startswith("none")
    assert all(lo <= hi for lo, hi in zip(iv["ci_low"], iv["ci_high"]))


def test_elliott_is_optional_flag_in_features():
    dates = pd.date_range("2025-01-01", periods=30, freq="D")
    s = pd.DataFrame({"date": dates, "y": np.arange(30.0), "coverage_pct_vessels": 1.0})
    feat = fe.build_feature_frame(s, elliott_flag=1, markov_laden_p=0.4)
    assert "elliott_flag" in feat.columns
    assert set(feat["elliott_flag"].unique()) == {1}
