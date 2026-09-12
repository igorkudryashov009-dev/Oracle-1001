#!/usr/bin/env python3
"""Append-only daily-ish health/coverage snapshot (Prompt 11).

Not a dashboard. Raw JSONL for long-horizon G3 oscillation evidence.
Called from sentinel-core TTF rollup every Nth run (default 4 ~= once/day
at 6h rollup cadence).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_LOG = ROOT / "logs" / "health_coverage_daily.jsonl"
COUNTER = ROOT / "logs" / ".health_snapshot_rollup_count"


def _append(row: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def build_snapshot() -> dict[str, Any]:
    from services.ais_health import compute_ais_freshness, compute_top500_live_coverage
    from services.dual_gate import (
        compute_fleet_sample_status,
        compute_pipeline_health_status,
    )

    freshness = compute_ais_freshness()
    cov = compute_top500_live_coverage()
    cov_n = int(cov.get("top500_live_coverage") or 0)
    sample = compute_fleet_sample_status(
        cov_n, universe=int(cov.get("top500_universe") or 500)
    )
    # Connector counters if health.json present
    health_disk: dict[str, Any] = {}
    hp = ROOT / "output" / "api" / "v1" / "health.json"
    if hp.is_file():
        try:
            health_disk = json.loads(hp.read_text(encoding="utf-8", errors="ignore"))
        except (json.JSONDecodeError, OSError):
            health_disk = {}
    connector = health_disk.get("connector") or {}
    pipe = compute_pipeline_health_status(
        freshness=freshness,
        connector=connector if isinstance(connector, dict) else {},
        port_ok=True,
        port_drift_8478=False,
    )
    return {
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pipeline_health_status": pipe.get("pipeline_health_status"),
        "fleet_sample_status": sample.get("fleet_sample_status"),
        "top500_live_coverage": cov_n,
        "top500_universe": sample.get("top500_universe"),
        "ais_lag_sec": pipe.get("ais_lag_sec"),
        "live_ok": pipe.get("live_ok"),
        "reconnects": pipe.get("reconnects"),
        "rate_limit_hits": pipe.get("rate_limit_hits"),
        "coverage_window_seconds": cov.get("coverage_window_seconds"),
        "source": "append_health_snapshot",
    }


def maybe_append_from_rollup(
    *,
    every_n: int | None = None,
    force: bool = False,
    log_path: Path | None = None,
) -> dict[str, Any]:
    """Increment rollup counter; append snapshot every ``every_n`` (default 4)."""
    n = int(every_n if every_n is not None else os.environ.get("HEALTH_SNAPSHOT_EVERY_N_ROLLUPS", "4"))
    path = log_path or Path(os.environ.get("HEALTH_SNAPSHOT_LOG", str(DEFAULT_LOG)))
    count = 0
    if COUNTER.is_file():
        try:
            count = int(COUNTER.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            count = 0
    count += 1
    COUNTER.parent.mkdir(parents=True, exist_ok=True)
    COUNTER.write_text(str(count), encoding="utf-8")
    if not force and n > 0 and (count % n) != 0:
        return {"appended": False, "rollup_count": count, "every_n": n, "path": str(path)}
    row = build_snapshot()
    row["rollup_count"] = count
    _append(row, path)
    return {"appended": True, "rollup_count": count, "every_n": n, "path": str(path), "row": row}


def main() -> int:
    force = "--force" in sys.argv
    out = maybe_append_from_rollup(force=force, every_n=1 if force else None)
    print(json.dumps({k: v for k, v in out.items() if k != "row"}, indent=2, default=str))
    if out.get("row"):
        print(json.dumps(out["row"], ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
