"""Tests for causal_analysis.py — offline synthetic prices + DWT-flow."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

import causal_analysis as ca


def _write_flow(path: Path, n: int, seed: int = 0) -> None:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    # AR-ish flow
    x = np.cumsum(rng.normal(0, 1, n)) + 1000
    df = pd.DataFrame(
        {
            "date": dates,
            "cargo_class": "crude_oil",
            "laden_dwt_sum": x * 1000,
            "coverage_pct_vessels": 1.0,
            "ballast_dwt_sum": 0.0,
            "uncertain_dwt_sum": 0.0,
            "n_laden": 1,
            "n_ballast": 0,
            "n_uncertain": 0,
            "n_vessels_observed": 1,
            "n_vessels_tracked": 1,
            "fleet_dwt_empirical_max": 1e9,
            "laden_dwt_vs_fleet_max_pct": 1.0,
            "observed_dwt_sum": x * 1000,
            "observed_dwt_vs_fleet_max_pct": 1.0,
            "n_low_confidence_draft": 0,
            "insufficient_data_flag": False,
            "insufficient_data_warning": None,
            "sufficiency_detail": "ok",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def _write_prices_wide(path: Path, n: int, flow_path: Path, lag: int = 3) -> None:
    flow = pd.read_parquet(flow_path)
    y = flow["laden_dwt_sum"].to_numpy()
    # Price leads flow by `lag` days (plus noise)
    rng = np.random.default_rng(1)
    shock = rng.normal(0, 1, n)
    brent = np.cumsum(shock) + 70
    ttf = np.cumsum(rng.normal(0, 1, n)) + 30
    # Make brent Granger-predict flow: flow_t ≈ brent_{t-lag}
    # Rebuild flow driven by lagged brent for a cleaner test signal
    driven = np.zeros(n)
    for t in range(n):
        driven[t] = (brent[t - lag] if t >= lag else brent[0]) + rng.normal(0, 0.5)
    flow2 = flow.copy()
    flow2["laden_dwt_sum"] = driven * 1000
    flow2.to_parquet(flow_path, index=False)

    dates = flow["date"]
    # Business-day-ish gaps: drop some weekends already daily; punch NaNs as holidays
    px = pd.DataFrame({"date": dates, "brent": brent, "ttf": ttf})
    # Blank weekends already continuous; set every 10th day NaN to exercise ffill
    px.loc[px.index % 10 == 0, ["brent", "ttf"]] = np.nan
    path.parent.mkdir(parents=True, exist_ok=True)
    px.to_csv(path, index=False)


def test_price_loader_wide(tmp_path: Path):
    p = tmp_path / "prices.csv"
    pd.DataFrame(
        {
            "Trade Date": pd.date_range("2025-01-01", periods=5, freq="D"),
            "Brent_Close": [70, 71, 72, 71, 73],
            "TTF_EUR": [30, 31, 29, 28, 30],
        }
    ).to_csv(p, index=False)
    out = ca.load_price_series(p)
    assert "brent" in out.columns and "ttf" in out.columns
    assert len(out) == 5


def test_price_loader_long(tmp_path: Path):
    p = tmp_path / "prices_long.csv"
    dates = pd.date_range("2025-01-01", periods=3, freq="D")
    rows = []
    for d in dates:
        rows.append({"date": d, "symbol": "ICE Brent", "close": 70.0})
        rows.append({"date": d, "symbol": "Dutch TTF", "close": 30.0})
    pd.DataFrame(rows).to_csv(p, index=False)
    out = ca.load_price_series(p)
    assert out["brent"].notna().sum() == 3
    assert out["ttf"].notna().sum() == 3


def test_price_loader_refuses_blind_guess(tmp_path: Path):
    p = tmp_path / "bad.csv"
    pd.DataFrame({"foo": [1], "bar": [2]}).to_csv(p, index=False)
    with pytest.raises(ValueError, match="Cannot map"):
        ca.load_price_series(p)


def test_ffill_capped(tmp_path: Path):
    flow = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=10, freq="D"),
            "dwt_flow": np.arange(10.0),
        }
    )
    prices = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=10, freq="D"),
            "brent": [70.0] + [np.nan] * 8 + [75.0],
        }
    )
    aligned = ca.align_series(flow, prices, "brent", max_ffill_days=3)
    # After day0=70, days1-3 ffilled, day4+ still nan until last — dropped
    assert aligned["brent"].isna().sum() == 0
    # Gap of 8 NaNs cannot be fully filled with limit=3 → fewer than 10 rows
    assert len(aligned) < 10


def test_refuse_short_intersection(tmp_path: Path):
    flow = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=20, freq="D"),
            "dwt_flow": np.random.default_rng(0).normal(size=20),
        }
    )
    prices = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=20, freq="D"),
            "brent": np.random.default_rng(1).normal(size=20) + 70,
        }
    )
    aligned = ca.align_series(flow, prices, "brent", max_ffill_days=3)
    result = ca.analyze_pair(
        aligned,
        "brent",
        max_lag=5,
        max_ffill_days=3,
        min_n=30,
        alpha=0.05,
        mt_method="fdr",
        plots_dir=tmp_path / "plots",
    )
    assert result["status"] == "refused"
    assert "n=" in result["message"]
    assert result["granger"] is None


def test_fdr_vs_bonferroni():
    p = [0.001] + [0.4] * 13
    fdr = ca.adjust_pvalues(p, "fdr")
    bon = ca.adjust_pvalues(p, "bonferroni")
    assert fdr[0] <= bon[0]
    assert bon[0] == pytest.approx(min(1.0, 0.001 * 14))


def test_end_to_end_with_disclaimer(tmp_path: Path):
    flow_path = tmp_path / "dwt_flow_timeseries.parquet"
    price_path = tmp_path / "prices.csv"
    _write_flow(flow_path, 80)
    _write_prices_wide(price_path, 80, flow_path, lag=3)

    cfg = {
        "paths": {"price_source": str(price_path)},
        "causal": {
            "max_ffill_days": 3,
            "min_intersection_points": 30,
            "granger_max_lag": 7,
            "multiple_testing": "fdr",
            "alpha": 0.05,
            "dwt_value_col": "laden_dwt_sum",
        },
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    report = ca.run_causal_analysis(
        config_path=cfg_path,
        timeseries_path=flow_path,
        price_path=price_path,
        out_report=tmp_path / "causal_report.json",
        plots_dir=tmp_path / "plots",
    )
    assert report["disclaimer"].startswith("DISCLAIMER")
    assert "NOT proof of physical" in report["disclaimer"]
    assert (tmp_path / "causal_report.json").exists()
    saved = json.loads((tmp_path / "causal_report.json").read_text(encoding="utf-8"))
    assert saved["disclaimer"] == ca.GRANGER_DISCLAIMER

    # At least one pair attempted
    assert any(p["status"] in {"ok", "failed", "refused"} for p in report["pairs"])
    brent_pair = next(p for p in report["pairs"] if p["pair"].endswith("brent"))
    if brent_pair["status"] == "ok":
        assert brent_pair["granger"]["ok"]
        assert "summary_statements" in brent_pair["granger"]
        for s in brent_pair["granger"]["summary_statements"]:
            assert "causes" not in s.lower() or "statistically" in s.lower()
            assert "statistically predicts" in s or "no lag" in s
        assert "ccf" in brent_pair["plots"]
        assert "granger_table" in brent_pair["plots"]


def test_missing_price_path_refused(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"paths": {}, "causal": {}}), encoding="utf-8")
    report = ca.run_causal_analysis(
        config_path=cfg_path,
        timeseries_path=tmp_path / "missing.parquet",
        out_report=tmp_path / "causal_report.json",
        plots_dir=tmp_path / "plots",
    )
    assert report["status"] == "refused"
    assert "PRICE_SOURCE_PATH" in report["message"] or "price_source" in report["message"]


def test_ccf_positive_lag_means_x_leads():
    # x leads y by 2: y_t = x_{t-2}
    n = 100
    rng = np.random.default_rng(0)
    x = rng.normal(size=n)
    y = np.zeros(n)
    y[2:] = x[:-2]
    ccf = ca.cross_correlation(y, x, max_lag=5)
    assert ccf["best_abs_lag"] == 2
    assert ccf["best_abs_corr"] > 0.8
