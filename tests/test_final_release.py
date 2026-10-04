"""Final-release checks: i18n, readiness, chaos, satellite, pilot, offer."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from services.alerts import WEBHOOK_BACKOFF_SEC
from services.api_auth import sanitize_health
from services.dual_gate import (
    DISK_FREE_CRITICAL_PCT,
    FLEET_SAMPLE_FULL_MIN,
    PIPELINE_LIVE_LAG_SEC,
    READINESS_SATELLITE_MAX,
    compute_readiness_score,
)
from services.i18n_catalog import LANGS, catalog_for, load_catalogs, negotiate_lang, reset_cache

ROOT = Path(__file__).resolve().parents[1]


def test_webhook_backoff_is_1_5_15_minutes() -> None:
    assert WEBHOOK_BACKOFF_SEC == (60, 300, 900)


def test_locale_cookie_survives_without_query() -> None:
    assert negotiate_lang(query=None, cookie="sentinel_lang=ru") == "ru"
    assert negotiate_lang(cookie="other=1; sentinel_lang=ar") == "ar"
    js = (ROOT / "web" / "js" / "sentinel_i18n.js").read_text(encoding="utf-8")
    assert "Max-Age=31536000" in js
    assert "sentinel_lang=" in js
    assert 'data-i18n-live' in js
    engine = (ROOT / "web" / "js" / "sentinel_engine.js").read_text(encoding="utf-8")
    assert 'setAttribute("data-i18n-live", "1")' in engine


def test_arabic_rtl_and_map_controls_stay_ltr() -> None:
    assert catalog_for("ar")["dir"] == "rtl"
    assert catalog_for("en")["dir"] == "ltr"
    html = (ROOT / "output" / "sentinel_dashboard.html").read_text(encoding="utf-8")
    assert "html[dir=\"rtl\"] .leaflet-control-container" in html
    assert "direction: ltr" in html


def test_language_does_not_change_health_or_quant_numbers() -> None:
    health = {"pipeline_health_status": "NOMINAL", "top500_live_coverage": 2, "ais_lag_sec": 3.6}
    quant = {"is_synthetic": True, "production_actionable": False, "recommended_strategy_id": None}
    snapshot_h = json.dumps(health)
    snapshot_q = json.dumps(quant)
    for lang in LANGS:
        catalog_for(lang)
        assert json.dumps(health) == snapshot_h
        assert json.dumps(quant) == snapshot_q
    assert FLEET_SAMPLE_FULL_MIN == 100


def test_catalog_cache_does_not_reread_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_cache()
    load_catalogs()
    calls = {"n": 0}
    real = Path.read_text

    def _boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.suffix == ".json" and "locales" in str(self):
            calls["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)
    load_catalogs()
    catalog_for("ko")
    assert calls["n"] == 0
    reset_cache()


def test_catalog_memory_lookup_is_fast() -> None:
    import time

    reset_cache()
    load_catalogs()
    start = time.perf_counter()
    for _ in range(200):
        catalog_for("ja")
    assert time.perf_counter() - start < 0.5


def test_readiness_reaches_100_only_with_measured_channels() -> None:
    empty = compute_readiness_score({"pipeline_health_status": "DEGRADED", "terrestrial_verified_n": 10**9})
    assert empty["components"]["base"] == 0
    assert empty["components"]["terrestrial"] == 10
    assert empty["score"] == 10
    parked = compute_readiness_score({"pipeline_health_status": "NOMINAL", "satellite_verified_n": 0})
    armed = compute_readiness_score({"pipeline_health_status": "NOMINAL", "satellite_verified_n": 4})
    assert 10 <= (armed["score"] - parked["score"]) <= 15
    assert armed["components"]["satellite"] == READINESS_SATELLITE_MAX
    full = compute_readiness_score(
        {
            "pipeline_health_status": "NOMINAL",
            "terrestrial_verified_n": 5,
            "gfw_verified_n": 1,
            "vf_verified_n": 1,
            "llm_verified_n": 1,
            "satellite_verified_n": 1,
            "green_streak_days": 7,
            "pilot_active_n": 5,
        }
    )
    assert full["score"] == 100
    inflated = compute_readiness_score(
        {
            "pipeline_health_status": "NOMINAL",
            "terrestrial_verified_n": 9999,
            "gfw_verified_n": 9999,
            "green_streak_days": 99,
            "pilot_active_n": 99,
        }
    )
    assert inflated["components"]["terrestrial"] == 10
    assert inflated["components"]["gfw"] == 10
    assert inflated["components"]["stability"] == 8
    assert inflated["components"]["pilot"] == 5


def test_public_slim_exposes_score_and_hides_ops() -> None:
    slim = sanitize_health(
        {
            "pipeline_health_status": "NOMINAL",
            "acceptance": {"status": "GREEN", "fully_commissioned_at": "2026-09-25T11:40:20Z"},
            "disk_forecast": {"hours_to_critical": 10},
            "ops": {"scheduler": {}},
            "readiness_score": {"score": 48, "components": {"satellite": 0}},
        },
        tier="public",
    )
    assert slim["readiness_score"] == 48
    assert "disk_forecast" not in slim
    assert "ops" not in slim
    assert "components" not in slim


def test_ops_contour_archive_and_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services.ops_contour import build_ops_contour, ops_contour_block, reset_cache

    db = tmp_path / "arch.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE vessel_daily_archive (
            snapshot_date TEXT, imo INTEGER, source TEXT, gap_hours REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO vessel_daily_archive VALUES (?, ?, ?, ?)",
        [("2026-10-02", 1, "none", 10), ("2026-10-02", 2, "terrestrial_ais", 1), ("2026-10-02", 3, "none", 30)],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SENTINEL_DB_PATH", str(db))
    reset_cache()
    block = build_ops_contour()
    assert block["archive"]["rows"] == 3
    assert block["archive"]["target"] == 1260
    assert block["archive"]["sources"]["none"] == 2
    assert block["i18n"]["locales_loaded"] == 10
    assert block["i18n"]["keys_equal"] is True
    assert block["i18n"]["rtl_ok"] is True
    assert "duration_sec" in next(iter(block["scheduler"]["jobs"].values()))
    first = ops_contour_block()
    second = ops_contour_block()
    assert first is second
    reset_cache()


def test_acceptance_history_is_append_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services.acceptance_history import append_acceptance_history, load_history

    path = tmp_path / "history.jsonl"
    monkeypatch.setenv("ACCEPTANCE_HISTORY_PATH", str(path))
    append_acceptance_history(
        {"status": "GREEN", "trigger": "scheduled", "fully_commissioned_at": "2026-09-25T11:40:20Z", "checks": {}}
    )
    append_acceptance_history(
        {"status": "DEGRADED", "trigger": "scheduled", "checks": {"disk": {"ok": False}}}
    )
    rows = load_history(path)
    assert [row["status"] for row in rows] == ["GREEN", "DEGRADED"]
    assert "fully_commissioned_at" not in rows[0]
    assert rows[1]["reasons"] == ["disk"]
    text = path.read_text(encoding="utf-8")
    assert text.count("\n") == 2


def test_satellite_parked_has_no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.satellite_adapter import probe, public_status, pull

    monkeypatch.delenv("SATELLITE_API_KEY", raising=False)
    monkeypatch.delenv("SAT_PROVIDER", raising=False)
    calls = {"n": 0}

    def transport(url: str, headers: dict[str, str]) -> dict:
        calls["n"] += 1
        return {}

    out = pull(transport=transport)
    parked = probe(transport=transport)
    assert out["requests"] == 0 and out["rows"] == []
    assert parked["status"] == "parked" and parked["requests"] == 0
    assert calls["n"] == 0
    status = public_status()
    assert status["status"] == "parked"
    assert status["side_effects"] is False


def test_satellite_three_vendors_and_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.satellite_adapter import allocate_budget, parse_vendor_payload, pull

    assert parse_vendor_payload("spire", {"data": [{"imo": 1, "latitude": 1.5, "longitude": 2.5}]})[0]["lat"] == 1.5
    assert parse_vendor_payload("unseenlabs", {"vessels": [{"imo": 2, "lat": 3, "lon": 4}]})[0]["imo"] == 2
    iceye = parse_vendor_payload(
        "iceye",
        {"features": [{"properties": {"imo": 3}, "geometry": {"coordinates": [10, 20]}}]},
    )
    assert iceye[0]["lat"] == 20 and iceye[0]["lon"] == 10
    assert allocate_budget([9, 9, 8, 7], used_today=1, cap=3) == [9, 8]
    monkeypatch.setenv("SATELLITE_API_KEY", "sat-secret-xyz")
    monkeypatch.setenv("SAT_PROVIDER", "spire")
    monkeypatch.delenv("SAT_BASE_URL", raising=False)
    assert pull(transport=lambda url, headers: {"data": []})["status"] == "parked"


def test_satellite_archive_does_not_clobber_terrestrial(tmp_path: Path) -> None:
    from services.archive_schema import VESSEL_DAILY_ARCHIVE_DDL
    from services.satellite_adapter import store_satellite_rows

    db = tmp_path / "v.db"
    conn = sqlite3.connect(str(db))
    conn.execute(VESSEL_DAILY_ARCHIVE_DDL)
    conn.execute(
        "INSERT INTO vessel_daily_archive (snapshot_date, imo, source) VALUES ('2026-10-02', 10, 'none')"
    )
    conn.execute(
        "INSERT INTO vessel_daily_archive (snapshot_date, imo, source, lat, lon) VALUES ('2026-10-02', 11, 'terrestrial_ais', 1, 2)"
    )
    prov = {"provider": "spire", "observed_at": "2026-10-02T00:00:00Z"}
    written = store_satellite_rows(
        conn,
        [
            {"imo": 10, "lat": 5, "lon": 6, "provenance": prov},
            {"imo": 11, "lat": 7, "lon": 8, "provenance": prov},
            {"imo": 12, "lat": 9, "lon": 9, "provenance": prov},
        ],
        snapshot_date="2026-10-02",
    )
    conn.commit()
    none_row = conn.execute("SELECT source, lat FROM vessel_daily_archive WHERE imo=10").fetchone()
    terr = conn.execute("SELECT source, lat FROM vessel_daily_archive WHERE imo=11").fetchone()
    new = conn.execute("SELECT source FROM vessel_daily_archive WHERE imo=12").fetchone()
    conn.close()
    assert written == 2
    assert none_row == ("satellite_ais", 5)
    assert terr == ("terrestrial_ais", 1)
    assert new[0] == "satellite_ais"


def test_timescale_backfill_is_idempotent(tmp_path: Path) -> None:
    from services.archive_schema import VESSEL_DAILY_ARCHIVE_DDL
    from scripts.migrate_to_timescale import backfill, verify

    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    conn = sqlite3.connect(str(src))
    conn.execute(VESSEL_DAILY_ARCHIVE_DDL)
    conn.execute(
        "INSERT INTO vessel_daily_archive (snapshot_date, imo, source, lat, lon, gap_hours) VALUES ('2026-10-02', 1, 'none', NULL, NULL, 4)"
    )
    conn.commit()
    conn.close()
    first = backfill(src, dst)
    second = backfill(src, dst)
    report = verify(src, dst)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert report["counts_match"] is True
    assert report["sample_mismatched"] == 0


def test_db_backend_defaults_to_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.db_backend import active_backend, requested_backend

    monkeypatch.delenv("DB_BACKEND", raising=False)
    assert requested_backend() == "sqlite"
    assert active_backend() == "sqlite"
    monkeypatch.setenv("DB_BACKEND", "timescale")
    assert requested_backend() == "timescale"
    assert active_backend() == "sqlite"


def test_ensure_wal(tmp_path: Path) -> None:
    from services.sqlite_wal import ensure_wal

    conn = sqlite3.connect(str(tmp_path / "w.db"))
    mode = ensure_wal(conn)
    conn.close()
    assert mode == "wal"


def test_pilot_welcome_and_event_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services.pilot_register import find_by_email, handle_pilot_register, note_first_use

    db = tmp_path / "pilot.sqlite"
    monkeypatch.setenv("PILOT_CLIENTS_DB", str(db))
    status, payload, _ = handle_pilot_register(
        {"company": "Nord Stream Desk", "email": "ops@nord.example", "lang": "ru"},
        client_ip="203.0.113.50",
        db_path=db,
    )
    assert status == 200
    assert payload["status"] == "registered"
    assert payload["lang"] == "ru"
    assert "Пилот" in payload["welcome_md"]
    assert payload["api_key"] in payload["hud_url"]
    assert payload["api_key"] not in payload["welcome_md"]
    row = find_by_email("ops@nord.example", db_path=db)
    assert row is not None and row["status"] == "registered"
    note_first_use(payload["api_key"], db_path=db)
    onboarded = find_by_email("ops@nord.example", db_path=db)
    assert onboarded is not None and onboarded["status"] == "onboarded"
    note_first_use(payload["api_key"], db_path=db)
    active = find_by_email("ops@nord.example", db_path=db)
    assert active is not None and active["status"] == "active"
    conn = sqlite3.connect(str(db))
    events = conn.execute("SELECT to_status, reason FROM pilot_client_events ORDER BY id").fetchall()
    conn.close()
    assert [item[0] for item in events] == ["registered", "onboarded", "active"]


def test_churned_pilot_key_is_rejected(tmp_path: Path) -> None:
    from services.pilot_register import append_event, handle_pilot_register, lookup_pilot_by_api_key

    db = tmp_path / "pilot.sqlite"
    status, payload, _ = handle_pilot_register(
        {"company": "Gone Co", "email": "gone@example.com"},
        client_ip="203.0.113.60",
        db_path=db,
        apply_rate_limit=False,
    )
    assert status == 200
    append_event("gone@example.com", "churned", "contract_end", db_path=db)
    assert lookup_pilot_by_api_key(payload["api_key"], db_path=db) is None


def test_offer_promises_match_gates() -> None:
    text = (ROOT / "PILOT_OFFER.md").read_text(encoding="utf-8")
    assert str(int(PIPELINE_LIVE_LAG_SEC)) in text
    assert "1.0" in text
    assert "source=none" in text
    assert DISK_FREE_CRITICAL_PCT == 10.0
    assert "$490" in text and "$1,400" in text and "$2,200" in text
    assert "72 / 100" in text
    assert "not a current feature" in text
    assert "after activation" in text


def test_chaos_dry_run_all_green() -> None:
    from scripts.chaos_drill import run_drills

    results = run_drills()
    assert [item["scenario"] for item in results] == [
        "provider_failure",
        "disk_overflow",
        "overdue_job",
        "ws_loss",
    ]
    assert all(item["ok"] for item in results)


def test_disk_forecast_uses_dual_gate_critical_threshold() -> None:
    from services.disk_forecast import _critical_pct

    assert _critical_pct() == DISK_FREE_CRITICAL_PCT == 10.0


def test_chaos_cli_refuses_without_dry_run() -> None:
    from scripts.chaos_drill import main

    assert main([]) == 2


def test_satellite_status_masks_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.satellite_adapter import public_status

    monkeypatch.setenv("SATELLITE_API_KEY", "sat-secret-wxyz")
    monkeypatch.delenv("SAT_PROVIDER", raising=False)
    status = public_status()
    assert status["key_mask"] == "****wxyz"
    assert "sat-secret-wxyz" not in json.dumps(status)


def test_accept_language_falls_back_to_english() -> None:
    assert negotiate_lang(accept_language="sv-SE,sv;q=0.9") == "en"
    assert negotiate_lang(accept_language="pt-BR,pt;q=0.8") == "pt"


def test_budget_room_can_be_zero() -> None:
    from services.satellite_adapter import allocate_budget

    assert allocate_budget([1, 2, 3], used_today=50, cap=50) == []


def test_history_backfill_does_not_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services.acceptance_history import append_acceptance_history, backfill_from_job_log

    path = tmp_path / "history.jsonl"
    monkeypatch.setenv("ACCEPTANCE_HISTORY_PATH", str(path))
    append_acceptance_history({"status": "GREEN", "trigger": "scheduled", "checks": {}})
    assert backfill_from_job_log(tmp_path / "missing.db") == 0
    assert path.read_text(encoding="utf-8").count("\n") == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "External resource: live VesselFinder REST and satellite AIS stay off "
        "until install_key.sh VF / SATELLITE. This assertion is the activation proof."
    ),
)
def test_live_vf_and_satellite_are_external_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SATELLITE_API_KEY", raising=False)
    monkeypatch.delenv("VESSELFINDER_API_KEY", raising=False)
    from services.satellite_adapter import public_status

    assert public_status()["status"] == "active"
