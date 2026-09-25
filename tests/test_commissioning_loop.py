#!/usr/bin/env python3
"""Commissioning loop tests — event-driven GREEN, one-shot stamp, silence regression."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.acceptance import (
    REPORT_PATH,
    evaluate_acceptance,
    maybe_rerun_acceptance_after_verification,
    write_commissioning_report,
)
from services.key_activation import record_provider_ok


def _core_doc(**fa_extra):
    fa = {
        "row_count": 1253,
        "expected_n": 1253,
        "completeness_pct": 100.0,
        "terrestrial_covered_n": 41,
        "gfw_verified_n": 0,
        "vf_verified_n": 0,
    }
    fa.update(fa_extra)
    return {
        "pipeline_health_status": "NOMINAL",
        "disk_free_pct": 40.0,
        "ais_lag_sec": 5.0,
        "replica": {"age_sec": 5.0},
        "scheduler": {"overdue_jobs_n": 0},
        "fleet_archive": fa,
        "gfw_status": {"configured": False},
        "active_node": "korolev",
    }


def test_event_driven_acceptance_green_without_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.acceptance.STATE_PATH", tmp_path / "acc.json")
    monkeypatch.setattr("services.acceptance.REPORT_PATH", tmp_path / "commissioning_report.json")
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "alerts.jsonl")
    monkeypatch.setattr("services.alerts.ALERTS_PATH_HOST", tmp_path / "nohost.jsonl")
    record_provider_ok("gfw")
    monkeypatch.setattr("services.acceptance._probe_health_latency", lambda: 0.2)
    monkeypatch.setattr("services.vesselfinder_client.resolve_userkey", lambda: "")
    monkeypatch.delenv("VESSELFINDER_API_KEY", raising=False)

    # Seed WAITING_KEYS
    doc0 = _core_doc()
    doc0["gfw_status"] = {"configured": True}
    blob0 = evaluate_acceptance(doc0, trigger="seed")
    assert blob0["status"] == "WAITING_KEYS"
    assert blob0.get("fully_commissioned_at") is None

    # Event: verified arrived (mock fleet metrics via doc)
    doc1 = _core_doc(gfw_verified_n=7)
    doc1["gfw_status"] = {"configured": True}

    def fake_build():
        return doc1

    monkeypatch.setattr("services.ais_health.build_health_document", fake_build)
    monkeypatch.setattr(
        "services.archive_snapshot_worker.invalidate_fleet_archive_cache", lambda: None
    )
    blob = maybe_rerun_acceptance_after_verification(channel="gfw", verified_hint=7)
    assert blob is not None
    assert blob["status"] == "GREEN"
    assert blob["trigger"] == "verified_data_arrived:gfw"
    assert blob.get("fully_commissioned_at")
    assert (tmp_path / "commissioning_report.json").is_file()
    report = json.loads((tmp_path / "commissioning_report.json").read_text(encoding="utf-8"))
    assert report["event"] == "commissioned"
    assert report["channels"]["gfw_verified_n"] == 7
    assert "git_head" in report


def test_fully_commissioned_at_one_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.acceptance.STATE_PATH", tmp_path / "acc.json")
    monkeypatch.setattr("services.acceptance.REPORT_PATH", tmp_path / "comm.json")
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "a.jsonl")
    monkeypatch.setattr("services.alerts.ALERTS_PATH_HOST", tmp_path / "nh.jsonl")
    record_provider_ok("gfw")
    monkeypatch.setattr("services.acceptance._probe_health_latency", lambda: 0.1)
    monkeypatch.setattr("services.vesselfinder_client.resolve_userkey", lambda: "")

    doc = _core_doc(gfw_verified_n=3)
    doc["gfw_status"] = {"configured": True}
    b1 = evaluate_acceptance(doc, trigger="first")
    stamp = b1["fully_commissioned_at"]
    assert stamp
    # Second GREEN must keep original stamp
    doc2 = _core_doc(gfw_verified_n=9)
    doc2["gfw_status"] = {"configured": True}
    b2 = evaluate_acceptance(doc2, trigger="again")
    assert b2["fully_commissioned_at"] == stamp
    assert b2["status"] == "GREEN"


def test_invalidated_stamp_allows_new_green_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalidated false-GREEN stamp is superseded; new date is append-only thereafter."""
    monkeypatch.setattr("services.acceptance.STATE_PATH", tmp_path / "acc.json")
    monkeypatch.setattr("services.acceptance.REPORT_PATH", tmp_path / "comm.json")
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "a.jsonl")
    monkeypatch.setattr("services.alerts.ALERTS_PATH_HOST", tmp_path / "nh.jsonl")
    record_provider_ok("gfw")
    monkeypatch.setattr("services.acceptance._probe_health_latency", lambda: 0.1)
    monkeypatch.setattr("services.vesselfinder_client.resolve_userkey", lambda: "")

    false_stamp = "2026-09-25T08:44:02Z"
    (tmp_path / "acc.json").write_text(
        json.dumps(
            {
                "status": "WAITING_KEYS",
                "fully_commissioned_at": false_stamp,
                "commissioning_invalidated_at": "2026-09-25T09:18:13Z",
                "commissioning_invalidated_reason": "gfw_auth_http_401_false_verified",
                "trigger": "auth_rollback_401",
            }
        ),
        encoding="utf-8",
    )

    doc = _core_doc(gfw_verified_n=20)
    doc["gfw_status"] = {"configured": True, "events_7d_n": 10}
    b1 = evaluate_acceptance(doc, trigger="verified_data_arrived:gfw")
    assert b1["status"] == "GREEN"
    new_stamp = b1["fully_commissioned_at"]
    assert new_stamp
    assert new_stamp != false_stamp
    assert b1.get("previous_fully_commissioned_at") == false_stamp
    hist = b1.get("commissioning_history") or []
    assert any(h.get("fully_commissioned_at") == false_stamp for h in hist)
    assert not b1.get("commissioning_invalidated_reason")
    report = json.loads((tmp_path / "comm.json").read_text(encoding="utf-8"))
    assert report["fully_commissioned_at"] == new_stamp
    assert report["previous_fully_commissioned_at"] == false_stamp

    # Re-run must NOT change the new date
    doc2 = _core_doc(gfw_verified_n=21)
    doc2["gfw_status"] = {"configured": True, "events_7d_n": 10}
    b2 = evaluate_acceptance(doc2, trigger="again")
    assert b2["fully_commissioned_at"] == new_stamp
    assert b2["status"] == "GREEN"


def test_green_to_degraded_silence_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.acceptance.STATE_PATH", tmp_path / "acc.json")
    monkeypatch.setattr("services.acceptance.REPORT_PATH", tmp_path / "comm.json")
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "a.jsonl")
    monkeypatch.setattr("services.alerts.ALERTS_PATH_HOST", tmp_path / "nh.jsonl")
    record_provider_ok("gfw")
    monkeypatch.setattr("services.acceptance._probe_health_latency", lambda: 0.1)
    monkeypatch.setattr("services.vesselfinder_client.resolve_userkey", lambda: "")

    doc = _core_doc(gfw_verified_n=5)
    doc["gfw_status"] = {"configured": True}
    b1 = evaluate_acceptance(doc)
    stamp = b1["fully_commissioned_at"]
    assert b1["status"] == "GREEN"

    # Simulate 3 zero-verified days with active key
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]
    st = json.loads((tmp_path / "acc.json").read_text(encoding="utf-8"))
    st["zero_verified_days"] = days
    st["status"] = "GREEN"
    (tmp_path / "acc.json").write_text(json.dumps(st), encoding="utf-8")

    doc0 = _core_doc(gfw_verified_n=0)
    doc0["gfw_status"] = {"configured": True}
    b2 = evaluate_acceptance(doc0)
    assert b2["status"] == "DEGRADED"
    assert b2["fully_commissioned_at"] == stamp  # never erased
    assert b2.get("degraded_reason", "").startswith("verified_silence")


def test_commissioning_report_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.acceptance.REPORT_PATH", tmp_path / "r.json")
    blob = {
        "fully_commissioned_at": "2026-09-25T06:00:00Z",
        "evaluated_at": "2026-09-25T06:00:01Z",
        "transition": "WAITING_KEYS->GREEN",
        "checks": {
            "gfw_verified": {"n": 4},
            "vf_verified": {"n": 0},
            "terrestrial_covered": {"n": 40},
        },
    }
    path = write_commissioning_report(blob, _core_doc(gfw_verified_n=4))
    report = json.loads(path.read_text(encoding="utf-8"))
    for key in (
        "event",
        "status",
        "fully_commissioned_at",
        "git_head",
        "contract_version",
        "channels",
    ):
        assert key in report
    assert report["event"] == "commissioned"
    assert report["status"] == "GREEN"


def test_waiting_keys_stable_without_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.acceptance.STATE_PATH", tmp_path / "acc.json")
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.delenv("VESSELFINDER_API_KEY", raising=False)
    monkeypatch.delenv("VESSEL_FINDER_USERKEY", raising=False)
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    monkeypatch.setattr("services.acceptance._probe_health_latency", lambda: 0.2)
    monkeypatch.setattr("services.vesselfinder_client.resolve_userkey", lambda: "")
    blob = evaluate_acceptance(_core_doc())
    assert blob["status"] == "WAITING_KEYS"
    assert blob.get("fully_commissioned_at") is None
