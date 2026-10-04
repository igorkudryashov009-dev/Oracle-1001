#!/usr/bin/env python3
"""Read-only morning sweep scorecard. Prints one line per check. Does not spend."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HOST = os.getenv("SENTINEL_HEALTH_URL") or "http://45.8.230.214:8765/api/v1/health"
STAMP = "2026-09-25T11:40:20Z"
ARCHIVE_CAP = 1260


def _readonly_key() -> str:
    raw = ""
    env = ROOT / ".env"
    if not env.is_file():
        return ""
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "API_KEYS_JSON":
            raw = value.strip().strip('"').strip("'")
            break
    data = json.loads(raw) if raw else {}
    if not isinstance(data, dict):
        return ""
    for item, meta in data.items():
        tier = str(meta.get("tier") or "").lower() if isinstance(meta, dict) else ""
        if tier == "readonly":
            return str(item).strip()
    return ""


def _db_path() -> Path | None:
    env = (os.getenv("SENTINEL_DB_PATH") or "").strip()
    if env and Path(env).is_file():
        return Path(env)
    for cand in (Path("/app/история1/sentinel_ais.db"),):
        if cand.is_file():
            return cand
    return None


def _health() -> dict:
    key = _readonly_key()
    headers = {"X-API-Key": key} if key else {}
    req = urllib.request.Request(HOST, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _verdict_from_db(db: Path) -> dict:
    from services.coverage_sweep import load_vessels_from_archive, sweep_verdict

    vessels = load_vessels_from_archive(db)
    used = 0
    budget = ROOT / "data" / "archive" / "sweep_gfw_budget.json"
    if budget.is_file():
        try:
            used = int(json.loads(budget.read_text(encoding="utf-8")).get("used") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            used = 0
    return sweep_verdict(vessels, used_today=used)


def _job_rows(db: Path, day: str) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT job_name, status FROM job_log
            WHERE started_at >= ?
            ORDER BY id
            """,
            (day,),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, str] = {}
    for name, status in rows:
        out[str(name)] = str(status)
    return out


def scorecard(doc: dict, *, verdict: str, jobs_today: dict[str, str], day: str) -> list[tuple[str, bool, str]]:
    acc = doc.get("acceptance") if isinstance(doc.get("acceptance"), dict) else {}
    sched = doc.get("scheduler") if isinstance(doc.get("scheduler"), dict) else {}
    jobs = sched.get("jobs") if isinstance(sched.get("jobs"), dict) else {}
    archive = doc.get("fleet_archive") if isinstance(doc.get("fleet_archive"), dict) else {}
    replica = doc.get("replica") if isinstance(doc.get("replica"), dict) else {}
    lag = replica.get("age_sec")
    if lag is None:
        lag = replica.get("live_ok_lag_sec")
    rows = int(archive.get("row_count") or 0)
    expected = int(archive.get("expected_n") or 0)
    disk = float(doc.get("disk_free_pct") or 0)
    used = doc.get("gfw_daily_requests_used")
    overdue = int(sched.get("overdue_jobs_n") or 0)

    def _ran(name: str, allowed: set[str]) -> tuple[bool, str]:
        block = jobs.get(name) if isinstance(jobs.get(name), dict) else {}
        status = str(jobs_today.get(name) or block.get("last_status") or "")
        started = str(block.get("last_run") or "")
        ok = status in allowed and (started.startswith(day) or name in jobs_today)
        return ok, f"status={status or 'missing'} last={started or 'none'}"

    snap_ok, snap_detail = _ran("archive_snapshot", {"ok"})
    gfw_ok, gfw_detail = _ran("gfw_poll", {"ok", "skipped"})
    vf_ok, vf_detail = _ran("vf_allocator", {"ok", "skipped"})
    acc_ok, acc_detail = _ran("acceptance_check", {"ok"})
    bud_ok, bud_detail = _ran("budget_sync", {"ok"})
    wd = jobs.get("pipeline_watchdog") if isinstance(jobs.get("pipeline_watchdog"), dict) else {}
    wd_ok = wd.get("overdue") is False and str(wd.get("last_status") or "") in {"ok", "skipped"}
    legal = verdict == "no_spend_coverage_ok" or verdict.startswith("spent_closing_gaps:")
    return [
        ("pipeline", doc.get("pipeline_health_status") == "NOMINAL", str(doc.get("pipeline_health_status"))),
        ("acceptance", acc.get("status") == "GREEN", str(acc.get("status"))),
        ("stamp", acc.get("fully_commissioned_at") == STAMP, str(acc.get("fully_commissioned_at"))),
        ("overdue", overdue == 0, f"overdue={overdue}"),
        ("disk", disk >= 20.0, f"free_pct={disk}"),
        ("lag", lag is not None and float(lag) <= 300.0, f"lag_sec={lag}"),
        (
            "archive",
            expected > 0 and rows == expected,
            f"rows={rows}/{ARCHIVE_CAP} expected={expected}",
        ),
        ("gfw_used", isinstance(used, int) and used >= 0, f"gfw_daily_requests_used={used}"),
        ("verdict", legal, verdict),
        ("archive_snapshot", snap_ok, snap_detail),
        ("gfw_poll", gfw_ok, gfw_detail),
        ("vf_allocator", vf_ok, vf_detail),
        ("acceptance_check", acc_ok, acc_detail),
        ("budget_sync", bud_ok, bud_detail),
        ("watchdog", wd_ok, f"status={wd.get('last_status')} overdue={wd.get('overdue')}"),
    ]


def _remote_numbers() -> dict:
    """Dry numbers from the node. Uses code already on the box. No writes."""
    import base64

    key = Path.home() / ".ssh" / "id_ed25519"
    code = """
import json
from pathlib import Path
from services.coverage_sweep import load_vessels_from_archive, sweep_numbers
from services.storage import DEFAULT_DB
v = load_vessels_from_archive(Path(DEFAULT_DB))
n = sweep_numbers(v, used_today=0)
print(json.dumps({
    "a": n["tier_a_planned_requests"],
    "b": n["tier_b_planned_requests"],
    "carried": n["tier_b_carried"],
    "used": n["gfw_daily_requests_used"],
}))
"""
    payload = base64.b64encode(code.encode("utf-8")).decode("ascii")
    remote = (
        f"echo {payload} | base64 -d > /tmp/sweep_ro.py && "
        "docker cp /tmp/sweep_ro.py sentinel-web:/tmp/sweep_ro.py && "
        "docker exec -w /app -e PYTHONPATH=/app sentinel-web python -u /tmp/sweep_ro.py"
    )
    proc = subprocess.run(
        [
            "ssh",
            "-i",
            str(key),
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=12",
            "root@45.8.230.214",
            remote,
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    line = ""
    for row in (proc.stdout or "").splitlines():
        if row.startswith("{"):
            line = row
    if not line:
        raise RuntimeError((proc.stderr or proc.stdout or "remote sweep failed")[:400])
    return json.loads(line)


def _verdict_from_numbers(nums: dict) -> str:
    planned = int(nums.get("a") or 0) + int(nums.get("b") or 0)
    carried = int(nums.get("carried") or 0)
    if planned == 0:
        return "no_spend_coverage_ok"
    return f"spent_closing_gaps: {planned} requests, holes {planned + carried}/{carried}"


def main() -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    doc = _health()
    db = _db_path()
    jobs_today: dict[str, str] = {}
    if db is not None:
        verdict_doc = _verdict_from_db(db)
        verdict = str(verdict_doc["verdict"])
        jobs_today = _job_rows(db, day)
        if "gfw_daily_requests_used" not in doc or doc.get("gfw_daily_requests_used") is None:
            doc = dict(doc)
            doc["gfw_daily_requests_used"] = int(verdict_doc["gfw_daily_requests_used"])
    else:
        nums = _remote_numbers()
        verdict = _verdict_from_numbers(nums)
        if doc.get("gfw_daily_requests_used") is None:
            doc = dict(doc)
            doc["gfw_daily_requests_used"] = int(nums.get("used") or 0)
    checks = scorecard(doc, verdict=verdict, jobs_today=jobs_today, day=day)
    failed = 0
    for name, ok, detail in checks:
        print(("PASS" if ok else "FAIL"), name, detail)
        if not ok:
            failed += 1
    archive = doc.get("fleet_archive") if isinstance(doc.get("fleet_archive"), dict) else {}
    sched = doc.get("scheduler") if isinstance(doc.get("scheduler"), dict) else {}
    print("sweep.verdict", verdict)
    print("gfw_daily_requests_used", doc.get("gfw_daily_requests_used"))
    print(f"archive rows={int(archive.get('row_count') or 0)}/{ARCHIVE_CAP}")
    print("overdue", int(sched.get("overdue_jobs_n") or 0))
    brief = (jobs_today.get("daily_brief") or "")
    if brief:
        print("note daily_brief", brief, "(LLM channel is out of scope)")
    total = len(checks)
    print(f"SWEEP: {total - failed}/{total}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
