#!/usr/bin/env python3
"""Map-tile paid-provider monthly budget tracker (VesselFinder-pattern).

Counts only real upstream hits to the paid provider — cache hits and Esri
fallback fetches do NOT increment ``used``.

Env:
  MAPTILES_MONTHLY_BUDGET          default 50000
  MAPTILES_BUDGET_WARN_REMAINING   default 2000
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
LOG = logging.getLogger("sentinel.maptiles_budget")

BUDGET_PATH = ROOT / "data" / "archive" / "maptiles_budget.json"
DEFAULT_MONTHLY_BUDGET = 50_000
DEFAULT_WARN_REMAINING = 2_000

_LOCK = threading.RLock()


class MaptilesBudgetExhausted(RuntimeError):
    """Raised when the monthly paid tile budget is exhausted."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    d = dt or _utc_now()
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def monthly_budget() -> int:
    try:
        return max(1, int(os.getenv("MAPTILES_MONTHLY_BUDGET", str(DEFAULT_MONTHLY_BUDGET))))
    except ValueError:
        return DEFAULT_MONTHLY_BUDGET


def warn_remaining() -> int:
    try:
        return max(0, int(os.getenv("MAPTILES_BUDGET_WARN_REMAINING", str(DEFAULT_WARN_REMAINING))))
    except ValueError:
        return DEFAULT_WARN_REMAINING


def _month_key(dt: Optional[datetime] = None) -> str:
    d = dt or _utc_now()
    return d.astimezone(timezone.utc).strftime("%Y-%m")


def _empty_state(month: Optional[str] = None) -> dict[str, Any]:
    m = month or _month_key()
    b = monthly_budget()
    return {
        "month": m,
        "monthly_budget": b,
        "used": 0,
        "remaining": b,
        "warn_remaining": warn_remaining(),
        "warn": False,
        "updated_at": _utc_iso(),
        "last_call_at": None,
        "last_provider": None,
        "last_ok": None,
        "last_detail": None,
        "history": [],
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
    data["warn"] = data["remaining"] <= data["warn_remaining"]
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
    state["warn_remaining"] = warn_remaining()
    state["warn"] = state["remaining"] <= state["warn_remaining"]
    tmp = BUDGET_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(BUDGET_PATH)


def get_budget_status() -> dict[str, Any]:
    with _LOCK:
        return dict(_load_unlocked())


def reserve_or_raise(n: int = 1) -> dict[str, Any]:
    """Reserve ``n`` paid upstream units or raise MaptilesBudgetExhausted."""
    n = max(1, int(n))
    with _LOCK:
        state = _load_unlocked()
        remaining = int(state.get("remaining") or 0)
        if remaining < n:
            raise MaptilesBudgetExhausted(
                f"maptiles budget exhausted: remaining={remaining} need={n} month={state.get('month')}"
            )
        state["used"] = int(state.get("used") or 0) + n
        _save_unlocked(state)
        if state["warn"]:
            LOG.warning(
                "maptiles budget warn remaining=%s threshold=%s",
                state["remaining"],
                state["warn_remaining"],
            )
        return dict(state)


def record_result(
    *,
    provider: str,
    ok: bool,
    detail: str | None = None,
    refund: bool = False,
) -> dict[str, Any]:
    """Update last_* fields; optionally refund 1 unit if upstream never charged."""
    with _LOCK:
        state = _load_unlocked()
        if refund and int(state.get("used") or 0) > 0:
            state["used"] = int(state["used"]) - 1
        state["last_call_at"] = _utc_iso()
        state["last_provider"] = provider
        state["last_ok"] = bool(ok)
        state["last_detail"] = (detail or "")[:240] or None
        hist = list(state.get("history") or [])
        hist.append(
            {
                "at": state["last_call_at"],
                "provider": provider,
                "ok": bool(ok),
                "detail": state["last_detail"],
            }
        )
        state["history"] = hist[-40:]
        _save_unlocked(state)
        return dict(state)
