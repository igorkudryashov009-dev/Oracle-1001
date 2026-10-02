"""Sweep planner: batches, the 450 hard stop, carry-forward, VF gap insurance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.coverage_sweep import (
    GFW_HARD_STOP,
    classify_tiers,
    coverage_from_rows,
    maybe_coverage_alert,
    plan_sweep,
    record_vf_attempt,
    vf_should_spend,
)

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


def _fleet(n: int, *, stale_days: int, dwt0: float = 200000) -> list[dict]:
    rows = []
    for i in range(n):
        last = (NOW - timedelta(days=stale_days, hours=i % 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({"imo": 1000000 + i, "mmsi": 200000000 + i, "dwt": dwt0 - i, "last_record": last})
    return rows


def test_tiers_and_batch_caps() -> None:
    fleet = classify_tiers(_fleet(1260, stale_days=8))
    assert sum(1 for r in fleet if r["tier"] == "A") == 500
    assert sum(1 for r in fleet if r["tier"] == "B") == 760
    plan_b = plan_sweep(fleet, tier="B", used_today=0, now=NOW)
    assert plan_b["planned"] == 109
    assert plan_b["carried"] == 760 - 109
    plan_a = plan_sweep(fleet, tier="A", used_today=0, now=NOW)
    assert plan_a["planned"] == 250


def test_hard_stop_carries_the_rest() -> None:
    fleet = _fleet(20, stale_days=8)
    for row in fleet:
        row["tier"] = "B"
    plan = plan_sweep(fleet, tier="B", used_today=GFW_HARD_STOP, now=NOW)
    assert plan["planned"] == 0
    assert plan["carried"] == 20
    assert plan["stopped"] is True
    plan2 = plan_sweep(fleet, tier="B", used_today=449, now=NOW)
    assert plan2["planned"] == 1
    assert plan2["carried"] == 19


def test_queue_is_widest_gap_first() -> None:
    fleet = [
        {"imo": 1, "tier": "B", "dwt": 1, "last_record": "2026-09-20T00:00:00Z", "gap_hours": 10},
        {"imo": 2, "tier": "B", "dwt": 2, "last_record": "2026-09-01T00:00:00Z", "gap_hours": 900},
    ]
    plan = plan_sweep(fleet, tier="B", used_today=0, now=NOW)
    assert plan["requests"][0]["imo"] == 2


def test_vf_gap_insurance_and_refund(tmp_path) -> None:
    assert vf_should_spend("A", 48) is False
    assert vf_should_spend("A", 48.1) is True
    assert vf_should_spend("B", 120) is False
    assert vf_should_spend("B", 121) is True
    ledger = tmp_path / "vf.jsonl"
    skipped = record_vf_attempt(mmsi="1", tier="A", gap_hours=10, api_ok=True, ledger_path=ledger)
    assert skipped["credit_spent"] == 0
    spent = record_vf_attempt(mmsi="2", tier="A", gap_hours=60, api_ok=True, ledger_path=ledger)
    assert spent["credit_spent"] == 1 and spent["result"] == "ok"
    refunded = record_vf_attempt(mmsi="3", tier="B", gap_hours=200, api_ok=False, ledger_path=ledger)
    assert refunded["credit_spent"] == 0 and refunded["result"] == "error_refunded"


def test_slim_health_does_not_copy_sweep_fields() -> None:
    from services.api_auth import sanitize_health

    slim = sanitize_health(
        {
            "pipeline_health_status": "NOMINAL",
            "acceptance": {"status": "DEGRADED"},
            "tierA_daily_coverage_pct": 1,
            "gfw_daily_requests_used": 10,
        },
        tier="public",
    )
    assert "tierA_daily_coverage_pct" not in slim
    assert "gfw_daily_requests_used" not in slim
    assert slim["status"] == "NOMINAL"


def test_weekly_coverage_and_alert() -> None:
    fresh = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (NOW - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"imo": i, "dwt": 1000 - i, "last_record": fresh if i < 400 else old} for i in range(600)]
    metrics = coverage_from_rows(rows, now=NOW)
    assert metrics["tierB_weekly_coverage_pct"] == 0.0
    calls = []

    def emit(kind, message, **kw):
        calls.append(kind)

    assert maybe_coverage_alert(metrics, emit) == "coverage_below_95"
    assert calls == ["coverage_below_95"]
    assert maybe_coverage_alert({"tierA_daily_coverage_pct": 100, "tierB_weekly_coverage_pct": 100}, emit) is None
