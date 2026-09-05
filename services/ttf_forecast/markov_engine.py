"""
3-State Gaussian Hidden Markov Model for TTF return regimes.

States:
  0 = Low Volatility / Accumulation
  1 = Mean-Reverting Transit
  2 = High Volatility Spike / Supply Shock

Implements Baum–Welch (EM) + Viterbi decoding in pure NumPy
(no hmmlearn dependency — VPS-safe).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from services.ttf_forecast.logging_utils import get_quant_logger

ROOT = Path(__file__).resolve().parents[2]
OUT_JSON = ROOT / "output" / "ttf_markov_regimes.json"
logger = get_quant_logger("ttf.markov")

STATE_NAMES = {
    0: "LowVol_Accumulation",
    1: "MeanReverting_Transit",
    2: "HighVol_SupplyShock",
}
N_STATES = 3


def _logsumexp(a: np.ndarray, axis: Optional[int] = None) -> np.ndarray:
    a = np.asarray(a, dtype=float)
    m = np.max(a, axis=axis, keepdims=True)
    out = m + np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True))
    if axis is not None:
        return np.squeeze(out, axis=axis)
    return out.squeeze()


def _gauss_logpdf(x: np.ndarray, mu: float, var: float) -> np.ndarray:
    var = max(float(var), 1e-10)
    return -0.5 * (np.log(2 * np.pi * var) + (x - mu) ** 2 / var)


class GaussianHMM3:
    """Univariate 3-state Gaussian HMM with row-stochastic transitions."""

    def __init__(self, n_states: int = N_STATES, seed: int = 1001):
        self.n_states = n_states
        self.rng = np.random.default_rng(seed)
        self.pi: np.ndarray = np.ones(n_states) / n_states
        self.A: np.ndarray = np.ones((n_states, n_states)) / n_states
        self.mu: np.ndarray = np.zeros(n_states)
        self.var: np.ndarray = np.ones(n_states) * 0.01
        self.ll_history: list[float] = []

    def _init_from_data(self, x: np.ndarray) -> None:
        q = np.quantile(x, [0.2, 0.5, 0.8])
        # Sort states by increasing |vol| proxy using absolute residuals to median
        abs_dev = np.abs(x - np.median(x))
        # Seed means near quantiles; vars by terciles of |returns|
        order = np.argsort(np.quantile(abs_dev, [0.33, 0.66, 0.9]))
        self.mu = q.copy()
        terc = np.quantile(abs_dev, [0.33, 0.66, 1.0])
        self.var = np.maximum(terc**2, 1e-6)
        # Prefer state 0 = lowest vol, 2 = highest vol
        idx = np.argsort(self.var)
        self.mu = self.mu[idx]
        self.var = self.var[idx]
        # Sticky diagonal prior
        self.A = np.full((self.n_states, self.n_states), 0.1 / (self.n_states - 1))
        np.fill_diagonal(self.A, 0.9)
        self.pi = np.array([0.45, 0.40, 0.15])

    def _emit_log(self, x: np.ndarray) -> np.ndarray:
        T = len(x)
        B = np.zeros((T, self.n_states))
        for k in range(self.n_states):
            B[:, k] = _gauss_logpdf(x, float(self.mu[k]), float(self.var[k]))
        return B

    def _forward_backward(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
        T = len(x)
        K = self.n_states
        logB = self._emit_log(x)
        logA = np.log(np.maximum(self.A, 1e-12))
        log_pi = np.log(np.maximum(self.pi, 1e-12))

        log_alpha = np.zeros((T, K))
        log_alpha[0] = log_pi + logB[0]
        for t in range(1, T):
            for j in range(K):
                log_alpha[t, j] = logB[t, j] + _logsumexp(log_alpha[t - 1] + logA[:, j])

        log_beta = np.zeros((T, K))
        for t in range(T - 2, -1, -1):
            for i in range(K):
                log_beta[t, i] = _logsumexp(logA[i, :] + logB[t + 1] + log_beta[t + 1])

        log_xi_norm = _logsumexp(log_alpha[-1])
        log_gamma = log_alpha + log_beta
        log_gamma -= _logsumexp(log_gamma, axis=1)[:, None]
        gamma = np.exp(log_gamma)

        # ξ_t(i,j)
        xi = np.zeros((T - 1, K, K))
        for t in range(T - 1):
            log_xi = log_alpha[t][:, None] + logA + logB[t + 1][None, :] + log_beta[t + 1][None, :]
            log_xi -= _logsumexp(log_xi)
            xi[t] = np.exp(log_xi)

        return gamma, xi, log_alpha, float(log_xi_norm)

    def fit(self, x: np.ndarray, n_iter: int = 40, tol: float = 1e-5) -> "GaussianHMM3":
        x = np.asarray(x, dtype=float)
        self._init_from_data(x)
        prev_ll = -np.inf
        for it in range(n_iter):
            gamma, xi, _, ll = self._forward_backward(x)
            self.ll_history.append(ll)
            # M-step
            self.pi = gamma[0] / max(gamma[0].sum(), 1e-12)
            self.A = xi.sum(axis=0)
            self.A = self.A / np.maximum(self.A.sum(axis=1, keepdims=True), 1e-12)
            for k in range(self.n_states):
                w = gamma[:, k]
                sw = max(w.sum(), 1e-12)
                self.mu[k] = float(np.sum(w * x) / sw)
                self.var[k] = float(np.sum(w * (x - self.mu[k]) ** 2) / sw)
                self.var[k] = max(self.var[k], 1e-8)
            # Enforce vol ordering: state0 ≤ state1 ≤ state2 by σ
            order = np.argsort(self.var)
            self.mu = self.mu[order]
            self.var = self.var[order]
            self.A = self.A[np.ix_(order, order)]
            self.pi = self.pi[order]
            if abs(ll - prev_ll) < tol:
                logger.info("HMM EM converged iter=%s ll=%.4f", it + 1, ll)
                break
            prev_ll = ll
        return self

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        gamma, _, _, _ = self._forward_backward(np.asarray(x, dtype=float))
        return gamma

    def viterbi(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        T = len(x)
        K = self.n_states
        logB = self._emit_log(x)
        logA = np.log(np.maximum(self.A, 1e-12))
        log_pi = np.log(np.maximum(self.pi, 1e-12))
        delta = np.zeros((T, K))
        psi = np.zeros((T, K), dtype=int)
        delta[0] = log_pi + logB[0]
        for t in range(1, T):
            for j in range(K):
                vals = delta[t - 1] + logA[:, j]
                psi[t, j] = int(np.argmax(vals))
                delta[t, j] = vals[psi[t, j]] + logB[t, j]
        states = np.zeros(T, dtype=int)
        states[-1] = int(np.argmax(delta[-1]))
        for t in range(T - 2, -1, -1):
            states[t] = psi[t + 1, states[t + 1]]
        return states

    def params_report(self) -> dict[str, Any]:
        return {
            "state_names": STATE_NAMES,
            "pi": self.pi.tolist(),
            "transition_matrix_Pij": self.A.tolist(),
            "gaussian_params": {
                str(k): {
                    "name": STATE_NAMES[k],
                    "mu": float(self.mu[k]),
                    "sigma": float(np.sqrt(self.var[k])),
                    "sigma2": float(self.var[k]),
                    "distribution": f"N({self.mu[k]:.6f}, {self.var[k]:.6e})",
                }
                for k in range(self.n_states)
            },
            "loglik_history_tail": self.ll_history[-5:],
        }


def load_returns(parquet: Path | None = None) -> np.ndarray:
    path = parquet or (ROOT / "output" / "ttf_features.parquet")
    df = pd.read_parquet(path)
    s = df["log_return"].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    return s.to_numpy()


def run_markov_engine(
    returns: Optional[np.ndarray] = None,
    *,
    out_path: Path | None = None,
) -> dict[str, Any]:
    logger.info("Markov HMM engine start")
    x = returns if returns is not None else load_returns()
    if len(x) < 60:
        raise ValueError(f"HMM needs ≥60 observations, got {len(x)}")
    model = GaussianHMM3().fit(x, n_iter=50)
    states = model.viterbi(x)
    proba = model.predict_proba(x)
    current = int(states[-1])
    report = {
        "engine": "ttf.markov",
        "n_obs": int(len(x)),
        **model.params_report(),
        "current_state": current,
        "current_state_name": STATE_NAMES[current],
        "current_state_probabilities": {
            STATE_NAMES[k]: float(proba[-1, k]) for k in range(N_STATES)
        },
        "state_occupancy_pct": {
            STATE_NAMES[k]: float(100.0 * np.mean(states == k)) for k in range(N_STATES)
        },
        "path_tail": states[-30:].tolist(),
    }
    out = out_path or OUT_JSON
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info(
        "HMM OK state=%s P=%s σ=%s → %s",
        STATE_NAMES[current],
        {k: round(v, 3) for k, v in report["current_state_probabilities"].items()},
        [round(np.sqrt(v), 5) for v in model.var],
        out,
    )
    return report


def main() -> int:
    run_markov_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
