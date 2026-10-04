#!/usr/bin/env python3
"""Checks that must be true before the 02:00 UTC acceptance cut.

Disk free and its forecast, scheduler overdue, AIS lag, and yesterday's archive.
Prints one line per check. Exit 1 when any check fails. Does not print secrets.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = os.getenv("SENTINEL_HEALTH_URL") or "http://45.8.230.214:8765/api/v1/health"
LAG_MAX = 300.0
DISK_MIN = 20.0


def _admin_key() -> str:
    env = (ROOT / ".env").read_text(encoding="utf-8", errors="replace")
    raw = ""
    for line in env.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "API_KEYS_JSON":
            raw = value.strip().strip('"').strip("'")
            break
    data = json.loads(raw) if raw else {}
    if not isinstance(data, dict):
        return ""
    for item, meta in data.items():
        tier = str(meta.get("tier") or "").lower() if isinstance(meta, dict) else ""
        if tier == "admin":
            return str(item).strip()
    return ""


def _next_cut(now: datetime) -> datetime:
    cut = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if now >= cut:
        cut = cut + timedelta(days=1)
    return cut


def main() -> int:
    key = _admin_key()
    if not key:
        print("FAIL admin_key missing")
        return 1
    req = urllib.request.Request(HOST, headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    hours_to_cut = (_next_cut(now) - now).total_seconds() / 3600.0
    disk = float(doc.get("disk_free_pct") or 0)
    forecast = doc.get("disk_forecast") or {}
    hours_crit = forecast.get("hours_to_critical")
    sched = ((doc.get("ops") or {}).get("scheduler") or doc.get("scheduler") or {})
    overdue = sched.get("overdue_jobs_n")
    lag = doc.get("ais_lag_sec")
    if lag is None:
        lag = doc.get("live_lag_sec")
    if lag is None:
        lag = (doc.get("pipeline") or {}).get("ais_lag_sec")
    if lag is None:
        lag = (doc.get("replica") or {}).get("age_sec")
    lag_v = None if lag is None else float(lag)
    archive = doc.get("fleet_archive") or {}
    rows = int(archive.get("row_count") or 0)
    expected = int(archive.get("expected_n") or 0)
    snap = str(archive.get("snapshot_date") or "")
    checks = [
        ("disk", disk >= DISK_MIN, f"free_pct={disk} min={DISK_MIN}"),
        (
            "forecast",
            hours_crit is None or float(hours_crit) > hours_to_cut,
            f"status={forecast.get('status')} hours_to_critical={hours_crit} hours_to_cut={round(hours_to_cut, 2)}",
        ),
        ("overdue", overdue == 0, f"overdue_jobs_n={overdue}"),
        ("lag", lag_v is not None and lag_v <= LAG_MAX, f"lag_sec={lag_v} max={LAG_MAX}"),
        (
            "archive_yesterday",
            snap == yesterday and expected > 0 and rows == expected,
            f"snapshot_date={snap} expected_yesterday={yesterday} rows={rows} expected_n={expected}",
        ),
    ]
    failed = 0
    for name, ok, detail in checks:
        print(("PASS" if ok else "FAIL"), name, detail)
        if not ok:
            failed += 1
    print("pipeline", doc.get("pipeline_health_status"), "llm", doc.get("llm_status"))
    score = doc.get("readiness_score") or {}
    print("readiness", score.get("score"), score.get("components"))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print("FAIL preflight", type(exc).__name__)
        raise SystemExit(1)
