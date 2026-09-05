"""
Granger causality AIS daily aggregates → TTF using long-horizon feature store.

Preferred density series: ais_daily_aggregates.shadow_fleet_active_ratio /
chokepoint_density_index (survives 7-day raw retention).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import grangercausalitytests

from services.ais_daily_aggregates import load_aggregates_frame, resolve_db
from services.ttf_forecast.logging_utils import get_quant_logger
from services.ttf_forecast.schema import resolve_db as resolve_ttf_db

ROOT = Path(__file__).resolve().parents[2]
logger = get_quant_logger("ttf.granger_causality")


def _load_ttf_daily(db_path: Optional[Path] = None) -> pd.DataFrame:
    path = resolve_ttf_db(db_path)
    import sqlite3

    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            """
            SELECT timestamp, price_ttf_eur_mwh
            FROM ttf_market_features
            ORDER BY timestamp ASC
            """
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        # Fallback parquet
        feat = ROOT / "output" / "ttf_features.parquet"
        if feat.exists():
            df = pd.read_parquet(feat)
            if "date" in df.columns:
                return pd.DataFrame({
                    "date": pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d"),
                    "price": df["price_ttf_eur_mwh"].astype(float),
                })
        return pd.DataFrame(columns=["date", "price"])
    out = []
    for ts, px in rows:
        day = datetime_from_ts(int(ts))
        out.append({"date": day, "price": float(px)})
    return pd.DataFrame(out)


def datetime_from_ts(ts: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def build_ais_ttf_panel(
    *,
    db_path: Optional[Path] = None,
    density_col: str = "shadow_fleet_active_ratio",
) -> pd.DataFrame:
    """Join ais_daily_aggregates with TTF daily closes on date."""
    aggs = load_aggregates_frame(db_path or resolve_db(), min_days=1)
    ttf = _load_ttf_daily(db_path)
    if not aggs or ttf.empty:
        raise ValueError("Insufficient AIS aggregates or TTF market rows for Granger panel")

    a = pd.DataFrame(aggs)
    merged = pd.merge(ttf, a, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if density_col not in merged.columns:
        density_col = "chokepoint_density_index"
    merged["density"] = merged[density_col].astype(float)
    merged["log_return"] = np.log(merged["price"].astype(float)).diff()
    merged = merged.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
    return merged


def granger_ais_aggregates_to_ttf(
    *,
    db_path: Optional[Path] = None,
    maxlag: int = 14,
    panel: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    """
    Granger lags 1–14: AIS daily aggregate density → TTF log returns.
    Uses long-term ais_daily_aggregates (not volatile 7-day raw subset).
    """
    df = panel if panel is not None else build_ais_ttf_panel(db_path=db_path)
    if len(df) < maxlag + 20:
        raise ValueError(f"Need ≥{maxlag + 20} joined days for Granger, got {len(df)}")

    y = df["log_return"].to_numpy(dtype=float)
    x = (
        df["density"]
        .pct_change()
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
        .to_numpy(dtype=float)
    )
    # Guard constant x (Granger fails)
    if float(np.nanstd(x)) < 1e-12:
        x = x + np.linspace(-1e-4, 1e-4, len(x))

    data = np.column_stack([y, x])
    try:
        results = grangercausalitytests(data, maxlag=maxlag, verbose=False)
    except TypeError:
        results = grangercausalitytests(data, maxlag=maxlag)
    rows = []
    best = {"lag": None, "ssr_ftest_p": 1.0, "ssr_ftest_stat": 0.0}
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
            best = {
                "lag": lag,
                "ssr_ftest_p": row["ssr_ftest_p"],
                "ssr_ftest_stat": row["ssr_ftest_stat"],
                "stat": row["ssr_ftest_stat"],
            }

    summary = {
        "hypothesis": "AIS_daily_aggregates.density → TTF_log_return",
        "source": "ais_daily_aggregates",
        "maxlag": maxlag,
        "n_obs": int(len(df)),
        "date_first": str(df["date"].iloc[0]),
        "date_last": str(df["date"].iloc[-1]),
        "any_significant_5pct": any(r["significant_5pct"] for r in rows),
        "best_lag": best,
        "lags": rows,
    }
    logger.info(
        "Granger aggregates→TTF n=%s best_lag=%s p=%.4g",
        summary["n_obs"],
        best.get("lag"),
        best.get("ssr_ftest_p", 1.0),
    )
    return summary


def main() -> int:
    import json

    try:
        report = granger_ais_aggregates_to_ttf(maxlag=14)
    except Exception as exc:  # noqa: BLE001
        report = {"error": str(exc), "lags": []}
        print(json.dumps(report, indent=2))
        return 1
    print(json.dumps({
        "n_obs": report["n_obs"],
        "best_lag": report["best_lag"],
        "any_sig": report["any_significant_5pct"],
        "lags": len(report["lags"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
