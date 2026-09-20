"""Stale-while-revalidate helpers for intel adapters — Contract 1.8.0-ops-gis-sot."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger("sentinel.cache_swr")

# Serve cache immediately up to STALE_TTL; background refresh after REVALIDATE_TTL.
REVALIDATE_TTL_SEC = 120.0
STALE_TTL_SEC = 3600.0 * 6

_locks: dict[str, threading.Lock] = {}
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    if key not in _locks:
        _locks[key] = threading.Lock()
    return _locks[key]


def parse_fetched_at(payload: dict[str, Any] | None) -> float | None:
    if not payload:
        return None
    raw = payload.get("fetched_at") or payload.get("updated_at")
    if not raw:
        return None
    try:
        ts = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def cache_age_sec(payload: dict[str, Any] | None) -> float | None:
    ts = parse_fetched_at(payload)
    if ts is None:
        return None
    return max(0.0, time.time() - ts)


def load_json_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def save_json_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("cache write failed %s: %s", path, exc)


def swr_fetch(
    *,
    cache_path: Path,
    fresh_fetch: Callable[[], dict[str, Any]],
    revalidate_ttl_sec: float = REVALIDATE_TTL_SEC,
    stale_ttl_sec: float = STALE_TTL_SEC,
    cache_key: str | None = None,
) -> dict[str, Any]:
    """Return cache immediately when present; refresh in background when aging.

    If cache missing/expired beyond stale_ttl, block on fresh_fetch.
    """
    key = cache_key or str(cache_path)
    cached = load_json_cache(cache_path)
    age = cache_age_sec(cached)

    def _mark(payload: dict[str, Any], *, cached_flag: bool, swr: bool) -> dict[str, Any]:
        out = dict(payload)
        out["is_cached"] = cached_flag
        out["swr"] = swr
        out["cache_age_sec"] = age
        return out

    def _bg_refresh() -> None:
        with _inflight_lock:
            if key in _inflight:
                return
            _inflight.add(key)
        try:
            fresh = fresh_fetch()
            if isinstance(fresh, dict) and fresh.get("ok", True):
                save_json_cache(cache_path, fresh)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("SWR background refresh failed (%s): %s", key, exc)
        finally:
            with _inflight_lock:
                _inflight.discard(key)

    if cached is not None and age is not None and age <= stale_ttl_sec:
        if age >= revalidate_ttl_sec:
            threading.Thread(target=_bg_refresh, name=f"swr-{key}", daemon=True).start()
        return _mark(cached, cached_flag=True, swr=age >= revalidate_ttl_sec)

    # Cold / too-stale: blocking fetch
    with _lock_for(key):
        try:
            fresh = fresh_fetch()
            if isinstance(fresh, dict):
                save_json_cache(cache_path, fresh)
                return _mark(fresh, cached_flag=False, swr=False)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("SWR fresh fetch failed (%s): %s", key, exc)
            if cached is not None:
                out = _mark(cached, cached_flag=True, swr=True)
                out["cache_fallback"] = True
                out["errors"] = list(out.get("errors") or []) + [f"swr_fresh_fail:{exc}"]
                return out
            raise
    if cached is not None:
        return _mark(cached, cached_flag=True, swr=True)
    return {
        "ok": False,
        "is_cached": False,
        "swr": False,
        "errors": ["swr_no_cache_no_fresh"],
    }


__all__ = (
    "REVALIDATE_TTL_SEC",
    "STALE_TTL_SEC",
    "swr_fetch",
    "load_json_cache",
    "save_json_cache",
    "cache_age_sec",
)
