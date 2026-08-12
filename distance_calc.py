"""Haversine distance and per-vessel trajectory aggregates by period."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd

# Period length in days, measured backward from each vessel's last_seen
PERIOD_DAYS = {
    "1m": 30,
    "3m": 90,
    "1y": 365,
    "all": None,
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlmb = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return float(2 * r * np.arcsin(np.sqrt(a)))


def _segment_distances(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    if len(lats) < 2:
        return np.array([], dtype=float)
    r = 6371.0
    p1 = np.radians(lats[:-1])
    p2 = np.radians(lats[1:])
    dphi = np.radians(lats[1:] - lats[:-1])
    dlmb = np.radians(lons[1:] - lons[:-1])
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def vessel_stats_for_period(df: pd.DataFrame, period: str) -> dict:
    """Compute trajectory stats for one vessel's points filtered by period.

    Period is relative to this vessel's MAX timestamp (last_seen), not calendar now.
    """
    if period not in PERIOD_DAYS:
        raise ValueError(f"Unknown period '{period}'. Use: {list(PERIOD_DAYS)}")

    points = df.sort_values("timestamp")
    if points.empty:
        return {
            "total_km": 0.0,
            "num_pings": 0,
            "avg_speed_knots": None,
            "first_seen": None,
            "last_seen": None,
            "track": [],
        }

    last_seen = points["timestamp"].max()
    days = PERIOD_DAYS[period]
    if days is not None:
        cutoff = last_seen - timedelta(days=days)
        points = points[points["timestamp"] >= cutoff]

    if points.empty:
        return {
            "total_km": 0.0,
            "num_pings": 0,
            "avg_speed_knots": None,
            "first_seen": None,
            "last_seen": last_seen.isoformat(),
            "track": [],
        }

    lats = points["lat"].to_numpy(dtype=float)
    lons = points["lon"].to_numpy(dtype=float)
    segs = _segment_distances(lats, lons)
    # Drop absurd teleports (>2500 km between pings) — leave long AIS gaps intact
    segs = segs[segs <= 2500.0]
    total_km = float(segs.sum()) if len(segs) else 0.0

    avg_speed: Optional[float] = None
    if "speed_knots" in points.columns and points["speed_knots"].notna().any():
        avg_speed = float(points["speed_knots"].mean())

    track = [
        {
            "lat": float(r.lat),
            "lon": float(r.lon),
            "t": r.timestamp.isoformat(),
            "sog": None if pd.isna(r.speed_knots) else float(r.speed_knots),
        }
        for r in points.itertuples()
    ]

    return {
        "total_km": round(total_km, 1),
        "num_pings": int(len(points)),
        "avg_speed_knots": None if avg_speed is None else round(avg_speed, 1),
        "first_seen": points["timestamp"].min().isoformat(),
        "last_seen": points["timestamp"].max().isoformat(),
        "track": track,
    }


def compute_all_periods(ais: pd.DataFrame) -> dict[str, dict[str, dict]]:
    """Return {period: {imo: stats}} for all vessels and periods."""
    result: dict[str, dict[str, dict]] = {p: {} for p in PERIOD_DAYS}
    for imo, grp in ais.groupby("imo", sort=False):
        for period in PERIOD_DAYS:
            result[period][str(imo)] = vessel_stats_for_period(grp, period)
    return result


def fleet_summary(period_stats: dict[str, dict]) -> dict:
    """Aggregate fleet-level metrics for one period's per-imo stats."""
    active = [s for s in period_stats.values() if s["num_pings"] >= 2 and s["total_km"] > 0]
    total_km = sum(s["total_km"] for s in active)
    n = len(active)
    speeds = [s["avg_speed_knots"] for s in active if s["avg_speed_knots"] is not None]
    return {
        "total_km": round(total_km, 1),
        "active_vessels": n,
        "avg_km_per_vessel": round(total_km / n, 1) if n else 0.0,
        "avg_speed_knots": round(sum(speeds) / len(speeds), 1) if speeds else None,
    }
