#!/usr/bin/env python3
"""GFW Events plane — Contract 1.8.0 (no live token required for unit tests)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from services.gfw_events import (
    gfw_status,
    persist_events,
    resolve_token,
    run_daily_gfw_batch,
)


def test_gfw_status_not_configured_without_token(monkeypatch) -> None:
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    monkeypatch.delenv("GFW_API_KEY", raising=False)
    monkeypatch.delenv("GLOBAL_FISHING_WATCH_TOKEN", raising=False)
    st = gfw_status()
    assert st["configured"] is False
    assert st["provider"] == "global_fishing_watch"
    assert "token" not in json.dumps(st).lower() or st.get("token_masked") in (None, "")


def test_run_batch_skips_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    monkeypatch.delenv("GFW_API_KEY", raising=False)
    monkeypatch.delenv("GLOBAL_FISHING_WATCH_TOKEN", raising=False)
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


def test_resolve_token_prefers_gfw_api_token(monkeypatch) -> None:
    monkeypatch.setenv("GFW_API_TOKEN", "abcd1234token")
    monkeypatch.delenv("GFW_API_KEY", raising=False)
    assert resolve_token().endswith("token")
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    assert resolve_token() == ""
