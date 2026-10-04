"""Linear disk-free forecast from the last 7 days of samples.

The CRITICAL floor is read from Dual Gate. This module does not redefine it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLES_PATH = ROOT / "data" / "archive" / "disk_free_samples.jsonl"
HORIZON_HOURS = 72.0
WINDOW_DAYS = 7.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _critical_pct() -> float:
    from services.dual_gate import DISK_FREE_CRITICAL_PCT

    return float(DISK_FREE_CRITICAL_PCT)


def record_disk_sample(free_pct: float, *, now: datetime | None = None, path: Path | None = None) -> None:
    now = now or _utc_now()
    dest = path or SAMPLES_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "free_pct": round(float(free_pct), 4)}
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_samples(*, now: datetime | None = None, path: Path | None = None) -> list[tuple[datetime, float]]:
    now = now or _utc_now()
    dest = path or SAMPLES_PATH
    if not dest.is_file():
        return []
    cutoff = now - timedelta(days=WINDOW_DAYS)
    out: list[tuple[datetime, float]] = []
    try:
        lines = dest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines[-5000:]:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        dt = _parse(str(raw.get("ts") or ""))
        if dt is None or dt < cutoff:
            continue
        try:
            out.append((dt, float(raw.get("free_pct"))))
        except (TypeError, ValueError):
            continue
    return out


def forecast_from_samples(
    samples: list[tuple[datetime, float]],
    *,
    now: datetime | None = None,
    critical_pct: float | None = None,
) -> dict[str, Any]:
    """Ordinary least squares of free_pct against hours. Slope < 0 means filling up."""
    now = now or _utc_now()
    crit = float(_critical_pct() if critical_pct is None else critical_pct)
    block: dict[str, Any] = {
        "method": "linear_7d",
        "critical_pct": crit,
        "sample_n": len(samples),
        "hours_to_critical": None,
        "slope_pct_per_hour": None,
        "alert": False,
        "recommendation": None,
    }
    if len(samples) < 2:
        block["status"] = "insufficient_history"
        return block
    t0 = samples[0][0]
    xs: list[float] = []
    ys: list[float] = []
    for dt, free in samples:
        xs.append((dt - t0).total_seconds() / 3600.0)
        ys.append(free)
    n = float(len(xs))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 1e-9:
        block["status"] = "insufficient_span"
        return block
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov / var_x
    intercept = mean_y - slope * mean_x
    block["slope_pct_per_hour"] = round(slope, 6)
    block["latest_free_pct"] = round(ys[-1], 3)
    if slope >= -1e-9:
        block["status"] = "stable_or_recovering"
        return block
    # free = intercept + slope * hours_since_first
    # crit = intercept + slope * t  => t = (crit - intercept) / slope
    t_crit = (crit - intercept) / slope
    hours_from_now = t_crit - xs[-1]
    block["hours_to_critical"] = round(hours_from_now, 2)
    if 0 <= hours_from_now < HORIZON_HOURS:
        block["status"] = "critical_within_72h"
        block["alert"] = True
        block["recommendation"] = (
            f"disk free projected to cross {crit:.0f}% within {hours_from_now:.0f}h — expand the disk"
        )
    elif hours_from_now < 0:
        block["status"] = "already_below_critical_projection"
        block["alert"] = True
        block["recommendation"] = f"disk free is projected through the {crit:.0f}% floor — expand the disk"
    else:
        block["status"] = "above_horizon"
    return block


def disk_forecast_block(*, now: datetime | None = None, path: Path | None = None) -> dict[str, Any]:
    now = now or _utc_now()
    return forecast_from_samples(load_samples(now=now, path=path), now=now)


def maybe_disk_forecast_alert(free_pct: float, *, path: Path | None = None) -> dict[str, Any]:
    now = _utc_now()
    record_disk_sample(free_pct, now=now, path=path)
    block = disk_forecast_block(now=now, path=path)
    if block.get("alert") and block.get("recommendation"):
        from services.alerts import emit_alert

        emit_alert(
            "disk_forecast",
            str(block["recommendation"]),
            severity="WARN",
            detail={"hours_to_critical": block.get("hours_to_critical"), "critical_pct": block.get("critical_pct")},
        )
    return block
