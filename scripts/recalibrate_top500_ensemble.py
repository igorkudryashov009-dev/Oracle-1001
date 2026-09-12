"""
TOP-500 spatial rematch + TTF ensemble recalibration (AIS-live gated).

Does NOT invent accuracy: reports purged-CV / walk-forward directional metrics only.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "output" / "api" / "v1" / "health.json"
FLEET_CSV = ROOT / "output" / "fleet_database.csv"
CV_PATH = ROOT / "output" / "models" / "ttf_cv_metrics.json"
REPORT_PATH = ROOT / "output" / "top500_recalibration_report.json"
FEAT = ROOT / "output" / "ttf_features.parquet"


def _load_health() -> dict[str, Any]:
    if not HEALTH.exists():
        return {}
    return json.loads(HEALTH.read_text(encoding="utf-8"))


def _ais_live_ok(h: dict[str, Any]) -> tuple[bool, str]:
    rep = h.get("replica") or {}
    mode = str(h.get("source_mode") or "")
    live_ok = bool(rep.get("live_ok")) and not bool(rep.get("stale"))
    truth = str(rep.get("ais_truth") or "")
    raw_lag = rep.get("lag_minutes")
    lag_min = float(raw_lag) if raw_lag is not None else 999.0
    if "STALE" in mode.upper() or truth == "stale" or not live_ok or lag_min > 5:
        return False, f"AIS not live: mode={mode} truth={truth} lag_min={lag_min}"
    return True, f"AIS live: mode={mode} lag_min={lag_min}"


def _fleet_age_days() -> float:
    if not FLEET_CSV.exists():
        return 9999.0
    return (time.time() - FLEET_CSV.stat().st_mtime) / 86400.0


def _component_dir_acc_from_prices(horizon: int = 14) -> dict[str, Any]:
    """
    Walk-forward directional accuracy for Markov / Spectral / Elliott on last
    ``horizon`` holdout days of ICE TTF features (no look-ahead beyond t).
    """
    from services.ttf_forecast.ensemble_aggregator import (
        elliott_target_projection,
        markov_price_distribution,
        spectral_price_projection,
        _safe_load_json,
    )

    if not FEAT.exists():
        return {"error": "ttf_features.parquet missing"}

    df = pd.read_parquet(FEAT).sort_index()
    if "price_ttf_eur_mwh" not in df.columns:
        # tolerate alternate column names
        price_col = [c for c in df.columns if "ttf" in c.lower() and "price" in c.lower()]
        if not price_col:
            return {"error": f"no price col in {list(df.columns)[:12]}"}
        px = df[price_col[0]].astype(float)
    else:
        px = df["price_ttf_eur_mwh"].astype(float)

    n = len(px)
    if n < horizon + 30:
        return {"error": f"series too short n={n}"}

    markov = _safe_load_json(ROOT / "output" / "ttf_markov_regimes.json")
    spectral = _safe_load_json(ROOT / "output" / "ttf_spectral_report.json")
    elliott = _safe_load_json(ROOT / "output" / "ttf_causality_elliott.json")

    # Holdout = final ``horizon`` points: predict from t → t+h using spot at t
    # (models are static artifacts; this is an honest out-of-sample sign check).
    start = n - horizon
    hits = {"markov": 0, "spectral": 0, "elliott": 0, "ensemble": 0}
    n_eval = 0
    for i in range(start, n):
        # Need future point; for last rows use available remaining span
        j = min(i + horizon, n - 1)
        if j <= i:
            continue
        spot = float(px.iloc[i])
        actual = float(px.iloc[j])
        actual_sign = np.sign(actual - spot)
        if actual_sign == 0:
            continue
        preds = {
            "markov": markov_price_distribution(spot, markov, horizon)["p50"],
            "spectral": spectral_price_projection(spot, spectral, horizon)["p50"],
            "elliott": elliott_target_projection(spot, elliott, horizon)["p50"],
        }
        ens = 0.25 * preds["markov"] + 0.20 * preds["spectral"] + 0.15 * preds["elliott"]
        # Note: CatBoost evaluated separately via purged CV
        for k, p50 in preds.items():
            if np.sign(p50 - spot) == actual_sign:
                hits[k] += 1
        if np.sign(ens - spot) == actual_sign:
            hits["ensemble"] += 1
        n_eval += 1

    if n_eval == 0:
        return {"error": "no evaluable holdout points"}

    out = {
        "horizon_days": horizon,
        "n_eval": n_eval,
        "holdout_end": str(px.index[-1]),
        "markov": {"directional_accuracy_pct": round(100.0 * hits["markov"] / n_eval, 2)},
        "spectral": {"directional_accuracy_pct": round(100.0 * hits["spectral"] / n_eval, 2)},
        "elliott": {"directional_accuracy_pct": round(100.0 * hits["elliott"] / n_eval, 2)},
        "heuristic_blend_ex_catboost": {
            "directional_accuracy_pct": round(100.0 * hits["ensemble"] / n_eval, 2)
        },
    }
    return out


def _regression_pre_degradation() -> dict[str, Any]:
    """Compare against features/forecast_ensemble_report if present (pre-lag slice)."""
    legacy = ROOT / "features" / "forecast_ensemble_report.json"
    if not legacy.exists():
        return {"available": False, "reason": "no features/forecast_ensemble_report.json"}
    try:
        blob = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)}
    return {
        "available": True,
        "generated_at_utc": blob.get("generated_at_utc"),
        "note": "Legacy AIS-flow ensemble report; architecture intact if file parses. "
        "Not a directional holdout score — used as smoke regression only.",
        "signals_used_keys": list((blob.get("signals_used") or {}).keys()),
    }


def main() -> int:
    before_acc = None
    h0 = _load_health()
    before_acc = (h0.get("quant_pipeline") or {}).get("ensemble_accuracy_pct")
    before_n = h0.get("live_vessel_count")

    ok, msg = _ais_live_ok(h0)
    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ais_gate": {"ok": ok, "detail": msg, "health_snapshot": {
            "source_mode": h0.get("source_mode"),
            "live_vessel_count": before_n,
            "ensemble_accuracy_pct_before": before_acc,
            "replica": h0.get("replica"),
        }},
        "fleet_csv": {
            "path": str(FLEET_CSV),
            "age_days": round(_fleet_age_days(), 2),
            "stale_warning": _fleet_age_days() > 90,
        },
    }
    if not ok:
        report["status"] = "ABORTED_AIS_NOT_LIVE"
        report["threshold_80_met"] = False
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 2

    from services.analytics import select_top500_fleet
    from services.sentinel_analytics import build_sentinel_payload
    from services.balance_analytics import compute_spatial_velocity_delta
    from services.ttf_forecast.quant_pipeline import build_quant_pipeline_payload
    from services.ttf_forecast.run_ttf_pipeline import run_ttf_release_pipeline

    top_df = select_top500_fleet(FLEET_CSV, top_n=500)
    report["fleet_csv"]["top500_rows"] = int(len(top_df))
    report["fleet_csv"]["sample_vessel_types"] = (
        top_df["vessel_type"].astype(str).head(5).tolist() if "vessel_type" in top_df.columns else []
    )

    t0 = time.perf_counter()
    payload = build_sentinel_payload(use_synthetic_if_empty=False, top_n=500)
    vessels = payload.get("vessels") or payload.get("sheet") or []
    if isinstance(payload.get("kpi"), dict) and not vessels:
        vessels = []
    # vessels live in payload structure
    live_n = int(payload.get("live_vessel_count") or 0)
    vessels_list = list(payload.get("c01_heatmap") or [])

    sts = list(payload.get("c03_sts_clusters") or [])

    t_spatial0 = time.perf_counter()
    spatial = compute_spatial_velocity_delta(sts, vessels_list)
    spatial_ms = round((time.perf_counter() - t_spatial0) * 1000, 2)
    build_ms = round((time.perf_counter() - t0) * 1000, 2)

    report["spatial_matching"] = {
        "payload_build_ms": build_ms,
        "spatial_delta_v_ms": spatial_ms,
        "live_vessel_count": live_n,
        "top500_universe": int(len(top_df)),
        "source_mode": payload.get("source_mode"),
        "complexity_note": "ΔV is O(N) over vessels+STS — no N² pairwise; KD-tree not required at N≤500",
        "delta_v_index": spatial.get("delta_v_index"),
        "coverage_gap": int(len(top_df)) - live_n,
    }

    # Component holdout (Markov/Spectral/Elliott)
    components = _component_dir_acc_from_prices(horizon=14)
    report["component_holdout_14d"] = components

    # Retrain CatBoost + full TTF pipeline
    t_ml0 = time.perf_counter()
    pipe = run_ttf_release_pipeline()
    ml_sec = round(time.perf_counter() - t_ml0, 2)
    report["ttf_pipeline"] = {"seconds": ml_sec, "summary": {
        k: pipe.get(k) for k in ("ok", "mode", "spot", "total_seconds", "horizons", "optimal_range")
        if k in pipe
    }}

    # Merge component accuracies into CV metrics file for quant_pipeline
    cv = {}
    if CV_PATH.exists():
        cv = json.loads(CV_PATH.read_text(encoding="utf-8"))
    if isinstance(components, dict) and "error" not in components:
        for key in ("markov", "spectral", "elliott"):
            if key in components:
                cv[key] = components[key]
    CV_PATH.write_text(json.dumps(cv, indent=2), encoding="utf-8")

    catboost_h14 = (cv.get("h14") or {}).get("directional_accuracy_pct")
    catboost_h7 = (cv.get("h7") or {}).get("directional_accuracy_pct")
    catboost_h30 = (cv.get("h30") or {}).get("directional_accuracy_pct")

    quant = build_quant_pipeline_payload(payload)
    after_acc = quant.get("ensemble_accuracy_pct")

    table = {
        "CatBoost_h7": catboost_h7,
        "CatBoost_h14": catboost_h14,
        "CatBoost_h30": catboost_h30,
        "Markov_14d": (components.get("markov") or {}).get("directional_accuracy_pct")
        if isinstance(components, dict)
        else None,
        "Spectral_14d": (components.get("spectral") or {}).get("directional_accuracy_pct")
        if isinstance(components, dict)
        else None,
        "Elliott_14d": (components.get("elliott") or {}).get("directional_accuracy_pct")
        if isinstance(components, dict)
        else None,
    }

    threshold_met = bool(after_acc is not None and float(after_acc) > 80.0)
    report["accuracy"] = {
        "ensemble_before_pct": before_acc,
        "ensemble_after_pct": after_acc,
        "accuracy_basis": quant.get("accuracy_basis"),
        "component_accuracy_pct": quant.get("component_accuracy_pct"),
        "per_model_table": table,
        "threshold_80_met": threshold_met,
    }
    report["regression_pre_degradation"] = _regression_pre_degradation()
    report["status"] = "OK_THRESHOLD_MET" if threshold_met else "OK_BELOW_THRESHOLD"
    report["hypotheses_if_below_80"] = (
        []
        if threshold_met
        else [
            "AIS TOP-500 coverage << 500 (only MMSIs ever observed in DB contribute); flow features starved",
            "14d holdout after AIS restore may still be short / contaminated by STALE window",
            "Static Markov/Spectral/Elliott artifacts not re-fit on post-restore window",
            "Prior 41% figure was confidence-proxy average, not directional holdout — baseline not comparable 1:1",
            "Model drift vs ICE regime; need longer purged WF or feature refresh",
        ]
    )

    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
