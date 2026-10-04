"""Pilot offer send, anti-spam, paid event, and admin funnel."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from services.api_auth import sanitize_health
from services.i18n_catalog import LANGS, load_catalogs, reset_cache
from services.pilot_outreach import UNSUBSCRIBE, check_audit_landing, landing_digest_line, send_pilot_offer
from services.pilot_register import (
    find_by_email,
    funnel_from_events,
    handle_pilot_register,
    note_first_use,
    record_paid,
)


def _ok_hop(url: str) -> tuple[int, str | None]:
    assert url.startswith("https://")
    return 200, None


def _loop_hop(url: str) -> tuple[int, str | None]:
    return 302, url


@pytest.fixture()
def pilot_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "pilot.sqlite"
    monkeypatch.setenv("PILOT_CLIENTS_DB", str(db))
    monkeypatch.setenv("AUDIT_LANDING_PATH", str(tmp_path / "audit_landing.json"))
    return db


def _register(db: Path, email: str = "buyer@example.com") -> dict:
    status, payload, _ = handle_pilot_register(
        {"company": "BIDV Desk", "email": email, "lang": "en"},
        client_ip="203.0.113.80",
        db_path=db,
        apply_rate_limit=False,
    )
    assert status == 200
    assert payload["status"] == "registered"
    return payload


def test_landing_requires_https_200_without_a_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_LANDING_PATH", str(tmp_path / "audit_landing.json"))
    ok = check_audit_landing(hop=_ok_hop)
    assert ok["ok"] is True and ok["status"] == 200 and ok["tls"] is True
    assert "audit_landing: ok=True http=200 tls=True loop=False" in landing_digest_line()
    loop = check_audit_landing(hop=_loop_hop)
    assert loop["ok"] is False and loop["redirect_loop"] is True


def test_send_once_then_repeat_is_refused(pilot_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(
        "services.alerts.emit_alert",
        lambda *a, **k: alerts.append(a[0]) or {"kind": a[0]},
    )
    payload = _register(pilot_db)
    sent: list[dict] = []

    def transport(message: dict) -> None:
        sent.append(message)

    first = send_pilot_offer("buyer@example.com", "ru", transport=transport, hop=_ok_hop, db_path=pilot_db)
    assert first["ok"] is True
    assert first["lang"] == "ru"
    assert len(sent) == 1
    assert sent[0]["list_unsubscribe"] == UNSUBSCRIBE
    assert "ГИС" in sent[0]["text"]
    assert "72 / 100" in sent[0]["text"]
    assert "$490" in sent[0]["text"]
    assert "после активации" in sent[0]["text"] or "after activation" in sent[0]["text"]
    assert payload["api_key"] not in sent[0]["text"]
    row = find_by_email("buyer@example.com", db_path=pilot_db)
    assert row is not None and row["status"] == "contacted"

    second = send_pilot_offer("buyer@example.com", "ru", transport=transport, hop=_ok_hop, db_path=pilot_db)
    assert second["ok"] is False
    assert second["error"] == "not_registered"
    assert len(sent) == 1
    conn = sqlite3.connect(str(pilot_db))
    n = conn.execute(
        "SELECT COUNT(*) FROM pilot_client_events WHERE to_status='contacted'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_bad_landing_refuses_and_alerts(pilot_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(
        "services.alerts.emit_alert",
        lambda *a, **k: alerts.append(str(a[0])),
    )
    _register(pilot_db)
    called = {"n": 0}

    def transport(_message: dict) -> None:
        called["n"] += 1

    result = send_pilot_offer(
        "buyer@example.com", "en", transport=transport, hop=_loop_hop, db_path=pilot_db
    )
    assert result["ok"] is False
    assert result["error"] == "audit_landing"
    assert called["n"] == 0
    assert alerts == ["audit_landing"]
    assert find_by_email("buyer@example.com", db_path=pilot_db)["status"] == "registered"


def test_funnel_counts_events_and_paid_is_once(pilot_db: Path) -> None:
    payload = _register(pilot_db, "pay@example.com")
    sent: list[dict] = []
    assert send_pilot_offer(
        "pay@example.com", "en", transport=sent.append, hop=_ok_hop, db_path=pilot_db
    )["ok"]
    note_first_use(payload["api_key"], db_path=pilot_db)
    note_first_use(payload["api_key"], db_path=pilot_db)
    assert find_by_email("pay@example.com", db_path=pilot_db)["status"] == "active"
    paid = record_paid("pay@example.com", db_path=pilot_db)
    assert paid["ok"] is True
    assert paid["score_trigger"] == "first_paid"
    again = record_paid("pay@example.com", db_path=pilot_db)
    assert again["ok"] is False and again["error"] == "already_paid"
    early = record_paid("nobody@example.com", db_path=pilot_db)
    assert early["error"] == "missing_client"
    funnel = funnel_from_events(db_path=pilot_db)
    assert funnel["source"] == "pilot_client_events"
    assert funnel["contacted"] == 1
    assert funnel["onboarded"] == 1
    assert funnel["active"] == 1
    assert funnel["paid"] == 1
    assert funnel["paid_score_trigger"] is True
    conn = sqlite3.connect(str(pilot_db))
    paid_rows = conn.execute(
        "SELECT COUNT(*) FROM pilot_client_events WHERE to_status='paid'"
    ).fetchone()[0]
    conn.close()
    assert paid_rows == 1


def test_paid_before_active_is_refused(pilot_db: Path) -> None:
    _register(pilot_db, "early@example.com")
    refused = record_paid("early@example.com", db_path=pilot_db)
    assert refused["ok"] is False
    assert refused["error"] == "paid_requires_active"


def test_readonly_health_hides_funnel() -> None:
    doc = {
        "pipeline_health_status": "NOMINAL",
        "pilot_funnel": {"paid": 1, "source": "pilot_client_events"},
        "acceptance": {"status": "GREEN"},
    }
    readonly = sanitize_health(doc, tier="readonly")
    public = sanitize_health(doc, tier="public")
    assert "pilot_funnel" not in readonly
    assert "pilot_funnel" not in public


def test_offer_locales_share_keys() -> None:
    reset_cache()
    cats = load_catalogs()
    en = set(cats["en"])
    for lang in LANGS:
        assert set(cats[lang]) == en
        assert "offer.llm_later" in cats[lang]
    assert "not a current feature" in cats["en"]["offer.llm_later"]
    reset_cache()
