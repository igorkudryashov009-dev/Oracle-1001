"""Integrity tests for Sections II–V (News / FIRMS / Market / Alerts) — Contract 1.8.0-ops-gis-sot."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api_server import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def test_news_latest_endpoint(client, monkeypatch):
    from services import news_service

    monkeypatch.setattr(
        news_service,
        "fetch_latest_news",
        lambda limit=25: {
            "ok": True,
            "contract_version": "1.8.0-ops-gis-sot",
            "count": 1,
            "items": [{"source": "newsapi", "title": "LNG Baltic test"}],
            "is_cached": True,
        },
    )
    # Patch the symbol bound in api_server
    import api_server as api

    monkeypatch.setattr(
        api,
        "fetch_latest_news",
        lambda limit=25: {
            "ok": True,
            "contract_version": "1.8.0-ops-gis-sot",
            "count": 1,
            "items": [{"source": "newsapi", "title": "LNG Baltic test"}],
            "is_cached": True,
        },
    )
    res = client.get("/api/v1/news/latest")
    assert res.status_code == 200
    assert res.headers.get("X-Contract-Version") == "1.8.0-ops-gis-sot"
    body = res.json()
    assert body["ok"] is True
    assert body["count"] == 1


def test_firms_anomalies_endpoint(client, monkeypatch):
    import api_server as api

    monkeypatch.setattr(
        api,
        "fetch_firms_anomalies",
        lambda days=1, max_distance_nm=50.0: {
            "ok": True,
            "contract_version": "1.8.0-ops-gis-sot",
            "type": "FeatureCollection",
            "features": [],
            "count": 0,
            "is_cached": True,
        },
    )
    res = client.get("/api/v1/gis/firms/anomalies")
    assert res.status_code == 200
    assert res.headers.get("X-Contract-Version") == "1.8.0-ops-gis-sot"
    body = res.json()
    assert body["type"] == "FeatureCollection"
    assert body["ok"] is True


def test_market_summary_endpoint(client, monkeypatch):
    import api_server as api

    monkeypatch.setattr(
        api,
        "fetch_market_summary",
        lambda: {
            "ok": True,
            "contract_version": "1.8.0-ops-gis-sot",
            "fx": {"configured": True, "base": "USD", "rates": {"EUR": 0.92}},
            "commodities": {},
            "is_cached": True,
        },
    )
    res = client.get("/api/v1/market/summary")
    assert res.status_code == 200
    assert res.headers.get("X-Contract-Version") == "1.8.0-ops-gis-sot"
    body = res.json()
    assert body["fx"]["rates"]["EUR"] == 0.92


def test_alerts_dispatch_endpoint(client, monkeypatch):
    import api_server as api

    monkeypatch.setattr(
        api,
        "dispatch_alert",
        lambda body: {
            "ok": True,
            "delivered": False,
            "dry_run": True,
            "contract_version": "1.8.0-ops-gis-sot",
            "error": "BREVO_SENDER_EMAIL_missing",
        },
    )
    res = client.post(
        "/api/v1/alerts/dispatch",
        json={
            "subject": "Pipeline test",
            "message": "Integrity probe",
            "to": "ops@example.com",
            "severity": "critical",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("X-Contract-Version") == "1.8.0-ops-gis-sot"
    body = res.json()
    assert body["ok"] is True
    assert body["delivered"] is False


def test_config_keys_registry_surface():
    from services.config_keys import API_REGISTRY_KEYS, registry_status

    assert len(API_REGISTRY_KEYS) == 7
    st = registry_status()
    assert st["total"] == 7
    assert "keys" in st
    # Never leak raw secrets into status blob
    blob = str(st)
    assert "xkeysib-" not in blob


def test_notify_payload_validation():
    from services.notify_service import validate_alert_payload

    ok, reason, _ = validate_alert_payload({"subject": "", "message": "x", "to": []})
    assert ok is False
    assert reason in ("invalid_subject", "missing_recipients")
    ok2, reason2, payload = validate_alert_payload(
        {"subject": "Alert", "message": "CS proximity", "to": "a@b.co"}
    )
    assert ok2 is True and reason2 == "ok"
    assert payload["to"][0]["email"] == "a@b.co"


def test_market_nasdaq_codes_include_ttf_brent_gold():
    from services.market_data_service import NASDAQ_CODES

    assert "ttf_proxy" in NASDAQ_CODES
    assert "brent" in NASDAQ_CODES
    assert "gold" in NASDAQ_CODES


def test_news_topic_filter():
    from services.news_service import TOPIC_TERMS, _matches_topics

    assert "LNG" in TOPIC_TERMS and "TTF" in TOPIC_TERMS
    assert _matches_topics("Baltic LNG terminal expansion")
    assert not _matches_topics("unrelated sports headline")


def test_firms_proximity_uses_compressor_stations():
    from services.compressor_stations import ALL_COMPRESSOR_STATIONS
    from services.firms_service import _attach_proximity

    assert len(ALL_COMPRESSOR_STATIONS) == 185
    # Portovaya-ish point should resolve nearest CS within 50 nm
    near = _attach_proximity(
        [{"lat": 60.55, "lon": 28.55, "frp": 12.0}],
        max_distance_nm=50.0,
    )
    assert near
    assert "nearest_station" in near[0]
    assert near[0]["distance_nm"] <= 50.0


def test_ais_tracker_is_g3_cache_first_not_second_ws():
    """G3 lock: ais_tracker must NOT open a second AISstream WebSocket."""
    src = (ROOT_DIR / "services" / "ais_tracker.py").read_text(encoding="utf-8")
    assert "must NOT open a" in src or "must NOT open" in src
    assert "aisstream_connector" in src or "single_persistent" in src
    # No live websocket client import in tracker
    assert "websockets.connect" not in src
    assert "aisstream.com" not in src.lower() or "stub" in src.lower()


def test_hud_sentinel_engine_has_news_and_firms_layers():
    hud = (ROOT_DIR / "web" / "sentinel_engine.js").read_text(encoding="utf-8")
    assert "Термоточки FIRMS" in hud
    assert "Новости" in hud
    assert "/api/v1/gis/firms/anomalies" in hud
    assert "/api/v1/news/latest" in hud
    assert "sentinelNewsTicker" in hud
    assert "startIntelPolls" in hud
