"""Unit tests for AISStream connector parser and Sentinel payload generators."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_assign_strategic_tier():
    from services.fleet_registry import assign_strategic_tier

    assert assign_strategic_tier(1) == "ALPHA"
    assert assign_strategic_tier(100) == "ALPHA"
    assert assign_strategic_tier(101) == "BRAVO"
    assert assign_strategic_tier(300) == "BRAVO"
    assert assign_strategic_tier(301) == "CHARLIE"
    assert assign_strategic_tier(601) == "DELTA"
    assert assign_strategic_tier(150, "EXTREME") == "ALPHA"


def test_fleet_registry_loads_and_tiers():
    from services.fleet_registry import FleetRegistry

    fleet = ROOT / "output" / "fleet_database.csv"
    if not fleet.exists():
        pytest.skip("fleet_database.csv missing")
    reg = FleetRegistry.from_csv(fleet, top_n=1001)
    assert len(reg.vessels) > 100
    counts = reg.tier_counts()
    assert counts["ALPHA"] > 0
    assert sum(counts.values()) == len(reg.vessels)
    sample = reg.vessels[0]
    assert reg.match(mmsi=sample.mmsi) is sample


def test_parse_position_report_matches_registry(tmp_path):
    from services.aisstream_connector import AISStreamConnector
    from services.fleet_registry import FleetRegistry
    from services.storage import AISStorage

    fleet = ROOT / "output" / "fleet_database.csv"
    if not fleet.exists():
        pytest.skip("fleet_database.csv missing")
    reg = FleetRegistry.from_csv(fleet, top_n=50)
    target = reg.vessels[0]
    storage = AISStorage(sqlite_path=tmp_path / "t.db")
    # Avoid requiring API key in constructor path used by parse only
    connector = object.__new__(AISStreamConnector)
    connector.registry = reg
    connector.storage = storage
    connector._static_cache = {}
    connector._last_positions = {}

    msg = {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": int(target.mmsi), "time_utc": "2026-09-05 06:00:00.000000000 +0000 UTC"},
        "Metadata": {"MMSI": target.mmsi, "time_utc": "2026-09-05T06:00:00Z"},
        "Message": {
            "PositionReport": {
                "UserID": int(target.mmsi),
                "Latitude": 1.25,
                "Longitude": 103.85,
                "Sog": 12.4,
                "Cog": 85.0,
                "TrueHeading": 86,
                "NavigationalStatus": 0,
            }
        },
    }
    row = AISStreamConnector.parse_message(connector, msg)
    assert row is not None
    assert row["matched"] is True
    assert row["tier"] == target.tier
    assert row["mmsi"] == target.mmsi
    assert abs(row["lat"] - 1.25) < 1e-6
    assert abs(row["sog"] - 12.4) < 1e-6


def test_parse_ship_static_data_enriches_cache(tmp_path):
    from services.aisstream_connector import AISStreamConnector
    from services.fleet_registry import FleetRegistry
    from services.storage import AISStorage

    fleet = ROOT / "output" / "fleet_database.csv"
    if not fleet.exists():
        pytest.skip("fleet_database.csv missing")
    reg = FleetRegistry.from_csv(fleet, top_n=20)
    target = reg.vessels[0]
    connector = object.__new__(AISStreamConnector)
    connector.registry = reg
    connector.storage = AISStorage(sqlite_path=tmp_path / "t.db")
    connector._static_cache = {}
    connector._last_positions = {
        target.mmsi: {
            "imo": target.imo,
            "mmsi": target.mmsi,
            "lat": 10.0,
            "lon": 20.0,
            "matched": True,
            "tier": target.tier,
        }
    }

    msg = {
        "MessageType": "ShipStaticData",
        "Metadata": {"MMSI": target.mmsi},
        "Message": {
            "ShipStaticData": {
                "UserID": int(target.mmsi),
                "ImoNumber": int(target.imo) if str(target.imo).isdigit() else 0,
                "Name": target.vessel_name,
                "MaximumStaticDraught": 11.4,
                "Destination": "SINGAPORE",
                "Dimension": {"A": 200, "B": 50, "C": 20, "D": 20},
            }
        },
    }
    row = AISStreamConnector.parse_message(connector, msg)
    assert target.mmsi in connector._static_cache
    assert connector._static_cache[target.mmsi]["draft_m"] == 11.4
    assert row is not None
    assert row["destination"] == "SINGAPORE"


def test_chokepoint_detection():
    from services.chokepoints import detect_chokepoint, in_sanctioned_zone

    assert detect_chokepoint(1.3, 103.8).id == "malacca"
    assert detect_chokepoint(30.0, 32.5).id == "suez"
    assert detect_chokepoint(0.0, 0.0) is None
    assert in_sanctioned_zone(44.0, 35.0) is not None


def test_storage_batch_insert(tmp_path):
    import asyncio

    from services.storage import AISStorage

    store = AISStorage(sqlite_path=tmp_path / "ais.db", batch_size=10, flush_interval_sec=0.2)

    async def _run():
        await store.open_async()
        for i in range(5):
            await store.enqueue({
                "imo": f"100000{i}",
                "mmsi": f"20000000{i}",
                "vessel_name": f"T{i}",
                "tier": "ALPHA",
                "timestamp_utc": f"2026-09-05T06:0{i}:00Z",
                "lat": 1.0 + i * 0.01,
                "lon": 100.0,
                "sog": 10.0,
                "cog": 90.0,
                "heading": 90.0,
                "nav_status": "Under way using engine",
                "draft_m": 11.0,
                "destination": "TEST",
                "matched": True,
                "message_type": "PositionReport",
                "received_at": "2026-09-05T06:00:00Z",
            })
        n = await store.flush()
        assert n >= 5
        rows = store.fetch_recent(limit=10, matched_only=True)
        assert len(rows) >= 5
        store.write_telemetry({
            "mps": 12.5,
            "matched_count": 5,
            "unmatched_count": 100,
            "insert_latency_ms": 3.2,
            "ws_uptime_sec": 60,
            "reconnects": 0,
            "heartbeat_ok": True,
        })
        tel = store.fetch_telemetry(limit=5)
        assert tel and float(tel[0]["mps"]) == 12.5
        await store.close()

    asyncio.run(_run())


def test_sentinel_payload_has_18_chart_keys():
    from services.sentinel_analytics import build_sentinel_payload

    fleet = ROOT / "output" / "fleet_database.csv"
    if not fleet.exists():
        pytest.skip("fleet_database.csv missing")
    payload = build_sentinel_payload(use_synthetic_if_empty=True)
    required = [
        "c01_heatmap", "c02_chokepoints", "c03_sts_clusters", "c04_alpha_tracks", "c05_port_matrix",
        "c06_dark_timeline", "c07_draft_scatter", "c08_speed_spectrum", "c09_course_deviation", "c10_spoofing_radar",
        "c11_tonnage_transit", "c12_flag_tree", "c13_risk_radar", "c14_geo_exposure",
        "c15_mps", "c16_latency", "c17_ws_health", "c18_match_efficiency",
    ]
    for key in required:
        assert key in payload, f"missing {key}"
    assert payload["live_vessel_count"] > 0
    assert len(payload["c01_heatmap"]) > 0
    assert len(payload["c08_speed_spectrum"]["labels"]) == 7


def test_backoff_curve():
    from services.aisstream_connector import next_backoff

    assert next_backoff(None) == 1.0
    assert next_backoff(1.0) == 2.0
    assert next_backoff(2.0) == 4.0
    assert next_backoff(4.0) == 8.0
    assert next_backoff(40.0, maximum=30.0) == 30.0
    assert next_backoff(32.0, maximum=60.0) == 60.0


def test_rate_limit_backoff_starts_higher():
    from services.aisstream_connector import next_backoff

    assert next_backoff(None, initial=30.0, maximum=300.0) == 30.0
    assert next_backoff(30.0, initial=30.0, maximum=300.0) == 60.0
    assert next_backoff(240.0, initial=30.0, maximum=300.0) == 300.0


def test_chunk_plan_rotation_for_top500():
    from services.aisstream_connector import AISStreamConnector, DOC_MMSI_PER_SUBSCRIPTION

    connector = object.__new__(AISStreamConnector)
    connector.settings = {
        "mmsi_per_subscription": DOC_MMSI_PER_SUBSCRIPTION,
        "rotation_interval_seconds": 180.0,
    }
    mmsis = [f"{i:09d}" for i in range(500)]
    strategy, chunks, window = AISStreamConnector._chunk_plan(connector, mmsis)
    assert strategy == "rotation"
    assert len(chunks) == 3
    assert all(len(c) <= 200 for c in chunks)
    assert window == 540.0


def test_chunk_plan_legacy_240_interval_still_maths():
    """Unit math only — production config uses 180s (Prompt-7)."""
    from services.aisstream_connector import AISStreamConnector, DOC_MMSI_PER_SUBSCRIPTION

    connector = object.__new__(AISStreamConnector)
    connector.settings = {
        "mmsi_per_subscription": DOC_MMSI_PER_SUBSCRIPTION,
        "rotation_interval_seconds": 240.0,
    }
    mmsis = [f"{i:09d}" for i in range(500)]
    _strategy, _chunks, window = AISStreamConnector._chunk_plan(connector, mmsis)
    assert window == 720.0


def test_chunk_plan_full_list_under_cap():
    from services.aisstream_connector import AISStreamConnector

    connector = object.__new__(AISStreamConnector)
    connector.settings = {
        "mmsi_per_subscription": 200,
        "rotation_interval_seconds": 180.0,
    }
    mmsis = [f"{i:09d}" for i in range(150)]
    strategy, chunks, window = AISStreamConnector._chunk_plan(connector, mmsis)
    assert strategy == "full_list"
    assert len(chunks) == 1
    assert len(chunks[0]) == 150


def test_coverage_window_seconds_in_health(monkeypatch):
    from services import ais_health

    monkeypatch.delenv("SENTINEL_COVERAGE_WINDOW_SECONDS", raising=False)
    derived = ais_health.coverage_window_from_config()
    assert derived == 540.0
    assert ais_health.resolve_coverage_window_seconds() == 540.0
    monkeypatch.setenv("SENTINEL_COVERAGE_WINDOW_SECONDS", "540")
    assert ais_health.resolve_coverage_window_seconds() == 540.0
    out = ais_health.compute_top500_live_coverage(window_seconds=540)
    assert out["coverage_window_seconds"] == 540.0
    assert abs(out["window_minutes"] - 9.0) < 1e-6
