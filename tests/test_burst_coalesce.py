"""Parallel-miss coalescing for health, ops contour, and intel SWR."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


def test_ops_contour_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import ops_contour

    ops_contour.reset_cache()
    calls = {"n": 0}

    def _slow() -> dict:
        calls["n"] += 1
        time.sleep(0.15)
        return {"n": calls["n"]}

    monkeypatch.setattr(ops_contour, "build_ops_contour", _slow)
    out: list[dict] = []

    def _run() -> None:
        out.append(ops_contour.ops_contour_block())

    threads = [threading.Thread(target=_run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1
    assert len(out) == 8
    assert all(row is out[0] for row in out)
    ops_contour.reset_cache()


def test_swr_cold_miss_single_flight(tmp_path: Path) -> None:
    from services.cache_swr import swr_fetch

    calls = {"n": 0}
    path = tmp_path / "news.json"

    def _fresh() -> dict:
        calls["n"] += 1
        time.sleep(0.15)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        return {"ok": True, "fetched_at": now, "n": calls["n"]}

    out: list[dict] = []

    def _run() -> None:
        out.append(swr_fetch(cache_path=path, fresh_fetch=_fresh, cache_key="news-test"))

    threads = [threading.Thread(target=_run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1
    assert len(out) == 6
    assert all(row.get("ok") is True for row in out)


def test_listen_backlog_holds_a_parallel_burst() -> None:
    from scripts.serve_dashboard import _ReuseHTTPServer

    assert _ReuseHTTPServer.request_queue_size >= 32


def test_maptiles_status_is_auth_exempt() -> None:
    from scripts.serve_dashboard import api_v1_auth_exempt

    assert api_v1_auth_exempt("/api/v1/maptiles/status") is True
    assert api_v1_auth_exempt("/api/v1/gis/tiles/status") is True
    assert api_v1_auth_exempt("/api/v1/gis/compressor-stations") is False
    assert api_v1_auth_exempt("/api/v1/gis/ais/status") is False


def test_shared_health_document_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.serve_dashboard as sd

    sd._HEALTH_BUILD["doc"] = None
    sd._HEALTH_BUILD["at"] = 0.0
    calls = {"n": 0}

    def _slow() -> dict:
        calls["n"] += 1
        time.sleep(0.12)
        return {"pipeline_health_status": "NOMINAL", "n": calls["n"]}

    monkeypatch.setattr(sd, "build_health_document", _slow)
    out: list[dict] = []

    def _run() -> None:
        out.append(sd.shared_health_document())

    threads = [threading.Thread(target=_run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1
    assert len(out) == 8
    sd._HEALTH_BUILD["doc"] = None
    sd._HEALTH_BUILD["at"] = 0.0
