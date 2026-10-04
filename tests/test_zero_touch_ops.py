"""Zero-touch ops: idempotency, remediation, disk forecast, secrets, locales."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.api_auth import sanitize_health
from services.disk_forecast import forecast_from_samples
from services.i18n_catalog import LANGS, catalog_for, load_catalogs, negotiate_lang, reset_cache
from services.secret_scan import find_secret_leaks, scan_health_and_logs


def test_archive_snapshot_is_idempotent_same_utc_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "sentinel_ais.db"
    monkeypatch.setenv("SENTINEL_DB_PATH", str(db))
    from services import job_log as jl
    from services.scheduler import run_job

    monkeypatch.setattr(jl, "_db_path", lambda: db)
    monkeypatch.setattr(
        "services.scheduler._job_archive_snapshot",
        lambda: {"ok": True, "rows": 3, "skipped": False},
    )
    first = run_job("archive_snapshot")
    second = run_job("archive_snapshot")
    assert first["status"] == "ok"
    assert second["status"] == "skipped"
    assert second["detail"]["reason"] == "already_finished_slot"
    import sqlite3

    conn = sqlite3.connect(str(db))
    n = conn.execute(
        "SELECT COUNT(*) FROM job_log WHERE job_name='archive_snapshot'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_overdue_remediation_once_per_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "sentinel_ais.db"
    monkeypatch.setenv("SENTINEL_DB_PATH", str(db))
    from services import job_log as jl
    from services import scheduler as sched

    monkeypatch.setattr(jl, "_db_path", lambda: db)
    monkeypatch.setattr(sched, "ROOT", tmp_path)
    monkeypatch.setattr("services.job_lock.LOCK_DIR", tmp_path / "locks")
    calls: list[str] = []

    def fake_run(name: str, *, force: bool = False) -> dict:
        calls.append(name)
        return {"job": name, "status": "ok"}

    monkeypatch.setattr(sched, "run_job", fake_run)
    old = (datetime.now(timezone.utc) - timedelta(seconds=4000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    jl.ensure_job_log_schema()
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO job_log (job_name, started_at, finished_at, status) VALUES (?,?,?,?)",
        ("pipeline_watchdog", old, old, "ok"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts_state.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "alerts.jsonl")
    monkeypatch.setattr("services.alerts.ALERTS_PATH_HOST", tmp_path / "nohost.jsonl")
    first = sched.remediate_overdue(background=False)
    second = sched.remediate_overdue(background=False)
    assert first == ["pipeline_watchdog"]
    assert second == []
    assert calls == ["pipeline_watchdog"]


def test_disk_forecast_alerts_inside_72h() -> None:
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    samples = [
        (now - timedelta(hours=48), 30.0),
        (now - timedelta(hours=24), 20.0),
        (now, 12.0),
    ]
    block = forecast_from_samples(samples, now=now, critical_pct=10.0)
    assert block["alert"] is True
    assert block["hours_to_critical"] is not None
    assert block["hours_to_critical"] < 72
    assert "expand the disk" in str(block["recommendation"])


def test_disk_forecast_stable_when_free_rises() -> None:
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    samples = [(now - timedelta(hours=24), 20.0), (now, 25.0)]
    block = forecast_from_samples(samples, now=now, critical_pct=10.0)
    assert block["alert"] is False
    assert block["status"] == "stable_or_recovering"


def test_key_cycle_probe_activate_acceptance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.setattr("services.runtime_env.RUNTIME_ENV_PATH", tmp_path / "runtime_env.json")
    monkeypatch.setattr("services.runtime_env.SIGNAL_PATH", tmp_path / "signal")
    monkeypatch.delenv("VESSELFINDER_API_KEY", raising=False)
    monkeypatch.delenv("VESSEL_FINDER_USERKEY", raising=False)
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    monkeypatch.delenv("GFW_API_KEY", raising=False)
    monkeypatch.delenv("GLOBAL_FISHING_WATCH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-testkey1234")
    monkeypatch.setattr(
        "services.llm_router.anthropic_models_probe",
        lambda key: {"ok": True},
    )
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel_ais.db"))
    monkeypatch.setattr(
        "services.llm_router.run_daily_brief",
        lambda **kwargs: {"ok": True, "text": "gap digest", "configured": True, "status": "ok"},
    )
    monkeypatch.setattr(
        "services.archive_snapshot_worker.take_daily_snapshot",
        lambda **kwargs: {"ok": True, "stored_for_date": 1, "live_ais_overlay": 0},
    )
    monkeypatch.setattr(
        "services.acceptance.maybe_rerun_acceptance_after_verification",
        lambda **kwargs: {
            "status": "WAITING_KEYS",
            "transition": None,
            "fully_commissioned_at": "2026-09-25T11:40:20Z",
            "trigger": f"verified_data_arrived:{kwargs.get('channel')}",
        },
    )
    from services.key_activation import run_key_activation_cycle

    out = run_key_activation_cycle(force_providers=["anthropic"])
    assert out["anthropic"]["ok"] is True
    assert out["anthropic"]["masked"] == "****1234"
    assert "sk-ant-testkey1234" not in json.dumps(out)
    run = out["immediate_runs"][0]
    assert run["llm_unlocked"] is True
    assert run["acceptance"]["trigger"] == "verified_data_arrived:anthropic"


def test_health_and_logs_have_no_secret_material(tmp_path: Path) -> None:
    health = {
        "status": "NOMINAL",
        "acceptance": {"status": "GREEN"},
        "note": "slim health",
        "disk_forecast": {"hours_to_critical": 80},
    }
    slim = sanitize_health(health, tier="public")
    assert "disk_forecast" not in slim
    clean = tmp_path / "app.log"
    clean.write_text("pipeline NOMINAL\n" * 5, encoding="utf-8")
    assert scan_health_and_logs(json.dumps(slim), [clean]) == []
    poisoned = tmp_path / "bad.log"
    poisoned.write_text("leak sk_sent_abcdef123456\n", encoding="utf-8")
    assert find_secret_leaks(poisoned.read_text(encoding="utf-8"))


def test_locale_catalogs_share_keys_and_do_not_touch_gates() -> None:
    reset_cache()
    cats = load_catalogs()
    assert set(cats) == set(LANGS)
    en_keys = set(cats["en"])
    for lang, table in cats.items():
        assert set(table) == en_keys, lang
    assert negotiate_lang(query="ar") == "ar"
    assert catalog_for("ar")["dir"] == "rtl"
    reset_cache()
    calls = {"n": 0}
    real_read = Path.read_text

    def counting_read(self, *args, **kwargs):
        if self.name.endswith(".json") and "locales" in str(self).replace("\\", "/"):
            calls["n"] += 1
        return real_read(self, *args, **kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(Path, "read_text", counting_read)
    try:
        load_catalogs()
        first = calls["n"]
        load_catalogs()
        assert calls["n"] == first
    finally:
        monkey.undo()
        reset_cache()
    from services.dual_gate import DISK_FREE_CRITICAL_PCT, FLEET_SAMPLE_FULL_MIN

    assert FLEET_SAMPLE_FULL_MIN == 100
    assert DISK_FREE_CRITICAL_PCT == 10.0
    payload = {
        "pipeline_health_status": "NOMINAL",
        "fleet_sample_status": "LIMITED",
        "top500_live_coverage": 4,
        "model_cv_accuracy_pct": 91.0,
    }
    before = dict(payload)
    for lang in LANGS:
        catalog_for(lang)
    assert payload == before
    slim = sanitize_health(payload, tier="public")
    assert slim["status"] == "NOMINAL"
    assert "top500_live_coverage" not in slim


def test_webhook_retries_then_dead_letter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services.alerts import deliver_webhook

    monkeypatch.setattr("services.alerts.DEAD_LETTER_PATH", tmp_path / "alerts_deadletter.jsonl")
    sleeps: list[float] = []

    class Resp:
        status_code = 503

    out = deliver_webhook(
        "http://example.invalid/hook",
        {"kind": "disk_forecast"},
        post=lambda *a, **k: Resp(),
        sleep=sleeps.append,
    )
    assert out["dead_letter"] is True
    assert sleeps == [60, 300, 900]
    assert (tmp_path / "alerts_deadletter.jsonl").is_file()
