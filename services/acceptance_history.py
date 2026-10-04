"""Append-only daily acceptance history. Does not rewrite commissioning stamps."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def history_path() -> Path:
    raw = (os.getenv("ACCEPTANCE_HISTORY_PATH") or "").strip()
    if raw:
        return Path(raw)
    return ROOT / "data" / "archive" / "acceptance_history.jsonl"


def append_acceptance_history(blob: dict[str, Any] | None) -> dict[str, Any]:
    """Append one evaluation. Never writes fully_commissioned_at into this log as a mutation."""
    src = blob or {}
    checks = src.get("checks") if isinstance(src.get("checks"), dict) else {}
    reasons = []
    for name, item in checks.items():
        if isinstance(item, dict) and item.get("ok") is False:
            reasons.append(str(name))
    row = {
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "status": src.get("status"),
        "trigger": src.get("trigger"),
        "reasons": reasons,
    }
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load_history(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or history_path()
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def latest_by_day(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Last record of each UTC day, oldest first."""
    chosen: dict[str, dict[str, Any]] = {}
    for row in rows if rows is not None else load_history():
        day = str(row.get("day") or "")[:10]
        if day:
            chosen[day] = row
    return [chosen[day] for day in sorted(chosen)]


def backfill_from_job_log(db_path: Path | None = None) -> int:
    """Copy existing acceptance_check rows once. Skips when the log already has lines."""
    if load_history():
        return 0
    if db_path is None:
        raw = (os.getenv("SENTINEL_DB_PATH") or "").strip()
        if raw:
            db_path = Path(raw)
        else:
            try:
                from services.storage import DEFAULT_DB

                db_path = Path(DEFAULT_DB)
            except Exception:  # noqa: BLE001
                return 0
    if not db_path.is_file():
        return 0
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            """
            SELECT started_at, detail_json
              FROM job_log
             WHERE job_name='acceptance_check' AND status='ok' AND detail_json IS NOT NULL
             ORDER BY started_at ASC
            """
        ).fetchall()
        conn.close()
    except Exception:  # noqa: BLE001
        return 0
    written = 0
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for started_at, detail_json in rows:
            try:
                detail = json.loads(detail_json or "{}")
            except json.JSONDecodeError:
                continue
            acceptance = detail.get("acceptance") if isinstance(detail, dict) else None
            if not isinstance(acceptance, dict) or not acceptance.get("status"):
                status = detail.get("status") if isinstance(detail, dict) else None
                if not status:
                    continue
                acceptance = {"status": status, "trigger": "backfill", "checks": {}}
            day = str(started_at or "")[:10]
            if len(day) != 10:
                continue
            reasons = []
            checks = acceptance.get("checks") if isinstance(acceptance.get("checks"), dict) else {}
            for name, item in checks.items():
                if isinstance(item, dict) and item.get("ok") is False:
                    reasons.append(str(name))
            handle.write(
                json.dumps(
                    {
                        "recorded_at": started_at,
                        "day": day,
                        "status": acceptance.get("status"),
                        "trigger": acceptance.get("trigger") or "backfill",
                        "reasons": reasons,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            written += 1
    return written


def green_streak_days(rows: list[dict[str, Any]] | None = None) -> int:
    streak = 0
    for row in reversed(latest_by_day(rows)):
        if row.get("status") == "GREEN":
            streak += 1
        else:
            break
    return streak
