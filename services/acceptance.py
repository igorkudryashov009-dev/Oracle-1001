#!/usr/bin/env python3
"""Acceptance + commissioning loop — WAITING_KEYS → GREEN → fully_commissioned_at.

GREEN = all non-skipped core checks OK AND ≥1 verification channel
(gfw OR vf) with verified_n > 0. Channels without keys stay skipped.
fully_commissioned_at is append-only (never overwritten).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "archive" / "acceptance_state.json"
REPORT_PATH = ROOT / "output" / "commissioning_report.json"
EXPECTED_FLEET_N = 1253
SILENCE_DAYS = 3
GFW_BUDGET_WARN_REMAINING = 50

LOG = logging.getLogger("sentinel.acceptance")


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
    # Preserve immutable commissioning timestamp
    prev = _load_state()
    if prev.get("fully_commissioned_at") and not st.get("fully_commissioned_at"):
        st["fully_commissioned_at"] = prev["fully_commissioned_at"]
    elif prev.get("fully_commissioned_at") and st.get("fully_commissioned_at"):
        # Never overwrite earlier stamp
        st["fully_commissioned_at"] = prev["fully_commissioned_at"]
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


def _git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except Exception:  # noqa: BLE001
        return (os.getenv("SENTINEL_GIT_HEAD") or "unknown")[:12]


def write_commissioning_report(blob: dict[str, Any], doc: dict[str, Any] | None = None) -> Path:
    """Pitch-ready commissioning artifact (no secrets)."""
    fa = (doc or {}).get("fleet_archive") or {}
    checks = blob.get("checks") or {}
    report = {
        "event": "commissioned",
        "status": "GREEN",
        "fully_commissioned_at": blob.get("fully_commissioned_at"),
        "evaluated_at": blob.get("evaluated_at") or _utc_iso(),
        "git_head": _git_head(),
        "contract_version": "1.8.0-ops-gis-sot",
        "channels": {
            "gfw_verified_n": int((checks.get("gfw_verified") or {}).get("n") or fa.get("gfw_verified_n") or 0),
            "vf_verified_n": int((checks.get("vf_verified") or {}).get("n") or fa.get("vf_verified_n") or 0),
            "terrestrial_covered_n": int(
                (checks.get("terrestrial_covered") or {}).get("n") or fa.get("terrestrial_covered_n") or 0
            ),
        },
        "pipeline_health_status": (doc or {}).get("pipeline_health_status")
        or (doc or {}).get("operational_status"),
        "disk_free_pct": (doc or {}).get("disk_free_pct"),
        "active_node": (doc or {}).get("active_node") or "korolev",
        "transition": blob.get("transition"),
        "note": "Sentinel fully commissioned — Dual Gate unchanged",
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(REPORT_PATH)
    return REPORT_PATH


def _notify_commissioned(report: dict[str, Any]) -> None:
    from services.alerts import emit_alert

    gfw_n = (report.get("channels") or {}).get("gfw_verified_n") or 0
    vf_n = (report.get("channels") or {}).get("vf_verified_n") or 0
    msg = f"Sentinel commissioned: GREEN, gfw_verified={gfw_n}, vf_verified={vf_n}"
    emit_alert(
        "commissioned",
        msg,
        severity="INFO",
        detail={
            "fully_commissioned_at": report.get("fully_commissioned_at"),
            "gfw_verified_n": gfw_n,
            "vf_verified_n": vf_n,
            "git_head": report.get("git_head"),
        },
    )


def _log_commissioned_event(blob: dict[str, Any]) -> None:
    """Immutable one-shot row in job_log (best-effort)."""
    try:
        from services.job_log import ensure_job_log_schema, log_job_finish, log_job_start

        ensure_job_log_schema()
        rid = log_job_start("commissioned")
        log_job_finish(
            rid,
            status="ok",
            rows_affected=1,
            detail={
                "event": "commissioned",
                "fully_commissioned_at": blob.get("fully_commissioned_at"),
                "transition": blob.get("transition"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("commissioned job_log write failed: %s", type(exc).__name__)


def _track_verified_silence(
    *,
    gfw_ver: int,
    vf_ver: int,
    keys_active: bool,
    prev: dict[str, Any],
) -> tuple[int, list[str]]:
    """Return (silence_streak_days, silence_days_list). Reset on any verified>0."""
    today = _utc_now().strftime("%Y-%m-%d")
    days: list[str] = list(prev.get("zero_verified_days") or [])
    if not keys_active:
        return 0, []
    total = int(gfw_ver) + int(vf_ver)
    if total > 0:
        return 0, []
    if today not in days:
        days.append(today)
    days = sorted(set(days))[-14:]
    # Count consecutive calendar days ending today
    streak = 0
    cur = _utc_now().date()
    dayset = set(days)
    for i in range(0, 14):
        d = (cur - timedelta(days=i)).strftime("%Y-%m-%d")
        if d in dayset:
            streak += 1
        else:
            break
    return streak, days


def evaluate_acceptance(
    doc: dict[str, Any] | None = None,
    *,
    trigger: str | None = None,
) -> dict[str, Any]:
    """Compute acceptance blob. Does not mutate Dual Gate."""
    built_doc = False
    if doc is None:
        from services.ais_health import build_health_document

        # Avoid recursion: acceptance_health_block may call us; build without acceptance re-entry
        doc = build_health_document()
        built_doc = True

    checks: dict[str, Any] = {}
    fa = doc.get("fleet_archive") or {}
    row_count = int(fa.get("row_count") or fa.get("expected_n") or 0)
    expected = int(fa.get("expected_n") or EXPECTED_FLEET_N)
    completeness = fa.get("completeness_pct")
    if completeness is None and expected:
        completeness = round(100.0 * row_count / expected, 2) if row_count else 0.0
    arch_ok = row_count == expected and float(completeness or 0) >= 100.0 - 1e-6
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
        if gfw_ver > 0:
            checks["gfw_verified"] = {"ok": True, "n": gfw_ver, "skipped": False}
        else:
            # Key present, data not yet — WAITING_KEYS, not hard DEGRADED
            checks["gfw_verified"] = {
                "ok": True,
                "n": 0,
                "skipped": True,
                "awaiting_data": True,
            }
    else:
        checks["gfw_verified"] = {"ok": True, "n": gfw_ver, "skipped": True}

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
        if vf_ver > 0 and (daily_used is None or daily_used <= 16):
            checks["vf_verified"] = {
                "ok": True,
                "n": vf_ver,
                "daily_used": daily_used,
                "skipped": False,
            }
        elif daily_used is not None and daily_used > 16:
            checks["vf_verified"] = {
                "ok": False,
                "n": vf_ver,
                "daily_used": daily_used,
                "skipped": False,
            }
        else:
            checks["vf_verified"] = {
                "ok": True,
                "n": vf_ver,
                "daily_used": daily_used,
                "skipped": True,
                "awaiting_data": True,
            }
    elif vf_key:
        # Key present but not activated — does not block GREEN if other channel delivers
        checks["vf_verified"] = {
            "ok": True,
            "n": vf_ver,
            "daily_used": daily_used,
            "skipped": True,
            "awaiting_activation": True,
        }
    else:
        checks["vf_verified"] = {"ok": True, "n": vf_ver, "skipped": True}

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

    # Non-skipped checks that must be ok for GREEN
    hard_fails = [
        name
        for name, c in checks.items()
        if isinstance(c, dict) and not c.get("skipped") and not c.get("ok")
    ]

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
    channel_verified = (gfw_configured and gfw_ver > 0) or (vf_active and vf_ver > 0)

    keys_active = gfw_configured or vf_active
    any_key_plane = gfw_configured or vf_key or vf_active

    prev = _load_state()
    silence_streak, zero_days = _track_verified_silence(
        gfw_ver=gfw_ver, vf_ver=vf_ver, keys_active=keys_active, prev=prev
    )

    if hard_fails:
        status = "DEGRADED"
    elif core_ok and channel_verified:
        status = "GREEN"
    elif core_ok and silence_streak >= SILENCE_DAYS and keys_active and prev.get("fully_commissioned_at"):
        # Provider silent 3d after commissioning
        status = "DEGRADED"
    elif core_ok and not any_key_plane:
        status = "WAITING_KEYS"
    elif core_ok and any_key_plane and not channel_verified:
        status = "WAITING_KEYS"
    else:
        status = "DEGRADED"

    blob: dict[str, Any] = {
        "status": status,
        "checks": checks,
        "evaluated_at": _utc_iso(),
        "trigger": trigger,
        "zero_verified_days": zero_days if keys_active else [],
        "silence_streak_days": silence_streak if keys_active else 0,
        "note": "WAITING_KEYS is legitimate — not an alert; Dual Gate unchanged",
    }
    prev_status = prev.get("status")
    blob["previous_status"] = prev_status
    blob["transition"] = None
    if prev_status and prev_status != status:
        blob["transition"] = f"{prev_status}->{status}"

    # Append-only commissioning stamp
    first_commission = False
    if prev.get("fully_commissioned_at"):
        blob["fully_commissioned_at"] = prev["fully_commissioned_at"]
    elif status == "GREEN":
        blob["fully_commissioned_at"] = _utc_iso()
        first_commission = True
        blob["transition"] = blob["transition"] or f"{prev_status or 'NEW'}->GREEN"

    if status == "DEGRADED" and silence_streak >= SILENCE_DAYS and keys_active:
        blob["degraded_reason"] = f"verified_silence_{silence_streak}d"
        from services.alerts import emit_alert

        emit_alert(
            "acceptance_verified_silence",
            f"verified_n=0 for {silence_streak} consecutive days with active keys",
            severity="WARN",
            detail={"silence_streak_days": silence_streak, "gfw_ver": gfw_ver, "vf_ver": vf_ver},
        )

    # Persist (preserves fully_commissioned_at)
    to_save = {**prev, **blob}
    _save_state(to_save)

    if first_commission:
        path = write_commissioning_report(blob, doc)
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {"fully_commissioned_at": blob.get("fully_commissioned_at"), "channels": {}}
        _notify_commissioned(report)
        _log_commissioned_event(blob)
        blob["commissioning_report"] = str(path.as_posix())

    _ = built_doc  # silence lint
    return blob


def maybe_rerun_acceptance_after_verification(
    *,
    channel: str,
    verified_hint: int | None = None,
    job_detail: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Event-driven acceptance after gfw_poll / vf_allocator produced verified data."""
    detail = job_detail or {}
    hint = verified_hint
    if hint is None:
        hint = int(
            detail.get("verified_n")
            or detail.get("gfw_verified_n")
            or detail.get("updated")
            or detail.get("billed")
            or detail.get("events_total")
            or detail.get("stored")
            or 0
        )
    if hint <= 0 and not detail.get("force"):
        return None
    # Invalidate fleet_archive cache so health sees fresh verified_n
    try:
        from services.archive_snapshot_worker import invalidate_fleet_archive_cache

        invalidate_fleet_archive_cache()
    except Exception:  # noqa: BLE001
        pass
    blob = evaluate_acceptance(trigger=f"verified_data_arrived:{channel}")
    return blob


def run_acceptance_check(*, trigger: str | None = None) -> dict[str, Any]:
    """Job entrypoint (daily timer or event-driven)."""
    blob = evaluate_acceptance(trigger=trigger or "scheduled")
    out: dict[str, Any] = {"ok": True, "acceptance": blob, "status": blob.get("status")}
    if blob.get("status") == "GREEN" and blob.get("transition") and "GREEN" in str(blob.get("transition")):
        out["commissioned"] = bool(blob.get("fully_commissioned_at"))
        out["rows"] = 1
    return out


def acceptance_health_block() -> dict[str, Any]:
    """Read-only health plane — never calls build_health_document (no recursion)."""
    st = _load_state()
    if not st.get("status"):
        return {
            "status": "WAITING_KEYS",
            "checks": {},
            "evaluated_at": None,
            "transition": None,
            "fully_commissioned_at": None,
            "note": "acceptance_check not yet run",
        }
    out: dict[str, Any] = {
        "status": st.get("status"),
        "checks": st.get("checks") or {},
        "evaluated_at": st.get("evaluated_at"),
        "transition": st.get("transition"),
        "fully_commissioned_at": st.get("fully_commissioned_at"),
        "silence_streak_days": st.get("silence_streak_days") or 0,
        "trigger": st.get("trigger"),
    }
    if REPORT_PATH.is_file():
        out["commissioning_report"] = str(REPORT_PATH.as_posix())
    return out


def gfw_budget_warn_block() -> dict[str, Any] | None:
    """WARN plane when GFW remaining < 50 (does not change Dual Gate)."""
    try:
        from services.gfw_events import get_budget_status, resolve_token

        if not resolve_token():
            return None
        bud = get_budget_status()
        rem = int(bud.get("remaining") or 0)
        if rem < GFW_BUDGET_WARN_REMAINING:
            return {
                "warn": True,
                "remaining": rem,
                "threshold": GFW_BUDGET_WARN_REMAINING,
                "message": f"gfw_budget remaining={rem} < {GFW_BUDGET_WARN_REMAINING}",
            }
        return {"warn": False, "remaining": rem, "threshold": GFW_BUDGET_WARN_REMAINING}
    except Exception:  # noqa: BLE001
        return None


__all__ = (
    "acceptance_health_block",
    "evaluate_acceptance",
    "gfw_budget_warn_block",
    "maybe_rerun_acceptance_after_verification",
    "run_acceptance_check",
    "write_commissioning_report",
)
