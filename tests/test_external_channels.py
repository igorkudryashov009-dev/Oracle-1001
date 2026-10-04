"""VF file install and satellite activation. No live HTTP."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from services.key_activation import probe_vesselfinder
from services.key_install import install_from_file, vessel_finder_autoinstall
from services.runtime_env import write_runtime_key
from services.satellite_adapter import allocate_budget, probe


class _Resp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.setattr("services.alerts.STATE_PATH", tmp_path / "alerts_state.json")
    monkeypatch.setattr("services.alerts.ALERTS_PATH", tmp_path / "alerts.jsonl")
    monkeypatch.setattr("services.alerts.ALERTS_PATH_HOST", tmp_path / "nohost.jsonl")
    monkeypatch.setattr("services.runtime_env.RUNTIME_ENV_PATH", tmp_path / "runtime.json")
    monkeypatch.setattr("services.runtime_env.SIGNAL_PATH", tmp_path / "signal")


def test_install_from_file_probe_reads_runtime_not_image_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    secret = "vf-from-file-OK99"
    key_file = tmp_path / "vessel_finder.key"
    key_file.write_text(f"  {secret}\r\n", encoding="utf-8")
    monkeypatch.setenv("VESSELFINDER_API_KEY", "stale-image-key-OLD1")
    monkeypatch.setenv("VESSEL_FINDER_USERKEY", "stale-image-key-OLD1")
    mask = install_from_file("VESSEL_FINDER", key_file, env_path=tmp_path / ".env")
    assert mask == "****OK99"
    assert secret not in mask
    monkeypatch.setenv("VESSELFINDER_API_KEY", "stale-image-key-OLD1")
    monkeypatch.setenv("VESSEL_FINDER_USERKEY", "stale-image-key-OLD1")
    seen: dict[str, str] = {}

    def fake_get(*_a, params=None, **_k):
        seen["userkey"] = str((params or {}).get("userkey") or "")
        return _Resp(200, {"AIS": {"IMO": "9388819"}})

    monkeypatch.setattr("requests.get", fake_get)
    out = probe_vesselfinder(force=True)
    assert out["status"] == "active"
    assert out["key_masked"] == "****OK99"
    assert seen["userkey"] == secret
    assert secret not in json.dumps(out)
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"VESSELFINDER_API_KEY={secret}" in env_text
    assert f"VESSEL_FINDER_USERKEY={secret}" in env_text
    assert (tmp_path / "signal").read_text(encoding="utf-8").strip() == "key_installed:vesselfinder"


def test_vessel_finder_sha_skips_until_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    key_file = tmp_path / "vessel_finder.key"
    key_file.write_text("vf-from-file-OK99\n", encoding="utf-8")
    stamp = tmp_path / "vessel_finder.key.sha256"
    digest = hashlib.sha256(key_file.read_bytes()).hexdigest()
    stamp.write_text(digest + "\n", encoding="utf-8")
    assert vessel_finder_autoinstall(key_file, stamp, env_path=tmp_path / ".env") == "skip"
    assert not (tmp_path / "runtime.json").exists()
    key_file.write_text("vf-from-file-NEW1\n", encoding="utf-8")
    assert vessel_finder_autoinstall(key_file, stamp, env_path=tmp_path / ".env") == "installed"
    assert stamp.read_text(encoding="utf-8").strip() == hashlib.sha256(key_file.read_bytes()).hexdigest()
    key_file.write_text("short\n", encoding="utf-8")
    assert vessel_finder_autoinstall(key_file, stamp, env_path=tmp_path / ".env") == "refused"


def test_satellite_probe_parked_without_network_and_active_from_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.satellite_adapter as sat

    source = inspect.getsource(sat)
    assert "https://" not in source
    monkeypatch.setattr(sat, "_default_transport", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("SATELLITE_API_KEY", raising=False)
    monkeypatch.delenv("SAT_PROVIDER", raising=False)
    monkeypatch.delenv("SAT_BASE_URL", raising=False)
    calls = {"n": 0}

    def transport(url: str, headers: dict[str, str]) -> dict:
        calls["n"] += 1
        raise AssertionError(url)

    parked = probe(transport=transport)
    assert parked == {"status": "parked", "requests": 0, "reason": "not_activated"}
    assert calls["n"] == 0

    secret = "sat-file-key-OK99"
    key_file = tmp_path / "satellite.key"
    key_file.write_text(secret + "\n", encoding="utf-8")
    mask = install_from_file(
        "SATELLITE",
        key_file,
        env_path=tmp_path / ".env",
        sat_provider="spire",
    )
    assert mask == "****OK99"
    write_runtime_key("SAT_BASE_URL", "https://vendor.example/v1")
    monkeypatch.setenv("SATELLITE_API_KEY", "stale-image-key-OLD1")
    monkeypatch.setenv("SAT_PROVIDER", "iceye")
    seen: dict[str, str] = {}

    def ok_transport(url: str, headers: dict[str, str]) -> dict:
        calls["n"] += 1
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        return {
            "data": [
                {
                    "imo": 9,
                    "latitude": 1.0,
                    "longitude": 2.0,
                    "timestamp": "2026-10-04T00:00:00Z",
                }
            ]
        }

    active = probe(transport=ok_transport)
    assert active["status"] == "active"
    assert active["requests"] == 1
    assert seen["auth"] == f"Bearer {secret}"
    assert seen["url"].startswith("https://vendor.example/v1")
    assert secret not in json.dumps(active)
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SAT_PROVIDER=spire" in env_text


def test_satellite_top500_gap_priority_and_provenance_gate(tmp_path: Path) -> None:
    from services.archive_schema import VESSEL_DAILY_ARCHIVE_DDL
    from services.satellite_adapter import store_satellite_rows

    assert allocate_budget([1, 2, 3, 4], used_today=0, cap=2, top500_imos=[4, 2]) == [2, 4]
    assert allocate_budget([1, 2, 3, 4], used_today=0, cap=3, top500_imos=[4]) == [4, 1, 2]
    assert allocate_budget([9, 9, 8, 7], used_today=1, cap=3) == [9, 8]

    db = tmp_path / "v.db"
    conn = __import__("sqlite3").connect(str(db))
    conn.execute(VESSEL_DAILY_ARCHIVE_DDL)
    written = store_satellite_rows(
        conn,
        [
            {"imo": 10, "lat": 5, "lon": 6},
            {"imo": 11, "lat": float("nan"), "lon": 1, "provenance": {"provider": "spire", "observed_at": "t"}},
            {
                "imo": 12,
                "lat": 1,
                "lon": 2,
                "provenance": {"provider": "spire", "observed_at": "2026-10-04T00:00:00Z"},
            },
        ],
        snapshot_date="2026-10-04",
    )
    conn.commit()
    stored = conn.execute("SELECT imo, source FROM vessel_daily_archive").fetchall()
    conn.close()
    assert written == 1
    assert stored == [(12, "satellite_ais")]


def test_satellite_without_endpoint_stays_parked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch, tmp_path)
    install_from_file(
        "SATELLITE",
        _write(tmp_path / "satellite.key", "sat-file-key-OK99"),
        env_path=tmp_path / ".env",
        sat_provider="unseenlabs",
    )
    monkeypatch.delenv("SAT_BASE_URL", raising=False)
    calls = {"n": 0}

    def transport(_url: str, _headers: dict[str, str]) -> dict:
        calls["n"] += 1
        return {}

    out = probe(transport=transport)
    assert out["status"] == "parked"
    assert out["requests"] == 0
    assert calls["n"] == 0


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path
