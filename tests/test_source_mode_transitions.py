"""Truth Contract: source_mode auto-transitions (STALE ↔ live_ais)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _freshness(*, age_sec: float, integrity_ok: bool = True, missing: bool = False) -> dict:
    from services.ais_health import LIVE_OK_LAG_SEC, STALE_LAG_SEC

    if missing:
        return {
            "stale": True,
            "live_ok": False,
            "status": "MISSING_REPLICA",
            "ais_truth": "missing",
            "age_sec": None,
        }
    live_ok = age_sec < LIVE_OK_LAG_SEC and integrity_ok
    stale = age_sec > STALE_LAG_SEC or not integrity_ok
    if live_ok:
        status = "FRESH"
    elif not integrity_ok:
        status = "CORRUPT"
    elif stale:
        status = "STALE"
    else:
        status = "WARMING"
    return {
        "stale": stale,
        "live_ok": live_ok,
        "status": status,
        "ais_truth": "live_ok" if live_ok else ("stale" if stale else "warming"),
        "age_sec": age_sec,
        "integrity_ok": integrity_ok,
    }


def test_strip_stale_tag_idempotent():
    from services.ais_health import strip_stale_tag

    assert strip_stale_tag("live_ais+STALE") == "live_ais"
    assert strip_stale_tag("live_ais+STALE+STALE") == "live_ais"
    assert strip_stale_tag("live_ais") == "live_ais"
    assert strip_stale_tag(None) == "live_ais"


def test_source_mode_auto_clears_stale_when_lag_under_300():
    from services.ais_health import resolve_source_mode

    # Sticky previous mode must NOT pin STALE forever
    fr = _freshness(age_sec=120.0)
    assert resolve_source_mode(fr, base_mode="live_ais+STALE") == "live_ais"
    assert resolve_source_mode(fr) == "live_ais"


def test_source_mode_marks_stale_when_lag_over_600():
    from services.ais_health import resolve_source_mode

    fr = _freshness(age_sec=900.0)
    assert resolve_source_mode(fr, base_mode="live_ais") == "live_ais+STALE"
    # No double-tagging
    assert resolve_source_mode(fr, base_mode="live_ais+STALE") == "live_ais+STALE"


def test_source_mode_warming_band_no_stale_tag():
    from services.ais_health import resolve_source_mode

    fr = _freshness(age_sec=450.0)  # 300 < age <= 600
    assert fr["live_ok"] is False
    assert fr["stale"] is False
    assert resolve_source_mode(fr, base_mode="live_ais+STALE") == "live_ais"


def test_mocked_message_stream_state_transitions(monkeypatch, tmp_path):
    """Mock AIS message timestamps with delays; verify STALE → live_ais transition."""
    from services import ais_health

    now = time.time()
    # Sequence of (received_at epoch offsets) simulating stream catch-up
    timeline = [
        now - 1200.0,  # STALE (>600)
        now - 400.0,   # WARMING (300..600)
        now - 60.0,    # LIVE (<300)
    ]
    modes: list[str] = []

    for epoch in timeline:
        def _fake_compute(*, db_path=None, live_ok_lag_sec=300, stale_after_sec=600, _e=epoch):
            age = max(0.0, time.time() - _e)
            live_ok = age < float(live_ok_lag_sec)
            stale = age > float(stale_after_sec)
            return {
                "stale": stale,
                "live_ok": live_ok,
                "status": "FRESH" if live_ok else ("STALE" if stale else "WARMING"),
                "ais_truth": "live_ok" if live_ok else ("stale" if stale else "warming"),
                "age_sec": age,
                "integrity_ok": True,
                "latest_ts": datetime.fromtimestamp(_e, tz=timezone.utc).isoformat(),
                "signal_source": "mock_stream",
            }

        monkeypatch.setattr(ais_health, "compute_ais_freshness", _fake_compute)
        # Sticky prior mode as if health.json still said +STALE
        doc = ais_health.build_health_document(source_mode="live_ais+STALE")
        modes.append(doc["source_mode"])

    assert modes[0] == "live_ais+STALE"
    assert modes[1] == "live_ais"  # WARMING clears sticky STALE automatically
    assert modes[2] == "live_ais"  # live_ok
    assert "STALE" not in modes[2]
