"""
Causal / predictive-association analysis: DWT-flow vs TTF / Brent prices.

Pipeline:
  1) Load DWT-flow (Prompt 1 parquet) + user price file (config paths.price_source
     = PRICE_SOURCE_PATH) via alias-based column resolver (ais_loader style).
  2) Align to a common daily calendar; limited forward-fill for exchange holidays.
  3) ADF + KPSS stationarity on BOTH series before any causal test; difference /
     log-return if needed (spurious-regression guard).
  4) Bidirectional Granger tests, lags 1..L, with FDR or Bonferroni correction
     across lags (anti p-hacking).
  5) CCF for visual lag inspection.
  6) Explicit disclaimer: Granger = statistical predictability, NOT physical cause.

Refuse if intersection length < min_intersection_points (default 30).

Outputs:
  features/causal_report.json
  features/plots/ccf_{pair}.png
  features/plots/granger_table_{pair}.png  (heatmap of p-values by lag/direction)
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import yaml
from scipy import stats
from statsmodels.tsa.stattools import adfuller, grangercausalitytests, kpss

ROOT = Path(__file__).resolve().parent
FEATURES_DIR = ROOT / "features"
TIMESERIES_PATH = FEATURES_DIR / "dwt_flow_timeseries.parquet"
OUT_REPORT = FEATURES_DIR / "causal_report.json"
PLOTS_DIR = FEATURES_DIR / "plots"
CONFIG_PATH = ROOT / "config.yaml"

GRANGER_DISCLAIMER = (
    "DISCLAIMER — Granger 'causality' is a test of whether lagged values of X "
    "improve the prediction of Y beyond Y's own lags. It is NOT proof of physical "
    "or structural causation. A common hidden factor (e.g. a geopolitical shock) "
    "can move fleet activity and prices together. Report results as: "
    "'X statistically predicts Y at lag N days (adjusted p < α)', never "
    "'X causes Y'."
)

# ---- Price loader aliases (mirror ais_loader discipline: no blind guessing) ----
DATE_ALIASES = (
    "date",
    "timestamp",
    "datetime",
    "trade_date",
    "settlement_date",
    "day",
)
VALUE_ALIASES = (
    "value",
    "price",
    "close",
    "settle",
    "settlement",
    "last",
    "px",
    "rate",
)
SYMBOL_ALIASES = (
    "symbol",
    "ticker",
    "name",
    "instrument",
    "contract",
    "series",
    "commodity",
    "market",
)
TTF_ALIASES = (
    "ttf",
    "ttf_price",
    "ttf_close",
    "ttf_eur",
    "dutch_ttf",
    "gas_ttf",
    "natural_gas_ttf",
)
BRENT_ALIASES = (
    "brent",
    "brent_price",
    "brent_close",
    "brent_usd",
    "ice_brent",
    "brent_crude",
    "crude_brent",
)

TTF_SYMBOL_TOKENS = ("ttf", "dutch ttf", "natural gas", "gas_ttf")
BRENT_SYMBOL_TOKENS = ("brent", "ice brent", "brent crude", "xbr")


def _normalize_col(name: str) -> str:
    return str(name).strip().lower().replace(" ", "_").replace("-", "_")


def _resolve_column(columns: list[str], aliases: tuple[str, ...]) -> Optional[str]:
    normalized = {_normalize_col(c): c for c in columns}
    for alias in aliases:
        if _normalize_col(alias) in normalized:
            return normalized[_normalize_col(alias)]
    return None


def _read_raw(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, comment="#")
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path, engine="openpyxl")
    if suffix == ".json":
        return pd.read_json(path)
    raise ValueError(
        f"Unsupported price file format '{suffix}'. Expected .csv, .xlsx, or .json."
    )


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_price_source_path(
    config: dict[str, Any],
    *,
    cli_path: Optional[Path] = None,
    env_path: Optional[str] = None,
) -> Optional[Path]:
    """PRIORITY: CLI > env PRICE_SOURCE_PATH > config paths.price_source."""
    if cli_path is not None:
        return Path(cli_path)
    if env_path:
        return Path(env_path)
    rel = (config.get("paths") or {}).get("price_source")
    if rel:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p
    return None


def load_price_series(path: str | Path) -> pd.DataFrame:
    """Load TTF/Brent into canonical wide daily frame: date, ttf, brent.

    Supports:
      - wide: date + ttf* + brent* columns
      - long: date + symbol + value, with symbol tokens matching TTF/Brent

    Raises with found columns if mapping cannot be resolved (no blind guess).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PRICE_SOURCE_PATH not found: {path}")

    raw = _read_raw(path)
    if raw.empty:
        raise ValueError(f"Price file is empty: {path}")

    cols = list(raw.columns)
    date_col = _resolve_column(cols, DATE_ALIASES)
    if date_col is None:
        raise ValueError(
            "Cannot map date column in price file.\n"
            f"  Need one of aliases: {DATE_ALIASES}\n"
            f"  Found columns: {cols}"
        )

    ttf_col = _resolve_column(cols, TTF_ALIASES)
    brent_col = _resolve_column(cols, BRENT_ALIASES)
    symbol_col = _resolve_column(cols, SYMBOL_ALIASES)
    value_col = _resolve_column(cols, VALUE_ALIASES)

    frames: list[pd.DataFrame] = []

    if ttf_col or brent_col:
        wide = pd.DataFrame()
        wide["date"] = pd.to_datetime(raw[date_col], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
        if ttf_col:
            wide["ttf"] = pd.to_numeric(raw[ttf_col], errors="coerce")
        if brent_col:
            wide["brent"] = pd.to_numeric(raw[brent_col], errors="coerce")
        frames.append(wide)

    if symbol_col and value_col and not (ttf_col and brent_col):
        long = pd.DataFrame(
            {
                "date": pd.to_datetime(raw[date_col], errors="coerce", utc=True)
                .dt.tz_localize(None)
                .dt.normalize(),
                "symbol": raw[symbol_col].astype(str).str.strip().str.lower(),
                "value": pd.to_numeric(raw[value_col], errors="coerce"),
            }
        )
        long = long.dropna(subset=["date"])

        def _match(tokens: tuple[str, ...], s: str) -> bool:
            return any(tok in s for tok in tokens)

        pieces = []
        ttf_mask = long["symbol"].map(lambda s: _match(TTF_SYMBOL_TOKENS, s))
        brent_mask = long["symbol"].map(lambda s: _match(BRENT_SYMBOL_TOKENS, s))
        if ttf_mask.any():
            g = long.loc[ttf_mask, ["date", "value"]].rename(columns={"value": "ttf"})
            pieces.append(g.groupby("date", as_index=False)["ttf"].last())
        if brent_mask.any():
            g = long.loc[brent_mask, ["date", "value"]].rename(columns={"value": "brent"})
            pieces.append(g.groupby("date", as_index=False)["brent"].last())
        if not pieces:
            raise ValueError(
                "Long-format price file: could not match TTF/Brent symbols.\n"
                f"  symbol tokens TTF={TTF_SYMBOL_TOKENS}, Brent={BRENT_SYMBOL_TOKENS}\n"
                f"  unique symbols sample: {sorted(long['symbol'].dropna().unique())[:20]}\n"
                f"  Found columns: {cols}"
            )
        merged = pieces[0]
        for p in pieces[1:]:
            merged = merged.merge(p, on="date", how="outer")
        frames.append(merged)

    if not frames:
        raise ValueError(
            "Cannot map TTF/Brent price columns.\n"
            f"  Wide aliases TTF={TTF_ALIASES}, Brent={BRENT_ALIASES}\n"
            f"  Or long format with symbol={SYMBOL_ALIASES} + value={VALUE_ALIASES}\n"
            f"  Found columns: {cols}\n"
            "Do not guess — confirm mapping before re-running."
        )

    out = frames[0]
    for fr in frames[1:]:
        out = out.merge(fr, on="date", how="outer", suffixes=("", "_dup"))
        for c in ("ttf", "brent"):
            if f"{c}_dup" in out.columns:
                out[c] = out[c].combine_first(out[f"{c}_dup"])
                out = out.drop(columns=[f"{c}_dup"])

    keep = ["date"] + [c for c in ("ttf", "brent") if c in out.columns]
    out = out[keep].dropna(subset=["date"]).sort_values("date")
    out = out.groupby("date", as_index=False).last()
    if out.empty or (("ttf" not in out.columns or out["ttf"].notna().sum() == 0) and (
        "brent" not in out.columns or out["brent"].notna().sum() == 0
    )):
        raise ValueError(f"No usable TTF/Brent values after cleaning: {path}")
    return out.reset_index(drop=True)


def load_dwt_flow(
    path: Path = TIMESERIES_PATH,
    *,
    value_col: str = "laden_dwt_sum",
) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["date", "dwt_flow"])
    df = pd.read_parquet(path)
    if df.empty:
        return pd.DataFrame(columns=["date", "dwt_flow"])
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    g = (
        df.dropna(subset=["date"])
        .groupby("date", as_index=False)
        .agg(dwt_flow=(value_col, "sum"), coverage_pct_vessels=("coverage_pct_vessels", "mean"))
        .sort_values("date")
    )
    return g


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------

def align_series(
    flow: pd.DataFrame,
    prices: pd.DataFrame,
    price_col: str,
    *,
    max_ffill_days: int = 3,
) -> pd.DataFrame:
    """Outer calendar between first/last overlapping span; limited ffill on prices."""
    if flow.empty or price_col not in prices.columns:
        return pd.DataFrame(columns=["date", "dwt_flow", price_col])

    p = prices[["date", price_col]].dropna(subset=["date"]).copy()
    f = flow[["date", "dwt_flow"]].dropna(subset=["date"]).copy()
    if p[price_col].notna().sum() == 0 or f["dwt_flow"].notna().sum() == 0:
        return pd.DataFrame(columns=["date", "dwt_flow", price_col])

    start = max(f["date"].min(), p["date"].min())
    end = min(f["date"].max(), p["date"].max())
    if pd.isna(start) or pd.isna(end) or start > end:
        return pd.DataFrame(columns=["date", "dwt_flow", price_col])

    cal = pd.DataFrame({"date": pd.date_range(start, end, freq="D")})
    out = cal.merge(f, on="date", how="left").merge(p, on="date", how="left")

    # Flow: no inventing fleet activity across gaps — leave NaN (dropped later)
    # Prices: forward-fill weekends/holidays with HARD cap
    out[price_col] = out[price_col].ffill(limit=max_ffill_days)
    # Optional: light backfill at start of window only within same cap
    out[price_col] = out[price_col].bfill(limit=min(1, max_ffill_days))

    out = out.dropna(subset=["dwt_flow", price_col]).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Stationarity
# ---------------------------------------------------------------------------

def _adf(x: np.ndarray) -> dict[str, Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        stat, p, usedlag, nobs, crit, icbest = adfuller(x, autolag="AIC")
    return {
        "test": "ADF",
        "statistic": round(float(stat), 4),
        "p_value": float(p),
        "usedlag": int(usedlag),
        "nobs": int(nobs),
        "stationary_at_5pct": bool(p < 0.05),
        "note": "H0: unit root (non-stationary). Reject H0 → evidence of stationarity.",
    }


def _kpss_test(x: np.ndarray) -> dict[str, Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        stat, p, lags, crit = kpss(x, regression="c", nlags="auto")
    return {
        "test": "KPSS",
        "statistic": round(float(stat), 4),
        "p_value": float(p),
        "lags": int(lags),
        "stationary_at_5pct": bool(p >= 0.05),
        "note": "H0: stationary. Reject H0 (p<0.05) → evidence of non-stationarity.",
    }


def assess_and_transform(
    series: pd.Series,
    name: str,
    *,
    prefer_log_return: bool = False,
) -> tuple[pd.Series, dict[str, Any]]:
    """ADF+KPSS; difference or log-return until both agree stationary or max 2 diffs."""
    x = pd.to_numeric(series, errors="coerce")
    meta: dict[str, Any] = {"name": name, "transforms_applied": [], "steps": []}

    cur = x.copy()
    label = "level"
    for _ in range(3):
        vals = cur.dropna().to_numpy(dtype=float)
        if len(vals) < 15:
            meta["final_stationary"] = False
            meta["reason"] = "too few points after transform"
            meta["final_series_kind"] = label
            return cur, meta
        adf_r = _adf(vals)
        kpss_r = _kpss_test(vals)
        meta["steps"].append({"kind": label, "adf": adf_r, "kpss": kpss_r})
        # Conservative: require ADF reject unit root AND KPSS not reject stationarity
        ok = adf_r["stationary_at_5pct"] and kpss_r["stationary_at_5pct"]
        if ok:
            meta["final_stationary"] = True
            meta["final_series_kind"] = label
            meta["message"] = f"{name}: stationary as '{label}'"
            return cur, meta

        # Transform
        if label == "level" and prefer_log_return and (vals > 0).all():
            cur = np.log(cur).diff()
            meta["transforms_applied"].append("log_return")
            label = "log_return"
        else:
            cur = cur.diff()
            meta["transforms_applied"].append("diff1" if "diff" not in label else "diff2")
            label = "diff1" if label in ("level", "log_return") else "diff2"

    vals = cur.dropna().to_numpy(dtype=float)
    if len(vals) >= 15:
        meta["steps"].append({"kind": label, "adf": _adf(vals), "kpss": _kpss_test(vals)})
    meta["final_stationary"] = bool(
        meta["steps"] and meta["steps"][-1]["adf"]["stationary_at_5pct"]
        and meta["steps"][-1]["kpss"]["stationary_at_5pct"]
    )
    meta["final_series_kind"] = label
    meta["message"] = (
        f"{name}: after transforms {meta['transforms_applied']} "
        f"stationary={meta['final_stationary']}"
    )
    return cur, meta


# ---------------------------------------------------------------------------
# Multiple testing
# ---------------------------------------------------------------------------

def adjust_pvalues(pvals: list[float], method: str = "fdr") -> list[float]:
    """Bonferroni or Benjamini–Hochberg FDR."""
    p = np.asarray(pvals, dtype=float)
    m = len(p)
    if m == 0:
        return []
    if method == "bonferroni":
        return [float(min(1.0, x * m)) for x in p]
    # FDR BH
    order = np.argsort(p)
    ranked = p[order]
    adj = np.empty(m, dtype=float)
    prev = 1.0
    for i in range(m - 1, -1, -1):
        rank = i + 1
        val = ranked[i] * m / rank
        prev = min(prev, val)
        adj[i] = prev
    out = np.empty(m, dtype=float)
    out[order] = np.clip(adj, 0, 1)
    return [float(x) for x in out]


# ---------------------------------------------------------------------------
# Granger + CCF
# ---------------------------------------------------------------------------

def run_granger_pair(
    y: np.ndarray,
    x: np.ndarray,
    *,
    max_lag: int = 14,
    alpha: float = 0.05,
    mt_method: str = "fdr",
) -> dict[str, Any]:
    """Bidirectional Granger on columns [y, x] as statsmodels expects.

    statsmodels grangercausalitytests(data[:, [y, x]]): tests whether x Granger-causes y.
    """
    data = np.column_stack([y, x])
    # Drop rows with nan
    mask = np.isfinite(data).all(axis=1)
    data = data[mask]
    n = len(data)
    if n < max_lag + 10:
        return {
            "ok": False,
            "reason": f"n={n} too small for max_lag={max_lag}",
            "n": n,
            "directions": {},
        }

    directions = {
        "x_predicts_y": {"label": "X → Y (second series predicts first)", "lags": []},
        "y_predicts_x": {"label": "Y → X (first series predicts second)", "lags": []},
    }

    # X → Y
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            try:
                res_xy = grangercausalitytests(
                    data[:, [0, 1]], maxlag=max_lag, verbose=False
                )
            except TypeError:
                res_xy = grangercausalitytests(data[:, [0, 1]], maxlag=max_lag)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"granger X→Y failed: {exc}", "n": n, "directions": {}}

    # Y → X
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            try:
                res_yx = grangercausalitytests(
                    data[:, [1, 0]], maxlag=max_lag, verbose=False
                )
            except TypeError:
                res_yx = grangercausalitytests(data[:, [1, 0]], maxlag=max_lag)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"granger Y→X failed: {exc}", "n": n, "directions": {}}

    def _collect(res_dict, key: str) -> list[float]:
        rows = []
        p_raw = []
        for lag in range(1, max_lag + 1):
            # ssr_ftest: (F_stat, p_value, df_denom, df_num)
            f_stat, p_val, _, _ = res_dict[lag][0]["ssr_ftest"]
            p_raw.append(float(p_val))
            rows.append({"lag": lag, "f_stat": round(float(f_stat), 4), "p_raw": float(p_val)})
        p_adj = adjust_pvalues(p_raw, method=mt_method)
        for row, pa in zip(rows, p_adj):
            row["p_adjusted"] = float(pa)
            row["significant_adjusted"] = bool(pa < alpha)
            row["formulation"] = (
                f"statistically predicts at lag {row['lag']}d "
                f"(adjusted p={pa:.4g})"
                if row["significant_adjusted"]
                else f"no adjusted significance at lag {row['lag']}d"
            )
        directions[key]["lags"] = rows
        directions[key]["p_raw"] = p_raw
        directions[key]["p_adjusted"] = p_adj
        directions[key]["any_significant_adjusted"] = any(r["significant_adjusted"] for r in rows)
        # Best lag by min adjusted p
        best = min(rows, key=lambda r: r["p_adjusted"])
        directions[key]["best_lag"] = best
        return p_raw

    _collect(res_xy, "x_predicts_y")
    _collect(res_yx, "y_predicts_x")

    return {
        "ok": True,
        "n": n,
        "max_lag": max_lag,
        "alpha": alpha,
        "multiple_testing": mt_method,
        "directions": directions,
    }


def cross_correlation(
    y: np.ndarray,
    x: np.ndarray,
    *,
    max_lag: int = 14,
) -> dict[str, Any]:
    """CCF: corr(y_t, x_{t-k}) for k in [-max_lag, +max_lag]."""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    mask = np.isfinite(y) & np.isfinite(x)
    y, x = y[mask], x[mask]
    y = (y - y.mean()) / (y.std(ddof=1) or 1.0)
    x = (x - x.mean()) / (x.std(ddof=1) or 1.0)
    lags = list(range(-max_lag, max_lag + 1))
    vals = []
    n = len(y)
    for k in lags:
        # corr(y_t, x_{t-k}): positive k ⇒ x leads y by k days
        if k > 0:
            a, b = y[k:], x[:-k]
        elif k < 0:
            kk = -k
            a, b = y[:-kk], x[kk:]
        else:
            a, b = y, x
        if len(a) < 5:
            vals.append(np.nan)
        else:
            vals.append(float(np.corrcoef(a, b)[0, 1]))
    arr = np.asarray(vals, dtype=float)
    if np.all(~np.isfinite(arr)):
        best_lag, best_corr = None, None
    else:
        i = int(np.nanargmax(np.abs(arr)))
        best_lag, best_corr = int(lags[i]), float(arr[i])
    return {
        "lags": lags,
        "correlation": [None if not np.isfinite(v) else round(float(v), 4) for v in arr],
        "best_abs_lag": best_lag,
        "best_abs_corr": None if best_corr is None else round(best_corr, 4),
        "interpretation": (
            "Positive lag k: corr(y_t, x_{t-k}) — x leads y by k days. "
            "Negative lag: y leads x."
        ),
    }


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def plot_ccf(ccf: dict[str, Any], out_path: Path, title: str) -> Optional[Path]:
    if not ccf.get("lags"):
        return None
    plt = _plt()
    lags = ccf["lags"]
    corr = [0.0 if v is None else v for v in ccf["correlation"]]
    fig, ax = plt.subplots(figsize=(9, 3.8))
    ax.bar(lags, corr, color="#5b9bd5", edgecolor="#1f4e79", width=0.8)
    ax.axhline(0, color="#333", lw=0.8)
    # rough 95% band for white noise: ±1.96/sqrt(n) — n unknown here; skip or annotate
    ax.set_xlabel("Lag k (days): corr(y_t, x_{t-k})")
    ax.set_ylabel("Correlation")
    ax.set_title(title)
    if ccf.get("best_abs_lag") is not None:
        ax.axvline(ccf["best_abs_lag"], color="#c00000", ls="--", lw=1)
        ax.text(
            0.02,
            0.95,
            f"max |ρ| at lag={ccf['best_abs_lag']} (ρ={ccf['best_abs_corr']})",
            transform=ax.transAxes,
            va="top",
            fontsize=9,
        )
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_granger_table(granger: dict[str, Any], out_path: Path, title: str) -> Optional[Path]:
    if not granger.get("ok"):
        return None
    plt = _plt()
    dirs = granger["directions"]
    lags = [r["lag"] for r in dirs["x_predicts_y"]["lags"]]
    mat = np.array(
        [
            [r["p_adjusted"] for r in dirs["x_predicts_y"]["lags"]],
            [r["p_adjusted"] for r in dirs["y_predicts_x"]["lags"]],
        ],
        dtype=float,
    )
    fig, ax = plt.subplots(figsize=(10, 2.8))
    im = ax.imshow(mat, aspect="auto", cmap="RdYlGn_r", vmin=0, vmax=0.2)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["X→Y adj.p", "Y→X adj.p"])
    ax.set_xticks(range(len(lags)))
    ax.set_xticklabels(lags)
    ax.set_xlabel("Lag (days)")
    ax.set_title(title)
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            ax.text(j, i, f"{mat[i, j]:.2f}", ha="center", va="center", fontsize=7)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="adjusted p")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Pair analysis
# ---------------------------------------------------------------------------

def analyze_pair(
    aligned: pd.DataFrame,
    price_col: str,
    *,
    max_lag: int,
    max_ffill_days: int,
    min_n: int,
    alpha: float,
    mt_method: str,
    plots_dir: Path,
) -> dict[str, Any]:
    pair_name = f"dwt_flow_vs_{price_col}"
    n = len(aligned)
    base = {
        "pair": pair_name,
        "price_series": price_col,
        "n_intersection": n,
        "date_start": str(aligned["date"].iloc[0].date()) if n else None,
        "date_end": str(aligned["date"].iloc[-1].date()) if n else None,
        "max_ffill_days": max_ffill_days,
        "disclaimer": GRANGER_DISCLAIMER,
    }

    if n < min_n:
        return {
            **base,
            "status": "refused",
            "message": (
                f"intersection length n={n} < {min_n}: causal test statistically "
                "untenable on this sample — refused (not computed)"
            ),
            "stationarity": None,
            "granger": None,
            "ccf": None,
            "plots": {},
        }

    flow_t, flow_meta = assess_and_transform(aligned["dwt_flow"], "dwt_flow", prefer_log_return=False)
    price_t, price_meta = assess_and_transform(
        aligned[price_col], price_col, prefer_log_return=True
    )

    # Align transforms (diff introduces NaN at start)
    work = pd.DataFrame(
        {
            "date": aligned["date"],
            "dwt_flow": flow_t.to_numpy(),
            price_col: price_t.to_numpy(),
        }
    ).dropna()
    n2 = len(work)
    if n2 < min_n:
        return {
            **base,
            "status": "refused",
            "message": (
                f"after stationarity transforms n={n2} < {min_n}: refused"
            ),
            "stationarity": {"dwt_flow": flow_meta, price_col: price_meta},
            "granger": None,
            "ccf": None,
            "plots": {},
        }

    if not (flow_meta.get("final_stationary") and price_meta.get("final_stationary")):
        # Still run but flag — better than silent spurious; user asked to transform first
        stationarity_warning = (
            "One or both series still fail joint ADF+KPSS stationarity after "
            "transforms — Granger results may be unreliable; interpret with caution."
        )
    else:
        stationarity_warning = None

    y = work["dwt_flow"].to_numpy(dtype=float)
    x = work[price_col].to_numpy(dtype=float)

    # Naming in granger: Y = dwt_flow, X = price
    granger = run_granger_pair(
        y, x, max_lag=max_lag, alpha=alpha, mt_method=mt_method
    )
    # Relabel for report clarity
    if granger.get("ok"):
        granger["series_y"] = "dwt_flow"
        granger["series_x"] = price_col
        granger["directions"]["x_predicts_y"]["plain_language"] = (
            f"{price_col} statistically predicts dwt_flow (lags of price → flow)"
        )
        granger["directions"]["y_predicts_x"]["plain_language"] = (
            f"dwt_flow statistically predicts {price_col} (lags of flow → price)"
        )
        # Human summary — never "causes"
        summaries = []
        for key, direction_label in (
            ("x_predicts_y", f"{price_col} → dwt_flow"),
            ("y_predicts_x", f"dwt_flow → {price_col}"),
        ):
            d = granger["directions"][key]
            if d["any_significant_adjusted"]:
                b = d["best_lag"]
                summaries.append(
                    f"{direction_label}: X statistically predicts Y with lag "
                    f"{b['lag']} days (adjusted p={b['p_adjusted']:.4g} < {alpha})"
                )
            else:
                summaries.append(
                    f"{direction_label}: no lag 1..{max_lag} survives {mt_method} "
                    f"adjustment at α={alpha}"
                )
        granger["summary_statements"] = summaries

    ccf = cross_correlation(y, x, max_lag=max_lag)

    plots = {}
    p_ccf = plots_dir / f"ccf_{pair_name}.png"
    if plot_ccf(ccf, p_ccf, title=f"CCF: dwt_flow vs {price_col}"):
        plots["ccf"] = str(p_ccf)
    p_g = plots_dir / f"granger_table_{pair_name}.png"
    if plot_granger_table(
        granger,
        p_g,
        title=f"Granger adjusted p-values: dwt_flow vs {price_col}",
    ):
        plots["granger_table"] = str(p_g)

    return {
        **base,
        "status": "ok" if granger.get("ok") else "failed",
        "message": stationarity_warning,
        "n_after_transform": n2,
        "stationarity": {"dwt_flow": flow_meta, price_col: price_meta},
        "granger": granger,
        "ccf": ccf,
        "plots": plots,
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_causal_analysis(
    *,
    config_path: Path = CONFIG_PATH,
    timeseries_path: Path = TIMESERIES_PATH,
    price_path: Optional[Path] = None,
    out_report: Path = OUT_REPORT,
    plots_dir: Path = PLOTS_DIR,
) -> dict[str, Any]:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import os

    config = load_config(config_path)
    causal_cfg = config.get("causal") or {}
    max_ffill = int(causal_cfg.get("max_ffill_days", 3))
    min_n = int(causal_cfg.get("min_intersection_points", 30))
    max_lag = int(causal_cfg.get("granger_max_lag", 14))
    mt_method = str(causal_cfg.get("multiple_testing", "fdr")).lower()
    alpha = float(causal_cfg.get("alpha", 0.05))
    value_col = str(causal_cfg.get("dwt_value_col", "laden_dwt_sum"))

    resolved_price = resolve_price_source_path(
        config,
        cli_path=price_path,
        env_path=os.environ.get("PRICE_SOURCE_PATH"),
    )

    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disclaimer": GRANGER_DISCLAIMER,
        "config": {
            "price_source": str(resolved_price) if resolved_price else None,
            "max_ffill_days": max_ffill,
            "min_intersection_points": min_n,
            "granger_max_lag": max_lag,
            "multiple_testing": mt_method,
            "alpha": alpha,
            "dwt_value_col": value_col,
        },
        "pairs": [],
    }

    print("=" * 72)
    print("CAUSAL ANALYSIS — Granger predictability (NOT physical causation)")
    print(f"  PRICE_SOURCE_PATH = {resolved_price}")
    print(f"  min_intersection  = {min_n}, max_lag={max_lag}, MT={mt_method}, α={alpha}")
    print("=" * 72)

    if resolved_price is None:
        report["status"] = "refused"
        report["message"] = (
            "No price source configured. Set paths.price_source in config.yaml "
            "or env PRICE_SOURCE_PATH or --prices."
        )
        out_report.parent.mkdir(parents=True, exist_ok=True)
        out_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(report["message"])
        return report

    try:
        prices = load_price_series(resolved_price)
    except (FileNotFoundError, ValueError) as exc:
        report["status"] = "refused"
        report["message"] = str(exc)
        out_report.parent.mkdir(parents=True, exist_ok=True)
        out_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"ERROR: {exc}")
        return report

    flow = load_dwt_flow(timeseries_path, value_col=value_col)
    report["dwt_flow_n"] = int(len(flow))
    report["price_columns_available"] = [c for c in ("ttf", "brent") if c in prices.columns]

    for col in ("ttf", "brent"):
        if col not in prices.columns or prices[col].notna().sum() == 0:
            report["pairs"].append(
                {
                    "pair": f"dwt_flow_vs_{col}",
                    "status": "skipped",
                    "message": f"{col} column absent or empty in price file",
                    "disclaimer": GRANGER_DISCLAIMER,
                }
            )
            continue
        aligned = align_series(flow, prices, col, max_ffill_days=max_ffill)
        print(f"  pair dwt_flow vs {col}: intersection n={len(aligned)}")
        pair_result = analyze_pair(
            aligned,
            col,
            max_lag=max_lag,
            max_ffill_days=max_ffill,
            min_n=min_n,
            alpha=alpha,
            mt_method=mt_method,
            plots_dir=plots_dir,
        )
        report["pairs"].append(pair_result)
        print(f"    → {pair_result['status']}: {pair_result.get('message') or 'ok'}")
        if pair_result.get("granger", {}) and pair_result["granger"].get("summary_statements"):
            for s in pair_result["granger"]["summary_statements"]:
                print(f"      {s}")

    report["status"] = "ok"
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_report}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Granger predictability: DWT-flow vs TTF/Brent (not physical causation)"
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--timeseries", type=Path, default=TIMESERIES_PATH)
    parser.add_argument("--prices", type=Path, default=None, help="Override PRICE_SOURCE_PATH")
    parser.add_argument("--out", type=Path, default=OUT_REPORT)
    parser.add_argument("--plots-dir", type=Path, default=PLOTS_DIR)
    args = parser.parse_args()
    run_causal_analysis(
        config_path=args.config,
        timeseries_path=args.timeseries,
        price_path=args.prices,
        out_report=args.out,
        plots_dir=args.plots_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
