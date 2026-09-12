"""
TTF forecast release sub-pipeline with graceful CatBoost fallback.

Sequence:
  1) ingest + feature engineering
  2) spectral + markov (+ causality/elliott best-effort)
  3) catboost ensemble OR spectral+markov-only fallback
  4) write ttf_ensemble_forecast.json for dashboard inject

Logs timing / RSS to logs/quant_engine.log and returns a summary dict.
"""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output"
FORECAST_JSON = OUT / "ttf_ensemble_forecast.json"


def _rss_mb() -> Optional[float]:
    try:
        import psutil  # type: ignore

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
    except Exception:
        try:
            import resource  # Unix

            # ru_maxrss is KB on Linux
            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
        except Exception:
            return None


def _timed(name: str, fn: Callable[[], Any], log) -> tuple[Any, dict[str, Any]]:
    t0 = time.perf_counter()
    rss0 = _rss_mb()
    err = None
    result = None
    try:
        result = fn()
        ok = True
    except Exception as exc:  # noqa: BLE001
        ok = False
        err = f"{type(exc).__name__}: {exc}"
        log.exception("TTF step failed: %s", name)
        traceback.print_exc()
    dt = round(time.perf_counter() - t0, 3)
    rss1 = _rss_mb()
    meta = {
        "step": name,
        "ok": ok,
        "seconds": dt,
        "rss_mb_before": rss0,
        "rss_mb_after": rss1,
        "error": err,
    }
    log.info(
        "PERF %s ok=%s sec=%.3f rss_mb=%s→%s err=%s",
        name,
        ok,
        dt,
        rss0,
        rss1,
        err,
    )
    return result, meta


def _spectral_markov_fallback_forecast() -> dict[str, Any]:
    """Build ensemble forecast without CatBoost (weights renormalized)."""
    from services.ttf_forecast.ensemble_aggregator import (
        HORIZONS,
        OUT_JSON,
        markov_price_distribution,
        persist_predictions,
        spectral_price_projection,
        elliott_target_projection,
        weighted_merge,
        _safe_load_json,
    )
    import pandas as pd

    feat = OUT / "ttf_features.parquet"
    spot = float(pd.read_parquet(feat)["price_ttf_eur_mwh"].iloc[-1])
    spectral = _safe_load_json(OUT / "ttf_spectral_report.json")
    markov = _safe_load_json(OUT / "ttf_markov_regimes.json")
    elliott = _safe_load_json(OUT / "ttf_causality_elliott.json")

    weights = {"markov": 0.45, "spectral": 0.35, "elliott": 0.20}
    horizons_out: dict[str, Any] = {}
    for h in HORIZONS:
        parts = {
            "markov": markov_price_distribution(spot, markov, h),
            "spectral": spectral_price_projection(spot, spectral, h),
            "elliott": elliott_target_projection(spot, elliott, h),
        }
        merged = weighted_merge(parts, weights=weights)
        horizons_out[str(h)] = {**merged, "components": parts, "weights": weights}

    forecast = {
        "model_id": "ttf_spectral_markov_fallback_v1",
        "spot_eur_mwh": spot,
        "weights": weights,
        "horizons": horizons_out,
        "optimal_range": {"label": "FALLBACK_SPECTRAL_MARKOV", "confidence_pct": 55.0},
        "range_proba_pct": {},
        "max_confidence_pct": 55.0,
        "fallback": True,
        "artifacts": {"forecast_json": str(OUT_JSON)},
    }
    from services.utils.path_sanitizer import sanitize_structure

    forecast = sanitize_structure(forecast)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(forecast, indent=2, default=str), encoding="utf-8")
    try:
        persist_predictions(forecast, model_id=forecast["model_id"])
    except Exception:
        pass
    return forecast


def run_ttf_release_pipeline(*, skip_catboost: bool = False) -> dict[str, Any]:
    from services.ttf_forecast.logging_utils import get_quant_logger

    log = get_quant_logger("ttf.release")
    log.info("=== TTF RELEASE PIPELINE START ===")
    perf: list[dict[str, Any]] = []
    summary: dict[str, Any] = {"ok": False, "mode": None, "perf": perf}

    # 1) Ingest + features
    def _ingest():
        from services.ttf_forecast.ingest_ttf import run_ingest
        from services.ttf_forecast.feature_engineering import run_feature_pipeline

        ing = run_ingest(days=365, force_synthetic=False)
        feat = run_feature_pipeline(ensure_ingest=False)
        return {"ingest": ing, "features": feat}

    _, meta = _timed("1_ingest_features", _ingest, log)
    perf.append(meta)
    if not meta["ok"]:
        summary["error"] = meta["error"]
        return summary

    # 2) Signal processing
    def _signals():
        from services.ttf_forecast.spectral_engine import run_spectral_analysis
        from services.ttf_forecast.markov_engine import run_markov_engine
        from services.ttf_forecast.causality_wave import run_causality_wave

        sp = run_spectral_analysis()
        mk = run_markov_engine()
        try:
            cw = run_causality_wave()
        except Exception as exc:  # noqa: BLE001
            log.warning("causality_wave soft-fail: %s", exc)
            cw = {"error": str(exc)}
        return {"spectral": sp, "markov": mk, "causality": cw}

    _, meta = _timed("2_spectral_markov_causality", _signals, log)
    perf.append(meta)
    if not meta["ok"]:
        summary["error"] = meta["error"]
        return summary

    # 3) Ensemble (CatBoost preferred)
    forecast = None
    mode = "catboost_ensemble"
    if not skip_catboost:
        def _ensemble():
            from services.ttf_forecast.ensemble_aggregator import run_ensemble

            return run_ensemble(train_if_needed=True)

        forecast, meta = _timed("3_catboost_ensemble", _ensemble, log)
        perf.append(meta)
        if not meta["ok"]:
            log.warning("CatBoost ensemble failed — falling back to Spectral+Markov")
            mode = "spectral_markov_fallback"
            forecast, meta2 = _timed("3b_spectral_markov_fallback", _spectral_markov_fallback_forecast, log)
            perf.append(meta2)
            if not meta2["ok"]:
                summary["error"] = meta2["error"]
                return summary
    else:
        mode = "spectral_markov_fallback"
        forecast, meta = _timed("3_spectral_markov_fallback", _spectral_markov_fallback_forecast, log)
        perf.append(meta)
        if not meta["ok"]:
            summary["error"] = meta["error"]
            return summary

    summary.update({
        "ok": True,
        "mode": mode,
        "forecast_path": str(FORECAST_JSON),
        "spot": (forecast or {}).get("spot_eur_mwh"),
        "horizons": {
            h: {
                "p10": round(float(v.get("p10", 0)), 2),
                "p50": round(float(v.get("p50", 0)), 2),
                "p90": round(float(v.get("p90", 0)), 2),
            }
            for h, v in ((forecast or {}).get("horizons") or {}).items()
            if isinstance(v, dict)
        },
        "optimal_range": (forecast or {}).get("optimal_range"),
        "total_seconds": round(sum(p["seconds"] for p in perf), 3),
    })

    # 4) Portfolio ROI + hedging strategies ($1,000 desk book)
    def _hedge():
        from services.ttf_forecast.hedging_engine import run_hedging_engine

        return run_hedging_engine(forecast=forecast or {}, write=True)

    hedge, meta_h = _timed("4_hedging_portfolio", _hedge, log)
    perf.append(meta_h)
    if meta_h["ok"] and isinstance(hedge, dict):
        summary["hedging"] = {
            "path": str(OUT / "ttf_hedging_portfolio.json"),
            "recommended": hedge.get("recommended_strategy_id"),
            "center": (hedge.get("panel_g") or {}).get("center"),
            "roi_30d_pct": (hedge.get("summary") or {}).get("roi_30d_pct"),
        }
    else:
        summary["hedging_error"] = meta_h.get("error")

    # 5) Paper execution ledger + MtM marks
    def _paper():
        from services.ttf_forecast.paper_ledger import auto_log_daily_paper_trades

        spot = float((forecast or {}).get("spot_eur_mwh") or 71.952)
        p50 = float((((forecast or {}).get("horizons") or {}).get("30") or {}).get("p50") or spot)
        return auto_log_daily_paper_trades(spot=spot, p50_30=p50, write_json=True)

    paper, meta_p = _timed("5_paper_ledger_mtm", _paper, log)
    perf.append(meta_p)
    if meta_p["ok"] and isinstance(paper, dict):
        summary["paper_ledger"] = {
            "open_count": paper.get("open_count"),
            "badge": paper.get("live_paper_badge"),
            "path": str(OUT / "ttf_paper_ledger.json"),
        }
    else:
        summary["paper_ledger_error"] = meta_p.get("error")

    summary["total_seconds"] = round(sum(p["seconds"] for p in perf), 3)
    log.info("=== TTF RELEASE PIPELINE OK mode=%s spot=%s ===", mode, summary.get("spot"))
    from services.utils.path_sanitizer import sanitize_structure

    summary = sanitize_structure(summary)
    (OUT / "ttf_release_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    s = run_ttf_release_pipeline()
    print(json.dumps({k: s[k] for k in s if k != "perf"}, indent=2, default=str))
    print(json.dumps({"perf": s.get("perf")}, indent=2))
    return 0 if s.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
