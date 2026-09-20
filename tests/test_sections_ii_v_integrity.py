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
