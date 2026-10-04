"""In-process caches for HUD catalogs, compressor stations, and health."""
from __future__ import annotations

import time
from pathlib import Path

import pytest


def test_catalog_bytes_do_not_touch_disk_again(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import i18n_catalog as cat

    cat.reset_cache()
    raw, packed = cat.catalog_body("en")
    assert raw.startswith(b"{")
    assert len(packed) < len(raw)

    def _boom(*_a, **_k):
        raise AssertionError("disk hit")

    monkeypatch.setattr(Path, "read_text", _boom)
    again, _gz = cat.catalog_body("ru")
    assert b'"lang":"ru"' in again or b'"lang": "ru"' in again
    cat.reset_cache()


def test_compressor_payload_is_memory_cached() -> None:
    from services.compressor_stations import (
        build_gis_stations_payload,
        gis_stations_body,
        reset_gis_cache,
    )

    reset_gis_cache()
    first, gz = gis_stations_body()
    second, _gz2 = gis_stations_body()
    assert first is second
    assert len(gz) < len(first)
    payload = build_gis_stations_payload()
    assert payload["count"] == 185
    assert payload["total_count"] == 185


def test_stale_health_does_not_block_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.ais_health as health

    health.reset_health_doc_cache()
    health._HEALTH_DOC_CACHE["doc"] = {"pipeline_health_status": "NOMINAL", "marker": 1}
    health._HEALTH_DOC_CACHE["ts"] = time.monotonic() - 30
    calls = {"n": 0}

    def _slow() -> dict:
        calls["n"] += 1
        time.sleep(0.25)
        return {"pipeline_health_status": "NOMINAL", "marker": 2}

    monkeypatch.setattr(health, "_build_health_document_uncached", _slow)
    started = time.perf_counter()
    out = health.build_health_document()
    assert time.perf_counter() - started < 0.1
    assert out["marker"] == 1
    assert out["cache_stale"] is True
    time.sleep(0.4)
    health.reset_health_doc_cache()


def test_static_gzip_cache_follows_mtime(tmp_path: Path) -> None:
    from scripts.serve_dashboard import _accepts_gzip, _cached_file_bytes

    path = tmp_path / "hud.html"
    path.write_text("<html>" + ("sentinel " * 200) + "</html>", encoding="utf-8")
    plain, packed = _cached_file_bytes(path)
    assert packed is not None and len(packed) < len(plain)
    again, packed2 = _cached_file_bytes(path)
    assert again is plain and packed2 is packed
    assert _accepts_gzip("gzip, deflate, br") is True
    assert _accepts_gzip(None) is False
