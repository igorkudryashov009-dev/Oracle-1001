"""
DWT-flow forecast ensemble — combines Prompts 1–4 signals with honest CIs.

Models (each has an explicit minimum-N gate — undocumented magic numbers forbidden):
  - naive_seasonal: y_hat(t+h) = y(t)   [or y(t+h-season) when season known]
      MIN_N = horizon + 1
  - arima_sarima: statsmodels ARIMA/SARIMA using Fourier-suggested season
      MIN_N = 40  # unstable below ~40 daily points for even modest ARIMA
  - gbm: LightGBM if installed, else sklearn HistGradientBoosting
      MIN_N = 60  # tabular boosters overfit / variance-explode on tiny panels
  - lstm: OPTIONAL — only if torch available AND n >= 300
      MIN_N = 300  # deep nets on < few hundred points almost always overfit

Walk-forward = expanding window (NO random K-fold — would leak future).
Metrics: MAE, MAPE, and explicit beat-baseline flag per model.
Prediction intervals: residual bootstrap (NOT Gaussian) — Prompt 3 showed
increments are typically non-normal.

Outputs:
  features/forecast_ensemble_report.json
  features/forecast_ensemble_series.parquet
  output/forecast_dashboard.html
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent
FEATURES_DIR = ROOT / "features"
OUTPUT_DIR = ROOT / "output"
TIMESERIES_PATH = FEATURES_DIR / "dwt_flow_timeseries.parquet"
MARKOV_PATH = FEATURES_DIR / "markov_transition_matrix.json"
SPECTRAL_PATH = FEATURES_DIR / "spectral_report.json"
ELLIOTT_PATH = FEATURES_DIR / "elliott_wave_report.json"
CAUSAL_PATH = FEATURES_DIR / "causal_report.json"
CONFIG_PATH = ROOT / "config.yaml"
OUT_REPORT = FEATURES_DIR / "forecast_ensemble_report.json"
OUT_SERIES = FEATURES_DIR / "forecast_ensemble_series.parquet"
OUT_DASHBOARD = OUTPUT_DIR / "forecast_dashboard.html"

# ---------------------------------------------------------------------------
# Explicit sample-size gates (documented thresholds — not silent magic)
# ---------------------------------------------------------------------------
# Naive needs the last observed point plus room for the horizon step.
MIN_N_NAIVE = "horizon + 1"  # resolved at runtime as horizon + 1
# ARIMA/SARIMA: with daily data, <~40 points → unreliable AR/MA + seasonality.
MIN_N_ARIMA = 40
# Gradient boosting on tabular lags: <60 rows → high variance / overfit risk.
MIN_N_GBM = 60
# LSTM: deep sequence models need hundreds of points; below this we refuse.
MIN_N_LSTM = 300

DEFAULT_HORIZON = 7
DEFAULT_LAGS = (1, 2, 3, 7, 14)
WF_MIN_TRAIN = 30  # first walk-forward origin needs at least this many train points
BOOTSTRAP_B = 500
CI_LO, CI_HI = 0.10, 0.90  # 80% central interval via residual bootstrap
MAPE_EPS = 1e-6


def _min_naive(horizon: int) -> int:
    return int(horizon) + 1


# ---------------------------------------------------------------------------
# Data / feature construction
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_dwt_series(
    path: Path = TIMESERIES_PATH,
    value_col: str = "laden_dwt_sum",
) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["date", "y", "coverage_pct_vessels"])
    df = pd.read_parquet(path)
    if df.empty:
        return pd.DataFrame(columns=["date", "y", "coverage_pct_vessels"])
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    g = (
        df.dropna(subset=["date"])
        .groupby("date", as_index=False)
        .agg(y=(value_col, "sum"), coverage_pct_vessels=("coverage_pct_vessels", "mean"))
        .sort_values("date")
    )
    # continuous calendar
    full = pd.date_range(g["date"].min(), g["date"].max(), freq="D")
    g = g.set_index("date").reindex(full).rename_axis("date").reset_index()
    g["y"] = g["y"].interpolate(limit_direction="both")
    g["coverage_pct_vessels"] = g["coverage_pct_vessels"].ffill().bfill()
    return g


def _load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def extract_fourier_periods(spectral: Optional[dict], default: tuple[float, ...] = (7.0,)) -> list[float]:
    """Use reliable spectral peaks; fall back to weekly seasonality."""
    periods: list[float] = []
    if spectral:
        for a in spectral.get("analyses") or []:
            for t in (a.get("spectrum") or {}).get("top_frequencies") or []:
                if t.get("claim_reliable") and t.get("period_days"):
                    p = float(t["period_days"])
                    if 2.5 <= p <= 120:
                        periods.append(p)
    if not periods:
        periods = list(default)
    # unique rounded
    uniq = sorted({round(p, 1) for p in periods})[:3]
    return uniq


def extract_markov_laden_prob(markov: Optional[dict]) -> Optional[float]:
    """Fleet-level expected laden share proxy from latest MC forecast if present."""
    if not markov:
        return None
    fc = markov.get("forecast") or {}
    if not fc.get("valid"):
        return None
    h = (fc.get("horizons") or {}).get("T+7") or {}
    v = h.get("laden_dwt_pct_mean")
    return None if v is None else float(v) / 100.0


def extract_elliott_flag(elliott: Optional[dict]) -> int:
    if not elliott:
        return 0
    if elliott.get("method_type") != "discretionary_heuristic_not_statistically_validated":
        # still treat as optional flag if present
        pass
    if elliott.get("status") == "ok" and elliott.get("impulse5"):
        return 1
    return 0


def extract_causal_flags(causal: Optional[dict]) -> dict[str, int]:
    """Binary: whether any adjusted-significant Granger direction survived."""
    flags = {"causal_ttf_any": 0, "causal_brent_any": 0}
    if not causal:
        return flags
    for pair in causal.get("pairs") or []:
        if pair.get("status") != "ok":
            continue
        g = pair.get("granger") or {}
        any_sig = False
        for d in (g.get("directions") or {}).values():
            if d.get("any_significant_adjusted"):
                any_sig = True
        name = str(pair.get("price_series") or pair.get("pair") or "")
        if "ttf" in name:
            flags["causal_ttf_any"] = int(any_sig)
        if "brent" in name:
            flags["causal_brent_any"] = int(any_sig)
    return flags


def build_feature_frame(
    series: pd.DataFrame,
    *,
    lags: tuple[int, ...] = DEFAULT_LAGS,
    fourier_periods: list[float] | None = None,
    markov_laden_p: Optional[float] = None,
    elliott_flag: int = 0,
    causal_flags: Optional[dict[str, int]] = None,
) -> pd.DataFrame:
    """Tabular features for GBM; also carries target y."""
    df = series.copy()
    fourier_periods = fourier_periods or [7.0]
    causal_flags = causal_flags or {}

    for L in lags:
        df[f"lag_{L}"] = df["y"].shift(L)

    # rolling stats
    df["roll_mean_7"] = df["y"].shift(1).rolling(7, min_periods=3).mean()
    df["roll_std_7"] = df["y"].shift(1).rolling(7, min_periods=3).std()

    t = np.arange(len(df), dtype=float)
    for p in fourier_periods:
        w = 2 * np.pi / p
        df[f"fourier_sin_{p}"] = np.sin(w * t)
        df[f"fourier_cos_{p}"] = np.cos(w * t)

    # Markov laden probability as a level feature (constant from latest report —
    # still useful as importance probe / regime marker; not a cheat of future y)
    df["markov_laden_p"] = float(markov_laden_p) if markov_laden_p is not None else np.nan
    df["elliott_flag"] = int(elliott_flag)  # discretionary — optional column only
    df["causal_ttf_any"] = int(causal_flags.get("causal_ttf_any", 0))
    df["causal_brent_any"] = int(causal_flags.get("causal_brent_any", 0))

    return df


def feature_columns(df: pd.DataFrame) -> list[str]:
    skip = {"date", "y", "coverage_pct_vessels"}
    return [c for c in df.columns if c not in skip]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs(y_true - y_pred)))


def mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    denom = np.maximum(np.abs(y_true), MAPE_EPS)
    return float(np.mean(np.abs((y_true - y_pred) / denom)) * 100.0)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def forecast_naive(y: np.ndarray, horizon: int, season: Optional[int] = None) -> np.ndarray:
    """Seasonal naive if season fits, else last-value persistence."""
    y = np.asarray(y, dtype=float)
    out = np.empty(horizon, dtype=float)
    if season and len(y) >= season:
        for h in range(1, horizon + 1):
            out[h - 1] = y[-season + ((h - 1) % season)]
    else:
        out[:] = y[-1]
    return out


def forecast_arima(
    y: np.ndarray,
    horizon: int,
    season: Optional[int] = None,
) -> tuple[Optional[np.ndarray], Optional[str]]:
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    y = np.asarray(y, dtype=float)
    if len(y) < MIN_N_ARIMA:
        return None, f"refused: n={len(y)} < MIN_N_ARIMA={MIN_N_ARIMA}"

    # Prefer short seasonal period if Fourier suggests weekly-ish
    seasonal = season if season and 4 <= season <= 14 and len(y) >= max(MIN_N_ARIMA, 2 * season) else None
    order = (1, 1, 1)
    seasonal_order = (1, 0, 1, seasonal) if seasonal else (0, 0, 0, 0)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            model = SARIMAX(
                y,
                order=order,
                seasonal_order=seasonal_order,
                enforce_stationarity=False,
                enforce_invertibility=False,
            )
            fit = model.fit(disp=False)
            fc = fit.forecast(horizon)
            return np.asarray(fc, dtype=float), None
        except Exception as exc:  # noqa: BLE001
            # fallback non-seasonal
            try:
                model = SARIMAX(y, order=order, enforce_stationarity=False, enforce_invertibility=False)
                fit = model.fit(disp=False)
                fc = fit.forecast(horizon)
                return np.asarray(fc, dtype=float), f"seasonal failed ({exc}); used ARIMA{order}"
            except Exception as exc2:  # noqa: BLE001
                return None, f"ARIMA failed: {exc2}"


def _get_gbm():
    try:
        import lightgbm as lgb

        def fit(X_train, y_train):
            model = lgb.LGBMRegressor(
                n_estimators=120,
                learning_rate=0.05,
                max_depth=4,
                subsample=0.9,
                colsample_bytree=0.9,
                random_state=42,
                verbosity=-1,
            )
            model.fit(X_train, y_train)
            importance = dict(zip(X_train.columns, model.feature_importances_.tolist()))
            return model, importance, "lightgbm"

        def predict(model, X_pred):
            return np.asarray(model.predict(X_pred), dtype=float)

        return fit, predict, "lightgbm"
    except Exception:
        from sklearn.ensemble import HistGradientBoostingRegressor

        def fit(X_train, y_train):
            model = HistGradientBoostingRegressor(
                max_depth=4,
                learning_rate=0.05,
                max_iter=150,
                random_state=42,
            )
            model.fit(X_train, y_train)
            importance = {c: 0.0 for c in X_train.columns}
            for c in X_train.columns:
                try:
                    importance[c] = float(abs(np.corrcoef(X_train[c], y_train)[0, 1]))
                except Exception:
                    importance[c] = 0.0
            return model, importance, "sklearn_hist_gradient_boosting"

        def predict(model, X_pred):
            return np.asarray(model.predict(X_pred), dtype=float)

        return fit, predict, "sklearn_hist_gradient_boosting"


def forecast_gbm_one_step_path(
    feat: pd.DataFrame,
    train_end_idx: int,
    horizon: int,
) -> tuple[Optional[np.ndarray], Optional[dict], Optional[str], Optional[str]]:
    """Fit GBM once on train; recursive multi-step via lag roll-forward."""
    if train_end_idx + 1 < MIN_N_GBM:
        return None, None, None, f"refused: train_n={train_end_idx + 1} < MIN_N_GBM={MIN_N_GBM}"

    cols = feature_columns(feat)
    train = feat.iloc[: train_end_idx + 1].dropna(subset=cols + ["y"])
    if len(train) < MIN_N_GBM:
        return None, None, None, f"refused: clean train_n={len(train)} < MIN_N_GBM={MIN_N_GBM}"

    fit_fn, predict_fn, backend = _get_gbm()
    X_train = train[cols]
    y_train = train["y"].to_numpy(dtype=float)
    try:
        model, importance, backend = fit_fn(X_train, y_train)
    except Exception as exc:  # noqa: BLE001
        return None, None, backend, f"GBM fit failed: {exc}"

    work = feat.iloc[: train_end_idx + 1].copy()
    preds: list[float] = []
    try:
        for _h in range(horizon):
            tmp = work.copy()
            for L in DEFAULT_LAGS:
                if f"lag_{L}" in tmp.columns:
                    tmp[f"lag_{L}"] = tmp["y"].shift(L)
            tmp["roll_mean_7"] = tmp["y"].shift(1).rolling(7, min_periods=3).mean()
            tmp["roll_std_7"] = tmp["y"].shift(1).rolling(7, min_periods=3).std()
            row = tmp.iloc[[-1]][cols].copy().fillna(X_train.median(numeric_only=True))
            yhat = float(predict_fn(model, row).ravel()[0])
            preds.append(yhat)

            next_date = work["date"].iloc[-1] + pd.Timedelta(days=1)
            new_row = {c: np.nan for c in work.columns}
            new_row["date"] = next_date
            new_row["y"] = yhat
            for c in ("markov_laden_p", "elliott_flag", "causal_ttf_any", "causal_brent_any"):
                if c in work.columns:
                    new_row[c] = work[c].iloc[-1]
            t_next = len(work)
            for c in cols:
                if c.startswith("fourier_sin_"):
                    p = float(c.replace("fourier_sin_", ""))
                    new_row[c] = math.sin(2 * math.pi * t_next / p)
                elif c.startswith("fourier_cos_"):
                    p = float(c.replace("fourier_cos_", ""))
                    new_row[c] = math.cos(2 * math.pi * t_next / p)
            work = pd.concat([work, pd.DataFrame([new_row])], ignore_index=True)
        return np.asarray(preds, dtype=float), importance, backend, None
    except Exception as exc:  # noqa: BLE001
        return None, importance, backend, f"GBM forecast failed: {exc}"


def forecast_lstm(
    y: np.ndarray,
    horizon: int,
    lookback: int = 14,
) -> tuple[Optional[np.ndarray], Optional[str]]:
    """Optional tiny LSTM — refused unless torch present and n >= MIN_N_LSTM."""
    try:
        import torch
        from torch import nn
    except Exception:
        return None, f"refused: torch not installed (MIN_N_LSTM={MIN_N_LSTM} also required)"

    y = np.asarray(y, dtype=float)
    if len(y) < MIN_N_LSTM:
        return None, f"refused: n={len(y)} < MIN_N_LSTM={MIN_N_LSTM} (deep net overfit risk)"

    # Minimal 1-layer LSTM — intentionally small; still easy to overfit
    device = torch.device("cpu")
    series = torch.tensor(y, dtype=torch.float32)
    mu, sigma = series.mean(), series.std().clamp_min(1e-6)
    z = (series - mu) / sigma

    xs, ys = [], []
    for i in range(lookback, len(z)):
        xs.append(z[i - lookback : i])
        ys.append(z[i])
    X = torch.stack(xs).unsqueeze(-1).to(device)
    Y = torch.stack(ys).to(device)

    class TinyLSTM(nn.Module):
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(1, 16, batch_first=True)
            self.fc = nn.Linear(16, 1)

        def forward(self, x):
            out, _ = self.lstm(x)
            return self.fc(out[:, -1, :]).squeeze(-1)

    model = TinyLSTM().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-2)
    loss_fn = nn.MSELoss()
    model.train()
    for _ in range(40):
        opt.zero_grad()
        pred = model(X)
        loss = loss_fn(pred, Y)
        loss.backward()
        opt.step()

    model.eval()
    window = z[-lookback:].tolist()
    preds_z = []
    with torch.no_grad():
        for _ in range(horizon):
            inp = torch.tensor(window[-lookback:], dtype=torch.float32).view(1, lookback, 1)
            p = float(model(inp).item())
            preds_z.append(p)
            window.append(p)
    preds = (np.asarray(preds_z) * float(sigma) + float(mu))
    return preds, None


# ---------------------------------------------------------------------------
# Walk-forward + bootstrap CI
# ---------------------------------------------------------------------------

def walk_forward_validate(
    feat: pd.DataFrame,
    *,
    horizon: int,
    season: Optional[int],
    fourier_periods: list[float],
) -> dict[str, Any]:
    """Expanding-window walk-forward. Compares each model to naive baseline."""
    n = len(feat)
    y_all = feat["y"].to_numpy(dtype=float)
    origins = list(range(max(WF_MIN_TRAIN, _min_naive(horizon), MIN_N_ARIMA // 2), n - horizon))
    # subsample origins if many (keep last ~12 folds for speed)
    if len(origins) > 12:
        origins = origins[-12:]

    models = ["naive", "arima", "gbm"]
    # LSTM only attempted if global n large enough
    try_lstm = n >= MIN_N_LSTM
    if try_lstm:
        models.append("lstm")

    store: dict[str, list] = {m: [] for m in models}
    actuals: list[np.ndarray] = []

    last_importance = None
    gbm_backend = None
    skip_reasons: dict[str, str] = {}

    for origin in origins:
        y_train = y_all[: origin + 1]
        y_true = y_all[origin + 1 : origin + 1 + horizon]
        actuals.append(y_true)

        # naive
        if len(y_train) >= _min_naive(horizon):
            store["naive"].append(forecast_naive(y_train, horizon, season=season))
        else:
            store["naive"].append(np.full(horizon, np.nan))
            skip_reasons["naive"] = f"n<{_min_naive(horizon)}"

        # arima
        fc, reason = forecast_arima(y_train, horizon, season=season)
        if fc is None:
            store["arima"].append(np.full(horizon, np.nan))
            skip_reasons["arima"] = reason or "failed"
        else:
            store["arima"].append(fc)

        # gbm
        fc_g, imp, backend, reason_g = forecast_gbm_one_step_path(feat, origin, horizon)
        gbm_backend = backend or gbm_backend
        if fc_g is None:
            store["gbm"].append(np.full(horizon, np.nan))
            skip_reasons["gbm"] = reason_g or "failed"
        else:
            store["gbm"].append(fc_g)
            last_importance = imp

        if try_lstm:
            fc_l, reason_l = forecast_lstm(y_train, horizon)
            if fc_l is None:
                store["lstm"].append(np.full(horizon, np.nan))
                skip_reasons["lstm"] = reason_l or "failed"
            else:
                store["lstm"].append(fc_l)

    def _score(preds_list: list[np.ndarray]) -> dict[str, Any]:
        if not preds_list or not actuals:
            return {"n_folds": 0, "mae": None, "mape": None, "beats_baseline": None}
        P = np.vstack(preds_list)
        A = np.vstack(actuals)
        mask = np.isfinite(P).all(axis=1) & np.isfinite(A).all(axis=1)
        if mask.sum() == 0:
            return {"n_folds": 0, "mae": None, "mape": None, "beats_baseline": None}
        P, A = P[mask], A[mask]
        return {
            "n_folds": int(mask.sum()),
            "mae": round(mae(A.ravel(), P.ravel()), 4),
            "mape": round(mape(A.ravel(), P.ravel()), 4),
        }

    baseline = _score(store["naive"])
    results = {}
    for m in models:
        sc = _score(store[m])
        if sc["mae"] is None or baseline["mae"] is None:
            sc["beats_baseline"] = None
            sc["mae_vs_baseline"] = None
        else:
            sc["beats_baseline"] = bool(sc["mae"] < baseline["mae"])
            sc["mae_vs_baseline"] = round(sc["mae"] - baseline["mae"], 4)
            sc["baseline_mae"] = baseline["mae"]
        if m in skip_reasons and sc["n_folds"] == 0:
            sc["refused_reason"] = skip_reasons[m]
        results[m] = sc

    return {
        "n_origins": len(origins),
        "horizon": horizon,
        "baseline_model": "naive",
        "models": results,
        "gbm_backend": gbm_backend,
        "feature_importance_last_fold": last_importance,
        "note": (
            "Walk-forward expanding window only. Random K-fold is intentionally "
            "not used (would leak future into past)."
        ),
    }


def residual_bootstrap_intervals(
    y_hist: np.ndarray,
    point_forecast: np.ndarray,
    *,
    in_sample_fitted: np.ndarray,
    B: int = BOOTSTRAP_B,
    seed: int = 42,
) -> dict[str, list[float]]:
    """Prediction intervals from residual bootstrap — no Gaussian assumption."""
    y_hist = np.asarray(y_hist, dtype=float)
    fitted = np.asarray(in_sample_fitted, dtype=float)
    m = min(len(y_hist), len(fitted))
    resid = y_hist[-m:] - fitted[-m:]
    resid = resid[np.isfinite(resid)]
    if len(resid) < 5:
        resid = np.diff(y_hist)
        resid = resid[np.isfinite(resid)]
    if len(resid) < 3:
        # last resort: empirical scale from y
        resid = y_hist - np.nanmean(y_hist)

    rng = np.random.default_rng(seed)
    H = len(point_forecast)
    sims = np.empty((B, H), dtype=float)
    for b in range(B):
        noise = rng.choice(resid, size=H, replace=True)
        # path-wise: accumulate shock around the point path (additive)
        sims[b] = point_forecast + noise

    lo = np.quantile(sims, CI_LO, axis=0)
    hi = np.quantile(sims, CI_HI, axis=0)
    return {
        "ci_low": [round(float(x), 4) for x in lo],
        "ci_high": [round(float(x), 4) for x in hi],
        "ci_level": f"{int((CI_HI - CI_LO) * 100)}% residual-bootstrap",
        "bootstrap_B": B,
        "distributional_assumption": "none (empirical residual resampling)",
    }


def _in_sample_naive_fitted(y: np.ndarray, season: Optional[int]) -> np.ndarray:
    fitted = np.full_like(y, np.nan, dtype=float)
    if season and season < len(y):
        for i in range(season, len(y)):
            fitted[i] = y[i - season]
    else:
        fitted[1:] = y[:-1]
    # fill first points
    fitted = np.where(np.isfinite(fitted), fitted, y)
    return fitted


def select_ensemble_weights(wf: dict[str, Any]) -> dict[str, float]:
    """Inverse-MAE weights among models that produced scores; always include naive."""
    models = wf.get("models") or {}
    usable = {}
    for name, sc in models.items():
        if sc.get("mae") is not None and sc.get("n_folds", 0) > 0:
            usable[name] = float(sc["mae"])
    if not usable:
        return {"naive": 1.0}
    # Prefer models that beat baseline, but never drop naive entirely (weight floor)
    inv = {k: 1.0 / max(v, 1e-9) for k, v in usable.items()}
    for k, sc in models.items():
        if sc.get("beats_baseline") is False and k != "naive":
            inv[k] = inv.get(k, 0) * 0.5  # downweight losers vs baseline
    s = sum(inv.values())
    return {k: round(v / s, 4) for k, v in inv.items()}


# ---------------------------------------------------------------------------
# Dashboard HTML (Apple HIG — consistent with design_system.css)
# ---------------------------------------------------------------------------

def build_dashboard_html(payload: dict[str, Any], out_path: Path = OUT_DASHBOARD) -> Path:
    meta_json = json.dumps(payload, ensure_ascii=False)
    html = _DASH_TEMPLATE.replace("__PAYLOAD__", meta_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


_DASH_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Fleet Intelligence · Forecast</title>
<link rel="stylesheet" href="design_system.css" />
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  .fc-grid { display:grid; grid-template-columns: 2fr 1fr; gap:16px; }
  @media (max-width:1000px){ .fc-grid { grid-template-columns:1fr; } }
  .fi-chart { height:360px; }
  .barline { height:8px; background:var(--bg-elevated-2); border-radius:999px; margin:4px 0 12px; overflow:hidden; }
  .barline > span { display:block; height:100%; background:var(--accent); border-radius:999px; }
  .beat { color: var(--success); font-weight:600; }
  .lose { color: var(--wait); font-weight:600; }
</style>
</head>
<body>
<a class="fi-skip" href="#main">Skip to content</a>
<header class="fi-nav no-print">
  <a class="fi-brand" href="mission_control.html"><span class="fi-brand-mark">FI</span> Fleet Intelligence</a>
  <div class="fi-nav-links">
    <a class="fi-pill" href="mission_control.html">Mission Control</a>
    <a class="fi-pill" href="dashboard.html">Fleet</a>
    <a class="fi-pill" href="history_dashboard.html">AIS Archive</a>
    <a class="fi-pill active" href="forecast_dashboard.html">Forecast</a>
    <button type="button" class="fi-theme-toggle" id="theme-toggle" aria-label="Toggle color theme">◐</button>
  </div>
</header>
<div class="fi-page" id="main">
  <div class="fi-skeleton-wrap"><div class="fi-skeleton" style="height:72px;margin-bottom:16px"></div><div class="fi-grid"><div class="fi-skeleton"></div><div class="fi-skeleton"></div><div class="fi-skeleton"></div><div class="fi-skeleton"></div></div></div>
  <div class="fi-ready-wrap">
    <h1 class="fi-title">Forecast Ensemble</h1>
    <p class="fi-muted" style="margin-top:8px">Fact + forecast with residual-bootstrap intervals — never a lone magic number</p>
    <div id="banner" class="fi-banner wait" style="margin-top:16px"></div>
    <div class="fi-grid" id="metrics" style="margin:16px 0"></div>
    <div class="fc-grid">
      <section class="fi-card">
        <div class="fi-card-title">Fact · ensemble forecast · CI band</div>
        <div class="fi-chart"><canvas id="chart"></canvas></div>
      </section>
      <aside class="fi-card">
        <div class="fi-card-title">Feature importance / signal sources</div>
        <div id="importance"></div>
        <div class="fi-card-title" style="margin-top:16px">Walk-forward vs baseline</div>
        <div id="wf"></div>
      </aside>
    </div>
    <div class="fi-card" style="margin-top:16px">
      <div class="fi-card-title">Model gates &amp; honest limits</div>
      <div id="gates"></div>
    </div>
  </div>
</div>
<script>
const P = __PAYLOAD__;
(function themeInit(){
  const root = document.documentElement;
  const saved = localStorage.getItem("fi-theme");
  if(saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  document.getElementById("theme-toggle").onclick = () => {
    const cur = root.getAttribute("data-theme");
    const preferDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const nowDark = cur === "dark" || (!cur && preferDark);
    const next = nowDark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem("fi-theme", next);
  };
})();
function fmt(x,d=2){ if(x===null||x===undefined||Number.isNaN(x)) return "—"; return Number(x).toLocaleString(undefined,{maximumFractionDigits:d}); }
function chartColors(){
  const cs = getComputedStyle(document.documentElement);
  return {
    accent: cs.getPropertyValue("--accent").trim()||"#0071E3",
    forecast: cs.getPropertyValue("--forecast").trim()||"#FF9F0A",
    tick: cs.getPropertyValue("--chart-tick").trim()||"#6E6E73",
    grid: cs.getPropertyValue("--chart-grid").trim()||"rgba(0,0,0,.06)",
    text: cs.getPropertyValue("--text").trim()||"#1D1D1F",
  };
}

const nObs = P.training_sample?.n_observations ?? 0;
const cov = P.training_sample?.coverage_pct_vessels_mean;
const refused = P.forecast?.issued === false;
const banner = document.getElementById("banner");
banner.className = "fi-banner " + (refused ? "wait hero" : "live");
banner.innerHTML = refused
  ? `<span class="fi-status wait"><span class="fi-dot wait"></span>NO FORECAST</span>
     <div style="margin-top:10px;font-size:17px"><strong>Forecast not issued</strong> — insufficient data (N=${nObs}). ${P.forecast?.reason || ""}</div>`
  : `<span class="fi-status live"><span class="fi-dot live"></span>LIVE</span>
     <div style="margin-top:10px">Model trained on <strong>N=${nObs}</strong> observations · fleet coverage ~${fmt(cov,1)}% · horizon T+${P.horizon} · CI: ${P.forecast?.interval?.ci_level || "bootstrap"}</div>`;

document.getElementById("metrics").innerHTML = [
  ["N TRAIN", nObs],
  ["COVERAGE %", cov==null?"—":fmt(cov,1)],
  ["HORIZON", "T+"+(P.horizon||"—")],
  ["ENSEMBLE", refused ? "REFUSED" : "OK"]
].map(([l,v])=>`<div class="fi-card"><div class="fi-card-title">${l}</div><div class="fi-metric ${refused&&l==="ENSEMBLE"?"wait":""}">${v}</div></div>`).join("");

const hist = P.history || {};
const fc = P.forecast || {};
const labels = (hist.dates||[]).concat(fc.dates||[]);
const yHist = (hist.values||[]).concat((fc.point||[]).map(_=>null));
const yFc = (hist.values||[]).map(_=>null).concat(fc.point||[]);
const yLo = (hist.values||[]).map(_=>null).concat(fc.interval?.ci_low||[]);
const yHi = (hist.values||[]).map(_=>null).concat(fc.interval?.ci_high||[]);
const c = chartColors();
new Chart(document.getElementById("chart"), {
  type: "line",
  data: {
    labels,
    datasets: [
      {label:"FACT", data:yHist, borderColor:c.accent, backgroundColor:"transparent", tension:.25, pointRadius:0, borderWidth:2},
      {label:"FORECAST", data:yFc, borderColor:c.forecast, borderDash:[6,4], tension:.25, pointRadius:2, borderWidth:2},
      {label:"CI HIGH", data:yHi, borderColor:c.forecast+"55", pointRadius:0, borderWidth:1, fill:false},
      {label:"CI LOW", data:yLo, borderColor:c.forecast+"55", pointRadius:0, borderWidth:1, fill:"-1", backgroundColor:c.forecast+"18"}
    ]
  },
  options: {
    responsive:true, maintainAspectRatio:false,
    plugins:{ legend:{ labels:{ color:c.tick, font:{ size:12 } } } },
    scales:{
      x:{ ticks:{ color:c.tick, maxTicksLimit:10 }, grid:{ color:c.grid } },
      y:{ ticks:{ color:c.tick }, grid:{ color:c.grid } }
    }
  }
});

const imp = P.feature_importance || {};
const items = Object.entries(imp).sort((a,b)=>b[1]-a[1]).slice(0,12);
const maxImp = Math.max(1e-9, ...items.map(x=>x[1]));
const impEl = document.getElementById("importance");
if(!items.length){
  impEl.innerHTML = '<p class="fi-muted">No GBM importance yet (model refused or not trained).</p>';
} else {
  impEl.innerHTML = items.map(([k,v])=>{
    const src = k.startsWith("fourier") ? "Fourier" : k.startsWith("markov") ? "Markov" : k.startsWith("causal") ? "Causal" : k.startsWith("elliott") ? "Elliott*" : "DWT lags";
    return `<div style="display:flex;justify-content:space-between;gap:8px"><div><strong>${k}</strong><div class="fi-muted" style="font-size:12px">${src}</div></div><div>${fmt(v,3)}</div></div>
      <div class="barline"><span style="width:${(100*v/maxImp).toFixed(1)}%"></span></div>`;
  }).join("") + '<p class="fi-muted" style="font-size:12px">* Elliott — discretionary heuristic only.</p>';
}

const wf = P.walk_forward?.models || {};
let rows = Object.entries(wf).map(([name,sc])=>{
  const beat = sc.beats_baseline===true ? '<span class="beat">beats baseline</span>' :
               sc.beats_baseline===false ? '<span class="lose">loses to baseline</span>' : '—';
  return `<tr><td>${name}</td><td>${fmt(sc.mae,3)}</td><td>${fmt(sc.mape,2)}%</td><td>${beat}</td></tr>`;
}).join("");
document.getElementById("wf").innerHTML = `<table class="fi-table"><thead><tr><th>Model</th><th>MAE</th><th>MAPE</th><th>vs naive</th></tr></thead><tbody>${rows||"<tr><td colspan=4>no folds</td></tr>"}</tbody></table>
  <p class="fi-muted" style="margin-top:8px;font-size:13px">${P.walk_forward?.note||""}</p>`;

const gates = P.model_min_n_gates || {};
document.getElementById("gates").innerHTML = `
  <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--separator)"><span class="fi-muted">naive</span><strong>${gates.naive||"—"}</strong></div>
  <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--separator)"><span class="fi-muted">arima/sarima</span><strong>${gates.arima||"—"}</strong></div>
  <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--separator)"><span class="fi-muted">gbm</span><strong>${gates.gbm||"—"}</strong></div>
  <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--separator)"><span class="fi-muted">lstm</span><strong>${gates.lstm||"—"}</strong></div>
  <div class="fi-muted" style="margin-top:12px">${(P.when_not_to_trust||[]).map(x=>"• "+x).join("<br/>")}</div>`;
document.body.classList.add("is-ready");
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_forecast_ensemble(
    *,
    timeseries_path: Path = TIMESERIES_PATH,
    horizon: int = DEFAULT_HORIZON,
    out_report: Path = OUT_REPORT,
    out_series: Path = OUT_SERIES,
    out_dashboard: Path = OUT_DASHBOARD,
    value_col: str = "laden_dwt_sum",
) -> dict[str, Any]:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    series = load_dwt_series(timeseries_path, value_col=value_col)
    n = int(series["y"].notna().sum()) if not series.empty else 0
    cov = float(series["coverage_pct_vessels"].mean()) if n else None

    spectral = _load_json(SPECTRAL_PATH)
    markov = _load_json(MARKOV_PATH)
    elliott = _load_json(ELLIOTT_PATH) or {}
    # elliott may live only inside spectral report
    if not elliott and spectral:
        elliott = spectral.get("elliott_wave_heuristic") or {}
    causal = _load_json(CAUSAL_PATH)

    periods = extract_fourier_periods(spectral)
    season = int(round(periods[0])) if periods else 7
    markov_p = extract_markov_laden_prob(markov)
    elliott_flag = extract_elliott_flag(elliott)
    causal_flags = extract_causal_flags(causal)

    print("=" * 72)
    print("FORECAST ENSEMBLE — sample disclosure")
    print(f"  N_observations = {n}")
    print(f"  coverage_pct   = {cov}")
    print(f"  fourier_periods= {periods}")
    print(f"  markov_laden_p = {markov_p}")
    print(f"  elliott_flag   = {elliott_flag} (discretionary, optional)")
    print(f"  causal_flags   = {causal_flags}")
    print(f"  gates: naive>={_min_naive(horizon)}, arima>={MIN_N_ARIMA}, gbm>={MIN_N_GBM}, lstm>={MIN_N_LSTM}")
    print("=" * 72)

    when_not_to_trust = [
        "Короткая история архива (≪ 60–90 дней): сезонные и марковские сигналы нестабильны.",
        "Низкое покрытие MMSI / высокий % no_signal: индекс DWT-flow смещён к береговым судам.",
        "Структурные сдвиги рынка (санкции, закрытие проливов, OPEC+) вне признаков модели.",
        "Elliott-flag — discretionary heuristic, не статистически валидирован.",
        "Granger/causal flags отражают предсказательную ассоциацию, не физическую причинность.",
        "Если ML не бьёт naive baseline на walk-forward — не доверяйте «сложному» прогнозу.",
    ]

    gates_doc = {
        "naive": f"horizon+1 = {_min_naive(horizon)} (persistence needs last obs + horizon)",
        "arima": f"{MIN_N_ARIMA} (ARIMA/SARIMA unstable on shorter daily samples)",
        "gbm": f"{MIN_N_GBM} (boosted trees overfit / high variance below this)",
        "lstm": f"{MIN_N_LSTM} (deep nets refused below hundreds of points)",
    }

    payload: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "horizon": horizon,
        "training_sample": {
            "n_observations": n,
            "coverage_pct_vessels_mean": None if cov is None else round(cov, 3),
            "date_start": str(series["date"].iloc[0].date()) if n else None,
            "date_end": str(series["date"].iloc[-1].date()) if n else None,
        },
        "model_min_n_gates": gates_doc,
        "when_not_to_trust": when_not_to_trust,
        "signals_used": {
            "dwt_flow_lags": list(DEFAULT_LAGS),
            "fourier_periods": periods,
            "markov_laden_p": markov_p,
            "elliott_flag": elliott_flag,
            "elliott_method_type": "discretionary_heuristic_not_statistically_validated",
            "causal_flags": causal_flags,
        },
    }

    # Hard refuse if even naive cannot run
    if n < _min_naive(horizon):
        payload["forecast"] = {
            "issued": False,
            "reason": (
                f"n={n} < MIN_N_NAIVE={_min_naive(horizon)} — forecast refused "
                "(insufficient sample for even the naive baseline)"
            ),
        }
        payload["walk_forward"] = None
        payload["history"] = {"dates": [], "values": []}
        payload["feature_importance"] = {}
        out_report.parent.mkdir(parents=True, exist_ok=True)
        out_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        build_dashboard_html(payload, out_dashboard)
        # empty series schema
        pd.DataFrame(columns=["date", "y_hat", "ci_low", "ci_high", "model"]).to_parquet(out_series, index=False)
        print(payload["forecast"]["reason"])
        print(f"Wrote {out_report}; dashboard {out_dashboard}")
        return payload

    feat = build_feature_frame(
        series,
        fourier_periods=periods,
        markov_laden_p=markov_p,
        elliott_flag=elliott_flag,
        causal_flags=causal_flags,
    )

    wf = walk_forward_validate(feat, horizon=horizon, season=season, fourier_periods=periods)
    weights = select_ensemble_weights(wf)

    # Final forecasts from full sample
    y = feat["y"].to_numpy(dtype=float)
    forecasts: dict[str, np.ndarray] = {}
    notes: dict[str, str] = {}

    forecasts["naive"] = forecast_naive(y, horizon, season=season)

    if n >= MIN_N_ARIMA:
        fc, reason = forecast_arima(y, horizon, season=season)
        if fc is not None:
            forecasts["arima"] = fc
        else:
            notes["arima"] = reason or "failed"
    else:
        notes["arima"] = f"refused: n={n} < MIN_N_ARIMA={MIN_N_ARIMA}"

    if n >= MIN_N_GBM:
        fc_g, imp, backend, reason_g = forecast_gbm_one_step_path(feat, len(feat) - 1, horizon)
        if fc_g is not None:
            forecasts["gbm"] = fc_g
            payload["feature_importance"] = {
                k: round(float(v), 6) for k, v in sorted((imp or {}).items(), key=lambda kv: -kv[1])
            }
            payload["gbm_backend"] = backend
        else:
            notes["gbm"] = reason_g or "failed"
            payload["feature_importance"] = {}
    else:
        notes["gbm"] = f"refused: n={n} < MIN_N_GBM={MIN_N_GBM}"
        payload["feature_importance"] = wf.get("feature_importance_last_fold") or {}

    if n >= MIN_N_LSTM:
        fc_l, reason_l = forecast_lstm(y, horizon)
        if fc_l is not None:
            forecasts["lstm"] = fc_l
        else:
            notes["lstm"] = reason_l or "failed"
    else:
        notes["lstm"] = f"refused: n={n} < MIN_N_LSTM={MIN_N_LSTM} (or torch missing)"

    # Ensemble point path
    w_use = {k: weights.get(k, 0.0) for k in forecasts}
    w_sum = sum(w_use.values()) or 1.0
    w_use = {k: v / w_sum for k, v in w_use.items()}
    point = np.zeros(horizon, dtype=float)
    for name, fc in forecasts.items():
        point += w_use[name] * fc

    fitted = _in_sample_naive_fitted(y, season)
    # Prefer ARIMA in-sample residuals if available
    if "arima" in forecasts and n >= MIN_N_ARIMA:
        try:
            from statsmodels.tsa.statespace.sarimax import SARIMAX

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fit = SARIMAX(y, order=(1, 1, 1), enforce_stationarity=False, enforce_invertibility=False).fit(disp=False)
                fitted = np.asarray(fit.fittedvalues, dtype=float)
        except Exception:
            pass

    interval = residual_bootstrap_intervals(y, point, in_sample_fitted=fitted)

    last_date = pd.Timestamp(feat["date"].iloc[-1])
    fc_dates = [(last_date + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, horizon + 1)]

    # Keep history tail for dashboard (last 90 points)
    hist_tail = feat.tail(min(90, len(feat)))
    payload["walk_forward"] = wf
    payload["ensemble_weights"] = w_use
    payload["model_notes"] = notes
    payload["history"] = {
        "dates": [str(pd.Timestamp(d).date()) for d in hist_tail["date"]],
        "values": [None if not np.isfinite(v) else round(float(v), 4) for v in hist_tail["y"]],
    }
    payload["forecast"] = {
        "issued": True,
        "dates": fc_dates,
        "point": [round(float(x), 4) for x in point],
        "components": {k: [round(float(x), 4) for x in v] for k, v in forecasts.items()},
        "interval": interval,
        "method": "inverse-MAE weighted ensemble of eligible models + residual bootstrap CI",
    }

    # Optional pressure proxy placeholder if causal flags on
    if causal_flags.get("causal_brent_any") or causal_flags.get("causal_ttf_any"):
        payload["optional_price_pressure_proxy"] = {
            "note": (
                "Not a price forecast. If Granger flags are on, DWT-flow changes are "
                "a candidate leading/lagging associate for TTF/Brent — see causal_report.json. "
                "No numeric price path is emitted without a validated price model."
            ),
            "causal_flags": causal_flags,
            "dwt_flow_forecast_point": payload["forecast"]["point"],
        }

    # Series parquet
    rows = []
    for i, d in enumerate(fc_dates):
        rows.append(
            {
                "date": d,
                "y_hat": point[i],
                "ci_low": interval["ci_low"][i],
                "ci_high": interval["ci_high"][i],
                "model": "ensemble",
            }
        )
    out_series.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(out_series, index=False)

    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    build_dashboard_html(payload, out_dashboard)

    print(f"Forecast issued: {payload['forecast']['issued']}")
    print(f"Weights: {w_use}")
    for m, sc in (wf.get("models") or {}).items():
        print(
            f"  WF {m}: MAE={sc.get('mae')} MAPE={sc.get('mape')} "
            f"beats_baseline={sc.get('beats_baseline')}"
        )
    print(f"Wrote {out_report}")
    print(f"Wrote {out_series}")
    print(f"Wrote {out_dashboard}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="DWT-flow forecast ensemble (Prompts 1–4)")
    parser.add_argument("--timeseries", type=Path, default=TIMESERIES_PATH)
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--value-col", default="laden_dwt_sum")
    parser.add_argument("--out-report", type=Path, default=OUT_REPORT)
    parser.add_argument("--out-series", type=Path, default=OUT_SERIES)
    parser.add_argument("--out-dashboard", type=Path, default=OUT_DASHBOARD)
    args = parser.parse_args()
    run_forecast_ensemble(
        timeseries_path=args.timeseries,
        horizon=args.horizon,
        value_col=args.value_col,
        out_report=args.out_report,
        out_series=args.out_series,
        out_dashboard=args.out_dashboard,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
