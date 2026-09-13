"""
Meta-ensemble aggregator for TTF multi-horizon forecasts.

Weights:
  0.40 CatBoost Quantile Regressor
  0.25 Markov Chain transition distribution
  0.20 Spectral / Fourier extrapolation
  0.15 Elliott Wave target projection
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from services.ttf_forecast.logging_utils import get_quant_logger
from services.ttf_forecast.schema import migrate_ttf_schema, resolve_db
from services.utils.path_sanitizer import sanitize_structure

ROOT = Path(__file__).resolve().parents[2]
OUT_JSON = ROOT / "output" / "ttf_ensemble_forecast.json"
MODELS_DIR = ROOT / "output" / "models"

WEIGHTS = {
    "catboost": 0.40,
    "markov": 0.25,
    "spectral": 0.20,
    "elliott": 0.15,
}
HORIZONS = (7, 14, 30)
CV_METRICS_JSON = MODELS_DIR / "ttf_cv_metrics.json"
# Soft floor: never hard-remove a component (governance, not deletion).
MIN_COMPONENT_WEIGHT = 0.02
logger = get_quant_logger("ttf.ensemble")


def _safe_load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_component_directional_accuracy(
    cv: dict[str, Any] | None = None,
) -> dict[str, float | None]:
    """Read per-model directional accuracy from ttf_cv_metrics.json."""
    blob = cv if cv is not None else _safe_load_json(CV_METRICS_JSON)
    out: dict[str, float | None] = {
        "catboost": None,
        "markov": None,
        "spectral": None,
        "elliott": None,
    }
    h14 = (blob.get("h14") or {}).get("directional_accuracy_pct")
    if h14 is not None:
        out["catboost"] = float(h14)
    else:
        vals = [
            float((blob.get(k) or {}).get("directional_accuracy_pct"))
            for k in ("h7", "h14", "h30")
            if (blob.get(k) or {}).get("directional_accuracy_pct") is not None
        ]
        if vals:
            out["catboost"] = float(sum(vals) / len(vals))
    for key in ("markov", "spectral", "elliott"):
        val = (blob.get(key) or {}).get("directional_accuracy_pct")
        if val is not None:
            out[key] = float(val)
    return out


def adaptive_ensemble_weights(
    base: dict[str, float] | None = None,
    *,
    cv: dict[str, Any] | None = None,
    margin_pp: float = 15.0,
) -> tuple[dict[str, float], dict[str, Any]]:
    """
    Adaptive downweighting (not hard removal).

    If a component's directional accuracy is below
    (mean of other measured components − margin_pp), scale its weight by
    relative_performance = max(eps, acc_i / peer_mean), then renormalize.
    Floor each weight at MIN_COMPONENT_WEIGHT before final renorm.
    """
    weights = dict(base or WEIGHTS)
    acc = load_component_directional_accuracy(cv)
    measured = {k: v for k, v in acc.items() if v is not None and k in weights}
    governance: dict[str, Any] = {
        "applied": False,
        "margin_pp": margin_pp,
        "component_accuracy_pct": {k: (round(v, 2) if v is not None else None) for k, v in acc.items()},
        "base_weights": dict(weights),
        "downweighted": [],
        "reason": "insufficient_component_metrics" if len(measured) < 2 else "ok",
    }
    if len(measured) < 2:
        governance["effective_weights"] = dict(weights)
        return weights, governance

    adjusted = dict(weights)
    for name, acc_i in measured.items():
        peers = [v for k, v in measured.items() if k != name]
        if not peers:
            continue
        peer_mean = float(sum(peers) / len(peers))
        threshold = peer_mean - float(margin_pp)
        if acc_i >= threshold:
            continue
        relative = max(0.05, float(acc_i) / max(peer_mean, 1e-6))
        old_w = float(adjusted[name])
        new_w = max(MIN_COMPONENT_WEIGHT, old_w * relative)
        adjusted[name] = new_w
        governance["downweighted"].append(
            {
                "model": name,
                "dir_acc_pct": round(float(acc_i), 2),
                "peer_mean_pct": round(peer_mean, 2),
                "threshold_pct": round(threshold, 2),
                "relative_performance": round(relative, 4),
                "weight_before": round(old_w, 4),
                "weight_after": round(new_w, 4),
            }
        )
        governance["applied"] = True

    # Renormalize to sum=1
    total = sum(adjusted.values()) or 1.0
    adjusted = {k: float(v) / total for k, v in adjusted.items()}
    governance["effective_weights"] = {k: round(v, 4) for k, v in adjusted.items()}
    if governance["applied"]:
        logger.warning(
            "Ensemble governance downweight applied: %s",
            governance["downweighted"],
        )
    return adjusted, governance


def markov_price_distribution(
    spot: float,
    markov_report: dict[str, Any],
    horizon: int,
) -> dict[str, float]:
    """
    Map current HMM Gaussian return params into price quantiles over horizon days.
    Uses mixture N(μ_k, σ_k²) with current state probabilities; scales by √h.
    """
    gauss = markov_report.get("gaussian_params") or {}
    probs = markov_report.get("current_state_probabilities") or {}
    if not gauss or not probs:
        return {"p10": spot * 0.95, "p50": spot, "p90": spot * 1.05}

    mus, sigs, ws = [], [], []
    for k, meta in gauss.items():
        name = meta.get("name")
        w = float(probs.get(name, 0.0))
        if w <= 0:
            # fallback uniform if degenerate
            w = 1.0 / max(len(gauss), 1)
        mus.append(float(meta["mu"]))
        sigs.append(float(meta["sigma"]))
        ws.append(w)
    ws_arr = np.asarray(ws, dtype=float)
    ws_arr = ws_arr / ws_arr.sum()
    mu = float(np.dot(ws_arr, mus))
    # Mixture variance
    second = float(np.dot(ws_arr, np.asarray(mus) ** 2 + np.asarray(sigs) ** 2))
    var = max(second - mu**2, 1e-12)
    sigma = float(np.sqrt(var) * np.sqrt(horizon))
    # Log-price approx: S_h ≈ S * exp(h*μ ± z σ√h) with daily μ already in return units
    mean_ret = mu * horizon
    p10 = float(spot * np.exp(mean_ret - 1.2816 * sigma))
    p50 = float(spot * np.exp(mean_ret))
    p90 = float(spot * np.exp(mean_ret + 1.2816 * sigma))
    return {"p10": p10, "p50": p50, "p90": p90}


def spectral_price_projection(
    spot: float,
    spectral_report: dict[str, Any],
    horizon: int,
) -> dict[str, float]:
    """Integrate extrapolated log-return trend profile into price path."""
    extr = (spectral_report.get("trend_extrapolation") or {}).get("extrapolation") or []
    if not extr:
        return {"p10": spot * 0.97, "p50": spot, "p90": spot * 1.03}
    # extrapolation is in detrended+filtered return space; cumulative sum → log price change
    use = extr[:horizon]
    if len(use) < horizon:
        use = use + [use[-1]] * (horizon - len(use))
    cum = float(np.cumsum(use)[-1])
    # Scale down — spectral residual amplitude can be large; squash with tanh
    cum = float(np.tanh(cum) * 0.08)
    p50 = float(spot * np.exp(cum))
    band = 0.02 + 0.0015 * horizon
    return {"p10": p50 * (1 - band), "p50": p50, "p90": p50 * (1 + band)}


def elliott_target_projection(
    spot: float,
    elliott_report: dict[str, Any],
    horizon: int,
) -> dict[str, float]:
    """Project next Elliott target from impulse/corrective fib matches."""
    ell = elliott_report.get("elliott") or elliott_report
    impulse = ell.get("impulse") or {}
    waves = impulse.get("waves") or []
    if not waves:
        drift = 0.001 * horizon
        p50 = spot * (1 + drift)
        return {"p10": p50 * 0.96, "p50": p50, "p90": p50 * 1.04}

    last = waves[-1]
    # Extension target: 1.618 of wave-1 from wave-0 start if available
    w1 = next((w for w in waves if w.get("label") == "1"), None)
    if w1:
        move1 = float(w1["end_price"]) - float(w1["start_price"])
        direction = 1.0 if move1 >= 0 else -1.0
        target = float(last["end_price"]) + direction * abs(move1) * 0.618
    else:
        target = float(last["end_price"])

    # Blend target toward spot with horizon decay (don't jump full fib instantly)
    alpha = min(1.0, horizon / 30.0)
    p50 = float((1 - alpha) * spot + alpha * target)
    width = abs(p50 - spot) * 0.35 + spot * 0.015
    return {"p10": p50 - width, "p50": p50, "p90": p50 + width}


def weighted_merge(
    parts: dict[str, dict[str, float]],
    weights: dict[str, float] = WEIGHTS,
) -> dict[str, float]:
    from services.ttf_forecast.ingest_ttf import PRICE_CAP_EUR, PRICE_FLOOR_EUR

    keys = ("p10", "p50", "p90")
    out = {}
    wsum = sum(weights[k] for k in parts if k in weights) or 1.0
    for q in keys:
        out[q] = float(
            sum(weights[k] * parts[k][q] for k in parts if k in weights) / wsum
        )
    # Monotone bands + operating band €60–€95
    lo, mid, hi = sorted([out["p10"], out["p50"], out["p90"]])
    lo = max(PRICE_FLOOR_EUR, min(PRICE_CAP_EUR, lo))
    mid = max(PRICE_FLOOR_EUR, min(PRICE_CAP_EUR, mid))
    hi = max(PRICE_FLOOR_EUR, min(PRICE_CAP_EUR, hi))
    lo, mid, hi = sorted([lo, mid, hi])
    return {"p10": lo, "p50": mid, "p90": hi}


def persist_predictions(
    forecast: dict[str, Any],
    *,
    db_path: Optional[Path] = None,
    model_id: str = "ttf_meta_ensemble_v1",
) -> int:
    path = resolve_db(db_path)
    migrate_ttf_schema(path)
    created = int(time.time())
    rows = []
    for h, band in forecast["horizons"].items():
        # target_date ≈ now + h days (midnight UTC approximation via epoch)
        target = created + int(h) * 86400
        rows.append(
            (
                model_id,
                int(h),
                int(target),
                float(band["p10"]),
                float(band["p50"]),
                float(band["p90"]),
                float(forecast.get("max_confidence_pct") or 0.0),
                created,
            )
        )
    conn = sqlite3.connect(str(path), timeout=60.0)
    try:
        conn.executemany(
            """
            INSERT INTO ttf_predictions_store (
                model_id, horizon_days, target_date,
                price_p10, price_p50, price_p90, top_prob_pct, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def run_ensemble(
    *,
    catboost_result: Optional[dict[str, Any]] = None,
    train_if_needed: bool = False,
    force_retrain: bool = False,
) -> dict[str, Any]:
    """Aggregate TTF ensemble forecast.

    Serving contract (Node A/B): default is inference-only.
    - force_retrain=True → offline batch training (dev/worker only)
    - existing .cbm → load_catboost_for_inference (never silent retrain)
    - train_if_needed=True and no .cbm → train once (local bootstrap only)
    """
    logger.info("Ensemble aggregator start")
    if catboost_result is None:
        from services.ttf_forecast.catboost_model import (
            ENSEMBLE_CBM,
            load_catboost_for_inference,
            run_catboost_pipeline,
        )

        if force_retrain:
            logger.warning("CatBoost force_retrain=True — offline batch path")
            catboost_result = run_catboost_pipeline()
        elif ENSEMBLE_CBM.exists():
            try:
                catboost_result = load_catboost_for_inference()
            except Exception as exc:  # noqa: BLE001
                logger.warning("CatBoost inference load failed: %s", exc)
                if train_if_needed:
                    catboost_result = run_catboost_pipeline()
        elif train_if_needed:
            logger.warning("No .cbm on disk — bootstrap train_if_needed")
            catboost_result = run_catboost_pipeline()

    # Spot from features
    feat_path = ROOT / "output" / "ttf_features.parquet"
    spot = float(pd.read_parquet(feat_path)["price_ttf_eur_mwh"].iloc[-1])

    # Ensure aux reports exist
    spectral_path = ROOT / "output" / "ttf_spectral_report.json"
    markov_path = ROOT / "output" / "ttf_markov_regimes.json"
    elliott_path = ROOT / "output" / "ttf_causality_elliott.json"
    if not spectral_path.exists():
        from services.ttf_forecast.spectral_engine import run_spectral_analysis

        run_spectral_analysis()
    if not markov_path.exists():
        from services.ttf_forecast.markov_engine import run_markov_engine

        run_markov_engine()
    if not elliott_path.exists():
        from services.ttf_forecast.causality_wave import run_causality_wave

        run_causality_wave()

    spectral = _safe_load_json(spectral_path)
    markov = _safe_load_json(markov_path)
    elliott = _safe_load_json(elliott_path)

    cb_q = catboost_result["latest_quantile_forecast"] if catboost_result else {}
    range_info = (catboost_result or {}).get("optimal_range") or {}
    range_proba = (catboost_result or {}).get("latest_range_proba_pct") or {}

    eff_weights, weight_governance = adaptive_ensemble_weights(WEIGHTS)

    horizons_out: dict[str, Any] = {}
    for h in HORIZONS:
        parts = {
            "catboost": cb_q.get(h) or {"p10": spot * 0.95, "p50": spot, "p90": spot * 1.05},
            "markov": markov_price_distribution(spot, markov, h),
            "spectral": spectral_price_projection(spot, spectral, h),
            "elliott": elliott_target_projection(spot, elliott, h),
        }
        merged = weighted_merge(parts, weights=eff_weights)
        horizons_out[str(h)] = {
            **merged,
            "components": parts,
            "weights": eff_weights,
        }

    max_conf = float(range_info.get("confidence_pct") or max(range_proba.values(), default=0.0))

    # Dual-Truth on confidence: offline CV vs live fleet representativeness
    from services.dual_gate import compute_fleet_sample_status, live_inference_confidence

    try:
        acc_map = load_component_directional_accuracy()
        measured = [v for v in acc_map.values() if v is not None]
        model_cv = round(sum(measured) / len(measured), 2) if measured else None
    except Exception:  # noqa: BLE001
        model_cv = None

    cov_n = 0
    try:
        from services.ais_health import compute_top500_live_coverage

        cov_n = int(compute_top500_live_coverage().get("top500_live_coverage") or 0)
    except Exception:  # noqa: BLE001
        pass
    sample = compute_fleet_sample_status(cov_n)
    live_conf = live_inference_confidence(
        model_cv_accuracy_pct=model_cv,
        fleet_sample_status=sample["fleet_sample_status"],
        coverage=cov_n,
    )

    forecast = {
        "model_id": "ttf_meta_ensemble_v1",
        "spot_eur_mwh": spot,
        "weights": eff_weights,
        "weights_base": WEIGHTS,
        "weight_governance": weight_governance,
        "horizons": horizons_out,
        "optimal_range": range_info or {
            "label": max(range_proba, key=range_proba.get) if range_proba else "B_68_75",
            "confidence_pct": max_conf,
        },
        "range_proba_pct": range_proba,
        "max_confidence_pct": max_conf,
        "model_cv_accuracy_pct": live_conf.get("model_cv_accuracy_pct"),
        "model_last_retrained": (catboost_result or {}).get("model_last_retrained"),
        "live_inference_confidence": live_conf.get("live_inference_confidence"),
        "live_inference_confidence_pct": live_conf.get("live_inference_confidence_pct"),
        "live_confidence_factor": live_conf.get("live_confidence_factor"),
        "fleet_sample_status": sample["fleet_sample_status"],
        "sample_size_caveat": sample.get("sample_size_caveat"),
        "artifacts": {
            "ensemble_cbm": str(MODELS_DIR / "ttf_ensemble.cbm"),
            "importance": str(MODELS_DIR / "ttf_feature_importance.json"),
            "forecast_json": str(OUT_JSON),
        },
    }
    if not forecast["model_last_retrained"]:
        try:
            from services.ttf_forecast.catboost_model import resolve_model_last_retrained

            forecast["model_last_retrained"] = resolve_model_last_retrained()
        except Exception:  # noqa: BLE001
            pass

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    # Strip host-absolute Windows paths before JSON dump (Linux/container safe)
    serializable = sanitize_structure(json.loads(json.dumps(forecast, default=str)))
    OUT_JSON.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    n = persist_predictions(serializable)
    logger.info(
        "Ensemble OK spot=%.2f h7_p50=%.2f range=%s conf=%.1f%% rows_db=%s → %s",
        spot,
        horizons_out["7"]["p50"],
        forecast["optimal_range"].get("label"),
        max_conf,
        n,
        OUT_JSON,
    )
    return serializable


def main() -> int:
    forecast = run_ensemble(train_if_needed=False, force_retrain=False)
    # Print compact summary (no model objects)
    summary = {
        "spot": forecast["spot_eur_mwh"],
        "horizons": {
            h: {k: round(v[k], 3) for k in ("p10", "p50", "p90")}
            for h, v in forecast["horizons"].items()
        },
        "optimal_range": forecast["optimal_range"],
        "max_confidence_pct": forecast["max_confidence_pct"],
        "cbm": forecast["artifacts"]["ensemble_cbm"],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
