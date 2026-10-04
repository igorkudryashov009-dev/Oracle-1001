"""VF watcher + allocator readiness. No live VesselFinder call."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.key_activation import (
    is_provider_paused,
    probe_vesselfinder,
    record_provider_error,
    vf_health_block,
)
from services.vf_budget_allocator import DAILY_TOTAL_QUOTA, MONTHLY_BUDGET, VesselSlot, plan_daily_allocation


class _Resp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts_state.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "alerts.jsonl")
    monkeypatch.setattr("services.alerts.ALERTS_PATH_HOST", tmp_path / "nohost.jsonl")
    monkeypatch.setattr("services.runtime_env.RUNTIME_ENV_PATH", tmp_path / "runtime.json")
    monkeypatch.setattr("services.runtime_env.SIGNAL_PATH", tmp_path / "signal")


def test_invalid_userkey_status_and_masked_alert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch, tmp_path)
    key = "vf-live-key-ZZ99"
    monkeypatch.setattr("services.vesselfinder_client.resolve_userkey", lambda: key)
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _Resp(200, {"error": "Invalid Userkey!"}),
    )
    out = probe_vesselfinder(force=True)
    assert out["status"] == "invalid_key"
    assert out["error"] == "invalid_key"
    assert out["key_masked"] == "****ZZ99"
    assert key not in json.dumps(out)
    health = vf_health_block()
    assert health["status"] == "invalid_key"
    assert health["key_masked"] == "****ZZ99"
    text = (tmp_path / "alerts.jsonl").read_text(encoding="utf-8")
    assert "vf_invalid_key" in text
    assert "****ZZ99" in text
    assert key not in text


def test_success_sets_active_and_clears_pause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch, tmp_path)
    key = "vf-live-key-OK12"
    for i in range(3):
        record_provider_error("vesselfinder", f"Invalid Userkey! #{i}")
    assert is_provider_paused("vesselfinder")
    monkeypatch.setattr("services.vesselfinder_client.resolve_userkey", lambda: key)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, {"AIS": {"IMO": "9388819"}}))
    out = probe_vesselfinder(force=True)
    assert out["ok"] is True
    assert out["status"] == "active"
    assert is_provider_paused("vesselfinder") is False
    health = vf_health_block()
    assert health["status"] == "active"
    assert health["paused"] is False
    assert key not in json.dumps(health)


def test_allocator_max_gap_skips_gfw_closed_and_caps_16(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gaps = {str(i): float(300 - i) for i in range(1, 40)}
    gaps["7"] = 10.0  # below the top-tier gap threshold
    closed = {"4"}
    monkeypatch.setattr(
        "services.vf_budget_allocator.load_terrestrial_gaps",
        lambda **kwargs: gaps,
    )
    monkeypatch.setattr(
        "services.vf_budget_allocator.load_gfw_closed_imos",
        lambda **kwargs: closed,
    )
    monkeypatch.setattr(
        "services.vesselfinder_budget.get_budget_status",
        lambda: {"remaining": MONTHLY_BUDGET, "used": 12, "monthly_budget": MONTHLY_BUDGET},
    )
    top = [VesselSlot(imo=str(i), dwt=1000 - i, tier="top", rank=i) for i in range(1, 25)]
    rest = [VesselSlot(imo=str(100 + i), dwt=100 - i, tier="rest", rank=i) for i in range(1, 15)]
    for slot in rest:
        gaps[slot.imo] = 200.0
    plan = plan_daily_allocation(
        day="2026-10-03",
        state_path=tmp_path / "alloc.json",
        now=datetime(2026, 10, 3, 12, tzinfo=timezone.utc),
        simulate=False,
        top_slots=top,
        rest_slots=rest,
        monthly_remaining=MONTHLY_BUDGET,
    )
    assert len(plan.all_imos) <= DAILY_TOTAL_QUOTA
    assert len(plan.all_imos) == 16
    assert plan.all_imos[0] == "1"
    assert "4" not in plan.all_imos
    assert "7" not in plan.all_imos


def test_vf_verified_idempotent_and_requires_provenance(tmp_path: Path) -> None:
    from services.archive_snapshot_worker import take_daily_snapshot

    fleet = tmp_path / "fleet.csv"
    with fleet.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["vessel_name", "imo", "mmsi", "dwt_tons", "vessel_category"],
        )
        w.writeheader()
        w.writerow(
            {"vessel_name": "PROVEN", "imo": "9000001", "mmsi": "200000001", "dwt_tons": "100", "vessel_category": "vessel"}
        )
        w.writerow(
            {"vessel_name": "BARE", "imo": "9000002", "mmsi": "200000002", "dwt_tons": "90", "vessel_category": "vessel"}
        )
    db = tmp_path / "sentinel.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE ais_positions (
            id INTEGER PRIMARY KEY, imo TEXT, mmsi TEXT, vessel_name TEXT,
            sog REAL, nav_status TEXT, draft_m REAL, destination TEXT,
            timestamp_utc TEXT, received_at TEXT, lat REAL, lon REAL, cog REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE vf_position_cache (
            imo TEXT PRIMARY KEY, lat REAL, lon REAL, sog REAL, cog REAL,
            nav_status TEXT, draught REAL, fetched_at TEXT, raw_json TEXT
        )
        """
    )
    proven = json.dumps(
        {
            "source": "vesselfinder",
            "endpoint": "https://api.vesselfinder.com/vessels",
            "imo": "9000001",
            "fetched_at": "2026-10-03T12:00:00Z",
            "status_code": 200,
            "billed": True,
        }
    )
    conn.execute(
        "INSERT INTO vf_position_cache VALUES (?,?,?,?,?,?,?,?,?)",
        ("9000001", 25.0, 55.0, 12.0, 90.0, "0", 11.0, "2026-10-03T12:00:00Z", proven),
    )
    conn.execute(
        "INSERT INTO vf_position_cache VALUES (?,?,?,?,?,?,?,?,?)",
        ("9000002", 26.0, 56.0, 10.0, 80.0, "0", 11.0, "2026-10-03T12:00:00Z", None),
    )
    conn.commit()
    conn.close()

    def _flags() -> dict[str, int]:
        c = sqlite3.connect(str(db))
        try:
            rows = c.execute(
                "SELECT imo, vf_verified FROM vessel_daily_archive WHERE snapshot_date=?",
                ("2026-10-03",),
            ).fetchall()
            return {str(imo): int(flag) for imo, flag in rows}
        finally:
            c.close()

    take_daily_snapshot(snapshot_date="2026-10-03", db_path=db, fleet_csv=fleet, export=False, target_n=2)
    first = _flags()
    take_daily_snapshot(snapshot_date="2026-10-03", db_path=db, fleet_csv=fleet, export=False, target_n=2)
    second = _flags()
    assert first == {"9000001": 1, "9000002": 0}
    assert second == first
    c = sqlite3.connect(str(db))
    try:
        n = c.execute(
            "SELECT COUNT(*) FROM vessel_daily_archive WHERE snapshot_date=? AND imo=9000001",
            ("2026-10-03",),
        ).fetchone()[0]
    finally:
        c.close()
    assert n == 1
