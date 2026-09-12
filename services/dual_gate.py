"""
Dual Deploy Gate semantics (post Prompt-7 / G3 terrestrial AIS ceiling).

pipeline_health_status  — blocks --prod-rebuild / --prod-gate when != NOMINAL
fleet_sample_status     — informational; never blocks publish; drives UI caveat

Observed terrestrial ceiling (60-min diagnostic 2026-09-08):
  peak rolling coverage = 5, cumulative unique = 7, plateau ≈ 3–5.
LIMITED threshold therefore = 5 (observed peak, not invented).
"""

from __future__ import annotations

from typing import Any, Optional

# Product aspirational target (FULL fleet-wide inference).
FLEET_SAMPLE_FULL_MIN = 100

# Observed steady-state peak from Prompt-7 60-min soak (coverage_tick max=5).
FLEET_SAMPLE_LIMITED_MIN = 5

# Minimum unique vessels for fleet-wide LSSI/DAR/DFS as production signals.
FLEET_WIDE_METRIC_MIN_N = 30

# Pipeline health: live AIS lag must stay under LIVE_OK (5 min).
PIPELINE_LIVE_LAG_SEC = 300

# Reconnect / 429 storm windows (from connector health.connector block).
RECONNECT_STORM_MAX = 5
RATE_LIMIT_STORM_MAX = 1


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


def compute_pipeline_health_status(
    *,
    freshness: dict[str, Any] | None = None,
    connector: dict[str, Any] | None = None,
    port_ok: bool = True,
    port_drift_8478: bool = False,
) -> dict[str, Any]:
    """
    NOMINAL / DEGRADED / CRITICAL — independent of coverage.

    NOMINAL: lag < 300s, integrity ok, no 429/reconnect storm, canonical port.
    DEGRADED: warming lag or mild reconnect noise without storm.
    CRITICAL: stale/missing replica, integrity fail, 429 storm, port drift.
    """
    fr = freshness or {}
    conn = connector or {}
    reasons: list[str] = []

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

    hard_prefixes = (
        "port_drift",
        "dashboard_not",
        "wal_",
        "missing_",
        "ais_stale",
        "http_429",
        "reconnect_storm",
    )
    hard = [r for r in reasons if r.startswith(hard_prefixes)]
    if hard:
        status = "CRITICAL"
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

    return {
        "pipeline_health_status": status,
        "reasons": reasons,
        "ais_lag_sec": None if age is None else float(age),
        "live_ok": live_ok,
        "reconnects": reconnects,
        "rate_limit_hits": rate_limits,
        "port_ok": port_ok,
    }


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
