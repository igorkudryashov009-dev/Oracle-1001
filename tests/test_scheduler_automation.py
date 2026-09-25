#!/usr/bin/env python3
"""Zero-touch scheduler / watchdog / key-activation tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.job_log import (
    JOB_SCHEDULE,
    ensure_job_log_schema,
    last_job_run,
    log_job_finish,
    log_job_start,
    scheduler_health_block,
)
from services.key_activation import (
    is_provider_paused,
    record_provider_error,
    record_provider_ok,
    run_key_activation_cycle,
)
from services.scheduler import JOB_HANDLERS, run_job


def test_scheduler_registers_six_jobs() -> None:
    assert set(JOB_SCHEDULE.keys()) == {
        "archive_snapshot",
        "gfw_poll",
        "vf_allocator",
        "pipeline_watchdog",
        "budget_sync",
        "acceptance_check",
    }
    assert set(JOB_HANDLERS.keys()) == set(JOB_SCHEDULE.keys())
    assert len(JOB_SCHEDULE) == 6


def test_scheduler_registers_five_jobs() -> None:
    # Compat alias — full set is six after acceptance_check
    assert len(JOB_SCHEDULE) >= 5
    assert "pipeline_watchdog" in JOB_SCHEDULE

def test_job_log_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "sentinel_ais.db"
    monkeypatch.setenv("SENTINEL_DB_PATH", str(db))
    # Re-import path uses env
    from services import job_log as jl

    monkeypatch.setattr(jl, "_db_path", lambda: db)
    ensure_job_log_schema()
    rid = log_job_start("budget_sync")
    log_job_finish(rid, status="ok", rows_affected=1, detail={"x": 1})
    last = last_job_run("budget_sync")
    assert last is not None
    assert last["status"] == "ok"
    assert last["rows_affected"] == 1


def test_overdue_detector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "sentinel_ais.db"
    monkeypatch.setenv("SENTINEL_DB_PATH", str(db))
    from services import job_log as jl

    monkeypatch.setattr(jl, "_db_path", lambda: db)
    ensure_job_log_schema()
    # Fresh — not overdue
    block = scheduler_health_block()
    assert block["overdue_jobs_n"] == 0
    # Inject stale run for watchdog (period 900 → overdue after 1800s)
    old = (datetime.now(timezone.utc) - timedelta(seconds=4000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO job_log (job_name, started_at, finished_at, status) VALUES (?,?,?,?)",
        ("pipeline_watchdog", old, old, "ok"),
    )
    conn.commit()
    conn.close()
    block2 = scheduler_health_block()
    assert block2["overdue_jobs_n"] >= 1
    assert "pipeline_watchdog" in block2["overdue_jobs"]


def test_key_activation_pauses_after_three_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "provider_activation.json"
    monkeypatch.setattr("services.key_activation.STATE_PATH", state)
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts_state.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "alerts.jsonl")
    for i in range(3):
        record_provider_error("vesselfinder", f"Invalid Userkey! #{i}")
    assert is_provider_paused("vesselfinder") is True
    record_provider_ok("vesselfinder")
    assert is_provider_paused("vesselfinder") is False


def test_key_activation_skips_without_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VESSELFINDER_API_KEY", raising=False)
    monkeypatch.delenv("VESSEL_FINDER_USERKEY", raising=False)
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    monkeypatch.delenv("GFW_API_KEY", raising=False)
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    out = run_key_activation_cycle()
    assert out["vf"]["configured"] is False
    assert out["gfw"]["configured"] is False


def test_watchdog_triggers_on_lag_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import watchdog as wd

    monkeypatch.setattr(wd, "STATE_PATH", tmp_path / "watchdog_state.json")
    monkeypatch.setattr(wd, "AIS_LAG_STREAK_LIMIT", 2)
    restarts: list[str] = []

    def fake_restart(name: str):
        restarts.append(name)
        return {"ok": True, "container": name}

    monkeypatch.setattr(wd, "_docker_restart", fake_restart)
    monkeypatch.setattr(
        wd,
        "run_pipeline_watchdog",
        wd.run_pipeline_watchdog,  # keep
    )

    def fake_health():
        return {
            "replica": {"age_sec": 500.0},
            "disk_free_pct": 40.0,
            "pipeline_health_status": "DEGRADED",
        }

    monkeypatch.setattr("services.ais_health.build_health_document", fake_health)
    monkeypatch.setattr(
        "services.key_activation.run_key_activation_cycle",
        lambda: {"vf": {"ok": False}, "gfw": {"ok": False}},
    )
    # Two ticks with high lag
    wd.run_pipeline_watchdog()
    out = wd.run_pipeline_watchdog()
    assert "sentinel-core" in restarts
    assert out["ok"] is True


def test_run_job_gfw_skips_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "sentinel_ais.db"
    monkeypatch.setenv("SENTINEL_DB_PATH", str(db))
    from services import job_log as jl

    monkeypatch.setattr(jl, "_db_path", lambda: db)
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    monkeypatch.delenv("GFW_API_KEY", raising=False)
    out = run_job("gfw_poll")
    assert out["status"] == "skipped"


def test_run_job_vf_skips_without_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "sentinel_ais.db"
    monkeypatch.setenv("SENTINEL_DB_PATH", str(db))
    from services import job_log as jl

    monkeypatch.setattr(jl, "_db_path", lambda: db)
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.setenv("VESSELFINDER_API_KEY", "deadkey0971deadkey0971deadkey09")
    out = run_job("vf_allocator")
    assert out["status"] == "skipped"
