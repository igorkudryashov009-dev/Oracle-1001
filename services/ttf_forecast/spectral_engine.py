"""
Fourier / spectral analysis engine for TTF log returns.

- DFT / FFT on linearly detrended log returns
- Dominant cycle extraction (7d / 14d / 30d target bands)
- Butterworth low-pass noise filter + dynamic trend extrapolation
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.fft import fft, fftfreq, ifft
from scipy.signal import butter, filtfilt

from services.ttf_forecast.logging_utils import get_quant_logger

ROOT = Path(__file__).resolve().parents[2]
OUT_JSON = ROOT / "output" / "ttf_spectral_report.json"
logger = get_quant_logger("ttf.spectral")

TARGET_CYCLES_DAYS = (7.0, 14.0, 30.0)
CYCLE_LABELS = {
    7.0: "weekly_logistics",
    14.0: "delivery_window",
    30.0: "monthly_futures_expiry",
}


@dataclass
class DominantCycle:
    period_days: float
    frequency_hz: float
    power: float
    label: str
    band_match: Optional[str] = None


def load_ttf_log_returns(parquet: Path | None = None) -> pd.Series:
    path = parquet or (ROOT / "output" / "ttf_features.parquet")
    if not path.exists():
        raise FileNotFoundError(f"Missing features parquet: {path}")
    df = pd.read_parquet(path)
    if "log_return" in df.columns:
        s = df["log_return"].astype(float)
    else:
        s = np.log(df["price_ttf_eur_mwh"].astype(float)).diff()
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    if len(s) < 32:
        raise ValueError(f"Need ≥32 log-return points, got {len(s)}")
    return s.reset_index(drop=True)


def detrend_linear(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Remove linear trend; return (detrended, trend)."""
    n = len(x)
    t = np.arange(n, dtype=float)
    coef = np.polyfit(t, x, 1)
    trend = np.polyval(coef, t)
    return x - trend, trend


def compute_dft_fft(x: np.ndarray, sample_spacing_days: float = 1.0) -> dict[str, Any]:
    """
    Discrete Fourier Transform via FFT.
    Returns two-sided spectrum metadata + positive-frequency power.
    """
    n = len(x)
    # Classical DFT definition (unitary-ish amplitude scale for reporting)
    X_dft = np.array(
        [np.sum(x * np.exp(-2j * np.pi * k * np.arange(n) / n)) for k in range(min(n, 64))],
        dtype=complex,
    )  # truncated DFT for audit; full spectrum via FFT
    X = fft(x)
    freqs = fftfreq(n, d=sample_spacing_days)
    # Positive frequencies only for power ranking
    pos = freqs > 0
    power = (np.abs(X[pos]) ** 2) / n
    f_pos = freqs[pos]
    periods = np.where(f_pos > 0, 1.0 / f_pos, np.inf)
    return {
        "n": n,
        "freqs_per_day": f_pos,
        "periods_days": periods,
        "power": power,
        "fft": X,
        "dft_head": X_dft,
        "freqs_full": freqs,
    }


def extract_dominant_cycles(
    freqs: np.ndarray,
    periods: np.ndarray,
    power: np.ndarray,
    *,
    top_k: int = 8,
    target_cycles: tuple[float, ...] = TARGET_CYCLES_DAYS,
    tolerance: float = 0.25,
) -> list[DominantCycle]:
    order = np.argsort(power)[::-1]
    cycles: list[DominantCycle] = []
    for idx in order[:top_k]:
        p = float(periods[idx])
        if not np.isfinite(p) or p > 180:
            continue
        band = None
        for t in target_cycles:
            if abs(p - t) / t <= tolerance:
                band = CYCLE_LABELS.get(t, f"{t}d")
                break
        cycles.append(
            DominantCycle(
                period_days=round(p, 3),
                frequency_hz=float(freqs[idx]),
                power=float(power[idx]),
                label=band or f"cycle_{p:.1f}d",
                band_match=band,
            )
        )
    # Ensure target bands appear with nearest power even if not top-k
    for t in target_cycles:
        if any(c.band_match == CYCLE_LABELS.get(t) for c in cycles):
            continue
        rel = np.abs(periods - t) / t
        j = int(np.nanargmin(rel))
        if rel[j] <= tolerance * 2:
            cycles.append(
                DominantCycle(
                    period_days=round(float(periods[j]), 3),
                    frequency_hz=float(freqs[j]),
                    power=float(power[j]),
                    label=CYCLE_LABELS.get(t, f"{t}d"),
                    band_match=CYCLE_LABELS.get(t),
                )
            )
    cycles.sort(key=lambda c: -c.power)
    return cycles


def butterworth_lowpass(
    x: np.ndarray,
    cutoff_period_days: float = 7.0,
    order: int = 3,
    sample_spacing_days: float = 1.0,
) -> np.ndarray:
    """Butterworth low-pass: keep components slower than cutoff_period_days."""
    fs = 1.0 / sample_spacing_days
    cutoff_hz = 1.0 / cutoff_period_days
    nyq = 0.5 * fs
    wn = min(0.99, max(1e-6, cutoff_hz / nyq))
    b, a = butter(order, wn, btype="low", analog=False)
    # filtfilt needs adequate length
    if len(x) < max(15, 3 * order):
        return x.copy()
    return filtfilt(b, a, x)


def extrapolate_trend(
    filtered: np.ndarray,
    horizon_days: int = 14,
    poly_degree: int = 2,
) -> dict[str, Any]:
    """Dynamic trend profile: fit local polynomial on filtered series, extrapolate."""
    n = len(filtered)
    t = np.arange(n, dtype=float)
    deg = min(poly_degree, max(1, n // 20))
    coef = np.polyfit(t[-min(90, n) :], filtered[-min(90, n) :], deg)
    t_future = np.arange(n, n + horizon_days, dtype=float)
    future = np.polyval(coef, t_future)
    # Reconstruct in-sample fitted curve
    fitted = np.polyval(coef, t)
    return {
        "horizon_days": horizon_days,
        "poly_degree": deg,
        "coefficients": coef.tolist(),
        "fitted_tail": fitted[-14:].tolist(),
        "extrapolation": future.tolist(),
        "extrapolation_timestamps_offset": list(range(1, horizon_days + 1)),
    }


def run_spectral_analysis(
    returns: Optional[pd.Series] = None,
    *,
    cutoff_period_days: float = 7.0,
    horizon_days: int = 14,
    out_path: Path | None = None,
) -> dict[str, Any]:
    logger.info("Spectral engine start")
    s = returns if returns is not None else load_ttf_log_returns()
    x = s.to_numpy(dtype=float)
    detrended, linear_trend = detrend_linear(x)
    spec = compute_dft_fft(detrended)
    cycles = extract_dominant_cycles(spec["freqs_per_day"], spec["periods_days"], spec["power"])
    filtered = butterworth_lowpass(detrended, cutoff_period_days=cutoff_period_days)
    # Trend in price-return space = linear + filtered low-frequency
    smooth = linear_trend + filtered
    extrap = extrapolate_trend(smooth, horizon_days=horizon_days)

    # Band power diagnostics for 7/14/30d
    band_power = {}
    for t in TARGET_CYCLES_DAYS:
        mask = np.abs(spec["periods_days"] - t) / t <= 0.25
        band_power[CYCLE_LABELS[t]] = float(np.sum(spec["power"][mask])) if np.any(mask) else 0.0

    report = {
        "engine": "ttf.spectral",
        "n_obs": int(len(x)),
        "dominant_cycles": [asdict(c) for c in cycles[:10]],
        "target_band_power": band_power,
        "butterworth": {
            "cutoff_period_days": cutoff_period_days,
            "order": 3,
            "filtered_std": float(np.std(filtered)),
            "raw_std": float(np.std(detrended)),
            "noise_reduction_ratio": float(1.0 - np.std(filtered) / max(np.std(detrended), 1e-12)),
        },
        "trend_extrapolation": extrap,
        "ifft_energy_check": float(np.mean(np.abs(ifft(spec["fft"]) - detrended))),
    }
    out = out_path or OUT_JSON
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info(
        "Spectral OK n=%s top_cycle=%.2fd band_power=%s → %s",
        report["n_obs"],
        cycles[0].period_days if cycles else float("nan"),
        band_power,
        out,
    )
    return report


def main() -> int:
    run_spectral_analysis()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
