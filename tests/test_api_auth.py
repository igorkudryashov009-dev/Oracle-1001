#!/usr/bin/env python3
"""API auth / rate-limit / health sanitization / llm fallback tests."""

from __future__ import annotations

import json
import os

import pytest

from services.api_auth import (
    generate_api_key,
    resolve_auth,
    sanitize_health,
    upsert_key_into_env_map,
)


@pytest.fixture(autouse=True)
def _clear_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_KEYS_JSON", raising=False)
    monkeypatch.delenv("API_KEYS_FILE", raising=False)


def test_health_public_slim_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = resolve_auth(api_key=None, contract_version=None, path="/api/v1/health")
    assert auth["ok"] is True
    assert auth["tier"] == "public"
    slim = sanitize_health(
        {
            "pipeline_health_status": "NOMINAL",
            "gfw_budget": {"used": 1},
            "acceptance": {"status": "GREEN", "fully_commissioned_at": "2026-09-25T11:40:20Z"},
            "replica": {"db_path": "/app/secret.db"},
        },
        tier="public",
    )
    assert "gfw_budget" not in slim
    assert slim["acceptance"]["status"] == "GREEN"
    assert slim["commissioned"] is True
    assert "db_path" not in str(slim)


def test_market_requires_key_when_keys_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    key = generate_api_key()
    monkeypatch.setenv(
        "API_KEYS_JSON",
        json.dumps({key: {"name": "x", "tier": "readonly"}}),
    )
    auth = resolve_auth(api_key=None, contract_version=None, path="/api/v1/market/summary")
    assert auth["ok"] is False
    assert auth["status"] == 401


def test_legacy_contract_version_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = resolve_auth(
        api_key=None,
        contract_version="1.8.0-ops-gis-sot",
        path="/api/v1/market/summary",
    )
    assert auth["ok"] is True
    assert auth["tier"] == "readonly"
    doc = sanitize_health(
        {
            "pipeline_health_status": "NOMINAL",
            "gfw_budget": {"used": 9},
            "llm_budget": {"used": 1},
            "fleet_archive": {"gfw_verified_n": 20, "vf_budget": {"x": 1}},
            "replica": {"db_path": "/app/x.db", "age_sec": 1},
        },
        tier="readonly",
    )
    assert "gfw_budget" not in doc
    assert "llm_budget" not in doc
    assert doc["fleet_archive"]["gfw_verified_n"] == 20
    assert "vf_budget" not in doc["fleet_archive"]
    assert doc["replica"]["db_path"] == "[redacted]"
    # Nested absolute paths (e.g. acceptance.commissioning_report) stay redacted
    nested = sanitize_health(
        {
            "acceptance": {
                "status": "GREEN",
                "commissioning_report": "/app/output/commissioning_report.json",
            }
        },
        tier="readonly",
    )
    assert "/app/" not in json.dumps(nested)
    assert nested["acceptance"]["commissioning_report"] == "[redacted]"


def test_admin_key_keeps_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    key = generate_api_key()
    monkeypatch.setenv(
        "API_KEYS_JSON",
        upsert_key_into_env_map("{}", key=key, name="admin", tier="admin"),
    )
    auth = resolve_auth(api_key=key, contract_version=None, path="/api/v1/health")
    assert auth["tier"] == "admin"
    full = sanitize_health({"gfw_budget": {"used": 3}, "status": "ok"}, tier="admin")
    assert full["gfw_budget"]["used"] == 3


def test_rate_limit_60(monkeypatch: pytest.MonkeyPatch) -> None:
    key = generate_api_key()
    monkeypatch.setenv(
        "API_KEYS_JSON",
        json.dumps({key: {"name": "rl", "tier": "readonly"}}),
    )
    # Isolate rate buckets by unique key
    statuses = []
    for _ in range(61):
        auth = resolve_auth(api_key=key, contract_version=None, path="/api/v1/market/summary")
        statuses.append(auth.get("status"))
    assert 429 in statuses
    assert statuses.count(200) == 60


def test_llm_fallback_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "services.runtime_env.RUNTIME_ENV_PATH",
        __import__("pathlib").Path("/tmp/no_runtime_env_llm_test.json"),
    )
    from services.llm_router import llm_health_block, run_daily_brief

    st = llm_health_block()
    assert st["status"] == "degraded"
    assert st["configured"] is False
    out = run_daily_brief(day="2026-09-24")
    assert out["ok"] is False
    assert out.get("error") == "not_configured"
