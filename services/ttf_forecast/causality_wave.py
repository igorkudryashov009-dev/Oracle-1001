"""
Granger causality (AIS shadow fleet density → TTF) + automated Elliott Wave detector.

Elliott impulse (1-5) / corrective (A-C) via local extrema + Fibonacci constraints
(0.618, 1.618) with tolerance bands.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema
from statsmodels.tsa.stattools import grangercausalitytests

from services.ttf_forecast.logging_utils import get_quant_logger

ROOT = Path(__file__).resolve().parents[2]
OUT_JSON = ROOT / "output" / "ttf_causality_elliott.json"
logger = get_quant_logger("ttf.causality_wave")

FIB_RATIOS = (0.382, 0.618, 1.0, 1.618, 2.618)
FIB_TOL = 0.12  # ±12% relative tolerance around Fibonacci targets


@dataclass
class WavePivot:
    index: int
    price: float
    kind: str  # peak | trough


@dataclass
class ElliottLabel:
    label: str  # 1..5 or A..C
    start_idx: int
    end_idx: int
    start_price: float
    end_price: float
    fib_ratio_vs_prev: Optional[float]
    fib_match: Optional[float]


def load_panel(parquet: Path | None = None) -> pd.DataFrame:
    path = parquet or (ROOT / "output" / "ttf_features.parquet")
    df = pd.read_parquet(path)
    need_price = "price_ttf_eur_mwh" if "price_ttf_eur_mwh" in df.columns else None
    if need_price is None:
        raise ValueError("price_ttf_eur_mwh missing from features parquet")
    # Shadow fleet density: prefer market feature column, else OSINT proxy
    dens_col = None
    for c in (
        "shadow_tanker_density_gulf",
        "lng_tanker_transit_index",
        "ais_gap_anomaly_vol_proxy",
    ):
        if c in df.columns:
            dens_col = c
            break
    if dens_col is None:
        raise ValueError("No AIS density / OSINT column found for Granger test")
    out = pd.DataFrame({
        "price": df[need_price].astype(float),
        "density": df[dens_col].astype(float),
        "log_return": df["log_return"].astype(float) if "log_return" in df.columns
        else np.log(df[need_price].astype(float)).diff(),
    }).replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
    return out


def granger_ais_to_ttf(
    panel: pd.DataFrame,
    maxlag: int = 14,
) -> dict[str, Any]:
    """
    Granger causality: does AIS shadow density help predict TTF log returns?
    statsmodels expects columns [y, x] for testing whether x Granger-causes y.
    """
    y = panel["log_return"].to_numpy()
    x = panel["density"].pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy()
    data = np.column_stack([y, x])
    # Drop leading NaNs already handled; require length
    if len(data) < maxlag + 20:
        raise ValueError(f"Need ≥{maxlag + 20} rows for Granger, got {len(data)}")

    results = grangercausalitytests(data, maxlag=maxlag)
    rows = []
    best = {"lag": None, "ssr_ftest_p": 1.0}
    for lag in range(1, maxlag + 1):
        res = results[lag][0]
        ssr_ftest = res["ssr_ftest"]
        ssr_chi2 = res["ssr_chi2test"]
        row = {
            "lag": lag,
            "ssr_ftest_stat": float(ssr_ftest[0]),
            "ssr_ftest_p": float(ssr_ftest[1]),
            "ssr_chi2_stat": float(ssr_chi2[0]),
            "ssr_chi2_p": float(ssr_chi2[1]),
            "significant_5pct": bool(ssr_ftest[1] < 0.05),
        }
        rows.append(row)
        if row["ssr_ftest_p"] < best["ssr_ftest_p"]:
            best = {"lag": lag, "ssr_ftest_p": row["ssr_ftest_p"], "stat": row["ssr_ftest_stat"]}

    any_sig = any(r["significant_5pct"] for r in rows)
    summary = {
        "hypothesis": "AIS_shadow_density → TTF_log_return",
        "maxlag": maxlag,
        "any_significant_5pct": any_sig,
        "best_lag": best,
        "lags": rows,
    }
    logger.info(
        "Granger AIS→TTF best_lag=%s p=%.4g any_sig=%s",
        best.get("lag"),
        best.get("ssr_ftest_p", 1.0),
        any_sig,
    )
    return summary


def find_pivots(prices: np.ndarray, order: int = 3) -> list[WavePivot]:
    peaks = argrelextrema(prices, np.greater, order=order)[0]
    troughs = argrelextrema(prices, np.less, order=order)[0]
    pivots: list[WavePivot] = []
    for i in peaks:
        pivots.append(WavePivot(int(i), float(prices[i]), "peak"))
    for i in troughs:
        pivots.append(WavePivot(int(i), float(prices[i]), "trough"))
    pivots.sort(key=lambda p: p.index)
    # Alternating extrema
    cleaned: list[WavePivot] = []
    for p in pivots:
        if not cleaned:
            cleaned.append(p)
            continue
        if p.kind == cleaned[-1].kind:
            # keep more extreme
            if p.kind == "peak" and p.price >= cleaned[-1].price:
                cleaned[-1] = p
            elif p.kind == "trough" and p.price <= cleaned[-1].price:
                cleaned[-1] = p
        else:
            cleaned.append(p)
    return cleaned


def _fib_near(ratio: float) -> Optional[float]:
    for f in FIB_RATIOS:
        if abs(ratio - f) / f <= FIB_TOL:
            return f
    return None


def detect_elliott_waves(prices: np.ndarray, order: int = 3) -> dict[str, Any]:
    """
    Heuristic Elliott detector:
      Impulse 1-5: 5 alternating legs starting from a trough (bull) or peak (bear)
      Corrective A-C: 3-leg zig-zag after impulse
    Fibonacci: |W3|/|W1| ≈ 1.618, |W2|/|W1| ≈ 0.618, |W4|/|W3| ≈ 0.382–0.618, etc.
    """
    pivots = find_pivots(prices, order=order)
    if len(pivots) < 6:
        return {
            "status": "insufficient_pivots",
            "n_pivots": len(pivots),
            "impulse": [],
            "corrective": [],
            "method": "discretionary_heuristic_fibonacci_constrained",
        }

    # Score candidate 6-pivot windows (5 legs)
    best_impulse = None
    best_score = -1.0
    for start in range(0, len(pivots) - 5):
        window = pivots[start : start + 6]
        legs = []
        ratios = []
        ok_alt = True
        for i in range(5):
            a, b = window[i], window[i + 1]
            if a.kind == b.kind:
                ok_alt = False
                break
            move = b.price - a.price
            legs.append(move)
            if i > 0 and abs(legs[0]) > 1e-12:
                ratios.append(abs(move) / abs(legs[0]))
            else:
                ratios.append(None)
        if not ok_alt:
            continue
        # Bullish impulse: starts trough, W1 up, W2 down, W3 up, W4 down, W5 up
        bull = window[0].kind == "trough" and legs[0] > 0 and legs[2] > 0 and legs[4] > 0
        bear = window[0].kind == "peak" and legs[0] < 0 and legs[2] < 0 and legs[4] < 0
        if not (bull or bear):
            continue
        # Classic rules (soft): W3 not shortest; W2 not beyond W1 start (soft fib)
        abs_legs = [abs(x) for x in legs]
        if abs_legs[2] < min(abs_legs[0], abs_legs[4]):
            continue  # wave 3 shouldn't be shortest among 1,3,5
        score = 0.0
        labels: list[ElliottLabel] = []
        fib_targets = {1: None, 2: 0.618, 3: 1.618, 4: 0.618, 5: 1.0}
        for i in range(5):
            ratio = None
            if i == 0:
                fib_m = None
            elif i in (1, 3) and abs(legs[0]) > 1e-12:
                ratio = abs(legs[i]) / abs(legs[0])
                fib_m = _fib_near(ratio)
                if fib_m:
                    score += 2.0
                elif abs(ratio - fib_targets[i + 1]) / fib_targets[i + 1] < 0.25:
                    score += 1.0
            elif i == 2 and abs(legs[0]) > 1e-12:
                ratio = abs(legs[i]) / abs(legs[0])
                fib_m = _fib_near(ratio)
                if fib_m in (1.618, 2.618, 1.0):
                    score += 3.0
                elif ratio >= 1.0:
                    score += 1.5
            else:
                ratio = abs(legs[i]) / abs(legs[0]) if abs(legs[0]) > 1e-12 else None
                fib_m = _fib_near(ratio) if ratio is not None else None
                if fib_m:
                    score += 1.0
            labels.append(
                ElliottLabel(
                    label=str(i + 1),
                    start_idx=window[i].index,
                    end_idx=window[i + 1].index,
                    start_price=window[i].price,
                    end_price=window[i + 1].price,
                    fib_ratio_vs_prev=None if ratio is None else round(float(ratio), 4),
                    fib_match=fib_m,
                )
            )
        direction = "bullish" if bull else "bearish"
        if score > best_score:
            best_score = score
            best_impulse = {
                "direction": direction,
                "score": round(score, 3),
                "waves": [asdict(w) for w in labels],
                "pivot_indices": [p.index for p in window],
            }

    # Corrective A-B-C after impulse end
    corrective = []
    if best_impulse and len(pivots) >= 4:
        end_idx = best_impulse["pivot_indices"][-1]
        # find pivot position
        pos = next(i for i, p in enumerate(pivots) if p.index == end_idx)
        if pos + 3 < len(pivots):
            w = pivots[pos : pos + 4]
            moves = [w[i + 1].price - w[i].price for i in range(3)]
            # Zigzag: A and C same direction, B opposite
            if moves[0] * moves[2] > 0 and moves[0] * moves[1] < 0:
                a_len = abs(moves[0])
                c_ratio = abs(moves[2]) / a_len if a_len > 1e-12 else None
                b_ratio = abs(moves[1]) / a_len if a_len > 1e-12 else None
                corrective = [
                    {
                        "label": lab,
                        "start_idx": w[i].index,
                        "end_idx": w[i + 1].index,
                        "start_price": w[i].price,
                        "end_price": w[i + 1].price,
                        "fib_ratio_vs_A": round(float(r), 4) if r is not None else None,
                        "fib_match": _fib_near(r) if r is not None else None,
                    }
                    for i, lab, r in (
                        (0, "A", 1.0),
                        (1, "B", b_ratio),
                        (2, "C", c_ratio),
                    )
                ]

    return {
        "status": "ok" if best_impulse else "no_impulse_match",
        "n_pivots": len(pivots),
        "impulse": best_impulse,
        "corrective": corrective,
        "fibonacci_ratios": list(FIB_RATIOS),
        "fib_tolerance": FIB_TOL,
        "method": "discretionary_heuristic_fibonacci_constrained",
        "disclaimer": (
            "Elliott wave labeling is a heuristic pattern screen, not a statistically "
            "validated forecasting identity. Use with regime/HMM filters."
        ),
    }


def run_causality_wave(
    panel: Optional[pd.DataFrame] = None,
    *,
    maxlag: int = 14,
    out_path: Path | None = None,
) -> dict[str, Any]:
    logger.info("Causality + Elliott engine start")
    df = panel if panel is not None else load_panel()

    # Prefer long-horizon AIS daily aggregates for Granger (survives 7d retention)
    granger: dict[str, Any]
    try:
        from services.ttf_forecast.granger_causality import granger_ais_aggregates_to_ttf

        granger = granger_ais_aggregates_to_ttf(maxlag=maxlag)
        logger.info("Granger using ais_daily_aggregates n=%s", granger.get("n_obs"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("aggregates Granger fallback to parquet panel: %s", exc)
        granger = granger_ais_to_ttf(df, maxlag=maxlag)

    elliott = detect_elliott_waves(df["price"].to_numpy(), order=3)
    report = {
        "engine": "ttf.causality_wave",
        "n_obs": int(len(df)),
        "granger": granger,
        "elliott": elliott,
    }
    out = out_path or OUT_JSON
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info(
        "Causality/Elliott OK granger_sig=%s elliott=%s → %s",
        granger.get("any_significant_5pct"),
        elliott.get("status"),
        out,
    )
    return report


def main() -> int:
    run_causality_wave()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
