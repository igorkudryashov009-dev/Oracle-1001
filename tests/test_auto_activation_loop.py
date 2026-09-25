#!/usr/bin/env python3
"""Auto-activation loop: install_key flow, alert resolve/dedup, acceptance."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.acceptance import evaluate_acceptance
from services.alerts import alerts_health_block, emit_alert, resolve_alert
from services.job_log import JOB_SCHEDULE
from services.key_activation import (
    is_provider_paused,
    record_provider_error,
    record_provider_ok,
    run_key_activation_cycle,
)
from services.runtime_env import (
    consume_install_signal,
    write_install_signal,
    write_runtime_key,
)
from services.scheduler import JOB_HANDLERS


def test_scheduler_registers_six_jobs() -> None:
    assert "acceptance_check" in JOB_SCHEDULE
    assert set(JOB_HANDLERS.keys()) == set(JOB_SCHEDULE.keys())
    assert len(JOB_SCHEDULE) == 6


def test_runtime_env_masked_no_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.runtime_env.RUNTIME_ENV_PATH", tmp_path / "runtime_env.json")
    monkeypatch.setattr("services.runtime_env.SIGNAL_PATH", tmp_path / "signal")
    secret = "supersecret_gfw_token_ABCD1234"
    write_runtime_key("GFW_API_TOKEN", secret)
    write_install_signal("gfw")
    text = (tmp_path / "runtime_env.json").read_text(encoding="utf-8")
    assert "ABCD1234" in text  # file holds secret (gitignored)
    # Public surfaces must mask
    from services.key_manager import mask_key

    masked = mask_key(secret)
    assert "supersecret" not in masked
    assert masked.endswith("1234")
    assert consume_install_signal() == "gfw"
    assert consume_install_signal() is None


def test_alert_dedup_24h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts_state.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "alerts.jsonl")
    monkeypatch.setattr("services.alerts.ALERTS_PATH_HOST", tmp_path / "no_host_alerts.jsonl")
    r1 = emit_alert("provider_paused_vesselfinder", "paused", detail={"provider": "vesselfinder"})
    assert r1 is not None
    r2 = emit_alert("provider_paused_vesselfinder", "paused again", detail={"provider": "vesselfinder"})
    assert r2 is None  # dedup
    block = alerts_health_block()
    assert block["active_n"] == 1


def test_alert_auto_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts_state.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "alerts.jsonl")
    monkeypatch.setattr("services.alerts.ALERTS_PATH_HOST", tmp_path / "no_host.jsonl")
    emit_alert("provider_paused_gfw", "paused", detail={"provider": "gfw"})
    resolve_alert("provider_paused_gfw", reason="probe_ok")
    block = alerts_health_block()
    assert block["active_n"] == 0
    assert block["resolved_24h_n"] >= 1
    lines = (tmp_path / "alerts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert any(json.loads(l).get("status") == "resolved" for l in lines)


def test_auto_activation_immediate_run_on_mock_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.setattr("services.runtime_env.RUNTIME_ENV_PATH", tmp_path / "runtime.json")
    monkeypatch.setattr("services.runtime_env.SIGNAL_PATH", tmp_path / "sig")
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts_state.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "alerts.jsonl")
    monkeypatch.setattr("services.alerts.ALERTS_PATH_HOST", tmp_path / "nohost.jsonl")

    # Pause VF first (3 errors)
    for i in range(3):
        record_provider_error("vesselfinder", f"Invalid Userkey! #{i}")
    assert is_provider_paused("vesselfinder")

    write_runtime_key("VESSELFINDER_API_KEY", "brand_new_valid_looking_key99")
    write_install_signal("vesselfinder")
    monkeypatch.setenv("VESSELFINDER_API_KEY", "brand_new_valid_looking_key99")

    runs: list[str] = []

    def fake_probe(*, force: bool = False):
        record_provider_ok("vesselfinder")
        return {"ok": True, "configured": True, "new_key": True}

    def fake_immediate(provider: str):
        runs.append(provider)
        return {"provider": provider, "vf_allocator": {"ok": True, "billed": 1}}

    monkeypatch.setattr("services.key_activation.probe_vesselfinder", fake_probe)
    monkeypatch.setattr("services.key_activation._immediate_first_run", fake_immediate)
    monkeypatch.setattr(
        "services.key_activation.probe_gfw",
        lambda **kw: {"ok": False, "configured": False},
    )

    out = run_key_activation_cycle()
    assert out["signal"] == "vesselfinder"
    assert out["vf"]["ok"] is True
    assert "vesselfinder" in runs
    assert is_provider_paused("vesselfinder") is False


def test_acceptance_waiting_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.acceptance.STATE_PATH", tmp_path / "acc.json")
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.delenv("VESSELFINDER_API_KEY", raising=False)
    monkeypatch.delenv("VESSEL_FINDER_USERKEY", raising=False)
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    monkeypatch.delenv("GFW_API_KEY", raising=False)

    doc = {
        "pipeline_health_status": "NOMINAL",
        "disk_free_pct": 40.0,
        "ais_lag_sec": 5.0,
        "replica": {"age_sec": 5.0},
        "scheduler": {"overdue_jobs_n": 0},
        "fleet_archive": {
            "row_count": 1253,
            "expected_n": 1253,
            "completeness_pct": 100.0,
            "terrestrial_covered_n": 41,
            "gfw_verified_n": 0,
            "vf_verified_n": 0,
        },
        "gfw_status": {"configured": False},
    }
    monkeypatch.setattr("services.acceptance._probe_health_latency", lambda: 0.4)
    blob = evaluate_acceptance(doc)
    assert blob["status"] == "WAITING_KEYS"


def test_acceptance_green_with_gfw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.acceptance.STATE_PATH", tmp_path / "acc.json")
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    record_provider_ok("gfw")
    monkeypatch.delenv("VESSELFINDER_API_KEY", raising=False)
    monkeypatch.delenv("VESSEL_FINDER_USERKEY", raising=False)

    doc = {
        "pipeline_health_status": "NOMINAL",
        "disk_free_pct": 40.0,
        "ais_lag_sec": 5.0,
        "replica": {"age_sec": 5.0},
        "scheduler": {"overdue_jobs_n": 0},
        "fleet_archive": {
            "row_count": 1253,
            "expected_n": 1253,
            "completeness_pct": 100.0,
            "terrestrial_covered_n": 41,
            "gfw_verified_n": 12,
            "vf_verified_n": 0,
        },
        "gfw_status": {"configured": True},
    }
    monkeypatch.setattr("services.acceptance._probe_health_latency", lambda: 0.3)
    monkeypatch.setattr(
        "services.vesselfinder_client.resolve_userkey",
        lambda: "",
    )
    blob = evaluate_acceptance(doc)
    assert blob["status"] == "GREEN"


def test_acceptance_degraded_on_overdue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.acceptance.STATE_PATH", tmp_path / "acc.json")
    monkeypatch.delenv("VESSELFINDER_API_KEY", raising=False)
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    doc = {
        "pipeline_health_status": "NOMINAL",
        "disk_free_pct": 40.0,
        "ais_lag_sec": 5.0,
        "scheduler": {"overdue_jobs_n": 2},
        "fleet_archive": {
            "row_count": 1253,
            "expected_n": 1253,
            "completeness_pct": 100.0,
            "terrestrial_covered_n": 10,
            "gfw_verified_n": 0,
            "vf_verified_n": 0,
        },
        "gfw_status": {"configured": False},
    }
    monkeypatch.setattr("services.acceptance._probe_health_latency", lambda: 0.2)
    blob = evaluate_acceptance(doc)
    assert blob["status"] == "DEGRADED"
