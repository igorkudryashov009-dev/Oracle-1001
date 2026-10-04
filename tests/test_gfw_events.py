#!/usr/bin/env python3
"""GFW Events plane — Contract 1.8.0 (no live token required for unit tests)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from services.gfw_events import (
    gfw_status,
    persist_events,
    resolve_token,
    run_daily_gfw_batch,
)


@pytest.fixture(autouse=True)
def _isolate_gfw_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read workstation .env / runtime_env during unit tests."""
    monkeypatch.setattr(
        "services.runtime_env.RUNTIME_ENV_PATH", tmp_path / "runtime_env.json"
    )
    monkeypatch.setattr("services.runtime_env.SIGNAL_PATH", tmp_path / "signal")
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    monkeypatch.delenv("GFW_API_KEY", raising=False)
    monkeypatch.delenv("GLOBAL_FISHING_WATCH_TOKEN", raising=False)


def test_gfw_status_not_configured_without_token() -> None:
    st = gfw_status()
    assert st["configured"] is False
    assert st["provider"] == "global_fishing_watch"
    dumped = json.dumps(st).lower()
    assert "eyj" not in dumped  # no JWT leakage
    assert st.get("token_masked") in (None, "", "****")


def test_run_batch_skips_when_not_configured() -> None:
    out = run_daily_gfw_batch(limit=5, dry_run=False)
    assert out["configured"] is False
    assert out.get("error") == "not_configured"
    assert out["fetched"] == 0


def test_persist_events_sets_flag_without_source_overwrite(tmp_path: Path) -> None:
    db = tmp_path / "sentinel_ais.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE vessel_daily_archive (
            snapshot_date TEXT NOT NULL,
            imo INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'none',
            gfw_verified INTEGER NOT NULL DEFAULT 0,
            gfw_events_n INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (snapshot_date, imo)
        )
        """
    )
    conn.execute(
        "INSERT INTO vessel_daily_archive (snapshot_date, imo, source) VALUES (?,?,?)",
        ("2026-09-24", 9123456, "terrestrial_ais"),
    )
    conn.commit()
    conn.close()

    events = [
        {
            "event_id": "evt-1",
            "event_type": "gap",
            "start_utc": "2026-09-20T00:00:00Z",
            "end_utc": "2026-09-20T06:00:00Z",
            "lat": 1.0,
            "lon": 2.0,
            "raw_json": '{"id":"evt-1","type":"gap"}',
        }
    ]
    pers = persist_events("9123456", events, db_path=db, snapshot_date="2026-09-24")
    assert pers["ok"] is True
    assert pers["inserted"] == 1

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT source, gfw_verified, gfw_events_n FROM vessel_daily_archive WHERE imo=9123456"
    ).fetchone()
    assert row[0] == "terrestrial_ais"  # never overwritten
    assert row[1] == 1
    assert row[2] == 1
    n = conn.execute("SELECT COUNT(*) FROM vessel_gfw_events").fetchone()[0]
    assert n == 1
    raw = conn.execute("SELECT raw_json FROM vessel_gfw_events").fetchone()[0]
    assert "evt-1" in raw
    conn.close()


def test_persist_same_start_does_not_duplicate(tmp_path: Path) -> None:
    db = tmp_path / "sentinel_ais.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE vessel_daily_archive (
            snapshot_date TEXT NOT NULL,
            imo INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'none',
            gfw_verified INTEGER NOT NULL DEFAULT 0,
            gfw_events_n INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (snapshot_date, imo)
        )
        """
    )
    conn.execute(
        "INSERT INTO vessel_daily_archive (snapshot_date, imo, source) VALUES (?,?,?)",
        ("2026-10-02", 9123456, "none"),
    )
    conn.commit()
    conn.close()
    first = {
        "event_id": "evt-a",
        "event_type": "gap",
        "start_utc": "2026-10-01T00:00:00Z",
        "end_utc": "2026-10-01T04:00:00Z",
        "lat": 1.0,
        "lon": 2.0,
        "raw_json": '{"id":"evt-a"}',
    }
    second = dict(first)
    second["event_id"] = "anon-other"
    persist_events("9123456", [first], db_path=db, snapshot_date="2026-10-02")
    persist_events("9123456", [second], db_path=db, snapshot_date="2026-10-02")
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM vessel_gfw_events").fetchone()[0]
    flag = conn.execute(
        "SELECT gfw_verified FROM vessel_daily_archive WHERE snapshot_date='2026-10-02'"
    ).fetchone()[0]
    conn.close()
    assert n == 1
    assert flag == 1


def test_snapshot_rewrite_keeps_gfw_verified(tmp_path: Path) -> None:
    from services.archive_snapshot_worker import take_daily_snapshot

    fleet = tmp_path / "fleet.csv"
    fleet.write_text(
        "vessel_name,imo,mmsi,dwt_tons,vessel_category\nGAP,9000001,200000001,100,vessel\n",
        encoding="utf-8",
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
    conn.commit()
    conn.close()
    take_daily_snapshot(
        snapshot_date="2026-10-03", db_path=db, fleet_csv=fleet, export=False, target_n=1
    )
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE vessel_daily_archive SET gfw_verified=1, gfw_events_n=4 WHERE snapshot_date='2026-10-03'"
    )
    conn.commit()
    conn.close()
    take_daily_snapshot(
        snapshot_date="2026-10-03", db_path=db, fleet_csv=fleet, export=False, target_n=1
    )
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT COUNT(*), MAX(gfw_verified), MAX(gfw_events_n) FROM vessel_daily_archive WHERE snapshot_date='2026-10-03'"
    ).fetchone()
    conn.close()
    assert row == (1, 1, 4)


def test_gap_queue_skips_verified_and_stops_at_daily_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    from services.gfw_events import load_gap48_imos

    monkeypatch.setattr(
        "services.gfw_events._utc_now",
        lambda: datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc),
    )
    db = tmp_path / "sentinel.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE vessel_daily_archive (
            snapshot_date TEXT NOT NULL,
            imo INTEGER NOT NULL,
            gap_hours REAL,
            gfw_verified INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (snapshot_date, imo)
        )
        """
    )
    # 20 already flagged, plus a wider unverified gap that must not be billed again today.
    for i in range(20):
        conn.execute(
            "INSERT INTO vessel_daily_archive (snapshot_date, imo, gap_hours, gfw_verified) VALUES (?,?,?,1)",
            ("2026-10-03", 9000000 + i, 100 + i),
        )
    conn.execute(
        "INSERT INTO vessel_daily_archive (snapshot_date, imo, gap_hours, gfw_verified) VALUES (?,?,?,0)",
        ("2026-10-03", 9000099, 500),
    )
    conn.commit()
    conn.close()
    assert load_gap48_imos(db_path=db, limit=20) == []

    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE vessel_daily_archive SET gfw_verified=0 WHERE imo=9000000")
    conn.commit()
    conn.close()
    # One slot left: the largest unverified gap, not a vessel already flagged.
    assert load_gap48_imos(db_path=db, limit=20) == ["9000099"]


def test_batch_spends_nothing_when_gap_queue_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.gfw_events.resolve_token", lambda: "token-not-a-secret")
    monkeypatch.setattr("services.gfw_events.load_gap48_imos", lambda **kwargs: [])
    calls = {"n": 0}

    def _fetch(*args, **kwargs):
        calls["n"] += 1
        return {"ok": True, "verify": True, "events": []}

    monkeypatch.setattr("services.gfw_events.fetch_events_for_imo", _fetch)
    out = run_daily_gfw_batch(limit=20)
    assert out["planned"] == 0
    assert out["fetched"] == 0
    assert calls["n"] == 0


def test_resolve_token_prefers_gfw_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GFW_API_TOKEN", "abcd1234token")
    monkeypatch.delenv("GFW_API_KEY", raising=False)
    assert resolve_token().endswith("token")
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    assert resolve_token() == ""


def test_sanitize_repairs_duplicated_jwt_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    """15-seg corrupt paste → header.payload.last_sig (3 segs)."""
    from services.gfw_events import sanitize_gfw_token

    hdr = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6ImtpZEtleSJ9"
    pay = "eyJpc3MiOiJnZnciLCJhdWQiOiJnZnciLCJpYXQiOjEsImV4cCI6OTk5fQ"
    sig_bad = "b" * 50
    sig_good = "c" * 40
    # header.payload.sig_bad.payload.sig_bad.payload.sig_good  (7 segs)
    corrupt = ".".join([hdr, pay, sig_bad, pay, sig_bad, pay, sig_good])
    fixed = sanitize_gfw_token(corrupt)
    assert fixed.count(".") == 2
    assert fixed == ".".join([hdr, pay, sig_good])
    assert fixed.endswith(sig_good)
    monkeypatch.setenv("GFW_API_TOKEN", corrupt + "\r")
    assert resolve_token() == fixed


def test_extract_vessel_id_from_v3_identity_shape() -> None:
    from services.gfw_events import _extract_vessel_id_from_entry

    ent = {
        "dataset": "public-global-vessel-identity:v4.0",
        "combinedSourcesInfo": [{"vesselId": "abc-vessel-1", "geartypes": []}],
        "selfReportedInfo": [{"id": "abc-vessel-1", "imo": "9905980", "shipname": "X"}],
    }
    assert _extract_vessel_id_from_entry(ent, imo="9905980") == "abc-vessel-1"
    assert _extract_vessel_id_from_entry({"id": "top"}, imo="1") == "top"


def test_auth_401_does_not_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 on vessel search must not flag gfw_verified / must not look like success."""
    monkeypatch.setenv("GFW_API_TOKEN", "deadtoken_for_unit_test_xxxx")

    def fake_resolve(imo, token, *, timeout=30.0):
        return None, "auth_http_401"

    monkeypatch.setattr("services.gfw_events._resolve_vessel_id", fake_resolve)
    from services.gfw_events import fetch_events_for_imo

    out = fetch_events_for_imo("9388819", use_cache=False)
    assert out["ok"] is False
    assert out.get("auth_failed") is True
    assert out.get("error") == "auth_http_401"