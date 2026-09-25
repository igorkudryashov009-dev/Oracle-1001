#!/usr/bin/env python3
"""Sentinel zero-touch job runner — invoked by systemd timers via docker exec.

Usage:
  python -m services.scheduler --job archive_snapshot
  python -m services.scheduler --list
  python -m services.scheduler --status
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.job_log import (  # noqa: E402
    JOB_SCHEDULE,
    ensure_job_log_schema,
    log_job_finish,
    log_job_start,
    scheduler_health_block,
)

LOG = logging.getLogger("sentinel.scheduler")


def _job_archive_snapshot() -> dict[str, Any]:
    from services.archive_snapshot_worker import take_daily_snapshot

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r1 = take_daily_snapshot(snapshot_date=yesterday, export=False)
    r2 = take_daily_snapshot(snapshot_date=today, export=False)
    return {
        "yesterday": r1,
        "today": {"ok": r2.get("ok"), "stored": r2.get("stored_for_date"), "overlay": r2.get("live_ais_overlay")},
        "rows": int(r1.get("stored_for_date") or 0),
    }


def _job_gfw_poll() -> dict[str, Any]:
    from services.gfw_events import resolve_token, run_daily_gfw_batch
    from services.key_activation import is_provider_paused, provider_state

    if not resolve_token():
        return {"skipped": True, "reason": "gfw_not_configured"}
    if is_provider_paused("gfw"):
        return {"skipped": True, "reason": "gfw_paused"}
    st = provider_state("gfw")
    if st.get("last_ok") is not True:
        return {"skipped": True, "reason": "awaiting_gfw_activation"}
    out = run_daily_gfw_batch(limit=20, dry_run=False)
    # Event-driven acceptance (do not wait for 02:00)
    try:
        from services.acceptance import maybe_rerun_acceptance_after_verification

        verified = int(out.get("updated") or out.get("events_total") or out.get("fetched") or 0)
        if out.get("ok") and verified > 0:
            acc = maybe_rerun_acceptance_after_verification(
                channel="gfw", verified_hint=verified, job_detail=out
            )
            if acc:
                out["acceptance"] = {
                    "status": acc.get("status"),
                    "transition": acc.get("transition"),
                    "fully_commissioned_at": acc.get("fully_commissioned_at"),
                    "trigger": acc.get("trigger"),
                }
    except Exception:  # noqa: BLE001
        pass
    return out


def _job_vf_allocator() -> dict[str, Any]:
    from services.vf_allocator_runner import run_vf_allocator_live

    out = run_vf_allocator_live(dry_run=False)
    try:
        from services.acceptance import maybe_rerun_acceptance_after_verification

        billed = int(out.get("billed") or 0)
        if out.get("ok") and not out.get("skipped") and billed > 0:
            acc = maybe_rerun_acceptance_after_verification(
                channel="vf", verified_hint=billed, job_detail=out
            )
            if acc:
                out["acceptance"] = {
                    "status": acc.get("status"),
                    "transition": acc.get("transition"),
                    "fully_commissioned_at": acc.get("fully_commissioned_at"),
                    "trigger": acc.get("trigger"),
                }
    except Exception:  # noqa: BLE001
        pass
    return out


def _job_pipeline_watchdog() -> dict[str, Any]:
    from services.watchdog import run_pipeline_watchdog

    return run_pipeline_watchdog()


def _job_budget_sync() -> dict[str, Any]:
    """Touch budget status objects (side effect: refresh ledger reads for health)."""
    from services.gfw_events import get_budget_status as gfw_budget
    from services.gfw_events import gfw_status
    from services.vesselfinder_budget import get_budget_status
    from services.vf_budget_allocator import allocator_status

    return {
        "vf": get_budget_status(),
        "vf_allocator": allocator_status(),
        "gfw": gfw_budget(),
        "gfw_status": {k: gfw_status().get(k) for k in ("configured", "last_ok", "events_7d_n")},
    }


def _job_acceptance_check() -> dict[str, Any]:
    from services.acceptance import run_acceptance_check

    return run_acceptance_check()


JOB_HANDLERS: dict[str, Callable[[], dict[str, Any]]] = {
    "archive_snapshot": _job_archive_snapshot,
    "gfw_poll": _job_gfw_poll,
    "vf_allocator": _job_vf_allocator,
    "pipeline_watchdog": _job_pipeline_watchdog,
    "budget_sync": _job_budget_sync,
    "acceptance_check": _job_acceptance_check,
}


def run_job(job_name: str) -> dict[str, Any]:
    if job_name not in JOB_HANDLERS:
        raise ValueError(f"unknown job: {job_name}")
    ensure_job_log_schema()
    row_id = log_job_start(job_name)
    try:
        detail = JOB_HANDLERS[job_name]()
        skipped = bool(detail.get("skipped"))
        status = "skipped" if skipped else ("ok" if detail.get("ok", True) else "error")
        rows = int(
            detail.get("rows")
            or detail.get("billed")
            or detail.get("fetched")
            or detail.get("events_total")
            or detail.get("stored_for_date")
            or 0
        )
        if status == "error":
            err = str(detail.get("error") or detail.get("reason") or "job_error")
            log_job_finish(row_id, status=status, rows_affected=rows, error=err, detail=detail)
            from services.alerts import emit_alert

            emit_alert(f"job_error_{job_name}", err, severity="WARN", detail={"job": job_name})
        else:
            log_job_finish(row_id, status=status, rows_affected=rows, detail=detail)
            # Overdue clear when job succeeds
            from services.alerts import clear_alert

            clear_alert("scheduler_overdue")
        return {"job": job_name, "status": status, "rows_affected": rows, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        LOG.error("job %s failed: %s\n%s", job_name, err, traceback.format_exc())
        log_job_finish(row_id, status="error", error=err[:500])
        try:
            from services.alerts import emit_alert

            emit_alert(f"job_error_{job_name}", err[:200], severity="WARN")
        except Exception:  # noqa: BLE001
            pass
        return {"job": job_name, "status": "error", "error": err}


def check_overdue_alerts() -> dict[str, Any]:
    from services.alerts import clear_alert, emit_alert

    block = scheduler_health_block()
    n = int(block.get("overdue_jobs_n") or 0)
    if n > 0:
        emit_alert(
            "scheduler_overdue",
            f"overdue_jobs_n={n}: {block.get('overdue_jobs')}",
            severity="WARN",
            detail={"overdue": block.get("overdue_jobs")},
        )
    else:
        clear_alert("scheduler_overdue")
    return block


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job", choices=sorted(JOB_HANDLERS.keys()), help="Run one job")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--check-overdue", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        print(json.dumps(JOB_SCHEDULE, indent=2))
        return 0
    if args.status or args.check_overdue:
        block = check_overdue_alerts() if args.check_overdue else scheduler_health_block()
        print(json.dumps(block, indent=2, ensure_ascii=False))
        return 0
    if not args.job:
        ap.error("--job required (or --list/--status)")
    out = run_job(args.job)
    # Safe print — strip any accidental secrets
    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    for env_key in ("VESSELFINDER_API_KEY", "GFW_API_TOKEN", "AISSTREAM_API_KEY"):
        import os

        val = (os.getenv(env_key) or "").strip()
        if val and val in text:
            text = text.replace(val, "***")
    print(text)
    return 0 if out.get("status") in {"ok", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
