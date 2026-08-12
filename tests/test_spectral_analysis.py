"""Tests for spectral_analysis + elliott_wave_heuristic (offline, synthetic)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import spectral_analysis as sa
import elliott_wave_heuristic as ew


def _synth_parquet(path: Path, n_days: int, *, period: float = 7.0, noise: float = 0.05) -> None:
    dates = pd.date_range("2026-01-01", periods=n_days, freq="D")
    t = np.arange(n_days)
    # weekly sinusoid + mild trend
    values = 1_000_000 + 50_000 * np.sin(2 * np.pi * t / period) + 100 * t
    rng = np.random.default_rng(0)
    values = values + noise * 50_000 * rng.normal(size=n_days)
    df = pd.DataFrame(
        {
            "date": dates,
            "cargo_class": "crude_oil",
            "laden_dwt_sum": values,
            "ballast_dwt_sum": 0.0,
            "uncertain_dwt_sum": 0.0,
            "n_laden": 10,
            "n_ballast": 0,
            "n_uncertain": 0,
            "n_vessels_observed": 10,
            "n_vessels_tracked": 2711,
            "coverage_pct_vessels": 0.4,
            "fleet_dwt_empirical_max": 5_000_000,
            "laden_dwt_vs_fleet_max_pct": 20.0,
            "observed_dwt_sum": values,
            "observed_dwt_vs_fleet_max_pct": 20.0,
            "n_low_confidence_draft": 0,
            "insufficient_data_flag": False,
            "insufficient_data_warning": None,
            "sufficiency_detail": "ok",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def test_length_gate_blocks_monthly_on_short_series():
    assess = sa.series_length_assessment(n_obs=40, n_calendar=40)
    assert assess["short_archive_flag"] is True
    assert assess["cycle_detectability"]["weekly"]["detectable_reliably"] is True  # 2.5*7=17.5 → 18
    assert assess["cycle_detectability"]["monthly"]["detectable_reliably"] is False
    assert assess["cycle_detectability"]["quarterly"]["detectable_reliably"] is False
    assert assess["short_archive_message"] is not None


def test_periodogram_finds_weekly_on_long_synth(tmp_path: Path):
    pq = tmp_path / "dwt_flow_timeseries.parquet"
    _synth_parquet(pq, n_days=120, period=7.0)
    series = sa.load_dwt_flow_series(pq)
    result = sa.analyze_series(series, label="synth_weekly", plots_dir=tmp_path / "plots")
    assert result["spectrum"]["ok"] is True
    assert result["detectable_cycle_classes"]["monthly"] is True  # 120 >= 75
    tops = result["spectrum"]["top_frequencies"]
    assert len(tops) <= 5
    # Dominant period should be near 7 days among reliable peaks
    periods = [t["period_days"] for t in tops if t.get("claim_reliable")]
    assert any(abs(p - 7.0) < 1.5 for p in periods)


def test_short_series_marks_peaks_unreliable(tmp_path: Path):
    pq = tmp_path / "dwt_flow_timeseries.parquet"
    _synth_parquet(pq, n_days=28, period=7.0)
    series = sa.load_dwt_flow_series(pq)
    result = sa.analyze_series(series, label="short", plots_dir=tmp_path / "plots")
    assert result["length_assessment"]["short_archive_flag"] is True
    assert result["detectable_cycle_classes"]["monthly"] is False
    # Any ~30d-ish peak must be flagged unreliable if present
    for t in result["spectrum"]["top_frequencies"]:
        if t["period_days"] >= 25:
            assert t["claim_reliable"] is False


def test_normality_rejects_heavy_tails():
    rng = np.random.default_rng(1)
    # Student-t df=3 increments accumulated
    inc = stats_t_rvs = rng.standard_t(3, size=200)
    levels = np.cumsum(inc)
    out = sa.normality_of_increments(levels)
    assert out["verdict"] == "increments_NOT_normal"
    assert "НЕ нормальны" in out["verdict_ru"]
    assert out["ci_recommendation"]["prefer"] in {"student_t", "empirical_quantiles"}
    assert out["ci_recommendation"]["do_not_use"] == "gaussian_ci_alone"


def test_normality_insufficient_data():
    out = sa.normality_of_increments(np.array([1.0, 2.0, 3.0]))
    assert out["verdict"] == "insufficient_data"


def test_end_to_end_report(tmp_path: Path):
    pq = tmp_path / "dwt_flow_timeseries.parquet"
    _synth_parquet(pq, n_days=90, period=7.0)
    # Make increments clearly non-normal
    df = pd.read_parquet(pq)
    rng = np.random.default_rng(2)
    shocks = rng.standard_t(3, size=len(df)) * 20_000
    df["laden_dwt_sum"] = df["laden_dwt_sum"] + np.cumsum(shocks)
    df.to_parquet(pq, index=False)

    report = sa.run_spectral_analysis(
        timeseries_path=pq,
        out_report=tmp_path / "spectral_report.json",
        plots_dir=tmp_path / "plots",
        run_elliott=True,
    )
    assert (tmp_path / "spectral_report.json").exists()
    assert report["methodology_notes"]["no_data_dredging"] is True
    assert "analyses" in report
    assert report["elliott_wave_heuristic"]["method_type"] == ew.METHOD_TYPE
    assert report["dashboard_separation_rule"]
    # plots created for primary series
    plots = report["analyses"][0]["plots"]
    assert "periodogram" in plots
    assert "increments_hist_qq" in plots


def test_empty_timeseries_honest(tmp_path: Path):
    pq = tmp_path / "empty.parquet"
    pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "cargo_class": pd.Series(dtype=str),
            "laden_dwt_sum": pd.Series(dtype=float),
            "coverage_pct_vessels": pd.Series(dtype=float),
        }
    ).to_parquet(pq, index=False)
    report = sa.run_spectral_analysis(
        timeseries_path=pq,
        out_report=tmp_path / "spectral_report.json",
        plots_dir=tmp_path / "plots",
    )
    primary = report["analyses"][0]
    assert primary["n_observations_non_nan"] == 0
    assert primary["spectrum"]["ok"] is False


def test_elliott_method_type_always_present(tmp_path: Path):
    dates = pd.date_range("2026-01-01", periods=40, freq="D")
    # Construct a crude impulse-like path
    y = np.array(
        [0, 1, 0.4, 2, 1.2, 2.5] + list(np.linspace(2.5, 1.0, 34)),
        dtype=float,
    )
    series = pd.DataFrame({"date": dates, "value": y * 1000})
    out = ew.run_elliott_heuristic(series, out_json=tmp_path / "elliott_wave_report.json")
    assert out["method_type"] == "discretionary_heuristic_not_statistically_validated"
    assert out["separated_from_spectral_scores"] is True
    saved = json.loads((tmp_path / "elliott_wave_report.json").read_text(encoding="utf-8"))
    assert saved["method_type"] == ew.METHOD_TYPE
