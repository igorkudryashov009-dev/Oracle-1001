"""Verification components read the settled archive day, not today's open slice."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "arch.db"))
    conn.execute(
        """
        CREATE TABLE vessel_daily_archive (
            snapshot_date TEXT NOT NULL,
            imo INTEGER NOT NULL,
            source TEXT,
            gap_hours REAL,
            gfw_verified INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return conn


def _score(tmp_path: Path, when: datetime) -> dict:
    from services.ops_contour import build_ops_contour, reset_cache
    import services.ops_contour as contour

    reset_cache()
    original = contour._now_utc
    contour._now_utc = lambda: when
    try:
        return build_ops_contour()
    finally:
        contour._now_utc = original
        reset_cache()


def test_today_zero_does_not_hide_yesterdays_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _db(tmp_path)
    rows = [("2026-10-02", i, "terrestrial_ais", 80, 1) for i in range(1, 21)]
    rows.append(("2026-10-03", 9001, "vf_api", 90, 0))
    rows.append(("2026-10-03", 9002, "satellite_ais", 90, 0))
    conn.executemany(
        "INSERT INTO vessel_daily_archive VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "arch.db"))
    block = _score(tmp_path, datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc))
    facts = block["facts"]
    assert facts["settled_snapshot_date"] == "2026-10-02"
    assert facts["gfw_verified_n"] == 20
    assert facts["vf_verified_n"] == 0
    assert facts["satellite_verified_n"] == 0
    assert block["readiness"]["components"]["gfw"] > 0
    assert block["archive"]["snapshot_date"] == "2026-10-03"


def test_missing_settled_slice_scores_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO vessel_daily_archive VALUES (?,?,?,?,?)",
        ("2026-10-03", 1, "terrestrial_ais", 10, 1),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "arch.db"))
    block = _score(tmp_path, datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc))
    facts = block["facts"]
    assert facts["settled_snapshot_date"] is None
    assert facts["gfw_verified_n"] == 0
    assert facts["vf_verified_n"] == 0
    assert facts["satellite_verified_n"] == 0
    assert block["readiness"]["components"]["gfw"] == 0
    assert block["readiness"]["components"]["vf"] == 0
    assert block["readiness"]["components"]["satellite"] == 0


def test_just_after_midnight_uses_flagged_yesterday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _db(tmp_path)
    conn.executemany(
        "INSERT INTO vessel_daily_archive VALUES (?,?,?,?,?)",
        [
            ("2026-10-03", i, "none", 100, 1)
            for i in range(1, 21)
        ]
        + [("2026-10-04", 1, "none", 100, 0)],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "arch.db"))
    block = _score(tmp_path, datetime(2026, 10, 4, 0, 45, tzinfo=timezone.utc))
    assert block["facts"]["settled_snapshot_date"] == "2026-10-03"
    assert block["facts"]["gfw_verified_n"] == 20
    assert block["readiness"]["components"]["gfw"] > 0
    assert block["archive"]["snapshot_date"] == "2026-10-04"


def test_utc_midnight_boundary_is_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db(tmp_path)
    conn.executemany(
        "INSERT INTO vessel_daily_archive VALUES (?,?,?,?,?)",
        [
            ("2026-10-02", 1, "none", 50, 1),
            ("2026-10-03", 1, "none", 50, 0),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "arch.db"))
    before = _score(tmp_path, datetime(2026, 10, 3, 23, 59, 59, tzinfo=timezone.utc))
    after = _score(tmp_path, datetime(2026, 10, 4, 0, 0, 0, tzinfo=timezone.utc))
    assert before["facts"]["settled_snapshot_date"] == "2026-10-02"
    assert before["facts"]["gfw_verified_n"] == 1
    assert before["readiness"]["components"]["gfw"] > 0
    assert after["facts"]["settled_snapshot_date"] == "2026-10-03"
    assert after["facts"]["gfw_verified_n"] == 0
    assert after["readiness"]["components"]["gfw"] == 0
