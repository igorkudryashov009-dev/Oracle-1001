"""Dead-reckoning forecast — explicitly labeled as estimates, not confirmed AIS."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "история1"
DB_PATH = HISTORY / "raw_positions.db"
FORECAST_DIR = HISTORY / "forecast"
FLEET = ROOT / "output" / "fleet_database.csv"
TARGETS = ROOT / "targets.json"

# Rough port coordinates for GC destination projection (best-effort)
PORT_COORDS = {
    "singapore": (1.264, 103.820),
    "сингапур": (1.264, 103.820),
    "rotterdam": (51.950, 4.140),
    "роттердам": (51.950, 4.140),
    "hong kong": (22.290, 114.170),
    "гонконг": (22.290, 114.170),
    "los angeles": (33.720, -118.270),
    "dubai": (25.270, 55.300),
    "дубай": (25.270, 55.300),
    "shanghai": (31.230, 121.470),
    "шанхай": (31.230, 121.470),
    "houston": (29.760, -95.370),
    "fujairah": (25.120, 56.340),
}


def dest_point(lat: float, lon: float, bearing_deg: float, distance_km: float) -> tuple[float, float]:
    """Move distance_km along bearing from (lat, lon)."""
    r = 6371.0
    br = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    ang = distance_km / r
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(br)
    )
    lon2 = lon1 + math.atan2(
        math.sin(br) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), ((math.degrees(lon2) + 540) % 360) - 180


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def initial_bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def resolve_port(text: str | None) -> tuple[float, float] | None:
    if not text:
        return None
    low = str(text).lower()
    for key, coords in PORT_COORDS.items():
        if key in low:
            return coords
    return None


def last_position(conn: sqlite3.Connection, imo: str) -> dict | None:
    cur = conn.execute(
        """
        SELECT imo, mmsi, timestamp_utc, lat, lon, speed_knots, heading, nav_status
        FROM positions WHERE imo = ?
        ORDER BY timestamp_utc DESC LIMIT 1
        """,
        (imo,),
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["imo", "mmsi", "timestamp_utc", "lat", "lon", "speed_knots", "heading", "nav_status"]
    return dict(zip(keys, row))


def forecast_vessel(pos: dict, dest_port: str | None, hours: list[int] | None = None) -> dict:
    hours = hours or [6, 12, 24, 48]
    lat, lon = float(pos["lat"]), float(pos["lon"])
    sog = pos.get("speed_knots")
    hdg = pos.get("heading")
    sog_f = float(sog) if sog is not None and not (isinstance(sog, float) and math.isnan(sog)) else None
    hdg_f = float(hdg) if hdg is not None and not (isinstance(hdg, float) and math.isnan(hdg)) else None

    # Dead-reckoning along heading
    dr_points = []
    if sog_f is not None and sog_f > 0.5 and hdg_f is not None and hdg_f < 360:
        for h in hours:
            dist_nm = sog_f * h
            dist_km = dist_nm * 1.852
            plat, plon = dest_point(lat, lon, hdg_f, dist_km)
            dr_points.append(
                {
                    "hours_ahead": h,
                    "lat": round(plat, 5),
                    "lon": round(plon, 5),
                    "method": "dead_reckoning_course_speed",
                    "label": "ОЦЕНКА (dead-reckoning)",
                }
            )

    # Great-circle toward destination port
    gc_points = []
    port_xy = resolve_port(dest_port)
    if port_xy and sog_f is not None and sog_f > 0.5:
        plat, plon = port_xy
        bearing = initial_bearing(lat, lon, plat, plon)
        remaining_km = haversine_km(lat, lon, plat, plon)
        speed_kmh = sog_f * 1.852
        eta_h = remaining_km / speed_kmh if speed_kmh > 0 else None
        for h in hours:
            dist_km = min(speed_kmh * h, remaining_km)
            glat, glon = dest_point(lat, lon, bearing, dist_km)
            gc_points.append(
                {
                    "hours_ahead": h,
                    "lat": round(glat, 5),
                    "lon": round(glon, 5),
                    "method": "great_circle_to_destination",
                    "destination_port": dest_port,
                    "eta_hours_estimate": None if eta_h is None else round(eta_h, 1),
                    "label": "ОЦЕНКА (dead-reckoning → destination)",
                }
            )

    return {
        "imo": pos["imo"],
        "as_of": pos["timestamp_utc"],
        "method": "dead_reckoning_estimate",
        "last_position": {"lat": lat, "lon": lon, "speed_knots": sog_f, "heading": hdg_f},
        "disclaimer": (
            "Все точки ниже — ОЦЕНКА (dead-reckoning), НЕ подтверждённые AIS-позиции. "
            "Не использовать как единственный источник для operational/compliance решений."
        ),
        "course_speed_projection": dr_points,
        "destination_projection": gc_points,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main() -> int:
    FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    if not TARGETS.exists():
        print("ERROR: targets.json missing")
        return 1
    if not DB_PATH.exists():
        print("WARNING: raw_positions.db missing — no forecasts generated")
        return 0

    dest_map = {}
    if FLEET.exists():
        fleet = pd.read_csv(FLEET, usecols=lambda c: c in ("imo", "destination_port"))
        for _, r in fleet.drop_duplicates("imo").iterrows():
            dest_map[str(r["imo"]).replace(".0", "")] = (
                None if pd.isna(r.get("destination_port")) else str(r["destination_port"])
            )

    targets = json.loads(TARGETS.read_text(encoding="utf-8"))["targets"]
    conn = sqlite3.connect(DB_PATH)
    n = 0
    for t in targets:
        imo = str(t["imo"])
        pos = last_position(conn, imo)
        if not pos:
            continue
        fc = forecast_vessel(pos, dest_map.get(imo))
        (FORECAST_DIR / f"{imo}.json").write_text(
            json.dumps(fc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        n += 1
    conn.close()
    print(f"Forecasts written: {n} → {FORECAST_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
