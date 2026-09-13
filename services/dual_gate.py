"""
Dual Deploy Gate semantics (post Prompt-7 / G3 terrestrial AIS ceiling).

pipeline_health_status  — blocks --prod-rebuild / --prod-gate when != NOMINAL
fleet_sample_status     — informational; never blocks publish; drives UI caveat

Observed terrestrial ceiling (60-min diagnostic 2026-09-08):
  peak rolling coverage = 5, cumulative unique = 7, plateau ≈ 3–5.
LIMITED threshold therefore = 5 (observed peak, not invented).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Optional

# Product aspirational target (FULL fleet-wide inference).
FLEET_SAMPLE_FULL_MIN = 100

# Observed steady-state peak from Prompt-7 60-min soak (coverage_tick max=5).
FLEET_SAMPLE_LIMITED_MIN = 5

# Minimum unique vessels for fleet-wide LSSI/DAR/DFS as production signals.
FLEET_WIDE_METRIC_MIN_N = 30

# Pipeline health: live AIS lag must stay under LIVE_OK (5 min).
PIPELINE_LIVE_LAG_SEC = 300

# Disk headroom (Node A is 20G root — prevent silent ENOSPC on WAL/logs).
DISK_FREE_MIN_PCT = 20.0       # below → DEGRADED (preemptive)
DISK_FREE_CRITICAL_PCT = 10.0  # below → CRITICAL (imminent ENOSPC)

# Reconnect / 429 storm windows (from connector health.connector block).
RECONNECT_STORM_MAX = 5
RATE_LIMIT_STORM_MAX = 1


def resolve_active_node(
    *,
    freshness: dict[str, Any] | None = None,
    explicit_node: str | None = None,
) -> str:
    """Resolve active ingest node: 'korolev' | 'london'.

    Precedence:
      1. explicit_node argument if in {'korolev', 'london'}.
      2. Environment variable SENTINEL_ACTIVE_NODE or ACTIVE_NODE.
      3. Environment variable SENTINEL_AIS_MODE (or AIS_MODE):
         If ais_mode in {'off', '0', 'false', 'no', 'analytics', 'replica'}:
           Korolev is in replica/analytics mode -> ingest is delegated to 'london'.
      4. Hostname inspection: if 'london' or 'ld8' in hostname -> 'london'.
      5. Default: 'korolev' (primary node).
    """
    if explicit_node and explicit_node.strip().lower() in {"korolev", "london"}:
        return explicit_node.strip().lower()

    env_node = (os.environ.get("SENTINEL_ACTIVE_NODE") or os.environ.get("ACTIVE_NODE") or "").strip().lower()
    if env_node in {"korolev", "london"}:
        return env_node

    ais_mode = (os.environ.get("SENTINEL_AIS_MODE") or os.environ.get("AIS_MODE") or "on").strip().lower()
    if ais_mode in {"off", "0", "false", "no", "analytics", "replica"}:
        return "london"

    import socket
    try:
        host = socket.gethostname().lower()
        if "london" in host or "ld8" in host:
            return "london"
    except Exception:
        pass

    return "korolev"


def check_failover_status(
    active_node: str,
    *,
    freshness: dict[str, Any] | None = None,
    root: Path | None = None,
) -> tuple[bool, Optional[str]]:
    """Check if failover is in progress (awaiting first sync from new edge).

    Returns (is_failover_warming: bool, reason: Optional[str]).
    If active_node == 'london':
      Verifies that London edge has delivered a validated replica sync since cutover.
      Prevents false NOMINAL window with stale Node A data before Node B warms up.
    """
    if active_node != "london":
        return False, None

    from datetime import datetime, timezone

    cutover_candidates = [
        Path("/opt/oracle1001/logs/failover_cutover.ts"),
        Path("/opt/oracle1001/sentinel/logs/failover_cutover.ts"),
        Path("/app/logs/failover_cutover.ts"),
        Path("/app/data/failover_cutover.ts"),
    ]
    sync_candidates = [
        Path("/opt/oracle1001/logs/last_london_sync.ts"),
        Path("/opt/oracle1001/sentinel/logs/last_london_sync.ts"),
        Path("/app/logs/last_london_sync.ts"),
        Path("/app/data/last_london_sync.ts"),
    ]
    if root:
        cutover_candidates.insert(0, root / "logs" / "failover_cutover.ts")
        sync_candidates.insert(0, root / "logs" / "last_london_sync.ts")

    cutover_mtime = 0.0
    for p in cutover_candidates:
        if p.is_file():
            try:
                cutover_mtime = max(cutover_mtime, p.stat().st_mtime)
            except OSError:
                pass

    sync_mtime = 0.0
    for p in sync_candidates:
        if p.is_file():
            try:
                sync_mtime = max(sync_mtime, p.stat().st_mtime)
            except OSError:
                pass

    if cutover_mtime > 0 and sync_mtime <= cutover_mtime:
        now_ts = datetime.now(timezone.utc).timestamp()
        if (now_ts - cutover_mtime) < 1800:
            return True, "failover_cutover_awaiting_first_london_sync"

    return False, None


def compute_fleet_sample_status(
    coverage: int | float | None,
    *,
    universe: int = 500,
) -> dict[str, Any]:
    """FULL / LIMITED / INSUFFICIENT from top500_live_coverage."""
    n = int(coverage or 0)
    if n >= FLEET_SAMPLE_FULL_MIN:
        status = "FULL"
    elif n >= FLEET_SAMPLE_LIMITED_MIN:
        status = "LIMITED"
    else:
        status = "INSUFFICIENT"
    caveat = None
    if status != "FULL":
        caveat = (
            f"based on N={n} vessels (of {universe}), statistically insufficient "
            f"for fleet-wide balance inference (terrestrial AIS coverage)"
        )
    return {
        "fleet_sample_status": status,
        "top500_live_coverage": n,
        "top500_universe": int(universe),
        "limited_min": FLEET_SAMPLE_LIMITED_MIN,
        "full_min": FLEET_SAMPLE_FULL_MIN,
        "sample_size_caveat": caveat,
        "fleet_wide_metrics_eligible": n >= FLEET_WIDE_METRIC_MIN_N,
    }


def probe_disk_usage(path: str | Path | None = None) -> dict[str, Any]:
    """Host disk headroom for the ingest/HUD root filesystem."""
    target = Path(path or os.environ.get("SENTINEL_DISK_PATH") or "/")
    try:
        usage = shutil.disk_usage(str(target))
    except OSError as exc:
        return {
            "path": str(target),
            "error": str(exc),
            "disk_free_pct": None,
            "disk_used_pct": None,
        }
    total = float(usage.total) or 1.0
    free_pct = 100.0 * float(usage.free) / total
    used_pct = 100.0 * float(usage.used) / total
    return {
        "path": str(target),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
        "disk_free_pct": round(free_pct, 2),
        "disk_used_pct": round(used_pct, 2),
        "disk_free_min_pct": DISK_FREE_MIN_PCT,
        "disk_free_critical_pct": DISK_FREE_CRITICAL_PCT,
    }


def compute_pipeline_health_status(
    *,
    freshness: dict[str, Any] | None = None,
    connector: dict[str, Any] | None = None,
    port_ok: bool = True,
    port_drift_8478: bool = False,
    active_node: str | None = None,
    failover_in_progress: bool = False,
    disk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    NOMINAL / DEGRADED / CRITICAL — independent of coverage.

    NOMINAL: lag < 300s, integrity ok, no 429/reconnect storm, canonical port,
             no failover in progress, disk_free_pct >= DISK_FREE_MIN_PCT.
    DEGRADED: warming lag, mild reconnect noise, failover cutover, or disk < 20%.
    CRITICAL: stale/missing replica, integrity fail, 429 storm, port drift, disk < 10%.
    """
    fr = freshness or {}
    conn = connector or {}
    reasons: list[str] = []

    node = active_node or resolve_active_node(freshness=fr)
    if failover_in_progress:
        reasons.append("failover_cutover_awaiting_first_edge_sync")

    age = fr.get("age_sec")
    live_ok = bool(fr.get("live_ok"))
    stale = bool(fr.get("stale"))
    integrity_ok = fr.get("integrity_ok", True)
    if integrity_ok is None:
        integrity_ok = True

    reconnects = int(conn.get("reconnects") or 0)
    http_429 = int(conn.get("http_429_count") or 0)
    rate_limits = int(conn.get("rate_limit_hits") or http_429 or 0)

    if port_drift_8478:
        reasons.append("port_drift_8478")
    if not port_ok:
        reasons.append("dashboard_not_on_8765")
    if not integrity_ok:
        reasons.append("wal_or_db_integrity_fail")
    if fr.get("status") == "MISSING_REPLICA" or fr.get("ais_truth") == "missing":
        reasons.append("missing_replica")
    if stale:
        reasons.append("ais_stale")
    if http_429 > 0:
        reasons.append(f"http_429_hits={http_429}")
    elif rate_limits >= RATE_LIMIT_STORM_MAX:
        reasons.append(f"rate_limit_hits={rate_limits}")
    if reconnects >= RECONNECT_STORM_MAX:
        reasons.append(f"reconnect_storm={reconnects}")

    disk_info = disk if isinstance(disk, dict) else None
    free_pct = None
    if disk_info is not None and disk_info.get("disk_free_pct") is not None:
        try:
            free_pct = float(disk_info["disk_free_pct"])
        except (TypeError, ValueError):
            free_pct = None
        if free_pct is not None:
            if free_pct < DISK_FREE_CRITICAL_PCT:
                reasons.append(f"disk_free_critical={free_pct:.1f}pct")
            elif free_pct < DISK_FREE_MIN_PCT:
                reasons.append(f"disk_free_low={free_pct:.1f}pct")

    hard_prefixes = (
        "port_drift",
        "dashboard_not",
        "wal_",
        "missing_",
        "ais_stale",
        "http_429",
        "reconnect_storm",
        "failover_",
        "disk_free_",
    )
    hard = [r for r in reasons if r.startswith(hard_prefixes)]
    if hard:
        status = (
            "CRITICAL"
            if any(
                r.startswith(
                    (
                        "port_drift",
                        "dashboard_not",
                        "wal_",
                        "missing_",
                        "ais_stale",
                        "http_429",
                        "reconnect_storm",
                        "disk_free_critical",
                    )
                )
                for r in hard
            )
            else "DEGRADED"
        )
    elif live_ok and (age is None or float(age) < PIPELINE_LIVE_LAG_SEC) and not reasons:
        status = "NOMINAL"
    elif live_ok and (age is None or float(age) < PIPELINE_LIVE_LAG_SEC):
        # Mild reconnect noise below storm threshold → still NOMINAL if live
        status = "NOMINAL" if reconnects < RECONNECT_STORM_MAX and http_429 == 0 else "DEGRADED"
    elif age is not None and float(age) <= 600:
        status = "DEGRADED"
        if "ais_warming" not in " ".join(reasons):
            reasons.append(f"ais_warming_lag={float(age):.0f}s")
    else:
        status = "CRITICAL"
        if not reasons:
            reasons.append("ais_not_live")

    out = {
        "pipeline_health_status": status,
        "reasons": reasons,
        "ais_lag_sec": None if age is None else float(age),
        "live_ok": live_ok,
        "reconnects": reconnects,
        "rate_limit_hits": rate_limits,
        "port_ok": port_ok,
        "active_node": node,
        "failover_in_progress": failover_in_progress,
    }
    if free_pct is not None:
        out["disk_free_pct"] = round(free_pct, 2)
    if disk_info:
        out["disk"] = {
            k: disk_info.get(k)
            for k in (
                "path",
                "total_bytes",
                "used_bytes",
                "free_bytes",
                "disk_free_pct",
                "disk_used_pct",
                "disk_free_min_pct",
                "disk_free_critical_pct",
            )
            if k in disk_info or disk_info.get(k) is not None
        }
    return out


def apply_fleet_sample_to_metric(
    metric: dict[str, Any],
    *,
    sample: dict[str, Any],
    metric_name: str,
) -> dict[str, Any]:
    """Mark LSSI/DAR/DFS as insufficient_sample when N below production threshold."""
    out = dict(metric)
    n = int(sample.get("top500_live_coverage") or 0)
    eligible = bool(sample.get("fleet_wide_metrics_eligible"))
    out["sample_n"] = n
    out["production_signal"] = bool(eligible)
    if not eligible:
        out["signal_status"] = "insufficient_sample"
        out["sample_size_caveat"] = (
            sample.get("sample_size_caveat")
            or (
                f"{metric_name}: N={n} < {FLEET_WIDE_METRIC_MIN_N} — "
                "not valid as fleet-wide production signal"
            )
        )
        # Neutralize actionable signal labels so downstream cannot trade on them.
        if "signal" in out:
            out["signal_raw"] = out.get("signal")
            out["signal"] = "INSUFFICIENT_SAMPLE"
        if "severity" in out:
            out["severity_raw"] = out.get("severity")
            out["severity"] = "INSUFFICIENT_SAMPLE"
    else:
        out["signal_status"] = "ok"
    return out


def live_inference_confidence(
    *,
    model_cv_accuracy_pct: Optional[float],
    fleet_sample_status: str,
    coverage: int = 0,
) -> dict[str, Any]:
    """
    Separate offline model CV accuracy from live fleet representativeness.

    model_cv_accuracy  — property of the trained model (unchanged by live N).
    live_inference_confidence — MUST drop when fleet_sample_status != FULL.
    """
    status = str(fleet_sample_status or "INSUFFICIENT").upper()
    cv = None if model_cv_accuracy_pct is None else float(model_cv_accuracy_pct)
    if status == "FULL":
        level = "HIGH"
        factor = 1.0
    elif status == "LIMITED":
        level = "LOW"
        factor = min(1.0, max(0.05, float(coverage) / float(FLEET_SAMPLE_FULL_MIN)))
    else:
        level = "LOW"
        factor = min(0.05, max(0.01, float(coverage) / float(FLEET_SAMPLE_FULL_MIN)))

    live_pct = None if cv is None else round(cv * factor, 2)
    return {
        "model_cv_accuracy_pct": None if cv is None else round(cv, 2),
        "live_inference_confidence": level,
        "live_inference_confidence_pct": live_pct,
        "live_confidence_factor": round(factor, 4),
        "fleet_sample_status": status,
        "note": (
            "model_cv_accuracy is offline purged-CV; live_inference_confidence "
            "reflects terrestrial AIS sample representativeness and must not be "
            "substituted by CV alone."
        ),
    }
