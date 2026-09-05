"""
TTF feature engineering pipeline.

Technical: log returns, rolling vol (7/14/30), Hurst, RSI(14), MACD.
OSINT: LNG transit index, EU terminal anchorage density, AIS gap anomaly proxy.
Output: RobustScaler-normalized vectors → output/ttf_features.parquet
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler

from services.ttf_forecast.schema import migrate_ttf_schema, resolve_db

ROOT = Path(__file__).resolve().parents[2]
OUT_PARQUET = ROOT / "output" / "ttf_features.parquet"

ROTTERDAM_BOX = (51.7, 52.2, 3.7, 4.6)
ZEEBRUGGE_BOX = (51.25, 51.45, 3.1, 3.35)
GATE_BOX = (51.9, 52.05, 3.95, 4.2)  # GATE LNG terminal approaches


def _load_market(db_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            """
            SELECT timestamp, price_ttf_eur_mwh, volume, lng_flow_rate_eia,
                   shadow_tanker_density_gulf, weather_degree_days,
                   temp_anomaly_europe, storage_fill_level_pct
            FROM ttf_market_features
            ORDER BY timestamp ASC
            """,
            conn,
        )
    finally:
        conn.close()
    if df.empty:
        raise RuntimeError("ttf_market_features is empty — run ingest_ttf first")
    df["date"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    return df


def log_returns(series: pd.Series) -> pd.Series:
    return np.log(series / series.shift(1))


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": hist})


def hurst_exponent(series: pd.Series, max_lag: int = 20) -> float:
    """R/S Hurst estimate; H>0.5 → long memory, H<0.5 → mean-reverting."""
    x = series.dropna().values.astype(float)
    if len(x) < max_lag * 2:
        return float("nan")
    lags = range(2, max_lag + 1)
    tau = []
    for lag in lags:
        # difference variance proxy
        diff = x[lag:] - x[:-lag]
        tau.append(math.sqrt(float(np.std(diff))))
    tau = np.array(tau, dtype=float)
    lags_arr = np.array(list(lags), dtype=float)
    mask = (tau > 0) & np.isfinite(tau)
    if mask.sum() < 5:
        return float("nan")
    poly = np.polyfit(np.log(lags_arr[mask]), np.log(tau[mask]), 1)
    return float(poly[0])


def rolling_hurst(series: pd.Series, window: int = 60, max_lag: int = 15) -> pd.Series:
    out = [np.nan] * len(series)
    vals = series.values
    for i in range(window, len(series)):
        out[i] = hurst_exponent(pd.Series(vals[i - window : i]), max_lag=max_lag)
    return pd.Series(out, index=series.index)


def _ais_osint_features(db_path: Path, dates: pd.Series) -> pd.DataFrame:
    """
    Daily OSINT proxies — prefer ais_daily_aggregates (durable), else raw ais_positions.
    """
    cal = pd.DataFrame({"date": pd.to_datetime(dates, utc=True)})
    cal["day"] = cal["date"].dt.strftime("%Y-%m-%d")
    idx = dates.index if hasattr(dates, "index") else None

    # Durable feature store (survives 7-day retention)
    try:
        from services.ais_daily_aggregates import load_aggregates_frame

        aggs = load_aggregates_frame(db_path, min_days=1)
    except Exception:  # noqa: BLE001
        aggs = []
    if aggs:
        a = pd.DataFrame(aggs).rename(columns={"date": "day"})
        merged = cal.merge(a, on="day", how="left")
        transit = (
            merged["total_active_tankers"].fillna(0).astype(float)
            * merged["shadow_fleet_active_ratio"].fillna(0.25).astype(float)
            * 0.05
            + 0.15 * merged["chokepoint_density_index"].fillna(0).astype(float)
        )
        anchorage = merged["anchorage_wait_hours_avg"].fillna(0).astype(float)
        gap_proxy = (1.0 - merged["shadow_fleet_active_ratio"].fillna(0.3).astype(float)).clip(0, 1)
        return pd.DataFrame({
            "lng_tanker_transit_index": transit.astype(float).values,
            "eu_terminal_anchorage_density": anchorage.astype(float).values,
            "ais_gap_anomaly_vol_proxy": gap_proxy.astype(float).values,
        }, index=idx)

    ais = pd.DataFrame()
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            try:
                sql = """
                    SELECT
                        substr(timestamp_utc, 1, 10) AS day,
                        SUM(CASE WHEN tier IN ('ALPHA','BRAVO')
                                  AND lat BETWEEN 51.7 AND 52.2 AND lon BETWEEN 3.7 AND 4.6
                             THEN 1 ELSE 0 END) AS rotterdam_fixes,
                        SUM(CASE WHEN tier IN ('ALPHA','BRAVO')
                                  AND lat BETWEEN 51.25 AND 51.45 AND lon BETWEEN 3.1 AND 3.35
                             THEN 1 ELSE 0 END) AS zeebrugge_fixes,
                        SUM(CASE WHEN lat BETWEEN 51.9 AND 52.05 AND lon BETWEEN 3.95 AND 4.2
                                  AND COALESCE(sog, 99) < 1.0
                             THEN 1 ELSE 0 END) AS gate_anchorage,
                        SUM(CASE WHEN matched = 1 THEN 1 ELSE 0 END) AS matched_fixes,
                        COUNT(*) AS total_fixes
                    FROM ais_positions
                    GROUP BY 1
                """
                ais = pd.read_sql_query(sql, conn)
            finally:
                conn.close()
        except (sqlite3.Error, pd.errors.DatabaseError) as exc:
            print(f"WARN: AIS OSINT query failed ({exc}) — using climatology proxies")
            ais = pd.DataFrame()

    if not ais.empty:
        merged = cal.merge(ais, on="day", how="left")
    else:
        merged = cal.copy()
        merged["rotterdam_fixes"] = np.nan
        merged["zeebrugge_fixes"] = np.nan
        merged["gate_anchorage"] = np.nan
        merged["matched_fixes"] = np.nan
        merged["total_fixes"] = np.nan

    # LNG Tanker Transit Index (Alpha/Bravo hub activity)
    transit = (
        merged["rotterdam_fixes"].fillna(0) + 0.8 * merged["zeebrugge_fixes"].fillna(0)
    )
    # If AIS history shorter than TTF panel, fill with seasonal proxy
    if float(transit.sum()) < 1:
        t = np.arange(len(merged))
        transit = pd.Series(
            5.0 + 2.0 * np.sin(2 * np.pi * t / 21.0) + 1.5 * np.sin(2 * np.pi * t / 365.0),
            index=merged.index,
        )

    anchorage = merged["gate_anchorage"].fillna(0)
    if float(anchorage.sum()) < 1:
        t = np.arange(len(merged))
        anchorage = pd.Series(3.0 + 1.2 * np.sin(2 * np.pi * t / 14.0), index=merged.index)

    # Gap anomaly volumetric proxy: 1 - matched/total (higher = more dark/unmatched)
    matched = merged["matched_fixes"].fillna(0)
    total = merged["total_fixes"].fillna(0).replace(0, np.nan)
    gap_proxy = 1.0 - (matched / total)
    gap_proxy = gap_proxy.fillna(gap_proxy.median() if gap_proxy.notna().any() else 0.85)
    if gap_proxy.isna().all():
        gap_proxy = pd.Series(0.82 + 0.05 * np.sin(np.arange(len(merged)) / 9.0), index=merged.index)

    return pd.DataFrame({
        "lng_tanker_transit_index": transit.astype(float).values,
        "eu_terminal_anchorage_density": anchorage.astype(float).values,
        "ais_gap_anomaly_vol_proxy": gap_proxy.astype(float).values,
    }, index=idx)


FEATURE_COLS = [
    "price_ttf_eur_mwh",
    "volume",
    "lng_flow_rate_eia",
    "shadow_tanker_density_gulf",
    "weather_degree_days",
    "temp_anomaly_europe",
    "storage_fill_level_pct",
    "log_return",
    "vol_7d",
    "vol_14d",
    "vol_30d",
    "hurst_60d",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "lng_tanker_transit_index",
    "eu_terminal_anchorage_density",
    "ais_gap_anomaly_vol_proxy",
]


def build_feature_frame(db_path: Path) -> pd.DataFrame:
    df = _load_market(db_path)
    px = df["price_ttf_eur_mwh"]

    df["log_return"] = log_returns(px)
    df["vol_7d"] = df["log_return"].rolling(7).std() * math.sqrt(365)
    df["vol_14d"] = df["log_return"].rolling(14).std() * math.sqrt(365)
    df["vol_30d"] = df["log_return"].rolling(30).std() * math.sqrt(365)
    df["hurst_60d"] = rolling_hurst(px, window=60, max_lag=15)
    df["rsi_14"] = rsi(px, 14)
    macd_df = macd(px)
    df = pd.concat([df, macd_df], axis=1)

    osint = _ais_osint_features(db_path, df["date"])
    for col in osint.columns:
        df[col] = osint[col].values

    # Point Hurst (full sample) as metadata column broadcast
    H = hurst_exponent(px, max_lag=20)
    df["hurst_full_sample"] = H

    return df


def scale_and_export(df: pd.DataFrame, out_path: Path = OUT_PARQUET) -> dict[str, Any]:
    work = df.copy()
    cols = [c for c in FEATURE_COLS if c in work.columns]
    # Drop warmup NaNs from rolling windows
    clean = work.dropna(subset=["log_return", "vol_30d", "rsi_14", "macd"]).copy()
    if clean.empty:
        raise RuntimeError("No rows left after indicator warmup — need longer series")

    scaler = RobustScaler()
    scaled = scaler.fit_transform(clean[cols].astype(float))
    scaled_df = pd.DataFrame(scaled, columns=[f"{c}__robust" for c in cols], index=clean.index)

    export = pd.concat(
        [
            clean[["timestamp", "date"]].reset_index(drop=True),
            clean[cols].reset_index(drop=True),
            scaled_df.reset_index(drop=True),
        ],
        axis=1,
    )
    export["date"] = export["date"].astype(str)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    export.to_parquet(out_path, index=False)

    meta = {
        "path": str(out_path),
        "rows": int(len(export)),
        "feature_cols": cols,
        "scaled_cols": [f"{c}__robust" for c in cols],
        "hurst_full_sample": float(clean["hurst_full_sample"].iloc[-1])
        if "hurst_full_sample" in clean.columns
        else None,
        "price_last": float(clean["price_ttf_eur_mwh"].iloc[-1]),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scaler": "RobustScaler",
        "center": scaler.center_.tolist(),
        "scale": scaler.scale_.tolist(),
    }
    meta_path = out_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def run_feature_pipeline(
    *,
    db_path: Optional[Path] = None,
    out_path: Optional[Path] = None,
    ensure_ingest: bool = True,
) -> dict[str, Any]:
    path = resolve_db(db_path)
    migrate_ttf_schema(path)

    if ensure_ingest:
        # Ensure market table populated
        n = 0
        conn = sqlite3.connect(str(path))
        try:
            n = int(conn.execute("SELECT COUNT(*) FROM ttf_market_features").fetchone()[0])
        except sqlite3.Error:
            n = 0
        finally:
            conn.close()
        if n < 60:
            from services.ttf_forecast.ingest_ttf import run_ingest

            run_ingest(db_path=path, days=365)

    df = build_feature_frame(path)
    return scale_and_export(df, out_path or OUT_PARQUET)


def main() -> int:
    parser = argparse.ArgumentParser(description="TTF feature engineering → parquet")
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default=str(OUT_PARQUET))
    parser.add_argument("--no-ingest", action="store_true")
    args = parser.parse_args()
    meta = run_feature_pipeline(
        db_path=args.db,
        out_path=Path(args.out),
        ensure_ingest=not args.no_ingest,
    )
    print(json.dumps({k: meta[k] for k in ("path", "rows", "price_last", "hurst_full_sample", "scaler")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
