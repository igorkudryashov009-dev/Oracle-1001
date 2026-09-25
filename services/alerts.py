#!/usr/bin/env python3
"""Fail-visible alerts for Sentinel automation (jsonl + optional webhook)."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.alerts")

ALERTS_PATH = ROOT / "logs" / "alerts.jsonl"
ALERTS_PATH_HOST = Path("/opt/oracle1001/logs/alerts.jsonl")
STATE_PATH = ROOT / "data" / "archive" / "alerts_state.json"

_LOCK = threading.RLock()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path() -> Path:
    if ALERTS_PATH_HOST.parent.is_dir():
        return ALERTS_PATH_HOST
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    return ALERTS_PATH


def _load_state() -> dict[str, Any]:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.is_file():
        return {"active": [], "last_alert_at": None, "last_alert_kind": None}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"active": []}
    except (OSError, json.JSONDecodeError):
        return {"active": []}


def _save_state(st: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def emit_alert(
    kind: str,
    message: str,
    *,
    severity: str = "WARN",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append alert to jsonl; optional ALERT_WEBHOOK_URL POST (never logs secrets)."""
    rec = {
        "at": _utc_iso(),
        "kind": str(kind),
        "severity": str(severity),
        "message": str(message)[:500],
        "detail": detail or {},
    }
    with _LOCK:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        st = _load_state()
        active = [a for a in (st.get("active") or []) if isinstance(a, dict)]
        active = [a for a in active if a.get("kind") != kind][-20:]
        active.append({"kind": kind, "severity": severity, "at": rec["at"], "message": rec["message"]})
        st["active"] = active[-30:]
        st["last_alert_at"] = rec["at"]
        st["last_alert_kind"] = kind
        _save_state(st)

    webhook = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if webhook:
        try:
            import requests

            requests.post(webhook, json=rec, timeout=8)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("alert webhook failed: %s", type(exc).__name__)
    return rec


def clear_alert(kind: str) -> None:
    with _LOCK:
        st = _load_state()
        st["active"] = [a for a in (st.get("active") or []) if a.get("kind") != kind]
        _save_state(st)


def alerts_health_block() -> dict[str, Any]:
    st = _load_state()
    active = [a for a in (st.get("active") or []) if isinstance(a, dict)]
    return {
        "active_n": len(active),
        "last_alert_at": st.get("last_alert_at"),
        "last_alert_kind": st.get("last_alert_kind"),
        "active": active[-10:],
        "webhook_configured": bool((os.getenv("ALERT_WEBHOOK_URL") or "").strip()),
    }


__all__ = ("alerts_health_block", "clear_alert", "emit_alert")
