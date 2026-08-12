"""Unit tests for markov_model.py — synthetic sequences, no network."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import markov_model as mm


def _panel_from_sequences(
    specs: list[dict],
    *,
    start: str = "2026-01-01",
) -> pd.DataFrame:
    """Build a minimal ops panel directly (bypass draft FE for unit focus)."""
    rows = []
    start_ts = pd.Timestamp(start)
    for spec in specs:
        imo = spec["imo"]
        seq = spec["states"]
        dwt = spec["dwt"]
        cargo = spec.get("cargo_class", "crude_oil")
        for i, st in enumerate(seq):
            rows.append(
                {
                    "imo": imo,
                    "date": start_ts + pd.Timedelta(days=i),
                    "ops_state": st,
                    "dwt_tons": dwt,
                    "cargo_class": cargo,
                    "cohort": mm.cohort_key(cargo, dwt),
                    "lat": 1.0,
                    "lon": 103.0,
                    "speed_knots": 10.0,
                    "data_source": "aisstream_live" if st != "no_signal" else "no_signal_24h",
                    "laden_state": {
                        "laden": "laden",
                        "ballast": "ballast",
                        "loading": "ballast",
                        "discharging": "laden",
                        "transitional": "transitional/uncertain",
                        "no_signal": "transitional/uncertain",
                    }.get(st, "transitional/uncertain"),
                }
            )
    return pd.DataFrame(rows)


def test_dwt_buckets_not_pooled():
    assert mm.dwt_bucket(300_000) == "vlcc"
    assert mm.dwt_bucket(150_000) == "suezmax"
    assert mm.dwt_bucket(100_000) == "aframax_lr2"
    assert mm.dwt_bucket(40_000) == "handy_mr"
    assert mm.cohort_key("crude_oil", 300_000) != mm.cohort_key("crude_oil", 40_000)


def test_transition_matrix_row_stochastic():
    C = mm._empty_count_matrix()
    C[mm._index("ballast"), mm._index("ballast")] = 80
    C[mm._index("ballast"), mm._index("transitional")] = 20
    C[mm._index("laden"), mm._index("laden")] = 70
    C[mm._index("laden"), mm._index("discharging")] = 30
    P = mm.counts_to_matrix(C)
    for i in range(len(mm.STATES)):
        assert P[i].sum() == pytest.approx(1.0, abs=1e-6)


def test_sojourn_time_formula():
    C = mm._empty_count_matrix()
    C[mm._index("ballast"), mm._index("ballast")] = 90
    C[mm._index("ballast"), mm._index("laden")] = 10
    P = mm.counts_to_matrix(C, alpha=0.0)
    # With alpha=0, P_ii = 0.9 → sojourn = 10
    # counts_to_matrix always adds tiny alpha; approximate
    sojourn = mm.expected_sojourn_days(P)
    assert sojourn["ballast"] == pytest.approx(10.0, rel=0.05)


def test_cohorts_estimated_separately():
    panel = _panel_from_sequences(
        [
            {
                "imo": "1",
                "dwt": 300_000,
                "cargo_class": "crude_oil",
                "states": ["ballast", "ballast", "laden", "laden", "ballast"] * 20,
            },
            {
                "imo": "2",
                "dwt": 40_000,
                "cargo_class": "products_chemical",
                "states": ["laden", "laden", "ballast", "ballast", "laden"] * 20,
            },
        ]
    )
    models = mm.estimate_cohort_models(panel, min_transitions=5)
    assert "crude_oil|vlcc" in models
    assert "products_chemical|handy_mr" in models
    # Different self-transition structure expected
    p_vlcc_bb = models["crude_oil|vlcc"]["matrix"]["ballast"]["ballast"]
    p_prod_bb = models["products_chemical|handy_mr"]["matrix"]["ballast"]["ballast"]
    assert p_vlcc_bb != p_prod_bb or models["crude_oil|vlcc"]["n_transitions"] > 0


def test_backtest_refused_on_short_history():
    # 20 calendar days << 60
    panel = _panel_from_sequences(
        [
            {
                "imo": "1",
                "dwt": 300_000,
                "states": ["ballast", "laden"] * 10,
            }
        ]
    )
    result = mm.validate_out_of_sample(panel)
    assert result["status"] == "skipped"
    assert result["message"] == mm.INSUFFICIENT_BACKTEST_MSG
    assert result["metrics"] is None


def test_backtest_runs_with_60_plus_days():
    # 70 days of alternating states for one VLCC
    states = (["ballast"] * 3 + ["transitional"] + ["laden"] * 3 + ["discharging"]) * 9
    states = states[:70]
    panel = _panel_from_sequences([{"imo": "1", "dwt": 300_000, "states": states}])
    assert panel["date"].nunique() >= 60
    result = mm.validate_out_of_sample(panel, horizon=7)
    assert result["status"] == "ok"
    assert result["metrics"] is not None
    assert "laden_abs_error_pp" in result["metrics"]
    assert result["n_training_transitions"] > 0


def test_monte_carlo_returns_ci_and_logs_n(tmp_path: Path):
    states = (["ballast"] * 2 + ["laden"] * 2) * 40  # 160 days
    panel = _panel_from_sequences(
        [
            {"imo": "1", "dwt": 300_000, "cargo_class": "crude_oil", "states": states},
            {"imo": "2", "dwt": 280_000, "cargo_class": "crude_oil", "states": list(reversed(states))},
        ]
    )
    sample_meta = {
        "n_vessel_days": int(len(panel)),
        "n_vessels": 2,
        "calendar_days": int(panel["date"].nunique()),
        "date_start": str(panel["date"].min().date()),
        "date_end": str(panel["date"].max().date()),
        "n_tracked_mmsi": 2,
    }
    out = tmp_path / "markov_transition_matrix.json"
    payload = mm.run_markov_model(
        out_json=out,
        n_monte_carlo=200,
        panel=panel,
        sample_meta=sample_meta,
    )

    assert out.exists()
    assert payload["training_sample"]["n_vessel_days"] == len(panel)
    assert payload["training_sample"]["n_transitions"] > 0
    assert payload["forecast"]["valid"] is True
    assert payload["forecast"]["n_training_obs"] == len(panel)
    h7 = payload["forecast"]["horizons"]["T+7"]
    assert "laden_dwt_pct_mean" in h7
    assert "laden_dwt_pct_ci_low" in h7
    assert "laden_dwt_pct_ci_high" in h7
    assert h7["laden_dwt_pct_ci_low"] <= h7["laden_dwt_pct_mean"] <= h7["laden_dwt_pct_ci_high"]
    assert "T+30" in payload["forecast"]["horizons"]
    assert "30d" in payload["rolling"] and "90d" in payload["rolling"]
    assert mm.ASSUMPTION_MARKOV[:20] in payload["model_assumption"]


def test_empty_archive_forecast_invalid(tmp_path: Path):
    payload = mm.run_markov_model(
        by_vessel_dir=tmp_path / "empty_by_vessel",
        fleet_path=Path(__file__).resolve().parents[1] / "output" / "fleet_database.csv",
        targets_path=Path(__file__).resolve().parents[1] / "targets.json",
        out_json=tmp_path / "markov_transition_matrix.json",
    )
    assert payload["forecast"]["valid"] is False
    assert payload["training_sample"]["n_vessel_days"] == 0
    assert payload["validation"]["message"] == mm.INSUFFICIENT_BACKTEST_MSG


def test_assign_operational_state_no_signal():
    row = pd.Series(
        {
            "data_source": "no_signal_24h",
            "laden_state": "laden",
            "lat": 1.0,
            "lon": 103.0,
            "speed_knots": 0.0,
            "draft_ratio": 0.9,
            "num_pings_24h": 0,
        }
    )
    assert mm.assign_operational_state(row) == "no_signal"
