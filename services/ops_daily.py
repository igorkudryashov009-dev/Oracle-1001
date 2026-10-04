"""One-page daily ops digest. Numbers come from health and job_log. No secrets."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_TARGET_N = 1260


def _audit_landing_line() -> str:
    try:
        from services.pilot_outreach import landing_digest_line

        return landing_digest_line()
    except Exception:  # noqa: BLE001
        return "- audit_landing: unavailable"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_ops_daily_markdown(*, now: datetime | None = None, acceptance: dict[str, Any] | None = None) -> str:
    now = now or _utc_now()
    from services.alerts import alerts_health_block
    from services.disk_forecast import disk_forecast_block
    from services.job_log import scheduler_health_block

    acc = acceptance or {}
    status = str(acc.get("status") or "UNKNOWN")
    checks = acc.get("checks") or {}
    archive = checks.get("archive") or {}
    rows = archive.get("row_count")
    forecast = disk_forecast_block(now=now)
    sched = scheduler_health_block()
    alerts = alerts_health_block()
    lag = None
    disk = None
    try:
        from services.ais_health import build_health_document

        doc = build_health_document()
        lag = doc.get("ais_lag_sec") or (doc.get("pipeline_health") or {}).get("ais_lag_sec")
        disk = doc.get("disk_free_pct")
        fa = doc.get("fleet_archive") or {}
    except Exception:  # noqa: BLE001
        fa = {}
    lines = [
        f"# Sentinel ops daily {now.strftime('%Y-%m-%d')}",
        "",
        f"- acceptance: {status}",
        f"- lag_sec: {lag}",
        f"- disk_free_pct: {disk}",
        f"- disk_forecast: {forecast.get('status')} hours_to_critical={forecast.get('hours_to_critical')}",
        f"- archive completeness: {rows}/{ARCHIVE_TARGET_N}",
        f"- terrestrial_covered_n: {(checks.get('terrestrial_covered') or {}).get('n')}",
        f"- gfw_verified_n: {(checks.get('gfw_verified') or {}).get('n', fa.get('gfw_verified_n') if isinstance(fa, dict) else None)}",
        f"- vf_verified_n: {(checks.get('vf_verified') or {}).get('n', fa.get('vf_verified_n') if isinstance(fa, dict) else None)}",
        f"- overdue_jobs_n: {sched.get('overdue_jobs_n')}",
        f"- active_alerts_n: {alerts.get('active_n')}",
        _audit_landing_line(),
        "",
        "LLM daily brief is not translated and is not copied here.",
    ]
    return "\n".join(lines) + "\n"


def write_ops_daily(*, now: datetime | None = None, acceptance: dict[str, Any] | None = None) -> Path:
    now = now or _utc_now()
    text = build_ops_daily_markdown(now=now, acceptance=acceptance)
    dest = ROOT / "output" / f"ops_daily_{now.strftime('%Y%m%d')}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest
