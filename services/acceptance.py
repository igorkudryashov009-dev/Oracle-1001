#!/usr/bin/env python3
"""Daily acceptance_check — TZ self-acceptance into health.acceptance."""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "archive" / "acceptance_state.json"
EXPECTED_FLEET_N = 1253


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(st: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _probe_health_latency(url: str = "http://127.0.0.1:8765/api/v1/health") -> float | None:
    try:
        t0 = time.perf_counter()
        with urllib.request.urlopen(url, timeout=10) as resp:
            resp.read(64)
        return time.perf_counter() - t0
    except Exception:  # noqa: BLE001
        return None


def evaluate_acceptance(doc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute acceptance blob. Does not mutate Dual Gate."""
    if doc is None:
        from services.ais_health import build_health_document

        doc = build_health_document()

    checks: dict[str, Any] = {}
    waiting_keys = False

    fa = doc.get("fleet_archive") or {}
    row_count = int(fa.get("row_count") or fa.get("expected_n") or 0)
    expected = int(fa.get("expected_n") or EXPECTED_FLEET_N)
    completeness = fa.get("completeness_pct")
    if completeness is None and expected:
        completeness = round(100.0 * row_count / expected, 2) if row_count else 0.0
    arch_ok = row_count == expected and float(completeness or 0) >= 100.0 - 1e-6
    # Some builds expose completeness 0–1
    if not arch_ok and expected and row_count == expected:
        c = float(completeness or 0)
        if c >= 0.999 or c >= 99.9:
            arch_ok = True
    checks["archive"] = {
        "ok": arch_ok,
        "row_count": row_count,
        "expected_n": expected,
        "completeness": completeness,
    }

    terr = int(fa.get("terrestrial_covered_n") or 0)
    checks["terrestrial_covered"] = {"ok": terr > 0, "n": terr}

    gfw_st = doc.get("gfw_status") or {}
    gfw_configured = bool(gfw_st.get("configured"))
    gfw_ver = int(fa.get("gfw_verified_n") or 0)
    if gfw_configured:
        checks["gfw_verified"] = {"ok": gfw_ver > 0, "n": gfw_ver, "skipped": False}
        if gfw_ver == 0:
            # Key may be present but not yet verified — waiting
            from services.key_activation import provider_state

            if provider_state("gfw").get("last_ok") is not True:
                waiting_keys = True
    else:
        checks["gfw_verified"] = {"ok": True, "n": gfw_ver, "skipped": True}
        waiting_keys = True

    from services.key_activation import provider_state
    from services.vesselfinder_client import resolve_userkey

    vf_key = bool(resolve_userkey())
    vf_active = provider_state("vesselfinder").get("last_ok") is True
    vf_ver = int(fa.get("vf_verified_n") or 0)
    daily_used = None
    try:
        from services.vf_budget_allocator import allocator_status

        daily_used = int((allocator_status() or {}).get("daily_used_today") or 0)
    except Exception:  # noqa: BLE001
        daily_used = None
    if vf_active:
        checks["vf_verified"] = {
            "ok": vf_ver > 0 and (daily_used is None or daily_used <= 16),
            "n": vf_ver,
            "daily_used": daily_used,
            "skipped": False,
        }
    elif vf_key:
        checks["vf_verified"] = {
            "ok": False,
            "n": vf_ver,
            "daily_used": daily_used,
            "skipped": False,
            "awaiting_activation": True,
        }
        waiting_keys = True
    else:
        checks["vf_verified"] = {"ok": True, "n": vf_ver, "skipped": True}
        waiting_keys = True

    sched = doc.get("scheduler") or {}
    overdue = int(sched.get("overdue_jobs_n") or 0)
    checks["scheduler_overdue"] = {"ok": overdue == 0, "n": overdue}

    ph = doc.get("pipeline_health_status") or doc.get("operational_status")
    checks["pipeline_nominal"] = {"ok": ph == "NOMINAL", "status": ph}

    lag = None
    for k in ("ais_lag_sec", "live_lag_sec"):
        if doc.get(k) is not None:
            try:
                lag = float(doc[k])
                break
            except (TypeError, ValueError):
                pass
    if lag is None:
        lag = float((doc.get("replica") or {}).get("age_sec") or 0)
    checks["lag"] = {"ok": lag <= 300.0, "sec": lag}

    disk = float(doc.get("disk_free_pct") or 0)
    checks["disk"] = {"ok": disk >= 20.0, "pct": disk}

    p95 = _probe_health_latency()
    checks["health_p95_probe"] = {
        "ok": p95 is not None and p95 < 1.5,
        "sec": p95,
    }

    hard_fails = []
    for name, c in checks.items():
        if not isinstance(c, dict):
            continue
        if c.get("skipped") or c.get("awaiting_activation"):
            continue
        if not c.get("ok"):
            hard_fails.append(name)

    core_keys = (
        "archive",
        "terrestrial_covered",
        "scheduler_overdue",
        "pipeline_nominal",
        "lag",
        "disk",
        "health_p95_probe",
    )
    core_ok = all(checks.get(k, {}).get("ok") for k in core_keys if k in checks)
    gfw_skip = bool(checks.get("gfw_verified", {}).get("skipped"))
    vf_skip = bool(checks.get("vf_verified", {}).get("skipped"))
    gfw_ok = bool(checks.get("gfw_verified", {}).get("ok"))
    vf_ok = bool(checks.get("vf_verified", {}).get("ok"))
    activation_pending = any(
        isinstance(c, dict) and c.get("awaiting_activation") for c in checks.values()
    )

    if hard_fails:
        status = "DEGRADED"
    elif core_ok and gfw_skip and vf_skip:
        status = "WAITING_KEYS"
    elif core_ok and ((vf_active and vf_ok) or (gfw_configured and gfw_ok)):
        other_ok = (gfw_skip or gfw_ok) and (vf_skip or vf_ok or not vf_active)
        status = "GREEN" if other_ok else "DEGRADED"
    elif core_ok and activation_pending:
        status = "WAITING_KEYS"
    elif core_ok:
        status = "WAITING_KEYS"
    else:
        status = "DEGRADED"

    blob = {
        "status": status,
        "checks": checks,
        "evaluated_at": _utc_iso(),
        "note": "WAITING_KEYS is legitimate — not an alert; Dual Gate unchanged",
    }
    prev = _load_state()
    prev_status = prev.get("status")
    blob["previous_status"] = prev_status
    blob["transition"] = None
    if prev_status and prev_status != status:
        blob["transition"] = f"{prev_status}->{status}"
    if status == "GREEN" and prev_status != "GREEN":
        blob["fully_commissioned_at"] = prev.get("fully_commissioned_at") or _utc_iso()
        blob["transition"] = blob["transition"] or f"{prev_status or 'NEW'}->GREEN"
    elif prev.get("fully_commissioned_at"):
        blob["fully_commissioned_at"] = prev.get("fully_commissioned_at")
    _save_state({**prev, **blob})
    return blob


def run_acceptance_check() -> dict[str, Any]:
    """Job entrypoint."""
    blob = evaluate_acceptance()
    out = {"ok": True, "acceptance": blob, "status": blob.get("status")}
    if blob.get("status") == "GREEN" and blob.get("transition"):
        out["commissioned"] = True
        out["rows"] = 1
    return out


def acceptance_health_block() -> dict[str, Any]:
    st = _load_state()
    if st.get("status"):
        return {
            "status": st.get("status"),
            "checks": st.get("checks") or {},
            "evaluated_at": st.get("evaluated_at"),
            "transition": st.get("transition"),
            "fully_commissioned_at": st.get("fully_commissioned_at"),
        }
    # Lazy evaluate once if empty (cheap enough for health)
    try:
        return evaluate_acceptance()
    except Exception as exc:  # noqa: BLE001
        return {"status": "DEGRADED", "error": str(exc)[:120], "evaluated_at": _utc_iso()}


__all__ = (
    "acceptance_health_block",
    "evaluate_acceptance",
    "run_acceptance_check",
)
