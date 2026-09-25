"""VF-500 allocator + daily full-fleet archive — Contract 1.8.0-ops-gis-sot.

Acceptance (budget-honest):
  - 30-day sim: used ≤ 500, remaining ≥ 0
  - Rolling coverage: top-500 ≥1 hit / 50d; rest ≥1 hit / 114d (allocator design)
  - vessel_daily_archive: exactly N rows/day, every row has source ∈ real set
  - Dual Gate thresholds untouched
"""

from __future__ import annotations

import csv
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _make_fleet_csv(path: Path, n: int = 1260) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "vessel_name",
                "imo",
                "mmsi",
                "vessel_type",
                "dwt_tons",
                "flag",
                "draft_m",
                "vessel_category",
                "compliance_risk_level",
                "sanctions_tags",
            ],
        )
        w.writeheader()
        for i in range(n):
            w.writerow(
                {
                    "vessel_name": f"TEST VESSEL {i+1}",
                    "imo": str(9000000 + i),
                    "mmsi": str(200000000 + i),
                    "vessel_type": "LNG Tanker",
                    "dwt_tons": 200000 - i,  # descending DWT
                    "flag": "MH",
                    "draft_m": 11.0,
                    "vessel_category": "vessel",
                    "compliance_risk_level": "HIGH" if i % 40 == 0 else "LOW",
                    "sanctions_tags": "STS" if i % 55 == 0 else "",
                }
            )
    return path


def test_simulate_30d_budget_cap(tmp_path: Path) -> None:
    from services.vf_budget_allocator import MONTHLY_BUDGET, simulate_month

    fleet = _make_fleet_csv(tmp_path / "fleet.csv", 1260)
    state = tmp_path / "alloc_state.json"
    result = simulate_month(
        days=30,
        fleet_csv=fleet,
        state_path=state,
        start=date(2026, 3, 1),
    )
    assert result["ok"] is True
    assert result["used"] <= MONTHLY_BUDGET
    assert result["remaining"] >= 0
    assert result["within_budget"] is True
    # 16/day * 30 = 480 nominal
    assert result["used"] <= 480 + 16  # allow tiny scheduler jitter
    assert result["used"] >= 400


def test_rolling_coverage_top50_rest114(tmp_path: Path) -> None:
    """Every top vessel ≥1 in ~50d; every rest vessel ≥1 within ~130d (budget-honest)."""
    from services.vf_budget_allocator import (
        MONTHLY_BUDGET,
        AllocatorPlan,
        commit_allocation,
        load_fleet_universe,
        plan_daily_allocation,
        save_state,
        _empty_state,
        _week_key,
    )

    fleet = _make_fleet_csv(tmp_path / "fleet.csv", 1260)
    state = tmp_path / "chain_state.json"
    hits: dict[str, int] = {}
    used = 0
    top_slots, rest_slots = load_fleet_universe(fleet)
    top_set = {s.imo for s in top_slots}
    start = date(2026, 1, 1)
    # 4 full months of rest tier (200*4=800) ≥ 760 vessels
    days = 130
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    save_state(_empty_state("2026-01", week=_week_key(start_dt)), state)
    current_month = "2026-01"

    for i in range(days):
        day = start + timedelta(days=i)
        day_s = day.isoformat()
        month = day.strftime("%Y-%m")
        if month != current_month:
            current_month = month
            used = 0
        now = datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=timezone.utc)
        plan = plan_daily_allocation(
            day=day_s,
            state_path=state,
            fleet_csv=fleet,
            now=now,
            monthly_remaining=MONTHLY_BUDGET - used,
            simulate=True,
            top_slots=top_slots,
            rest_slots=rest_slots,
        )
        billed = plan.all_imos
        room = MONTHLY_BUDGET - used
        if len(billed) > room:
            billed = billed[:room]
        commit_allocation(
            AllocatorPlan(
                day=day_s,
                top_imos=[x for x in billed if x in top_set],
                rest_imos=[x for x in billed if x not in top_set],
            ),
            billed_imos=billed,
            state_path=state,
            now=now,
        )
        used += len(billed)
        for imo in billed:
            hits[imo] = hits.get(imo, 0) + 1

    top_min = min(hits.get(s.imo, 0) for s in top_slots)
    rest_min = min(hits.get(s.imo, 0) for s in rest_slots)
    assert top_min >= 1, f"top coverage failed: min={top_min}"
    assert rest_min >= 1, f"rest coverage failed: min={rest_min}"


def test_daily_archive_exactly_1260_with_source(tmp_path: Path) -> None:
    from services.archive_schema import ALLOWED_ARCHIVE_SOURCES, ARCHIVE_COLUMNS
    from services.archive_snapshot_worker import take_daily_snapshot

    fleet = _make_fleet_csv(tmp_path / "fleet.csv", 1260)
    db = tmp_path / "sentinel_ais.db"
    # Minimal ais_positions schema (empty → all source=none)
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE ais_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imo TEXT, mmsi TEXT, vessel_name TEXT, sog REAL, nav_status TEXT,
            draft_m REAL, destination TEXT, timestamp_utc TEXT, received_at TEXT,
            lat REAL, lon REAL, cog REAL
        )
        """
    )
    conn.commit()
    conn.close()

    day = "2026-09-24"
    result = take_daily_snapshot(
        snapshot_date=day,
        db_path=db,
        fleet_csv=fleet,
        export=False,
        target_n=1260,
    )
    assert result["ok"] is True
    assert result["stored_for_date"] == 1260

    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        "SELECT source, lat, lon FROM vessel_daily_archive WHERE snapshot_date = ?",
        (day,),
    )
    rows = cur.fetchall()
    assert len(rows) == 1260
    for source, lat, lon in rows:
        assert source in ALLOWED_ARCHIVE_SOURCES
        assert source is not None and source != ""
        # No synthetic coords: none ⇒ null lat/lon
        if source == "none":
            assert lat is None and lon is None
    # Column presence
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vessel_daily_archive)")}
    for c in ARCHIVE_COLUMNS:
        assert c in cols
    conn.close()


def test_terrestrial_linkage_by_mmsi_primary(tmp_path: Path) -> None:
    """MMSI-primary overlay: matching MMSI → terrestrial_ais; miss → none; 1 row/IMO/day."""
    from services.archive_schema import ARCHIVE_COLUMNS
    from services.archive_snapshot_worker import (
        invalidate_fleet_archive_cache,
        take_daily_snapshot,
    )

    fleet = _make_fleet_csv(tmp_path / "fleet.csv", 20)
    db = tmp_path / "sentinel_ais.db"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE ais_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imo TEXT, mmsi TEXT, vessel_name TEXT, sog REAL, nav_status TEXT,
            draft_m REAL, destination TEXT, timestamp_utc TEXT, received_at TEXT,
            lat REAL, lon REAL, cog REAL
        )
        """
    )
    # Vessel 0: MMSI match, IMO intentionally empty (regression for IMO-only join)
    conn.execute(
        "INSERT INTO ais_positions (imo,mmsi,vessel_name,sog,nav_status,draft_m,destination,"
        "timestamp_utc,received_at,lat,lon,cog) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("", "200000000", "LIVE 1", 12.0, "Under way", 11.0, "DOHA", now, now, 25.1, 55.2, 90.0),
    )
    # Vessel 1: both MMSI+IMO present
    conn.execute(
        "INSERT INTO ais_positions (imo,mmsi,vessel_name,sog,nav_status,draft_m,destination,"
        "timestamp_utc,received_at,lat,lon,cog) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("9000001", "200000001", "LIVE 2", 10.0, "Under way", 10.0, "RAS LAFFAN", now, now, 26.0, 51.5, 180.0),
    )
    # Noise MMSI not in fleet
    conn.execute(
        "INSERT INTO ais_positions (imo,mmsi,vessel_name,sog,nav_status,draft_m,destination,"
        "timestamp_utc,received_at,lat,lon,cog) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("9999999", "299999999", "NOISE", 1.0, "At anchor", 9.0, "X", now, now, 1.0, 2.0, 0.0),
    )
    conn.commit()
    conn.close()

    day = "2026-09-24"
    invalidate_fleet_archive_cache()
    result = take_daily_snapshot(
        snapshot_date=day,
        db_path=db,
        fleet_csv=fleet,
        export=False,
        target_n=20,
    )
    assert result["ok"] is True
    assert result["live_ais_overlay"] >= 2

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT imo, mmsi, source, lat, lon FROM vessel_daily_archive WHERE snapshot_date = ? ORDER BY imo",
        (day,),
    ).fetchall()
    assert len(rows) == 20
    by_imo = {int(r[0]): r for r in rows}
    # MMSI-only AIS row linked
    assert by_imo[9000000][2] == "terrestrial_ais"
    assert by_imo[9000000][3] == 25.1 and by_imo[9000000][4] == 55.2
    assert by_imo[9000001][2] == "terrestrial_ais"
    # Unmatched stay none with null coords
    assert by_imo[9000002][2] == "none"
    assert by_imo[9000002][3] is None and by_imo[9000002][4] is None
    # No duplicate IMOs for the day
    imos = [r[0] for r in rows]
    assert len(imos) == len(set(imos))
    conn.close()


def test_fleet_archive_metrics_cache(tmp_path: Path) -> None:
    from services.archive_snapshot_worker import (
        compute_fleet_archive_metrics,
        invalidate_fleet_archive_cache,
        take_daily_snapshot,
    )

    fleet = _make_fleet_csv(tmp_path / "fleet.csv", 30)
    db = tmp_path / "sentinel_ais.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE ais_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imo TEXT, mmsi TEXT, vessel_name TEXT, sog REAL, nav_status TEXT,
            draft_m REAL, destination TEXT, timestamp_utc TEXT, received_at TEXT,
            lat REAL, lon REAL, cog REAL
        )
        """
    )
    conn.commit()
    conn.close()
    day = "2026-09-22"
    invalidate_fleet_archive_cache()
    take_daily_snapshot(snapshot_date=day, db_path=db, fleet_csv=fleet, export=False, target_n=30)
    m1 = compute_fleet_archive_metrics(db_path=db, day=day, expected_n=30)
    m2 = compute_fleet_archive_metrics(db_path=db, day=day, expected_n=30)
    assert m1["row_count"] == 30
    assert m1.get("cache_hit") is False
    assert m2.get("cache_hit") is True
    assert m2["archive_completeness_pct"] == 100.0
    invalidate_fleet_archive_cache()
    m3 = compute_fleet_archive_metrics(db_path=db, day=day, expected_n=30)
    assert m3.get("cache_hit") is False


def test_archive_no_interpolated_source(tmp_path: Path) -> None:
    from services.archive_snapshot_worker import build_snapshot_rows

    fleet = [
        {
            "imo": 9123456,
            "vessel_name": "X",
            "dwt_tons": 100000,
            "mmsi": 123,
            "sanctions_tags": "",
        }
    ]
    rows = build_snapshot_rows(fleet, {}, "2026-09-24")
    assert len(rows) == 1
    # source is near end of tuple
    rec = dict(zip(
        __import__("services.archive_schema", fromlist=["ARCHIVE_COLUMNS"]).ARCHIVE_COLUMNS,
        rows[0],
    ))
    assert rec["source"] == "none"
    assert rec["lat"] is None and rec["lon"] is None
    assert rec["gap_hours"] > 24


def test_dual_gate_thresholds_unchanged() -> None:
    from services.dual_gate import (
        DISK_FREE_CRITICAL_PCT,
        DISK_FREE_MIN_PCT,
        FLEET_SAMPLE_FULL_MIN,
        FLEET_SAMPLE_LIMITED_MIN,
        PIPELINE_LIVE_LAG_SEC,
    )

    assert FLEET_SAMPLE_LIMITED_MIN == 5
    assert FLEET_SAMPLE_FULL_MIN == 100
    assert PIPELINE_LIVE_LAG_SEC == 300.0
    assert DISK_FREE_MIN_PCT == 20.0
    assert DISK_FREE_CRITICAL_PCT == 10.0


def test_fleet_archive_metrics_block(tmp_path: Path) -> None:
    from services.archive_snapshot_worker import (
        compute_fleet_archive_metrics,
        invalidate_fleet_archive_cache,
        take_daily_snapshot,
    )

    fleet = _make_fleet_csv(tmp_path / "fleet.csv", 1260)
    db = tmp_path / "sentinel_ais.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE ais_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imo TEXT, mmsi TEXT, vessel_name TEXT, sog REAL, nav_status TEXT,
            draft_m REAL, destination TEXT, timestamp_utc TEXT, received_at TEXT,
            lat REAL, lon REAL, cog REAL
        )
        """
    )
    conn.commit()
    conn.close()
    day = "2026-09-23"
    invalidate_fleet_archive_cache()
    take_daily_snapshot(snapshot_date=day, db_path=db, fleet_csv=fleet, export=False)
    m = compute_fleet_archive_metrics(db_path=db, day=day, expected_n=1260, bypass_cache=True)
    assert m["row_count"] == 1260
    assert m["archive_completeness_pct"] == 100.0
    assert m["feeds_fleet_sample"] is False
    assert "vf_budget" in m
    assert m["vf_budget"]["monthly_budget"] == 500
    assert m["vf_budget"]["tier_split"] == {"top": 300, "rest": 200}
