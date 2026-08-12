"""
Elliott Wave heuristic markup on the DWT-flow index.

THIS IS NOT A STATISTICALLY VALIDATED MODEL.

Every output carries:
  "method_type": "discretionary_heuristic_not_statistically_validated"

Do NOT blend Elliott signals into spectral / normality scores.
Classical impulse (5) + corrective (3) rules + Fibonacci retracement levels
are applied as a discretionary pattern search on local extrema — fragile,
path-dependent, and easy to overfit by eye.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
FEATURES_DIR = ROOT / "features"
OUT_JSON = FEATURES_DIR / "elliott_wave_report.json"

METHOD_TYPE = "discretionary_heuristic_not_statistically_validated"

# Fibonacci retracement ratios (classical textbook set — not estimated from data)
FIB_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786)


def _local_extrema(y: np.ndarray, order: int = 2) -> tuple[np.ndarray, np.ndarray]:
    """Simple turning-point detector without scipy.signal dependency extras."""
    n = len(y)
    peaks, troughs = [], []
    if n < 2 * order + 1:
        return np.array([], dtype=int), np.array([], dtype=int)
    for i in range(order, n - order):
        window = y[i - order : i + order + 1]
        if np.argmax(window) == order and y[i] == np.max(window):
            peaks.append(i)
        if np.argmin(window) == order and y[i] == np.min(window):
            troughs.append(i)
    return np.array(peaks, dtype=int), np.array(troughs, dtype=int)


def _alternating_pivots(y: np.ndarray, dates: pd.DatetimeIndex, order: int = 2) -> list[dict]:
    peaks, troughs = _local_extrema(y, order=order)
    pivots = []
    for i in peaks:
        pivots.append({"i": int(i), "kind": "peak", "value": float(y[i]), "date": str(dates[i].date())})
    for i in troughs:
        pivots.append({"i": int(i), "kind": "trough", "value": float(y[i]), "date": str(dates[i].date())})
    pivots.sort(key=lambda p: p["i"])
    # Enforce alternation
    alt = []
    for p in pivots:
        if not alt:
            alt.append(p)
            continue
        if p["kind"] == alt[-1]["kind"]:
            # keep more extreme
            if p["kind"] == "peak" and p["value"] >= alt[-1]["value"]:
                alt[-1] = p
            elif p["kind"] == "trough" and p["value"] <= alt[-1]["value"]:
                alt[-1] = p
        else:
            alt.append(p)
    return alt


def _fib_retracements(swing_high: float, swing_low: float) -> dict[str, float]:
    span = swing_high - swing_low
    return {f"fib_{lvl}": round(swing_high - lvl * span, 4) for lvl in FIB_LEVELS}


def _score_impulse5(pivots: list[dict]) -> Optional[dict]:
    """
    Look for a 5-point impulse: trough-peak-trough-peak-trough (bullish)
    or peak-trough-peak-trough-peak (bearish).

    Soft classical checks (not optimized / not validated):
      - wave 2 does not retrace beyond start of 1
      - wave 3 not the shortest among 1,3,5
      - wave 4 does not overlap wave 1 price territory (soft)
    """
    if len(pivots) < 5:
        return None

    best = None
    for start in range(0, len(pivots) - 4):
        w = pivots[start : start + 5]
        kinds = [p["kind"] for p in w]
        bullish = kinds == ["trough", "peak", "trough", "peak", "trough"]
        bearish = kinds == ["peak", "trough", "peak", "trough", "peak"]
        if not (bullish or bearish):
            continue

        vals = [p["value"] for p in w]
        if bullish:
            # waves measured as absolute moves
            len1 = vals[1] - vals[0]
            len2 = vals[1] - vals[2]  # retrace
            len3 = vals[3] - vals[2]
            len4 = vals[3] - vals[4]
            len5 = abs(vals[4] - vals[3])  # may extend or fail
            # Prefer upward impulse where end > start
            rules = {
                "wave2_not_beyond_start": vals[2] >= vals[0],
                "wave3_not_shortest": len3 >= min(len1, len5) if len5 > 0 else len3 >= len1,
                "wave4_no_overlap_wave1": vals[4] >= vals[1],  # soft
            }
            direction = "bullish_impulse_5"
            fib = _fib_retracements(max(vals[1], vals[3]), vals[0])
        else:
            len1 = vals[0] - vals[1]
            len3 = vals[2] - vals[3]
            len5 = vals[4] - vals[3] if False else vals[2] - vals[4]
            rules = {
                "wave2_not_beyond_start": vals[2] <= vals[0],
                "wave3_not_shortest": (vals[2] - vals[3]) >= min(vals[0] - vals[1], abs(vals[4] - vals[3])),
                "wave4_no_overlap_wave1": vals[4] <= vals[1],
            }
            direction = "bearish_impulse_5"
            fib = _fib_retracements(vals[0], min(vals[1], vals[3]))

        score = sum(1 for v in rules.values() if v)
        cand = {
            "pattern": direction,
            "rules_passed": score,
            "rules_total": 3,
            "rules": rules,
            "pivots": w,
            "fibonacci_retracements_from_impulse": fib,
            "wave_lengths_proxy": {
                "w1": round(float(abs(vals[1] - vals[0])), 4),
                "w3": round(float(abs(vals[3] - vals[2])), 4),
                "w5": round(float(abs(vals[4] - vals[3])), 4),
            },
        }
        if best is None or cand["rules_passed"] > best["rules_passed"]:
            best = cand
    return best


def _score_corrective3(pivots: list[dict], after_i: int) -> Optional[dict]:
    """ABC-style 3-pivot corrective after an impulse end index."""
    rest = [p for p in pivots if p["i"] >= after_i]
    if len(rest) < 3:
        return None
    w = rest[:3]
    return {
        "pattern": "corrective_abc_candidate",
        "pivots": w,
        "note": "Alternating 3 pivots after impulse — not validated as true Elliott ABC",
    }


def run_elliott_heuristic(
    series: pd.DataFrame,
    *,
    out_json: Optional[Path] = OUT_JSON,
) -> dict[str, Any]:
    """
    series: DataFrame with date + value (from spectral_analysis.load_dwt_flow_series).
    """
    result: dict[str, Any] = {
        "method_type": METHOD_TYPE,
        "run": True,
        "disclaimer": (
            "Discretionary Elliott Wave heuristic. Not statistically validated. "
            "Do not combine with Fourier/normality results into a single score."
        ),
        "separated_from_spectral_scores": True,
    }

    if series is None or series.empty or series["value"].notna().sum() < 15:
        result.update(
            {
                "status": "skipped",
                "reason": "need ≥15 non-NaN points for even a fragile extrema scan",
                "waves": None,
            }
        )
        if out_json is not None:
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    s = series.dropna(subset=["value"]).copy()
    y = s["value"].to_numpy(dtype=float)
    dates = pd.DatetimeIndex(pd.to_datetime(s["date"]))
    pivots = _alternating_pivots(y, dates, order=2)
    impulse = _score_impulse5(pivots)
    corrective = None
    if impulse is not None:
        after = impulse["pivots"][-1]["i"]
        corrective = _score_corrective3(pivots, after_i=after)

    result.update(
        {
            "status": "ok" if impulse is not None else "no_clear_impulse5",
            "n_points": int(len(y)),
            "n_pivots": len(pivots),
            "pivots": pivots[:40],  # cap for JSON size
            "impulse5": impulse,
            "corrective3": corrective,
            "fibonacci_levels_used": list(FIB_LEVELS),
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )

    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        # Ensure method_type is top-level in file
        out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    from spectral_analysis import TIMESERIES_PATH, load_dwt_flow_series

    series = load_dwt_flow_series(TIMESERIES_PATH)
    out = run_elliott_heuristic(series)
    print(json.dumps({k: out[k] for k in ("method_type", "status", "n_points")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
