#!/usr/bin/env python3
"""Append-only job execution log + overdue detector for Sentinel automation."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
from services.storage import DEFAULT_DB  # noqa: E402

_LOCK = threading.RLock()

JOB_LOG_DDL = """
CREATE TABLE IF NOT EXISTS job_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    rows_affected INTEGER DEFAULT 0,
    error TEXT,
    detail_json TEXT
)
"""
JOB_LOG_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_job_log_name_started ON job_log(job_name, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_job_log_status ON job_log(status)",
)

# Static schedule metadata (UTC). period_sec used for overdue (2×).
JOB_SCHEDULE: dict[str, dict[str, Any]] = {
    "archive_snapshot": {
        "hour": 0,
        "minute": 30,
        "period_sec": 86400,
        "description": "Daily vessel_daily_archive resnapshot (yesterday UTC)",
    },
    "gfw_poll": {
        "hour": 1,
        "minute": 0,
        "period_sec": 86400,
        "description": "GFW Events batch ≤20 from gap_48h (if configured)",
    },
    "vf_allocator": {
        "hour": 1,
        "minute": 30,
        "period_sec": 86400,
        "description": "VF allocator ≤16 credits (if last_ok)",
    },
    "pipeline_watchdog": {
        "every_sec": 900,
        "period_sec": 900,
        "description": "AIS lag / disk / replica / key activation probe",
    },
    "budget_sync": {
        "every_sec": 3600,
        "period_sec": 3600,
        "description": "Refresh vf/gfw budget blobs into health side-cache",
    },
    "acceptance_check": {
        "hour": 2,
        "minute": 0,
        "period_sec": 86400,
        "description": "TZ self-acceptance → health.acceptance GREEN|DEGRADED|WAITING_KEYS",
    },
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_path() -> Path:
    env = (os.environ.get("SENTINEL_DB_PATH") or "").strip()
    return Path(env) if env else Path(DEFAULT_DB)


def ensure_job_log_schema(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if own:
        conn = sqlite3.connect(str(_db_path()), timeout=30.0)
    assert conn is not None
    try:
        conn.execute(JOB_LOG_DDL)
        for stmt in JOB_LOG_INDEXES:
            conn.execute(stmt)
        conn.commit()
    finally:
        if own:
            conn.close()


def log_job_start(job_name: str) -> int:
    """Insert running row; return row id."""
    with _LOCK:
        ensure_job_log_schema()
        conn = sqlite3.connect(str(_db_path()), timeout=30.0)
        try:
            cur = conn.execute(
                "INSERT INTO job_log (job_name, started_at, status) VALUES (?,?,?)",
                (job_name, _utc_iso(), "running"),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def log_job_finish(
    row_id: int,
    *,
    status: str,
    rows_affected: int = 0,
    error: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    with _LOCK:
        conn = sqlite3.connect(str(_db_path()), timeout=30.0)
        try:
            conn.execute(
                """
                UPDATE job_log
                   SET finished_at=?, status=?, rows_affected=?, error=?, detail_json=?
                 WHERE id=?
                """,
                (
                    _utc_iso(),
                    status,
                    int(rows_affected),
                    (error or "")[:500] or None,
                    json.dumps(detail, ensure_ascii=False) if detail else None,
                    int(row_id),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def last_job_run(job_name: str) -> dict[str, Any] | None:
    ensure_job_log_schema()
    try:
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            """
            SELECT id, started_at, finished_at, status, rows_affected, error
              FROM job_log
             WHERE job_name=?
             ORDER BY id DESC LIMIT 1
            """,
            (job_name,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "started_at": row[1],
            "finished_at": row[2],
            "status": row[3],
            "rows_affected": row[4],
            "error": row[5],
        }
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _last_runs_batch(job_names: list[str]) -> dict[str, dict[str, Any] | None]:
    """One RO connection for all job last-run rows (health hot path)."""
    out: dict[str, dict[str, Any] | None] = {n: None for n in job_names}
    ensure_job_log_schema()
    try:
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        for name in job_names:
            row = conn.execute(
                """
                SELECT id, started_at, finished_at, status, rows_affected, error
                  FROM job_log
                 WHERE job_name=?
                 ORDER BY id DESC LIMIT 1
                """,
                (name,),
            ).fetchone()
            if row:
                out[name] = {
                    "id": row[0],
                    "started_at": row[1],
                    "finished_at": row[2],
                    "status": row[3],
                    "rows_affected": row[4],
                    "error": row[5],
                }
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


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


def next_run_utc(
    job_name: str,
    *,
    now: datetime | None = None,
    last: dict[str, Any] | None = None,
) -> str:
    now = now or _utc_now()
    meta = JOB_SCHEDULE.get(job_name) or {}
    every = meta.get("every_sec")
    if every:
        if last is None:
            last = last_job_run(job_name)
        last_dt = _parse_iso((last or {}).get("finished_at") or (last or {}).get("started_at"))
        if last_dt:
            nxt = last_dt + timedelta(seconds=int(every))
            if nxt < now:
                nxt = now + timedelta(seconds=30)
            return _utc_iso(nxt)
        return _utc_iso(now + timedelta(seconds=min(60, int(every))))
    hour = int(meta.get("hour") or 0)
    minute = int(meta.get("minute") or 0)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return _utc_iso(candidate)


def scheduler_health_block() -> dict[str, Any]:
    """Public /api/v1/health.scheduler blob."""
    now = _utc_now()
    jobs: dict[str, Any] = {}
    overdue: list[str] = []
    lasts = _last_runs_batch(list(JOB_SCHEDULE.keys()))
    for name, meta in JOB_SCHEDULE.items():
        last = lasts.get(name)
        period = int(meta.get("period_sec") or 86400)
        last_status = (last or {}).get("status")
        last_run = (last or {}).get("finished_at") or (last or {}).get("started_at")
        last_dt = _parse_iso(last_run)
        is_overdue = False
        if last_dt is None:
            is_overdue = False
        else:
            age = (now - last_dt).total_seconds()
            if age > 2 * period:
                is_overdue = True
                overdue.append(name)
        jobs[name] = {
            "last_run": last_run,
            "last_status": last_status,
            "next_run": next_run_utc(name, now=now, last=last),
            "period_sec": period,
            "description": meta.get("description"),
            "overdue": is_overdue,
        }
    return {
        "jobs": jobs,
        "overdue_jobs_n": len(overdue),
        "overdue_jobs": overdue,
        "note": "Sentinel automation — systemd timers; Dual Gate unchanged",
    }


__all__ = (
    "JOB_SCHEDULE",
    "ensure_job_log_schema",
    "last_job_run",
    "log_job_finish",
    "log_job_start",
    "next_run_utc",
    "scheduler_health_block",
)
