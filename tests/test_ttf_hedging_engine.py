"""Hedging / portfolio ROI engine smoke tests."""

from __future__ import annotations

from services.ttf_forecast.hedging_engine import run_hedging_engine


def test_hedging_three_strategies_and_panels():
    fc = {
        "spot_eur_mwh": 71.952,
        "horizons": {
            "7": {"p10": 66.68, "p50": 71.19, "p90": 76.08},
            "14": {"p10": 64.40, "p50": 72.62, "p90": 78.06},
            "30": {"p10": 61.91, "p50": 72.21, "p90": 84.47},
        },
    }
    report = run_hedging_engine(forecast=fc, write=False)
    assert report["base_investment_usd"] == 1000.0
    assert report["recommended_strategy_id"] == "B"
    strats = report["panel_h"]["strategies"]
    assert len(strats) == 3
    ids = {s["id"] for s in strats}
    assert ids == {"A", "B", "C"}
    b = next(s for s in strats if s["id"] == "B")
    assert abs(b["expected_yield_pct"]["lo"] - 18.4) < 1.5
    assert b["var_95_pct"] == -5.8
    center = report["panel_g"]["center"]
    assert center["from_usd"] == 1000.0
    assert abs(center["to_usd"] - 1184.0) < 1.0
    assert abs(center["return_pct"] - 18.4) < 0.05
    eq = report["panel_i"]
    assert len(eq["labels"]) == 31
    assert len(eq["benchmark"]) == 31
    assert set(eq["series"]) == {"A", "B", "C"}
    assert eq["series"]["B"]["values"][0] == 1000.0
    assert abs(eq["series"]["B"]["values"][-1] - 1184.0) < 1.0
