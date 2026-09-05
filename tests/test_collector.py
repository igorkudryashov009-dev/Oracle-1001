"""Unit tests for collector.py — no live AISstream network."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

import collector as col


def test_next_backoff_sequence_default():
    assert col.next_backoff(None) == 2.0
    assert col.next_backoff(2.0) == 4.0
    assert col.next_backoff(4.0) == 8.0
    assert col.next_backoff(16.0) == 30.0
    assert col.next_backoff(30.0) == 30.0


def test_next_backoff_respects_custom_caps():
    assert col.next_backoff(None, initial=1.0, maximum=10.0) == 1.0
    assert col.next_backoff(1.0, initial=1.0, maximum=10.0) == 2.0
    assert col.next_backoff(8.0, initial=1.0, maximum=10.0) == 10.0


def test_open_db_enables_wal_and_busy_timeout(tmp_path: Path):
    db = tmp_path / "raw_positions.db"
    conn = col.open_db(db, timeout_seconds=15.0)
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert journal.lower() == "wal"
        assert busy == 15000
    finally:
        conn.close()


def test_open_db_creates_positions_table(tmp_path: Path):
    db = tmp_path / "raw_positions.db"
    conn = col.open_db(db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "positions" in tables
    finally:
        conn.close()


def test_insert_position_idempotent(tmp_path: Path):
    db = tmp_path / "raw_positions.db"
    conn = col.open_db(db)
    row = {
        "imo": "9123456",
        "mmsi": "123456789",
        "timestamp_utc": "2026-08-12T10:00:00Z",
        "lat": 1.0,
        "lon": 2.0,
        "speed_knots": 10.0,
        "heading": 90.0,
        "nav_status": "Under way using engine",
        "received_at": "2026-08-12T10:00:01Z",
    }
    try:
        assert col.insert_position(conn, row) is True
        assert col.insert_position(conn, row) is False
        count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_load_aisstream_settings_merges_config(tmp_path: Path):
    cfg = {
        "aisstream": {
            "stagger_seconds": 3.0,
            "reconnect_backoff_max_seconds": 25,
            "sqlite_timeout_seconds": 20,
        },
        "paths": {"db": "custom/db.sqlite"},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    settings = col.load_aisstream_settings(cfg_path)
    assert settings["stagger_seconds"] == 3.0
    assert settings["reconnect_backoff_max_seconds"] == 25
    assert settings["sqlite_timeout_seconds"] == 20
    assert settings["db_path"] == "custom/db.sqlite"


def test_write_unreachable_report_honest_degradation(tmp_path: Path, monkeypatch):
    unreachable = tmp_path / "collector_unreachable_mmsi.json"
    monkeypatch.setattr(col, "HISTORY", tmp_path)
    monkeypatch.setattr(col, "UNREACHABLE_PATH", unreachable)

    batches = [["111", "222"], ["333"]]
    stats = {
        "failed_batches": {
            1: {
                "mmsi_batch": ["333"],
                "consecutive_failures": 3,
                "status": "unreachable_this_session",
                "last_error": "ConnectionRefusedError()",
            }
        },
        "unreachable_threshold": 3,
    }
    col.write_unreachable_report(batches, stats)
    assert unreachable.exists()
    payload = json.loads(unreachable.read_text(encoding="utf-8"))
    assert payload["honest_degradation"] is True
    assert payload["unreachable_batch_count"] == 1
    assert payload["total_batches"] == 2
    assert payload["unreachable_mmsi_count"] == 1
    assert payload["batches"]["1"]["status"] == "unreachable_this_session"


def test_parse_position_valid_message():
    mmsi_to_imo = {"123456789": "9123456"}
    msg = {
        "MessageType": "PositionReport",
        "Metadata": {"MMSI": "123456789"},
        "Message": {
            "PositionReport": {
                "UserID": 123456789,
                "Latitude": 12.34,
                "Longitude": 56.78,
                "Sog": 10.5,
                "TrueHeading": 180,
                "NavigationalStatus": 0,
            }
        },
    }
    row = col.parse_position(msg, mmsi_to_imo)
    assert row is not None
    assert row["imo"] == "9123456"
    assert row["mmsi"] == "123456789"
    assert row["lat"] == 12.34


def test_batch_worker_stops_after_unreachable_handshake_failures(
    tmp_path: Path, monkeypatch
):
    """Three handshake failures → UNREACHABLE, report written, worker exits (no crash)."""

    async def _run() -> None:
        db = tmp_path / "raw_positions.db"
        log_calls: list[str] = []

        def _capture_log(msg: str) -> None:
            log_calls.append(msg)

        monkeypatch.setattr(col, "log", _capture_log)
        monkeypatch.setattr(col, "HISTORY", tmp_path)
        monkeypatch.setattr(col, "UNREACHABLE_PATH", tmp_path / "unreachable.json")

        connect_mock = AsyncMock(side_effect=ConnectionRefusedError("API down"))
        sleep_mock = AsyncMock()

        stats: dict = {"messages": 0, "inserted": 0, "failed_batches": {}}
        stop_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        with patch.object(col.websockets, "connect", connect_mock):
            with patch.object(asyncio, "sleep", sleep_mock):
                await col.batch_worker(
                    batch_id=0,
                    mmsi_batch=["111111111", "222222222"],
                    api_key="test-key",
                    mmsi_to_imo={"111111111": "9100001"},
                    db_path=db,
                    stop_at=stop_at,
                    stats=stats,
                    stagger_seconds=0,
                    handshake_sem=asyncio.Semaphore(5),
                    db_write_lock=asyncio.Lock(),
                    all_batches=[["111111111", "222222222"]],
                    unreachable_threshold=3,
                    backoff_initial=0.01,
                    backoff_max=0.04,
                )

        assert connect_mock.call_count == 3
        assert stats["failed_batches"][0]["status"] == "unreachable_this_session"
        assert (tmp_path / "unreachable.json").exists()
        unreachable_logs = [m for m in log_calls if "UNREACHABLE" in m]
        assert len(unreachable_logs) == 1

    asyncio.run(_run())


def test_batch_worker_post_subscribe_disconnect_uses_backoff(
    tmp_path: Path, monkeypatch
):
    """Post-subscribe disconnect applies 2s initial backoff (not handshake unreachable)."""

    async def _run() -> None:
        db = tmp_path / "raw_positions.db"
        monkeypatch.setattr(col, "log", lambda msg: None)
        monkeypatch.setattr(col, "HISTORY", tmp_path)

        sleep_delays: list[float] = []

        async def capture_sleep(delay: float) -> None:
            sleep_delays.append(delay)
            if any(d >= 2.0 for d in sleep_delays):
                raise asyncio.CancelledError()

        class FakeWS:
            async def recv(self) -> str:
                raise asyncio.TimeoutError()

            async def send(self, _payload: str) -> None:
                return None

            async def close(self) -> None:
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("disconnect after subscribe")

        connect_mock = AsyncMock(return_value=FakeWS())

        stats: dict = {"messages": 0, "inserted": 0, "failed_batches": {}}
        stop_at = datetime.now(timezone.utc) + timedelta(minutes=1)

        with patch.object(col.websockets, "connect", connect_mock):
            with patch.object(asyncio, "sleep", side_effect=capture_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await col.batch_worker(
                    batch_id=0,
                    mmsi_batch=["111111111"],
                    api_key="test-key",
                    mmsi_to_imo={"111111111": "9100001"},
                    db_path=db,
                    stop_at=stop_at,
                    stats=stats,
                    stagger_seconds=0,
                    handshake_sem=asyncio.Semaphore(5),
                    db_write_lock=asyncio.Lock(),
                    all_batches=[["111111111"]],
                    backoff_initial=2.0,
                    backoff_max=30.0,
                )

        backoff_delays = [d for d in sleep_delays if d >= 2.0]
        assert backoff_delays[0] == 2.0
        assert 0 not in stats.get("failed_batches", {})

    asyncio.run(_run())


def test_concurrent_db_writes_no_crash(tmp_path: Path, monkeypatch):
    """Two writers inserting concurrently — WAL + lock should not raise."""

    async def _run() -> None:
        db = tmp_path / "raw_positions.db"
        monkeypatch.setattr(col, "log", lambda msg: None)

        lock = asyncio.Lock()
        conn_a = col.open_db(db)
        conn_b = col.open_db(db)
        row_a = {
            "imo": "9100001",
            "mmsi": "111111111",
            "timestamp_utc": "2026-08-12T10:00:00Z",
            "lat": 1.0,
            "lon": 2.0,
            "speed_knots": None,
            "heading": None,
            "nav_status": None,
            "received_at": "2026-08-12T10:00:01Z",
        }
        row_b = {
            **row_a,
            "mmsi": "222222222",
            "imo": "9100002",
            "timestamp_utc": "2026-08-12T10:00:02Z",
        }

        async def writer(conn, row):
            for i in range(20):
                await col.safe_insert_position(
                    conn,
                    {**row, "timestamp_utc": f"2026-08-12T10:00:{i:02d}Z"},
                    lock,
                )

        await asyncio.gather(writer(conn_a, row_a), writer(conn_b, row_b))
        conn_a.close()
        conn_b.close()
        verify = col.open_db(db)
        count = verify.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        verify.close()
        assert count == 40

    asyncio.run(_run())


def test_run_collector_exits_without_api_key(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AISSTREAM_API_KEY", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("aisstream: {}\npaths: {}\n", encoding="utf-8")
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps({"targets": [{"imo": "1", "mmsi": "111"}]}), encoding="utf-8")
    monkeypatch.setattr(col, "TARGETS_PATH", targets)

    with pytest.raises(SystemExit) as exc:
        asyncio.run(
            col.run_collector(
                test_minutes=0.01,
                max_batches=1,
                config_path=cfg,
            )
        )
    assert exc.value.code == 1
