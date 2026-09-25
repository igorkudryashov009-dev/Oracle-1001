#!/usr/bin/env python3
"""Pipeline self-healing watchdog — workers only, never edge-gateway."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.watchdog")
STATE_PATH = ROOT / "data" / "archive" / "watchdog_state.json"

AIS_LAG_STREAK_LIMIT = 3
AIS_LAG_THRESHOLD_SEC = 300.0
REPLICA_RESTART_SEC = 120.0
REPLICA_WARN_SEC = 600.0


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state() -> dict[str, Any]:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.is_file():
        return {"ais_lag_streak": 0, "actions": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"ais_lag_streak": 0}
    except (OSError, json.JSONDecodeError):
        return {"ais_lag_streak": 0}


def _save_state(st: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _docker_restart(container: str) -> dict[str, Any]:
    """Queue worker restart for host wrapper — never sentinel-web.

    Jobs run via ``docker exec``; docker CLI is unavailable inside the
    container. Write a one-shot request under ``data/archive/`` (bind-mounted);
    ``run_sentinel_job.sh`` on the host performs ``docker restart``.
    """
    name = container.strip()
    if name in {"sentinel-web", "sentinel_api_edge"}:
        return {"ok": False, "error": "edge_gateway_restart_forbidden", "container": name}
    req = ROOT / "data" / "archive" / "watchdog_restart_request"
    try:
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text(name + "\n", encoding="utf-8")
        return {"ok": True, "queued": True, "container": name, "request_path": str(req)}
    except OSError as exc:
        return {"ok": False, "container": name, "error": str(exc)[:160]}


def _prune_tile_caches(*, max_age_sec: float) -> dict[str, Any]:
    removed = 0
    bytes_freed = 0
    roots = [
        ROOT / "data" / "tile_cache",
        ROOT / "output" / "tile_cache",
        Path("/app/data/tile_cache"),
        Path("/app/output/tile_cache"),
    ]
    now = time.time()
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                age = now - p.stat().st_mtime
                if age > max_age_sec:
                    sz = p.stat().st_size
                    p.unlink(missing_ok=True)
                    removed += 1
                    bytes_freed += sz
            except OSError:
                continue
    return {"removed": removed, "bytes_freed": bytes_freed}


def _disk_guard(disk_free_pct: float | None) -> dict[str, Any]:
    from services.alerts import emit_alert
    from services.log_retention import run_retention

    out: dict[str, Any] = {"triggered": False}
    if disk_free_pct is None:
        return out
    if disk_free_pct < 20.0:
        out["triggered"] = True
        out["retention"] = run_retention(max_days=14)
        out["tiles"] = _prune_tile_caches(max_age_sec=14 * 86400)
        emit_alert(
            "disk_guard",
            f"disk_free_pct={disk_free_pct:.1f} — retention + tile prune",
            severity="WARN" if disk_free_pct >= 10.0 else "CRITICAL",
            detail={"disk_free_pct": disk_free_pct},
        )
    return out


def run_pipeline_watchdog() -> dict[str, Any]:
    """Single watchdog tick: lag streak, disk, replica, key activation."""
    from services.alerts import clear_alert, emit_alert
    from services.ais_health import build_health_document
    from services.key_activation import run_key_activation_cycle

    st = _load_state()
    actions: list[dict[str, Any]] = []
    doc = build_health_document()
    replica = doc.get("replica") or {}
    age = float(replica.get("age_sec") or doc.get("ais_lag_sec") or 0.0)
    # Prefer explicit lag if present
    lag = doc.get("pipeline_health") or {}
    if isinstance(lag, dict) and lag.get("ais_lag_sec") is not None:
        age = float(lag.get("ais_lag_sec") or age)
    # top-level ais lag aliases used in some builds
    for k in ("ais_lag_sec", "live_lag_sec"):
        if doc.get(k) is not None:
            try:
                age = float(doc[k])
            except (TypeError, ValueError):
                pass

    disk_free = doc.get("disk_free_pct")
    try:
        disk_free_f = float(disk_free) if disk_free is not None else None
    except (TypeError, ValueError):
        disk_free_f = None

    # W1 AIS lag streak → restart sentinel-core (ingest), never web
    if age > AIS_LAG_THRESHOLD_SEC:
        streak = int(st.get("ais_lag_streak") or 0) + 1
    else:
        streak = 0
        clear_alert("ais_lag_streak")
    st["ais_lag_streak"] = streak
    st["last_ais_lag_sec"] = age
    st["last_check_at"] = _utc_iso()

    if streak >= AIS_LAG_STREAK_LIMIT:
        act = _docker_restart("sentinel-core")
        act["reason"] = f"ais_lag_streak={streak} lag={age:.1f}s"
        actions.append(act)
        emit_alert(
            "ais_lag_streak",
            f"AIS lag {age:.0f}s for {streak} checks — restarted sentinel-core",
            severity="WARN",
            detail=act,
        )
        st["ais_lag_streak"] = 0  # reset after action

    # W2 disk
    disk_act = _disk_guard(disk_free_f)
    if disk_act.get("triggered"):
        actions.append({"disk_guard": disk_act})

    # W3 replica age
    if age > REPLICA_WARN_SEC:
        emit_alert(
            "replica_stale",
            f"replica/ais age_sec={age:.0f} > {REPLICA_WARN_SEC:.0f}",
            severity="WARN",
            detail={"age_sec": age},
        )
    elif age > REPLICA_RESTART_SEC:
        # Soft recovery: restart core ingest (not web)
        act = _docker_restart("sentinel-core")
        act["reason"] = f"replica_age={age:.1f}s"
        actions.append(act)
        clear_alert("replica_stale")
    else:
        clear_alert("replica_stale")

    # K1 key activation (same 15-min cadence)
    keys = run_key_activation_cycle()
    actions.append({"key_activation": keys})

    st["actions"] = (st.get("actions") or [])[-20:] + [
        {"at": _utc_iso(), "n": len(actions)}
    ]
    _save_state(st)
    return {
        "ok": True,
        "ais_lag_sec": age,
        "ais_lag_streak": st.get("ais_lag_streak"),
        "disk_free_pct": disk_free_f,
        "pipeline_health_status": doc.get("pipeline_health_status"),
        "actions": actions,
        "key_activation": keys,
    }


__all__ = ("run_pipeline_watchdog",)
