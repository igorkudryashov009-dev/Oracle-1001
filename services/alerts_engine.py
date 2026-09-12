"""
Oracle-1001 / Sentinel — Reactive Alerts & Webhook Engine

Monitors HMM Destination Probability Graph.
If P(Europe) shifts by ΔP > 25% within a 6-hour window → emit structured OSINT alert
with estimated TTF price impact.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "output" / "alerts_hmm_state.json"
ALERTS_LOG = ROOT / "output" / "alerts_osint.json"

# Thresholds
DELTA_P_THRESHOLD = 0.25          # 25 percentage points
WINDOW_HOURS = 6.0
# Empirical: +1 pp fleet EU probability ≈ +0.08 €/MWh short-term TTF (proxy)
TTF_IMPACT_PER_PP = 0.08
# Per-vessel LNG cargo proxy (MWh thermal) for impact scaling
VESSEL_MWH_PROXY = 1_250_000.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"vessels": {}, "updated_at": None}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"vessels": {}, "updated_at": None}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _estimate_ttf_impact(delta_p: float, ttf_spot: float | None) -> dict[str, Any]:
    """ΔP (fraction) → estimated €/MWh impact + directional tag."""
    pp = delta_p * 100.0
    impact = round(pp * TTF_IMPACT_PER_PP, 3)
    # Positive ΔP (more Europe-bound) → bullish TTF
    direction = "BULLISH" if delta_p > 0 else "BEARISH" if delta_p < 0 else "NEUTRAL"
    spot = float(ttf_spot) if ttf_spot is not None else None
    implied = round(spot + impact, 3) if spot is not None else None
    return {
        "delta_p_pp": round(pp, 2),
        "estimated_ttf_impact_eur_mwh": impact,
        "direction": direction,
        "ttf_spot": spot,
        "implied_ttf": implied,
        "cargo_mwh_proxy": VESSEL_MWH_PROXY,
    }


def _post_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Optional webhook POST if SENTINEL_ALERT_WEBHOOK_URL is set."""
    url = (os.environ.get("SENTINEL_ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        return {"delivered": False, "reason": "no_webhook_configured"}
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Oracle-1001-Sentinel/alerts"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return {"delivered": True, "status": getattr(resp, "status", 200)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"delivered": False, "reason": str(exc)}


def evaluate_hmm_destination_alerts(
    quant_pipeline: dict[str, Any],
    *,
    ttf_spot: float | None = None,
    delta_threshold: float = DELTA_P_THRESHOLD,
    window_hours: float = WINDOW_HOURS,
) -> dict[str, Any]:
    """
    Compare current HMM P(Europe) vectors vs persisted snapshot within window_hours.
    Emit alerts when |ΔP| > delta_threshold.
    """
    routing = (quant_pipeline or {}).get("routing") or {}
    # Collect vessel probability rows from top lists
    candidates: list[dict[str, Any]] = []
    for bucket in ("top_eu_bound", "top_asia_bound", "rerouting_risk"):
        for row in routing.get(bucket) or []:
            candidates.append(row)

    # Also accept flat vessel_predictions if present
    for row in routing.get("vessel_predictions") or []:
        candidates.append(row)

    now = datetime.now(timezone.utc)
    state = _load_state()
    prev_map: dict[str, Any] = dict(state.get("vessels") or {})
    alerts: list[dict[str, Any]] = []
    new_map: dict[str, Any] = {}

    for row in candidates:
        imo = str(row.get("imo") or "")
        if not imo:
            continue
        try:
            p_eu = float(row.get("p_europe") if row.get("p_europe") is not None else row.get("p_eu") or 0.5)
        except (TypeError, ValueError):
            continue

        new_map[imo] = {
            "p_europe": p_eu,
            "p_asia": float(row.get("p_asia") or (1.0 - p_eu)),
            "name": row.get("name") or row.get("vessel_name"),
            "hmm_state": row.get("hmm_state"),
            "ts": _now_iso(),
        }

        prev = prev_map.get(imo)
        if not prev:
            continue
        prev_ts = _parse_ts(prev.get("ts"))
        if prev_ts is None:
            continue
        age_h = (now - prev_ts).total_seconds() / 3600.0
        if age_h > window_hours:
            # Outside reactive window — refresh baseline only
            continue
        try:
            prev_p = float(prev.get("p_europe") or 0.5)
        except (TypeError, ValueError):
            continue
        delta = p_eu - prev_p
        if abs(delta) <= delta_threshold:
            continue

        impact = _estimate_ttf_impact(delta, ttf_spot)
        alert = {
            "alert_id": f"HMM-DEST-{imo}-{now.strftime('%Y%m%d%H%M')}",
            "type": "HMM_DESTINATION_SHIFT",
            "severity": "HIGH" if abs(delta) >= 0.40 else "MEDIUM",
            "imo": imo,
            "vessel_name": row.get("name") or row.get("vessel_name") or prev.get("name"),
            "p_europe_prev": round(prev_p, 3),
            "p_europe_now": round(p_eu, 3),
            "delta_p": round(delta, 3),
            "window_hours": round(age_h, 2),
            "threshold": delta_threshold,
            "ttf_impact": impact,
            "message": (
                f"OSINT ALERT: IMO {imo} Europe delivery probability shifted "
                f"{prev_p:.0%} → {p_eu:.0%} (ΔP={delta:+.0%}) within {age_h:.1f}h. "
                f"Est. TTF impact {impact['estimated_ttf_impact_eur_mwh']:+.2f} €/MWh "
                f"({impact['direction']})."
            ),
            "generated_at_utc": _now_iso(),
        }
        webhook = _post_webhook(alert)
        alert["webhook"] = webhook
        alerts.append(alert)

    # Persist latest probabilities
    state = {"vessels": new_map or prev_map, "updated_at": _now_iso(), "window_hours": window_hours}
    # Merge: keep new observations; retain prev for vessels not in this snapshot (within window)
    merged = dict(prev_map)
    merged.update(new_map)
    state["vessels"] = merged
    _save_state(state)

    # Append to rolling alerts log (keep last 100)
    log: list[dict[str, Any]] = []
    if ALERTS_LOG.exists():
        try:
            log = json.loads(ALERTS_LOG.read_text(encoding="utf-8"))
            if not isinstance(log, list):
                log = []
        except (OSError, json.JSONDecodeError):
            log = []
    log = (alerts + log)[:100]
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    ALERTS_LOG.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "generated_at_utc": _now_iso(),
        "engine": "reactive_alerts_v1",
        "delta_threshold": delta_threshold,
        "window_hours": window_hours,
        "monitored_vessels": len(new_map),
        "alert_count": len(alerts),
        "alerts": alerts,
        "recent_log_count": len(log),
        "webhook_url_configured": bool((os.environ.get("SENTINEL_ALERT_WEBHOOK_URL") or "").strip()),
    }


def build_alerts_payload(
    sentinel_payload: dict[str, Any],
) -> dict[str, Any]:
    """Convenience wrapper used by build_sentinel_dashboard."""
    qp = sentinel_payload.get("quant_pipeline") or {}
    ttf = sentinel_payload.get("ttf_forecast") or {}
    spot = ttf.get("spot_eur_mwh")
    try:
        spot_f = float(spot) if spot is not None else None
    except (TypeError, ValueError):
        spot_f = None
    out = evaluate_hmm_destination_alerts(qp, ttf_spot=spot_f)

    # Dual-gate: do not treat HMM/alerts as fleet-wide trading signals when sample is thin
    fs = str(
        sentinel_payload.get("fleet_sample_status")
        or qp.get("fleet_sample_status")
        or ""
    ).upper()
    caveat = sentinel_payload.get("sample_size_caveat") or qp.get("sample_size_caveat")
    out["fleet_sample_status"] = fs or None
    out["sample_size_caveat"] = caveat
    if fs and fs != "FULL":
        out["production_actionable"] = False
        out["signal_status"] = "insufficient_sample"
        for a in out.get("alerts") or []:
            if isinstance(a, dict):
                a["production_actionable"] = False
                a["signal_status"] = "insufficient_sample"
                a["sample_size_caveat"] = caveat
    else:
        out["production_actionable"] = True
        out["signal_status"] = "ok"
    return out
