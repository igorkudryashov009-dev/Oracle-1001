"""
Assemble TTF forecast UI payload for Sentinel dashboard sheet.
Loads ensemble / quant / importance artifacts; avoids retrain on every build.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output"
MODELS = OUT / "models"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _ensure_forecast() -> dict[str, Any]:
    path = OUT / "ttf_ensemble_forecast.json"
    data = _load_json(path)
    if data.get("horizons"):
        return data
    # Lightweight rebuild without forcing CatBoost retrain if CBM exists
    try:
        from services.ttf_forecast.ensemble_aggregator import run_ensemble
        from services.ttf_forecast.catboost_model import (
            ENSEMBLE_CBM,
            load_feature_matrix,
            select_feature_columns,
            build_supervised,
            predict_quantiles,
            predict_range_proba,
        )
        from catboost import CatBoostRegressor, CatBoostClassifier

        if ENSEMBLE_CBM.exists():
            # Reconstruct minimal catboost_result from saved models
            df = load_feature_matrix()
            cols = select_feature_columns(df)
            bundle = build_supervised(df, cols)
            X_last = bundle["X"].iloc[[-1]]
            models = {"regressors": {}, "classifiers": {}}
            for h in (7, 14, 30):
                models["regressors"][h] = {}
                for q in ("p10", "p50", "p90"):
                    m = CatBoostRegressor()
                    m.load_model(str(MODELS / f"ttf_h{h}_{q}.cbm"))
                    models["regressors"][h][q] = m
                clf = CatBoostClassifier()
                clf.load_model(str(MODELS / f"ttf_h{h}_range_clf.cbm"))
                models["classifiers"][h] = clf
            q_pred = predict_quantiles(models, X_last, spot=float(df["price_ttf_eur_mwh"].iloc[-1]))
            range_proba = predict_range_proba(models, X_last, 7)
            best = max(range_proba.items(), key=lambda kv: kv[1])
            catboost_result = {
                "latest_quantile_forecast": q_pred,
                "latest_range_proba_pct": range_proba,
                "optimal_range": {"label": best[0], "confidence_pct": best[1]},
            }
            return run_ensemble(catboost_result=catboost_result, train_if_needed=False)
        return run_ensemble(train_if_needed=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "horizons": {}}


def _kde_1d(samples: np.ndarray, grid: np.ndarray, bandwidth: Optional[float] = None) -> np.ndarray:
    samples = np.asarray(samples, dtype=float)
    samples = samples[np.isfinite(samples)]
    if len(samples) < 5:
        return np.zeros_like(grid)
    std = float(np.std(samples)) or 1.0
    bw = bandwidth or (1.06 * std * len(samples) ** (-0.2))
    bw = max(bw, 0.25)
    dens = np.zeros_like(grid, dtype=float)
    for x in samples:
        dens += np.exp(-0.5 * ((grid - x) / bw) ** 2)
    dens /= (len(samples) * bw * math.sqrt(2 * math.pi))
    return dens


def _samples_from_quantiles(p10: float, p50: float, p90: float, n: int = 800) -> np.ndarray:
    """Approximate lognormal draws matching p10/p50/p90."""
    p10, p50, p90 = map(float, (p10, p50, p90))
    # Enforce order
    lo, mid, hi = sorted([p10, p50, p90])
    # Map to log space z-scores ≈ -1.28, 0, +1.28
    if mid <= 0:
        mid = max(lo, 1.0)
    sigma = max((math.log(max(hi, mid * 1.01)) - math.log(mid)) / 1.2816, 1e-3)
    mu = math.log(mid)
    rng = np.random.default_rng(1001 + int(mid * 10))
    return rng.lognormal(mean=mu, sigma=sigma, size=n)


def _forecast_cone(spot: float, horizons: dict[str, Any], hist_tail: int = 90) -> dict[str, Any]:
    feat = OUT / "ttf_features.parquet"
    labels: list[str] = []
    hist: list[Optional[float]] = []
    if feat.exists():
        df = pd.read_parquet(feat).tail(hist_tail)
        for _, row in df.iterrows():
            labels.append(str(row.get("date") or row.get("timestamp"))[:10])
            hist.append(float(row["price_ttf_eur_mwh"]))
    else:
        labels = [f"T-{i}" for i in range(hist_tail, 0, -1)]
        hist = [spot] * hist_tail

    # Forward 30 days: interpolate p10/p50/p90 through knots at 7,14,30
    knots = sorted(
        [
            (0, spot, spot, spot),
            (7, float(horizons.get("7", {}).get("p10", spot)), float(horizons.get("7", {}).get("p50", spot)), float(horizons.get("7", {}).get("p90", spot))),
            (14, float(horizons.get("14", {}).get("p10", spot)), float(horizons.get("14", {}).get("p50", spot)), float(horizons.get("14", {}).get("p90", spot))),
            (30, float(horizons.get("30", {}).get("p10", spot)), float(horizons.get("30", {}).get("p50", spot)), float(horizons.get("30", {}).get("p90", spot))),
        ]
    )
    xs = [k[0] for k in knots]
    fwd_labels = [f"+{d}d" for d in range(1, 31)]
    p10, p50, p90 = [], [], []
    for d in range(1, 31):
        p10.append(float(np.interp(d, xs, [k[1] for k in knots])))
        p50.append(float(np.interp(d, xs, [k[2] for k in knots])))
        p90.append(float(np.interp(d, xs, [k[3] for k in knots])))

    # Chart.js: concat history + forward; history has nulls on forecast series
    n_hist = len(hist)
    return {
        "labels": labels + fwd_labels,
        "history": hist + [None] * 30,
        "p50": [None] * (n_hist - 1) + [hist[-1] if hist else spot] + p50,
        "p10": [None] * (n_hist - 1) + [hist[-1] if hist else spot] + p10,
        "p90": [None] * (n_hist - 1) + [hist[-1] if hist else spot] + p90,
        "hist_len": n_hist,
    }


def _top_importance(n: int = 10) -> dict[str, Any]:
    imp = _load_json(MODELS / "ttf_feature_importance.json")
    # Prefer h7 p50
    block = ((imp.get("h7") or {}).get("p50")) or {}
    if not block:
        # flatten first available
        for h, qs in imp.items():
            if isinstance(qs, dict):
                for q, feats in qs.items():
                    if isinstance(feats, dict) and feats:
                        block = feats
                        break
            if block:
                break
    items = sorted(block.items(), key=lambda kv: -float(kv[1]))[:n]
    return {
        "labels": [k.replace("__robust", "").replace("_", " ")[:28] for k, _ in items],
        "values": [round(float(v), 3) for _, v in items],
        "raw": [{ "feature": k, "importance": float(v)} for k, v in items],
    }


def _elliott_badge(elliott: dict[str, Any]) -> dict[str, Any]:
    ell = elliott.get("elliott") or elliott
    impulse = ell.get("impulse") or {}
    status = ell.get("status") or "unknown"
    direction = impulse.get("direction") or "—"
    waves = impulse.get("waves") or []
    active = waves[-1]["label"] if waves else "—"
    label = f"Wave {active} Impulse Active" if active not in ("—",) and direction == "bullish" else (
        f"Wave {active} Bearish Impulse" if active not in ("—",) else f"Elliott: {status}"
    )
    if ell.get("corrective"):
        clab = ell["corrective"][-1].get("label")
        if clab:
            label = f"Corrective {clab} Active · after impulse"
    return {
        "badge": label,
        "status": status,
        "direction": direction,
        "score": impulse.get("score"),
    }


def build_ttf_forecast_payload(*, refresh_ensemble: bool = False) -> dict[str, Any]:
    from services.ttf_forecast.integrity import (
        SREBuildError,
        assert_market_features_integrity,
        assert_spot_in_band,
        assert_ttf_ui_payload,
        ensure_granger_nonempty,
        ensure_importance_nonempty,
        run_pre_build_gates,
    )

    # HARD integrity: market table + ensemble file before UI assembly
    gates = run_pre_build_gates()

    if refresh_ensemble:
        try:
            from services.ttf_forecast.ensemble_aggregator import run_ensemble

            forecast = run_ensemble(train_if_needed=True)
        except Exception as exc:  # noqa: BLE001
            forecast = _ensure_forecast()
            forecast["refresh_error"] = str(exc)
    else:
        forecast = _ensure_forecast()

    horizons = forecast.get("horizons") or {}
    spot = float(forecast.get("spot_eur_mwh") or gates["market"]["spot_eur_mwh"] or 0)
    if not spot:
        feat = OUT / "ttf_features.parquet"
        if feat.exists():
            spot = float(pd.read_parquet(feat)["price_ttf_eur_mwh"].iloc[-1])
    assert_spot_in_band(spot)

    markov = _load_json(OUT / "ttf_markov_regimes.json")
    causality = _load_json(OUT / "ttf_causality_elliott.json")
    granger = causality.get("granger") or {}

    # KDE panels
    grid = np.linspace(max(5, spot * 0.4), spot * 2.2 + 20, 120)
    kde = {}
    for h in ("7", "14", "30"):
        band = horizons.get(h) or {}
        if not band:
            raise SREBuildError("TTF_HORIZON_MISSING", f"ensemble missing horizon {h}")
        samples = _samples_from_quantiles(band.get("p10", spot), band.get("p50", spot), band.get("p90", spot))
        dens = _kde_1d(samples, grid)
        kde[h] = {
            "x": [round(float(x), 2) for x in grid],
            "density": [round(float(y), 6) for y in dens],
            "p50": float(band.get("p50", spot)),
            "p10": float(band.get("p10", spot)),
            "p90": float(band.get("p90", spot)),
        }

    # Granger lag graph (never blank — placeholder if soft-fail)
    lags = granger.get("lags") or []
    granger_chart = {
        "labels": [str(r.get("lag")) for r in lags],
        "neg_log10_p": [
            round(-math.log10(max(float(r.get("ssr_ftest_p") or 1.0), 1e-12)), 3) for r in lags
        ],
        "significant": [bool(r.get("significant_5pct")) for r in lags],
        "best_lag": granger.get("best_lag"),
        "any_significant_5pct": bool(granger.get("any_significant_5pct")),
        "causal_index": round(
            float((granger.get("best_lag") or {}).get("ssr_ftest_stat") or 0.0), 3
        ),
    }
    granger_chart = ensure_granger_nonempty(granger_chart)

    # Markov heatmap
    Pij = markov.get("transition_matrix_Pij") or [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    state_names = markov.get("state_names") or {
        "0": "LowVol", "1": "Transit", "2": "HighVol"
    }
    labels = [state_names.get(str(i), str(i)) for i in range(len(Pij))]

    opt = forecast.get("optimal_range") or {}
    state_probs = markov.get("current_state_probabilities") or {}
    current_state = markov.get("current_state_name") or "—"

    # Portfolio ROI + hedging panels G/H/I — hard fail if hedge engine dies
    from services.ttf_forecast.hedging_engine import run_hedging_engine

    try:
        hedging = run_hedging_engine(forecast=forecast, write=True)
    except Exception as exc:  # noqa: BLE001
        raise SREBuildError("TTF_HEDGE_BUILD", str(exc)) from exc

    importance = ensure_importance_nonempty(_top_importance(10), spot)
    cone = _forecast_cone(spot, horizons)
    if int(cone.get("hist_len") or 0) < 20:
        raise SREBuildError(
            "TTF_CONE_HISTORY",
            f"history points={cone.get('hist_len')} (need ≥20) — rebuild features.parquet",
        )

    payload = {
        "spot_eur_mwh": spot,
        "model_id": forecast.get("model_id"),
        "integrity_status": "PASS",
        "integrity_gates": gates,
        "kpi": {
            "h7_p50": round(float((horizons.get("7") or {}).get("p50") or 0), 2),
            "h14_p50": round(float((horizons.get("14") or {}).get("p50") or 0), 2),
            "h30_p50": round(float((horizons.get("30") or {}).get("p50") or 0), 2),
            "h7_band": {
                "p10": round(float((horizons.get("7") or {}).get("p10") or 0), 2),
                "p90": round(float((horizons.get("7") or {}).get("p90") or 0), 2),
            },
            "market_state": current_state,
            "market_state_probs": {k: round(float(v) * 100, 1) for k, v in state_probs.items()},
            "ais_causal_index": granger_chart["causal_index"],
            "ais_causal_best_lag": (granger_chart.get("best_lag") or {}).get("lag"),
            "ais_causal_p": (granger_chart.get("best_lag") or {}).get("ssr_ftest_p"),
            "optimal_range": opt.get("label"),
            "max_confidence_pct": round(float(opt.get("confidence_pct") or forecast.get("max_confidence_pct") or 0), 1),
            "portfolio_roi_pct": (hedging.get("summary") or {}).get("roi_30d_pct"),
            "portfolio_terminal_usd": ((hedging.get("panel_g") or {}).get("center") or {}).get("to_usd"),
        },
        "cone": cone,
        "kde": kde,
        "granger": granger_chart,
        "elliott": _elliott_badge(causality),
        "importance": importance,
        "markov_heatmap": {
            "labels": labels,
            "matrix": [[round(float(x), 3) for x in row] for row in Pij],
            "current_state": current_state,
            "occupancy_pct": markov.get("state_occupancy_pct") or {},
        },
        "range_proba_pct": forecast.get("range_proba_pct") or {},
        "weights": forecast.get("weights") or {},
        "horizons": {
            h: {
                "p10": round(float(v.get("p10", 0)), 2),
                "p50": round(float(v.get("p50", 0)), 2),
                "p90": round(float(v.get("p90", 0)), 2),
            }
            for h, v in horizons.items()
            if isinstance(v, dict)
        },
        "hedging": hedging,
    }
    assert_ttf_ui_payload(payload)
    # Frontend alias expected by SRE brief
    payload["SENTINEL_TTF_DATA"] = True
    return payload
