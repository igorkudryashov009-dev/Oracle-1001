"""Unit tests for weekly_gas_carrier_monitor — no live network."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import weekly_gas_carrier_monitor as mon


def _fleet_csv(path: Path, n: int = 12) -> Path:
    rows = []
    for i in range(n):
        rows.append(
            {
                "imo": 9100000 + i,
                "vessel_name": f"GAS TEST {i}",
                "vessel_category": "vessel",
                "vessel_type": "LNG Tanker" if i % 2 == 0 else "LPG Tanker (VLGC)",
                "flag": "Marshall Islands",
                "dwt_tons": 100000 - i * 1000,
                "compliance_risk_level": (
                    "ВЫСОКИЙ РИСК" if i < 2 else ("СРЕДНИЙ РИСК" if i < 5 else "НИЗКИЙ РИСК")
                ),
                "mmsi": 200000000 + i,
            }
        )
    rows.append(
        {
            "imo": 9999999,
            "vessel_name": "CRUDE ONLY",
            "vessel_category": "vessel",
            "vessel_type": "Crude Oil Tanker (VLCC)",
            "flag": "Panama",
            "dwt_tons": 300000,
            "compliance_risk_level": "ВЫСОКИЙ РИСК",
            "mmsi": 211111111,
        }
    )
    df = pd.DataFrame(rows)
    path.write_text(df.to_csv(index=False), encoding="utf-8")
    return path


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> mon.MonitorConfig:
    monkeypatch.setenv("PROVIDER_API_KEY", "test-key-not-a-placeholder")
    monkeypatch.setenv("PROVIDER_COST_PER_CALL_USD", "1.0")
    monkeypatch.setenv("PROVIDER_MONTHLY_BUDGET_USD", "50")
    with patch.object(mon, "load_dotenv", lambda *a, **k: None):
        fleet = _fleet_csv(tmp_path / "fleet.csv", n=12)
        return mon.load_config(
            env_file=tmp_path / "no.env",
            fleet_csv=fleet,
            out_dir=tmp_path / "out",
            monthly_budget_usd=50.0,
        )


def test_config_missing_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("PROVIDER_COST_PER_CALL_USD", "0.05")
    with patch.object(mon, "load_dotenv", lambda *a, **k: None):
        with pytest.raises(mon.ConfigError, match="PROVIDER_API_KEY"):
            mon.load_config(env_file=tmp_path / "no.env", fleet_csv=tmp_path / "x.csv")


def test_config_invalid_cost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PROVIDER_API_KEY", "test-key-not-a-placeholder")
    monkeypatch.setenv("PROVIDER_COST_PER_CALL_USD", "not-a-number")
    with patch.object(mon, "load_dotenv", lambda *a, **k: None):
        with pytest.raises(mon.ConfigError, match="PROVIDER_COST_PER_CALL_USD"):
            mon.load_config(env_file=tmp_path / "no.env")


def test_normal_run_with_http_mock(cfg: mon.MonitorConfig):
    def fake_api(imo, _cfg, **_kwargs):
        return {
            "imo": str(imo),
            "provider": "mock",
            "fetched_at_utc": "2026-08-12T00:00:00Z",
            "raw": {"AIS": {"LATITUDE": 1.0, "LONGITUDE": 2.0}},
        }

    out = mon.run_monitor(cfg, call_api=fake_api)
    assert out["summary"]["success_count"] == out["summary"]["selected_for_poll"]
    assert out["summary"]["error_count"] == 0
    assert (cfg.out_dir / "summary.json").exists()
    assert (cfg.out_dir / "results.json").exists()
    assert (cfg.out_dir / "mission_control_module.json").exists()
    mc = json.loads((cfg.out_dir / "mission_control_module.json").read_text(encoding="utf-8"))
    assert mc["id"] == "gas_weekly"
    assert "status" in mc and "metrics" in mc and "tables" in mc


def test_budget_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROVIDER_API_KEY", "test-key-not-a-placeholder")
    monkeypatch.setenv("PROVIDER_COST_PER_CALL_USD", "2.0")
    with patch.object(mon, "load_dotenv", lambda *a, **k: None):
        cfg = mon.load_config(
            env_file=tmp_path / "no.env",
            fleet_csv=_fleet_csv(tmp_path / "fleet.csv", n=15),
            out_dir=tmp_path / "out",
            monthly_budget_usd=20.0,
        )
    # floor(20/4.345/2) = floor(2.30) = 2
    assert cfg.weekly_call_budget == 2

    calls: list[Any] = []

    def fake_api(imo, _cfg, **_kwargs):
        calls.append(imo)
        return {"imo": str(imo), "provider": "mock", "fetched_at_utc": "t", "raw": {}}

    out = mon.run_monitor(cfg, call_api=fake_api)
    summary = out["summary"]
    assert summary["budget_trimmed"] is True
    assert summary["coverage_of_top_n_target_pct"] < 100
    assert summary["selected_for_poll"] == 2
    assert summary["gas_carriers_total"] == 15
    assert len(calls) == 2
    assert len(out["results"]) == 2


def test_network_error_recorded(cfg: mon.MonitorConfig):
    def boom(imo, _cfg, **_kwargs):
        raise mon.ProviderError("Network error: timed out", retryable=False)

    cfg = cfg.model_copy(update={"monthly_budget_usd": 50.0, "provider_cost_per_call_usd": 10.0})
    out = mon.run_monitor(cfg, limit=5, call_api=boom)
    assert out["summary"]["success_count"] == 0
    assert out["summary"]["error_count"] == 1
    assert out["errors"][0]["status"] == "error"
    assert "timed out" in out["errors"][0]["error"]


def test_call_provider_retries_then_ok(cfg: mon.MonitorConfig):
    import urllib.error

    class FakeResp:
        def __init__(self, payload: Any, status: int = 200):
            self._payload = payload
            self.status = status

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def getcode(self):
            return self.status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    attempts = {"n": 0}

    def opener(req, timeout=30):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.URLError("temporary failure")
        return FakeResp([{"AIS": {"MMSI": 1}}])

    sleeper = MagicMock()
    result = mon.call_provider_api(9123456, cfg, opener=opener, sleeper=sleeper)
    assert result["imo"] == "9123456"
    assert attempts["n"] == 3
    assert sleeper.call_count == 2


def test_call_provider_auth_no_retry(cfg: mon.MonitorConfig):
    import urllib.error

    def opener(req, timeout=30):
        raise urllib.error.HTTPError(
            url="http://x",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b"bad key"),
        )

    sleeper = MagicMock()
    with pytest.raises(mon.AuthError):
        mon.call_provider_api(9123456, cfg, opener=opener, sleeper=sleeper)
    assert sleeper.call_count == 0
