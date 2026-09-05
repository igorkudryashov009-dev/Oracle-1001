"""AIS daily aggregates roll-up smoke tests."""

from __future__ import annotations

from services.ais_daily_aggregates import (
    count_aggregate_days,
    migrate_ais_daily_schema,
    roll_up_daily_ais_telemetry,
)


def test_roll_up_produces_30_plus_days():
    migrate_ais_daily_schema()
    summary = roll_up_daily_ais_telemetry(lookback_days=90, min_history_days=45)
    assert summary["rows_total"] >= 30
    assert count_aggregate_days() >= 30
    assert summary.get("ok") is True
