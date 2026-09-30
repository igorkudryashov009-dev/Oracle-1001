#!/usr/bin/env python3
"""POST /api/v1/pilot/register — happy path, 400, 429, idempotency."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from services.api_auth import resolve_auth
from services.pilot_register import (
    PILOT_RATE_LIMIT_PER_MIN,
    find_by_email,
    handle_pilot_register,
    hash_api_key,
    lookup_pilot_by_api_key,
    reset_ip_rate_limits,
    validate_register_body,
)


@pytest.fixture(autouse=True)
def _clean_rate_limits() -> None:
    reset_ip_rate_limits()
    yield
    reset_ip_rate_limits()


@pytest.fixture()
def pilot_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "pilot_clients.sqlite"
    monkeypatch.setenv("PILOT_CLIENTS_DB", str(db))
    return db


def test_pilot_register_happy_path(pilot_db: Path) -> None:
    status, payload, headers = handle_pilot_register(
        {"company": "Archea OSINT", "email": "pilot@archea.example"},
        client_ip="203.0.113.10",
        db_path=pilot_db,
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload.get("existing") is not True
    assert payload["tier"] == "readonly"
    assert payload["rate_limit"] == "60 req/min"
    api_key = payload["api_key"]
    assert isinstance(api_key, str) and api_key.startswith("sk_sent_")
    assert "X-Contract-Version" in headers

    row = find_by_email("pilot@archea.example", db_path=pilot_db)
    assert row is not None
    assert row["company"] == "Archea OSINT"
    assert row["status"] == "active"
    assert row["vessels"] == "[]"
    assert row["key_mask"].startswith("****")
    assert row["key_hash"] == hash_api_key(api_key)
    # Never persist plaintext
    conn = sqlite3.connect(str(pilot_db))
    blob = conn.execute("SELECT * FROM pilot_clients").fetchone()
    conn.close()
    assert api_key not in str(blob)

    auth = resolve_auth(api_key=api_key, contract_version=None, path="/api/v1/market/summary")
    assert auth["ok"] is True
    assert auth["tier"] == "readonly"


def test_pilot_register_validation_400(pilot_db: Path) -> None:
    cases = [
        {},
        {"company": "A", "email": "ok@example.com"},
        {"company": "Valid Co", "email": "not-an-email"},
        {"company": "Valid Co", "email": 123},
        None,
    ]
    for body in cases:
        status, payload, _ = handle_pilot_register(
            body, client_ip="203.0.113.20", db_path=pilot_db
        )
        assert status == 400, body
        assert payload["ok"] is False
        assert payload["error"] == "validation_error"
        assert "detail" in payload

    ok, err = validate_register_body({"company": "AB", "email": "a@b.co"})
    assert err is None and ok is not None


def test_pilot_register_rate_limit_429(pilot_db: Path) -> None:
    ip = "198.51.100.77"
    statuses = []
    for i in range(PILOT_RATE_LIMIT_PER_MIN + 2):
        status, payload, headers = handle_pilot_register(
            {"company": f"Co{i:02d}", "email": f"u{i}@rate.example"},
            client_ip=ip,
            db_path=pilot_db,
        )
        statuses.append(status)
        if status == 429:
            assert payload["error"] == "rate_limited"
            assert "Retry-After" in headers
            assert int(headers["Retry-After"]) >= 1
    assert statuses.count(200) == PILOT_RATE_LIMIT_PER_MIN
    assert 429 in statuses


def test_pilot_register_idempotent(pilot_db: Path) -> None:
    body = {"company": "Same Co", "email": "same@example.com"}
    s1, p1, _ = handle_pilot_register(body, client_ip="203.0.113.30", db_path=pilot_db)
    assert s1 == 200
    first_key = p1["api_key"]

    s2, p2, _ = handle_pilot_register(body, client_ip="203.0.113.31", db_path=pilot_db)
    assert s2 == 200
    assert p2 == {"ok": True, "existing": True}
    assert "api_key" not in p2

    # Case-insensitive email
    s3, p3, _ = handle_pilot_register(
        {"company": "Same Co", "email": "SAME@example.com"},
        client_ip="203.0.113.32",
        db_path=pilot_db,
    )
    assert s3 == 200
    assert p3.get("existing") is True

    row = find_by_email("same@example.com", db_path=pilot_db)
    assert row is not None
    assert lookup_pilot_by_api_key(first_key, db_path=pilot_db) is not None


def test_pilot_register_path_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "API_KEYS_JSON",
        '{"sk_sent_dummykeydummykeydummykeydum": {"name": "x", "tier": "admin"}}',
    )
    auth = resolve_auth(
        api_key=None,
        contract_version=None,
        path="/api/v1/pilot/register",
    )
    assert auth["ok"] is True
    assert auth["tier"] == "public"
    assert auth["reason"] == "public_pilot_register"
