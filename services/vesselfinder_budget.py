#!/usr/bin/env python3
"""VesselFinder monthly request budget tracker (hard cap for Premium credit plans).

Persists usage in ``data/archive/vesselfinder_budget.json`` (gitignored data dir).
Every commercial HTTP call to api.vesselfinder.com MUST go through
``reserve_or_raise`` / ``record_spend`` so the 500/mo envelope is visible and
cannot be silently exhausted.

Env:
  VESSELFINDER_MONTHLY_BUDGET   default 500
  VESSELFINDER_BUDGET_WARN_REMAINING  default 20
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.vesselfinder_budget")

BUDGET_PATH = ROOT / "data" / "archive" / "vesselfinder_budget.json"
DEFAULT_MONTHLY_BUDGET = 500
DEFAULT_WARN_REMAINING = 20

_LOCK = threading.RLock()


class BudgetExhausted(RuntimeError):
    """Raised when the monthly VesselFinder request budget is exhausted."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    d = dt or _utc_now()
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def monthly_budget() -> int:
    try:
        return max(1, int(os.getenv("VESSELFINDER_MONTHLY_BUDGET", str(DEFAULT_MONTHLY_BUDGET))))
    except ValueError:
        return DEFAULT_MONTHLY_BUDGET


def warn_remaining() -> int:
    try:
        return max(0, int(os.getenv("VESSELFINDER_BUDGET_WARN_REMAINING", str(DEFAULT_WARN_REMAINING))))
    except ValueError:
        return DEFAULT_WARN_REMAINING


def _month_key(dt: Optional[datetime] = None) -> str:
    d = dt or _utc_now()
    return d.astimezone(timezone.utc).strftime("%Y-%m")


def _empty_state(month: Optional[str] = None) -> dict[str, Any]:
    m = month or _month_key()
    return {
        "month": m,
        "monthly_budget": monthly_budget(),
        "used": 0,
        "remaining": monthly_budget(),
        "warn_remaining": warn_remaining(),
        "updated_at": _utc_iso(),
        "last_call_at": None,
        "last_endpoint": None,
        "last_imo": None,
        "last_ok": None,
        "history": [],  # truncated recent calls
    }


def _load_unlocked() -> dict[str, Any]:
    BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not BUDGET_PATH.is_file():
        return _empty_state()
    try:
        data = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    month = _month_key()
    if str(data.get("month") or "") != month:
        # Calendar month rollover — reset counter, keep audit note
        rolled = _empty_state(month)
        rolled["previous_month"] = data.get("month")
        rolled["previous_used"] = data.get("used")
        return rolled
    budget = monthly_budget()
    used = max(0, int(data.get("used") or 0))
    data["monthly_budget"] = budget
    data["used"] = used
    data["remaining"] = max(0, budget - used)
    data["warn_remaining"] = warn_remaining()
    if not isinstance(data.get("history"), list):
        data["history"] = []
    return data


def _save_unlocked(state: dict[str, Any]) -> None:
    BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _utc_iso()
    budget = int(state.get("monthly_budget") or monthly_budget())
    used = int(state.get("used") or 0)
    state["monthly_budget"] = budget
    state["used"] = used
    state["remaining"] = max(0, budget - used)
    tmp = BUDGET_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(BUDGET_PATH)


def get_budget_status() -> dict[str, Any]:
    """Public snapshot for health.json / diagnostics (no secrets)."""
    with _LOCK:
        st = _load_unlocked()
        rem = int(st.get("remaining") or 0)
        warn_at = int(st.get("warn_remaining") or warn_remaining())
        status = "OK"
        if rem <= 0:
            status = "EXHAUSTED"
        elif rem <= warn_at:
            status = "LOW"
        return {
            "provider": "vesselfinder",
            "month": st.get("month"),
            "monthly_budget": int(st.get("monthly_budget") or monthly_budget()),
            "used": int(st.get("used") or 0),
            "remaining": rem,
            "warn_remaining_threshold": warn_at,
            "status": status,
            "updated_at": st.get("updated_at"),
            "last_call_at": st.get("last_call_at"),
            "last_endpoint": st.get("last_endpoint"),
            "last_imo": st.get("last_imo"),
            "last_ok": st.get("last_ok"),
            "ledger_path": str(BUDGET_PATH.relative_to(ROOT)).replace("\\", "/"),
        }


def reserve_or_raise(n: int = 1, *, endpoint: str = "", imo: str | None = None) -> dict[str, Any]:
    """Atomically check remaining budget. Does not spend until ``record_spend``."""
    if n < 1:
        raise ValueError("n must be >= 1")
    with _LOCK:
        st = _load_unlocked()
        rem = int(st.get("remaining") or 0)
        if rem < n:
            LOG.error(
                "VesselFinder budget EXHAUSTED month=%s used=%s budget=%s need=%s endpoint=%s",
                st.get("month"),
                st.get("used"),
                st.get("monthly_budget"),
                n,
                endpoint,
            )
            raise BudgetExhausted(
                f"VesselFinder monthly budget exhausted "
                f"({st.get('used')}/{st.get('monthly_budget')} in {st.get('month')}); "
                f"refusing call to {endpoint or 'api'}"
            )
        if rem - n <= warn_remaining():
            LOG.warning(
                "VesselFinder budget LOW remaining=%s (threshold=%s) after planned spend=%s endpoint=%s imo=%s",
                rem - n,
                warn_remaining(),
                n,
                endpoint,
                imo,
            )
        return {
            "ok": True,
            "remaining_before": rem,
            "would_remain": rem - n,
            "month": st.get("month"),
        }


def record_spend(
    n: int = 1,
    *,
    endpoint: str,
    imo: str | None = None,
    ok: bool = True,
    detail: str | None = None,
    estimated_credits: float | None = None,
) -> dict[str, Any]:
    """Commit spend after an HTTP attempt (count failed auth attempts too — they still hit VF)."""
    if n < 1:
        n = 1
    with _LOCK:
        st = _load_unlocked()
        st["used"] = int(st.get("used") or 0) + n
        st["last_call_at"] = _utc_iso()
        st["last_endpoint"] = endpoint
        st["last_imo"] = imo
        st["last_ok"] = bool(ok)
        hist = list(st.get("history") or [])
        hist.append(
            {
                "at": st["last_call_at"],
                "n": n,
                "endpoint": endpoint,
                "imo": imo,
                "ok": bool(ok),
                "detail": (detail or "")[:200] or None,
                "estimated_credits": estimated_credits,
            }
        )
        st["history"] = hist[-100:]
        _save_unlocked(st)
        status = get_budget_status()
        if status["status"] == "LOW":
            LOG.warning(
                "VesselFinder budget remaining=%s/%s (%s)",
                status["remaining"],
                status["monthly_budget"],
                status["month"],
            )
        return status
