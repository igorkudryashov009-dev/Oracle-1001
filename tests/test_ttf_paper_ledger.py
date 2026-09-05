"""Paper ledger + MtM smoke tests."""

from __future__ import annotations

from services.ttf_forecast.paper_ledger import (
    auto_log_daily_paper_trades,
    compute_mtm_pnl,
    count_open_orders,
    migrate_paper_ledger_schema,
)


def test_compute_mtm_positive_on_up_move():
    pnl = compute_mtm_pnl(
        entry_spot=71.952,
        live_spot=75.0,
        p50_30=72.21,
        size_usd=1000.0,
        projected_return_pct=18.4,
        beta=1.0,
        days_held=10,
    )
    assert pnl > 0


def test_auto_log_opens_three_tickets():
    migrate_paper_ledger_schema()
    report = auto_log_daily_paper_trades(spot=71.952, p50_30=72.21, write_json=True)
    assert report["open_count"] >= 3
    assert count_open_orders() >= 3
    assert "MtM" in report["live_paper_badge"]
    b = report["strategy_b"]
    assert b is not None
    assert b["strategy_id"] == "STRAT_B_AIS_FUTURES"
    assert b["position_size_usd"] == 1000.0
