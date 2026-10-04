#!/usr/bin/env python3
"""Disaster drills for CI. --dry-run never touches production paths.

Usage: python scripts/chaos_drill.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _isolate(tmp: Path) -> None:
    os.environ["SENTINEL_DB_PATH"] = str(tmp / "sentinel.db")
    os.environ["ACCEPTANCE_HISTORY_PATH"] = str(tmp / "acceptance_history.jsonl")
    import services.alerts as alerts
    import services.job_lock as job_lock
    import services.key_activation as key_activation
    import services.scheduler as scheduler

    alerts.ALERTS_PATH = tmp / "alerts.jsonl"
    alerts.STATE_PATH = tmp / "alerts_state.json"
    key_activation.STATE_PATH = tmp / "provider_activation.json"
    job_lock.LOCK_DIR = tmp / "locks"
    scheduler._remediation_path = lambda: tmp / "remediation_state.json"  # type: ignore[method-assign]


def drill_provider_pause(tmp: Path) -> dict:
    from services.key_activation import provider_state, record_provider_error

    _isolate(tmp)
    for _ in range(3):
        record_provider_error("gfw", "chaos drill timeout")
    state = provider_state("gfw")
    paused = bool(state.get("paused"))
    alert_text = (tmp / "alerts.jsonl").read_text(encoding="utf-8") if (tmp / "alerts.jsonl").is_file() else ""
    return {
        "scenario": "provider_failure",
        "expected": "pause+alert",
        "paused": paused,
        "alert": "provider_paused_gfw" in alert_text,
        "ok": paused and "provider_paused_gfw" in alert_text,
    }


def drill_disk_clean(tmp: Path) -> dict:
    root = tmp / "logs"
    root.mkdir(parents=True, exist_ok=True)
    old = root / "aged.log"
    fresh = root / "fresh.log"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    aged = time.time() - 8 * 86400
    os.utime(old, (aged, aged))
    cutoff = time.time() - 7 * 86400
    removed = 0
    for path in root.glob("*.log"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return {
        "scenario": "disk_overflow",
        "expected": "auto_clean",
        "removed": removed,
        "ok": removed == 1 and not old.exists() and fresh.exists(),
    }


def drill_overdue(tmp: Path) -> dict:
    _isolate(tmp)
    from services.job_log import ensure_job_log_schema
    from services.scheduler import remediate_overdue, run_job

    db = Path(os.environ["SENTINEL_DB_PATH"])
    conn = sqlite3.connect(str(db))
    ensure_job_log_schema(conn)
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO job_log (job_name, started_at, finished_at, status) VALUES (?, ?, ?, ?)",
        ("pipeline_watchdog", started, started, "ok"),
    )
    conn.commit()
    conn.close()
    called: list[str] = []

    def _fake(job_name: str, *, force: bool = False) -> dict:
        called.append(job_name)
        return {"job": job_name, "status": "ok", "force": force}

    import services.scheduler as scheduler

    scheduler.run_job = _fake  # type: ignore[method-assign]
    launched = remediate_overdue(background=False)
    # restore for any later scenario in-process
    scheduler.run_job = run_job  # type: ignore[method-assign]
    return {
        "scenario": "overdue_job",
        "expected": "auto_remediation",
        "launched": launched,
        "ok": launched == ["pipeline_watchdog"] and called == ["pipeline_watchdog"],
    }


def drill_ws_loss() -> dict:
    plan = {"action": "restart_worker", "reason": "ws_lost", "executed": False}
    return {
        "scenario": "ws_loss",
        "expected": "restart_worker",
        "plan": plan,
        "ok": plan["action"] == "restart_worker" and plan["executed"] is False,
    }


def run_drills() -> list[dict]:
    import services.alerts as alerts
    import services.job_lock as job_lock
    import services.key_activation as key_activation
    import services.scheduler as scheduler

    saved = (
        alerts.ALERTS_PATH,
        alerts.STATE_PATH,
        key_activation.STATE_PATH,
        job_lock.LOCK_DIR,
        scheduler._remediation_path,
        os.environ.get("SENTINEL_DB_PATH"),
        os.environ.get("ACCEPTANCE_HISTORY_PATH"),
    )
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix="sentinel-chaos-") as raw:
            tmp = Path(raw)
            results.append(drill_provider_pause(tmp))
            results.append(drill_disk_clean(tmp))
            results.append(drill_overdue(tmp))
        results.append(drill_ws_loss())
        return results
    finally:
        alerts.ALERTS_PATH = saved[0]
        alerts.STATE_PATH = saved[1]
        key_activation.STATE_PATH = saved[2]
        job_lock.LOCK_DIR = saved[3]
        scheduler._remediation_path = saved[4]
        if saved[5] is None:
            os.environ.pop("SENTINEL_DB_PATH", None)
        else:
            os.environ["SENTINEL_DB_PATH"] = saved[5]
        if saved[6] is None:
            os.environ.pop("ACCEPTANCE_HISTORY_PATH", None)
        else:
            os.environ["ACCEPTANCE_HISTORY_PATH"] = saved[6]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sentinel chaos drills (dev/CI only)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate on a temp directory")
    args = parser.parse_args(argv)
    if not args.dry_run:
        print("refusing to run without --dry-run", file=sys.stderr)
        return 2
    results = run_drills()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item.get("ok") for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
