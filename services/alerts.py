#!/usr/bin/env python3
"""Fail-visible alerts for Sentinel automation (jsonl + optional webhook)."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.alerts")

ALERTS_PATH = ROOT / "logs" / "alerts.jsonl"
ALERTS_PATH_HOST = Path("/opt/oracle1001/logs/alerts.jsonl")
STATE_PATH = ROOT / "data" / "archive" / "alerts_state.json"

DEDUP_HOURS = 24

_LOCK = threading.RLock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        raw = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _path() -> Path:
    # Prefer Korolev host log dir when present (Linux Node A only).
    try:
        if ALERTS_PATH_HOST.parent.is_dir() and str(ALERTS_PATH_HOST).startswith("/opt/"):
            return ALERTS_PATH_HOST
    except OSError:
        pass
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    return ALERTS_PATH


def _load_state() -> dict[str, Any]:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.is_file():
        return {
            "active": [],
            "last_alert_at": None,
            "last_alert_kind": None,
            "resolved": [],
            "last_emit_at": {},
        }
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


def _append_jsonl(rec: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    month = _utc_now().strftime("%Y-%m")
    monthly = path.parent / f"alerts-{month}.jsonl"
    if monthly.name != path.name:
        with monthly.open("a", encoding="utf-8") as fh:
            fh.write(line)


WEBHOOK_BACKOFF_SEC = (60, 300, 900)
DEAD_LETTER_PATH = ROOT / "data" / "archive" / "alerts_deadletter.jsonl"


def deliver_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    post: Any,
    sleep: Any,
) -> dict[str, Any]:
    """Immediate POST, then 3 retries after 1, 5 and 15 minutes on 5xx or network errors."""
    attempts: list[Any] = []
    delays = (0,) + WEBHOOK_BACKOFF_SEC
    for delay in delays:
        if delay:
            sleep(delay)
        try:
            resp = post(url, json=payload, timeout=8)
            code = int(getattr(resp, "status_code", 0) or 0)
            attempts.append(code)
            if code and code < 500:
                return {"ok": 200 <= code < 400, "attempts": attempts, "dead_letter": False}
        except Exception as exc:  # noqa: BLE001
            attempts.append(type(exc).__name__)
    rec = {
        "ts": _utc_iso(),
        "kind": payload.get("kind"),
        "attempts": attempts,
        "dead_letter": True,
    }
    try:
        DEAD_LETTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DEAD_LETTER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return {"ok": False, "attempts": attempts, "dead_letter": True}


def time_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _start_webhook(url: str, payload: dict[str, Any]) -> None:
    import threading

    def _run() -> None:
        import requests

        deliver_webhook(url, payload, post=requests.post, sleep=time_sleep)

    threading.Thread(target=_run, name="alert-webhook", daemon=True).start()


def rotate_alert_files(active: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """One alerts-YYYY-MM.jsonl per month. Gzip copies older than two months."""
    import gzip
    import shutil

    now = now or _utc_now()
    active = active or _path()
    active.parent.mkdir(parents=True, exist_ok=True)
    month = now.strftime("%Y-%m")
    current = active.parent / f"alerts-{month}.jsonl"
    if active.is_file() and active.name != current.name and not current.exists():
        shutil.copyfile(active, current)
    cutoff_month = (now.replace(day=1) - timedelta(days=62)).strftime("%Y-%m")
    gzipped = 0
    for path in list(active.parent.glob("alerts-*.jsonl")):
        stamp = path.stem.replace("alerts-", "")
        if len(stamp) == 7 and stamp < cutoff_month:
            gz = path.with_suffix(".jsonl.gz")
            with path.open("rb") as src, gzip.open(gz, "wb") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
            gzipped += 1
    return {"ok": True, "month": month, "gzipped": gzipped, "current": current.name}


def _dedup_key(kind: str, detail: dict[str, Any] | None) -> str:
    provider = ""
    if detail:
        provider = str(detail.get("provider") or "")
    if not provider and kind.startswith("provider_paused_"):
        provider = kind.replace("provider_paused_", "", 1)
    return f"{kind}|{provider}"


def emit_alert(
    kind: str,
    message: str,
    *,
    severity: str = "WARN",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append alert to jsonl; optional webhook. Dedup identical unresolved ≤1/24h."""
    detail = detail or {}
    dkey = _dedup_key(kind, detail)
    now = _utc_now()
    with _LOCK:
        st = _load_state()
        last_emit = dict(st.get("last_emit_at") or {})
        last_at = _parse_iso(last_emit.get(dkey))
        active = [a for a in (st.get("active") or []) if isinstance(a, dict)]
        already_active = any(a.get("kind") == kind for a in active)
        if already_active and last_at and (now - last_at) < timedelta(hours=DEDUP_HOURS):
            return None  # suppress noise

        rec = {
            "at": _utc_iso(now),
            "ts": _utc_iso(now),
            "kind": str(kind),
            "severity": str(severity),
            "message": str(message)[:500],
            "detail": detail,
            "status": "active",
        }
        _append_jsonl(rec)
        # delivery_log for webhook audit (no secrets)
        try:
            dlog = ROOT / "data" / "archive" / "alert_delivery_log.jsonl"
            dlog.parent.mkdir(parents=True, exist_ok=True)
            with dlog.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": rec["ts"],
                            "kind": rec["kind"],
                            "severity": rec["severity"],
                            "queued": True,
                            "webhook_configured": False,  # updated below
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass
        active = [a for a in active if a.get("kind") != kind][-20:]
        active.append(
            {
                "kind": kind,
                "severity": severity,
                "at": rec["at"],
                "message": rec["message"],
                "status": "active",
            }
        )
        st["active"] = active[-30:]
        st["last_alert_at"] = rec["at"]
        st["last_alert_kind"] = kind
        last_emit[dkey] = rec["at"]
        st["last_emit_at"] = last_emit
        _save_state(st)

    webhook = ""
    try:
        from services.runtime_env import getenv_secret

        webhook = getenv_secret("ALERT_WEBHOOK_URL")
    except Exception:  # noqa: BLE001
        webhook = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if webhook:
        _start_webhook(webhook, rec)
    return rec


def resolve_alert(kind: str, *, reason: str = "auto") -> dict[str, Any] | None:
    """Mark alert resolved in state + jsonl (auto-resolve on successful probe)."""
    with _LOCK:
        st = _load_state()
        active = [a for a in (st.get("active") or []) if isinstance(a, dict)]
        hit = next((a for a in active if a.get("kind") == kind), None)
        if not hit and kind not in {(a.get("kind") for a in active)}:
            # Still allow resolve record if previously known
            pass
        was_active = hit is not None
        st["active"] = [a for a in active if a.get("kind") != kind]
        resolved_list = [r for r in (st.get("resolved") or []) if isinstance(r, dict)]
        rec = {
            "at": _utc_iso(),
            "kind": str(kind),
            "status": "resolved",
            "resolved_at": _utc_iso(),
            "reason": str(reason)[:120],
            "message": (hit or {}).get("message") or f"resolved:{kind}",
        }
        resolved_list.append(rec)
        # Keep 7d of resolved for health.resolved_24h_n
        cutoff = _utc_now() - timedelta(days=7)
        pruned: list[dict[str, Any]] = []
        for r in resolved_list:
            dt = _parse_iso(r.get("resolved_at") or r.get("at"))
            if dt and dt >= cutoff:
                pruned.append(r)
        st["resolved"] = pruned[-100:]
        _save_state(st)
        _append_jsonl(rec)
        return rec if was_active or True else None


def clear_alert(kind: str) -> None:
    """Backward-compatible: resolve + drop from active."""
    resolve_alert(kind, reason="clear")


def alerts_health_block() -> dict[str, Any]:
    st = _load_state()
    active = [a for a in (st.get("active") or []) if isinstance(a, dict)]
    resolved = [r for r in (st.get("resolved") or []) if isinstance(r, dict)]
    cutoff = _utc_now() - timedelta(hours=24)
    resolved_24h = 0
    for r in resolved:
        dt = _parse_iso(r.get("resolved_at") or r.get("at"))
        if dt and dt >= cutoff:
            resolved_24h += 1
    webhook = False
    try:
        from services.runtime_env import getenv_secret

        webhook = bool(getenv_secret("ALERT_WEBHOOK_URL"))
    except Exception:  # noqa: BLE001
        webhook = bool((os.getenv("ALERT_WEBHOOK_URL") or "").strip())
    return {
        "active_n": len(active),
        "last_alert_at": st.get("last_alert_at"),
        "last_alert_kind": st.get("last_alert_kind"),
        "active": active[-10:],
        "resolved_24h_n": resolved_24h,
        "webhook_configured": webhook,
    }


__all__ = (
    "alerts_health_block",
    "clear_alert",
    "DEAD_LETTER_PATH",
    "deliver_webhook",
    "emit_alert",
    "resolve_alert",
    "rotate_alert_files",
)
