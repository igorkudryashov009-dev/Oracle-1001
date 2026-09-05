"""Unit tests for TTF spectral / HMM / Granger+Elliott engines."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _synthetic_returns(n: int = 256, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    # Mix of weekly + monthly cycles + noise regimes
    sig = 0.01 * np.sin(2 * np.pi * t / 7) + 0.008 * np.sin(2 * np.pi * t / 30)
    noise = rng.normal(0, 0.012, size=n)
    noise[n // 2 : n // 2 + 20] *= 3.5  # supply shock window
    return sig + noise


def test_spectral_engine_fft_and_butterworth(tmp_path: Path):
    from services.ttf_forecast.spectral_engine import (
        butterworth_lowpass,
        compute_dft_fft,
        detrend_linear,
        extract_dominant_cycles,
        run_spectral_analysis,
    )

    x = _synthetic_returns(200)
    d, trend = detrend_linear(x)
    assert len(d) == len(x)
    assert np.allclose(d + trend, x, atol=1e-9)
    spec = compute_dft_fft(d)
    assert len(spec["power"]) > 10
    cycles = extract_dominant_cycles(spec["freqs_per_day"], spec["periods_days"], spec["power"])
    assert len(cycles) >= 1
    filt = butterworth_lowpass(d, cutoff_period_days=7.0)
    assert len(filt) == len(d)
    assert np.std(filt) <= np.std(d) * 1.05

    # End-to-end with synthetic parquet-like series
    s = pd.Series(x)
    out = tmp_path / "spectral.json"
    report = run_spectral_analysis(s, out_path=out)
    assert out.exists()
    assert report["n_obs"] == 200
    assert "trend_extrapolation" in report


def test_markov_hmm_three_states(tmp_path: Path):
    from services.ttf_forecast.markov_engine import GaussianHMM3, run_markov_engine

    x = _synthetic_returns(300)
    model = GaussianHMM3(seed=42).fit(x, n_iter=25)
    assert model.A.shape == (3, 3)
    assert np.allclose(model.A.sum(axis=1), 1.0, atol=1e-5)
    assert np.all(model.var > 0)
    # Ordered by volatility
    assert model.var[0] <= model.var[1] <= model.var[2] + 1e-9
    states = model.viterbi(x)
    assert set(np.unique(states)).issubset({0, 1, 2})
    proba = model.predict_proba(x)
    assert proba.shape == (len(x), 3)
    assert np.allclose(proba.sum(axis=1), 1.0, atol=1e-4)

    out = tmp_path / "markov.json"
    report = run_markov_engine(x, out_path=out)
    assert out.exists()
    assert report["current_state"] in (0, 1, 2)
    assert "transition_matrix_Pij" in report


def test_granger_and_elliott(tmp_path: Path):
    from services.ttf_forecast.causality_wave import (
        detect_elliott_waves,
        granger_ais_to_ttf,
        run_causality_wave,
    )

    rng = np.random.default_rng(0)
    n = 220
    density = np.cumsum(rng.normal(0, 0.5, size=n)) + 10
    # TTF returns partially driven by lagged density changes
    dens_chg = np.diff(density, prepend=density[0])
    rets = 0.02 * dens_chg + rng.normal(0, 0.01, size=n)
    # Causal lag embedding
    rets[5:] = rets[5:] + 0.15 * dens_chg[:-5]
    price = 40 * np.exp(np.cumsum(rets))
    # Add zig-zag impulse-like swings
    for i, amp in ((40, 3), (55, -2), (70, 4), (90, -1.5), (110, 2.5)):
        price[i : i + 8] += np.linspace(0, amp, 8)

    panel = pd.DataFrame({"price": price, "density": density, "log_return": rets})
    g = granger_ais_to_ttf(panel, maxlag=10)
    assert "lags" in g and len(g["lags"]) == 10

    ell = detect_elliott_waves(price, order=2)
    assert ell["n_pivots"] >= 4
    assert "impulse" in ell

    out = tmp_path / "causality.json"
    report = run_causality_wave(panel, maxlag=8, out_path=out)
    assert out.exists()
    assert report["granger"]["maxlag"] == 8


def test_quant_log_file_written():
    from services.ttf_forecast.logging_utils import LOG_PATH, get_quant_logger

    log = get_quant_logger("ttf.test")
    log.info("unit_test_heartbeat")
    assert LOG_PATH.exists()
    text = LOG_PATH.read_text(encoding="utf-8")
    assert "unit_test_heartbeat" in text
