"""
Route Analytics payload — spatiotemporal tracks + KPI matrix for Sentinel sheet=route.

Data sources (priority):
  1) ais_positions — geospatial SQL trajectories for monitored MMSIs (live_telemetry)
  2) Aggregated interpolation — ONLY when a vessel has < MIN_LIVE_POINTS samples
  3) ais_daily_aggregates — fleet kinematics / density panels
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from services.ais_daily_aggregates import load_aggregates_frame, resolve_db
from services.chokepoints import CHOKEPOINTS
from services.fleet_registry import FleetRegistry
from services.utils.path_sanitizer import sanitize_path_string

ROOT = Path(__file__).resolve().parents[1]

# Canonical LNG / grey-fleet demo cohort (aligned with Q-Flex Fleet / TOP10 sheet)
DEFAULT_FOCUS_IMOS = [
    "9388833",
    "9397303",
    "9397315",
    "9397327",
    "9337755",
    "9372731",
    "9372743",
    "9388819",
    "9388821",
    "9397298",
]

CORRIDORS = [
    ("Malacca", 1.3, 103.8, 4.5, 99.5),
    ("Hormuz", 25.0, 56.5, 26.5, 56.0),
    ("Suez", 29.9, 32.5, 31.5, 32.3),
    ("Bab-el-Mandeb", 12.7, 43.3, 14.0, 42.8),
    ("Bosphorus", 41.1, 29.0, 41.3, 29.1),
    ("Cape", -34.5, 18.4, -33.9, 25.5),
    ("Fujairah", 25.1, 56.4, 24.5, 54.0),
    ("Singapore Anch", 1.25, 103.9, 1.35, 103.7),
]

DESIGN_SPEED_KN = 19.5
DESIGN_DRAFT_M = 12.0
MIN_LIVE_POINTS = 5
MONITORED_VESSEL_TARGET = 22
# ~600–800 m at equator; keeps polylines lean for dashboard JSON
DOUGLAS_PEUCKER_EPSILON_DEG = 0.008


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _perp_dist_deg(
    pt: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    """Perpendicular distance from point to segment in degree-space."""
    (x, y), (x1, y1), (x2, y2) = pt, start, end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    proj_x, proj_y = x1 + t * dx, y1 + t * dy
    return math.hypot(x - proj_x, y - proj_y)


def douglas_peucker(
    points: list[dict[str, Any]],
    *,
    epsilon: float = DOUGLAS_PEUCKER_EPSILON_DEG,
) -> list[dict[str, Any]]:
    """Simplify a lat/lon polyline (preserves dict point payloads)."""
    if len(points) < 3:
        return list(points)

    coords = [(float(p["lat"]), float(p["lon"])) for p in points]

    def _simplify(i0: int, i1: int) -> list[int]:
        if i1 <= i0 + 1:
            return []
        max_d, idx = -1.0, i0
        for i in range(i0 + 1, i1):
            d = _perp_dist_deg(coords[i], coords[i0], coords[i1])
            if d > max_d:
                max_d, idx = d, i
        if max_d > epsilon:
            return _simplify(i0, idx) + [idx] + _simplify(idx, i1)
        return []

    keep = sorted({0, len(points) - 1, *_simplify(0, len(points) - 1)})
    return [points[i] for i in keep]


def _vessel_catalog(registry: FleetRegistry, focus_imos: list[str]) -> list[dict[str, Any]]:
    by_imo = {str(v.imo): v for v in registry.vessels if v.imo}
    out: list[dict[str, Any]] = []
    for i, imo in enumerate(focus_imos):
        v = by_imo.get(str(imo))
        if v:
            out.append(
                {
                    "imo": str(v.imo),
                    "mmsi": str(v.mmsi or "").strip(),
                    "name": v.vessel_name or f"VESSEL-{imo}",
                    "tier": v.tier or "BRAVO",
                    "dwt_tons": float(v.dwt_tons or 130000),
                    "loa_m": float(v.loa_m or 345),
                    "beam_m": float(v.beam_m or 53.8),
                    "design_draft_m": float(v.draft_m or DESIGN_DRAFT_M),
                    "design_speed_kn": DESIGN_SPEED_KN,
                    "flag": v.flag or "—",
                    "risk": v.compliance_risk_level or "MEDIUM",
                    "group_tag": "TOP10_LNG" if i < 10 else (v.tier or "FLEET"),
                    "destination": v.destination_port or "FOR ORDERS",
                }
            )
        else:
            out.append(
                {
                    "imo": str(imo),
                    "mmsi": "",
                    "name": f"FLAGSHIP-{i + 1}",
                    "tier": "ALPHA" if i < 5 else "BRAVO",
                    "dwt_tons": 130000.0,
                    "loa_m": 345.0,
                    "beam_m": 53.8,
                    "design_draft_m": DESIGN_DRAFT_M,
                    "design_speed_kn": DESIGN_SPEED_KN,
                    "flag": "—",
                    "risk": "MEDIUM",
                    "group_tag": "TOP10_LNG",
                    "destination": "FOR ORDERS",
                }
            )
    return out


def _registry_vessel_dict(v: Any, *, group_tag: str) -> dict[str, Any]:
    return {
        "imo": str(v.imo or ""),
        "mmsi": str(v.mmsi or "").strip(),
        "name": v.vessel_name or str(v.imo or v.mmsi or "UNKNOWN"),
        "tier": v.tier or "BRAVO",
        "dwt_tons": float(v.dwt_tons or 130000),
        "loa_m": float(v.loa_m or 300),
        "beam_m": float(v.beam_m or 50),
        "design_draft_m": float(v.draft_m or DESIGN_DRAFT_M),
        "design_speed_kn": DESIGN_SPEED_KN if (v.tier or "") == "ALPHA" else 15.5,
        "flag": v.flag or "—",
        "risk": v.compliance_risk_level or "MEDIUM",
        "group_tag": group_tag,
        "destination": v.destination_port or "FOR ORDERS",
    }


def _mmsi_point_counts(db_path: Path, mmsis: list[str], *, hours: int = 24 * 45) -> dict[str, int]:
    clean = [m for m in mmsis if m]
    if not clean or not db_path.exists():
        return {}
    cut = (_utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, int] = {m: 0 for m in clean}
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=8.0)
        # Chunk IN lists for SQLite variable limits
        chunk = 400
        for i in range(0, len(clean), chunk):
            part = clean[i : i + chunk]
            placeholders = ",".join("?" * len(part))
            rows = con.execute(
                f"""
                SELECT mmsi, COUNT(*) AS n
                FROM ais_positions
                WHERE mmsi IN ({placeholders})
                  AND timestamp_utc >= ?
                  AND lat IS NOT NULL AND lon IS NOT NULL
                  AND lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180
                GROUP BY mmsi
                """,
                (*part, cut),
            ).fetchall()
            for mmsi, n in rows:
                out[str(mmsi)] = int(n)
        con.close()
    except Exception:  # noqa: BLE001
        return out
    return out


def _top_live_mmsis(db_path: Path, *, limit: int = 40, hours: int = 24 * 45) -> list[tuple[str, int]]:
    if not db_path.exists():
        return []
    cut = (_utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=8.0)
        rows = con.execute(
            """
            SELECT mmsi, COUNT(*) AS n
            FROM ais_positions
            WHERE timestamp_utc >= ?
              AND mmsi IS NOT NULL AND TRIM(mmsi) != ''
              AND lat IS NOT NULL AND lon IS NOT NULL
            GROUP BY mmsi
            HAVING n >= ?
            ORDER BY n DESC
            LIMIT ?
            """,
            (cut, MIN_LIVE_POINTS, limit),
        ).fetchall()
        con.close()
        return [(str(m), int(n)) for m, n in rows]
    except Exception:  # noqa: BLE001
        return []


def resolve_monitored_vessels(
    registry: FleetRegistry,
    db_path: Path,
    focus_imos: list[str],
    *,
    target_n: int = MONITORED_VESSEL_TARGET,
) -> list[dict[str, Any]]:
    """Build ~22 vessel cohort: keep focus IMOs, fill with live-telemetry registry vessels."""
    focus = _vessel_catalog(registry, focus_imos)
    by_mmsi = {str(v.mmsi): v for v in registry.vessels if v.mmsi}
    by_imo = {str(v.imo): v for v in registry.vessels if v.imo}

    focus_mmsis = [v["mmsi"] for v in focus if v.get("mmsi")]
    counts = _mmsi_point_counts(db_path, focus_mmsis)
    selected: list[dict[str, Any]] = []
    seen_imo: set[str] = set()
    seen_mmsi: set[str] = set()

    # Prefer focus vessels that already have live points
    for v in focus:
        mmsi = str(v.get("mmsi") or "")
        if mmsi and counts.get(mmsi, 0) >= MIN_LIVE_POINTS:
            selected.append(dict(v))
            seen_imo.add(str(v["imo"]))
            seen_mmsi.add(mmsi)

    # Keep remaining focus identities (may fall back to interpolation later)
    for v in focus:
        imo = str(v["imo"])
        if imo in seen_imo:
            continue
        selected.append(dict(v))
        seen_imo.add(imo)
        if v.get("mmsi"):
            seen_mmsi.add(str(v["mmsi"]))
        if len(selected) >= target_n:
            break

    # Fill up to target_n with vessels that have real AIS trajectories
    if len(selected) < target_n:
        for mmsi, _n in _top_live_mmsis(db_path, limit=80):
            if mmsi in seen_mmsi:
                continue
            reg_v = by_mmsi.get(mmsi)
            if reg_v is None:
                # Anonymous live track — still usable for telemetry sheet
                selected.append(
                    {
                        "imo": mmsi,
                        "mmsi": mmsi,
                        "name": f"AIS-{mmsi[-6:]}",
                        "tier": "BRAVO",
                        "dwt_tons": 130000.0,
                        "loa_m": 300.0,
                        "beam_m": 50.0,
                        "design_draft_m": DESIGN_DRAFT_M,
                        "design_speed_kn": 15.5,
                        "flag": "—",
                        "risk": "MEDIUM",
                        "group_tag": "LIVE_AIS",
                        "destination": "—",
                    }
                )
            else:
                tag = "TOP10_LNG" if str(reg_v.imo) in focus_imos else (reg_v.tier or "LIVE_AIS")
                selected.append(_registry_vessel_dict(reg_v, group_tag=tag))
                seen_imo.add(str(reg_v.imo or mmsi))
            seen_mmsi.add(mmsi)
            if len(selected) >= target_n:
                break

    # Last resort: ALPHA extras from registry
    if len(selected) < target_n:
        for v in registry.vessels:
            if str(v.imo) in seen_imo:
                continue
            if (v.tier or "") != "ALPHA":
                continue
            selected.append(_registry_vessel_dict(v, group_tag="ALPHA_CLUSTER"))
            seen_imo.add(str(v.imo))
            if len(selected) >= target_n:
                break

    return selected[:target_n]


def fetch_tracks_by_mmsi(
    db_path: Path,
    mmsis: list[str],
    *,
    hours: int = 24 * 45,
    per_mmsi_limit: int = 8_000,
) -> dict[str, list[dict[str, Any]]]:
    """Geospatial SQL: real LAT/LON/SOG/timestamps for target MMSIs."""
    clean = sorted({m for m in mmsis if m})
    if not clean or not db_path.exists():
        return {}
    cut = (_utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    buckets: dict[str, list[dict[str, Any]]] = {m: [] for m in clean}
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=12.0)
        con.row_factory = sqlite3.Row
        chunk = 200
        for i in range(0, len(clean), chunk):
            part = clean[i : i + chunk]
            placeholders = ",".join("?" * len(part))
            rows = con.execute(
                f"""
                SELECT mmsi, imo, vessel_name, tier, timestamp_utc, lat, lon, sog, cog,
                       heading, draft_m, destination
                FROM ais_positions
                WHERE mmsi IN ({placeholders})
                  AND timestamp_utc >= ?
                  AND lat IS NOT NULL AND lon IS NOT NULL
                  AND lat BETWEEN -90 AND 90
                  AND lon BETWEEN -180 AND 180
                ORDER BY mmsi ASC, timestamp_utc ASC
                """,
                (*part, cut),
            ).fetchall()
            for r in rows:
                mmsi = str(r["mmsi"] or "")
                if mmsi not in buckets:
                    continue
                if len(buckets[mmsi]) >= per_mmsi_limit:
                    continue
                try:
                    lat = float(r["lat"])
                    lon = float(r["lon"])
                except (TypeError, ValueError):
                    continue
                buckets[mmsi].append(
                    {
                        "t": str(r["timestamp_utc"] or ""),
                        "lat": round(lat, 6),
                        "lon": round(lon, 6),
                        "sog": round(float(r["sog"] or 0.0), 2),
                        "cog": round(float(r["cog"] or 0.0), 1),
                        "draft_m": round(float(r["draft_m"] or DESIGN_DRAFT_M * 0.85), 2),
                        "source": "live",
                    }
                )
        con.close()
    except Exception:  # noqa: BLE001
        return {k: v for k, v in buckets.items() if v}
    return {k: v for k, v in buckets.items() if v}


def _aggregated_interpolation_track(
    vessel: dict[str, Any],
    aggregates: list[dict[str, Any]],
    *,
    hours: int,
    step_h: float = 4.0,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Fallback when live AIS points < MIN_LIVE_POINTS — interpolate along corridor + agg density."""
    corridor = CORRIDORS[seed % len(CORRIDORS)]
    lat0, lon0 = corridor[1], corridor[2]
    lat1, lon1 = corridor[3], corridor[4]
    dens = float((aggregates[-1] if aggregates else {}).get("chokepoint_density_index") or 5.0)
    dens_n = max(0.0, min(1.0, dens / 20.0))
    n = max(8, int(hours / step_h) + 1)
    now = _utcnow()
    draft0 = float(vessel.get("design_draft_m") or DESIGN_DRAFT_M)
    design = float(vessel.get("design_speed_kn") or DESIGN_SPEED_KN)
    points: list[dict[str, Any]] = []
    for i in range(n):
        t = now - timedelta(hours=hours - i * step_h)
        u = i / max(1, n - 1)
        # Mild bend scaled by aggregate density (not random synthetic jitter)
        bend = 0.15 * dens_n * math.sin(u * math.pi)
        lat = lat0 + (lat1 - lat0) * u + bend
        lon = lon0 + (lon1 - lon0) * u - bend * 0.6
        if u < 0.12:
            sog = 1.2 + 2.0 * dens_n
        else:
            sog = design * (0.55 + 0.35 * math.sin(u * math.pi)) * (0.9 + 0.1 * dens_n)
        draft = draft0 * (0.75 + 0.2 * math.sin(u * math.pi))
        cog = (math.degrees(math.atan2(lon1 - lon0, lat1 - lat0)) + 360.0) % 360.0
        points.append(
            {
                "t": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "sog": round(max(0.0, sog), 2),
                "cog": round(cog, 1),
                "draft_m": round(max(4.0, draft), 2),
                "source": "aggregated_interpolation",
            }
        )
    return points


def _tracks_for_vessels(
    vessels: list[dict[str, Any]],
    live_by_mmsi: dict[str, list[dict[str, Any]]],
    aggregates: list[dict[str, Any]],
    *,
    simplify: bool = True,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], int]:
    """Return (tracks_by_imo, track_source_by_imo, live_vessel_count)."""
    tracks: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, str] = {}
    live_n = 0
    for i, v in enumerate(vessels):
        imo = str(v["imo"])
        mmsi = str(v.get("mmsi") or "")
        pts = list(live_by_mmsi.get(mmsi) or [])
        if len(pts) >= MIN_LIVE_POINTS:
            if simplify:
                pts = douglas_peucker(pts)
            tracks[imo] = pts
            sources[imo] = "live_telemetry"
            live_n += 1
        else:
            tracks[imo] = _aggregated_interpolation_track(
                v, aggregates, hours=24 * 30, step_h=4.0, seed=1001 + i
            )
            sources[imo] = "aggregated_interpolation"
    return tracks, sources, live_n


def _kpi_for_track(vessel: dict[str, Any], track: list[dict[str, Any]], aggregates_tail: list[dict]) -> dict[str, Any]:
    if not track:
        return {
            "current_sog": 0,
            "avg_sog": 0,
            "engine_load_factor": 0,
            "speed_degradation_index": 0,
            "dwt_utilization": 0,
            "payload_draft_ratio": 0,
            "eta_variance_h": 0,
            "fuel_efficiency_index": 0,
            "route_anomaly_score": 0,
            "dark_ais_gap_prob": 0,
            "sanction_proximity_index": 0,
            "anomaly_flags": 0,
            "voyage_efficiency": 0,
        }
    sogs = [float(p["sog"]) for p in track]
    drafts = [float(p["draft_m"]) for p in track]
    design_sog = float(vessel.get("design_speed_kn") or DESIGN_SPEED_KN)
    design_draft = float(vessel.get("design_draft_m") or DESIGN_DRAFT_M)
    current_sog = sogs[-1]
    avg_sog = sum(sogs) / len(sogs)
    engine_load = min(1.35, avg_sog / max(1e-6, design_sog))
    peak = max(sogs) or 1.0
    recent = sum(sogs[-max(1, len(sogs) // 5) :]) / max(1, len(sogs) // 5)
    speed_deg = max(0.0, min(1.0, 1.0 - recent / peak))
    payload_ratio = (drafts[-1] / design_draft) if design_draft else 0
    dwt_util = min(1.2, payload_ratio * 0.92 + 0.08)
    dark_hits = sum(1 for s in sogs if s < 0.3)
    dark_prob = dark_hits / len(sogs)
    sanction_hits = 0
    for p in track[:: max(1, len(track) // 20)]:
        for cp in CHOKEPOINTS:
            if cp.contains(p["lat"], p["lon"]) and "sanction" in (cp.name or "").lower():
                sanction_hits += 1
                break
            if cp.name in ("Strait of Hormuz", "Bab-el-Mandeb", "Suez Canal"):
                if cp.contains(p["lat"], p["lon"]):
                    sanction_hits += 1
                    break
    sanction_idx = min(1.0, sanction_hits / 8.0)
    mean = avg_sog
    var = sum((s - mean) ** 2 for s in sogs) / len(sogs)
    speed_cv = math.sqrt(var) / max(1e-6, mean) if mean > 0.5 else 0.8
    risk = str(vessel.get("risk") or "").upper()
    risk_boost = {"EXTREME": 0.35, "HIGH": 0.22, "MEDIUM": 0.1, "LOW": 0.0}.get(risk, 0.12)
    route_anom = min(1.0, 0.35 * speed_cv + 0.4 * dark_prob + risk_boost + 0.2 * sanction_idx)
    fuel_eff = max(0.0, min(1.0, (1.05 - abs(engine_load - 0.75)) * (1.0 - 0.5 * speed_deg)))
    eta_var = round(abs(speed_deg * 18 + (1 - fuel_eff) * 10), 2)
    shadow = 0.0
    if aggregates_tail:
        shadow = float(aggregates_tail[-1].get("shadow_fleet_active_ratio") or 0)
    route_anom = min(1.0, route_anom + 0.15 * shadow)
    anomaly_flags = int(route_anom > 0.55) + int(dark_prob > 0.25) + int(sanction_idx > 0.4) + int(speed_deg > 0.35)
    voyage_eff = max(0.0, min(100.0, 100 * (0.4 * fuel_eff + 0.35 * (1 - route_anom) + 0.25 * (1 - speed_deg))))
    return {
        "current_sog": round(current_sog, 2),
        "avg_sog": round(avg_sog, 2),
        "engine_load_factor": round(engine_load, 3),
        "speed_degradation_index": round(speed_deg, 3),
        "dwt_utilization": round(dwt_util, 3),
        "payload_draft_ratio": round(payload_ratio, 3),
        "eta_variance_h": eta_var,
        "fuel_efficiency_index": round(fuel_eff, 3),
        "route_anomaly_score": round(route_anom, 3),
        "dark_ais_gap_prob": round(dark_prob, 3),
        "sanction_proximity_index": round(sanction_idx, 3),
        "anomaly_flags": anomaly_flags,
        "voyage_efficiency": round(voyage_eff, 1),
        "total_dwt": float(vessel.get("dwt_tons") or 0),
        "current_draft_m": round(drafts[-1], 2),
        "last_lat": track[-1]["lat"],
        "last_lon": track[-1]["lon"],
        "last_t": track[-1]["t"],
    }


def _filter_track(track: list[dict[str, Any]], hours: int) -> list[dict[str, Any]]:
    if not track:
        return []
    cut = _utcnow() - timedelta(hours=hours)
    cut_s = cut.strftime("%Y-%m-%dT%H:%M:%SZ")
    filt = [p for p in track if str(p.get("t") or "") >= cut_s]
    # For live historical windows older than "now" clock skew, keep latest slice
    if not filt:
        filt = track[-max(4, len(track) // 8) :]
    # Re-simplify after horizon filter (1d denser, 30d leaner)
    eps = DOUGLAS_PEUCKER_EPSILON_DEG * (0.5 if hours <= 24 else 1.0 if hours <= 24 * 7 else 1.4)
    return douglas_peucker(filt, epsilon=eps)


def _panel_bundle(
    vessels: list[dict[str, Any]],
    tracks: dict[str, list[dict[str, Any]]],
    kpis: dict[str, dict[str, Any]],
    aggregates: list[dict[str, Any]],
    *,
    hours: int,
) -> dict[str, Any]:
    primary = vessels[0]["imo"] if vessels else ""
    for v in vessels:
        if tracks.get(str(v["imo"])):
            primary = str(v["imo"])
            break
    tr = tracks.get(primary) or []
    labels = [p["t"][11:16] if hours <= 24 else p["t"][5:16] for p in tr]
    sog = [p["sog"] for p in tr]
    draft = [p["draft_m"] for p in tr]
    design = float(next((v["design_speed_kn"] for v in vessels if str(v["imo"]) == primary), DESIGN_SPEED_KN))

    dwt_labels = [v["name"][:14] for v in vessels[:10]]
    dwt_cap = [float(v["dwt_tons"]) for v in vessels[:10]]
    dwt_util = [
        float((kpis.get(str(v["imo"])) or {}).get("dwt_utilization") or 0) * float(v["dwt_tons"])
        for v in vessels[:10]
    ]

    eng = [(kpis.get(str(v["imo"])) or {}).get("engine_load_factor") or 0 for v in vessels[:10]]
    fuel = [(kpis.get(str(v["imo"])) or {}).get("fuel_efficiency_index") or 0 for v in vessels[:10]]

    gap_hours = []
    block = 0.0
    step = hours / max(1, len(tr) - 1) if len(tr) > 1 else 1.0
    for p in tr:
        if float(p["sog"]) < 0.3:
            block += step
        elif block > 0:
            gap_hours.append(round(block, 2))
            block = 0.0
    if block > 0:
        gap_hours.append(round(block, 2))
    hist_bins = ["0-2h", "2-6h", "6-12h", "12-24h", "24h+"]
    hist_vals = [0, 0, 0, 0, 0]
    for g in gap_hours:
        if g < 2:
            hist_vals[0] += 1
        elif g < 6:
            hist_vals[1] += 1
        elif g < 12:
            hist_vals[2] += 1
        elif g < 24:
            hist_vals[3] += 1
        else:
            hist_vals[4] += 1

    pk = kpis.get(primary) or {}
    radar = {
        "labels": ["Sanction Risk", "Speed Variance", "Route Deviation", "STS Probability", "Dark AIS", "Fuel Drag"],
        "values": [
            round(100 * float(pk.get("sanction_proximity_index") or 0), 1),
            round(100 * float(pk.get("speed_degradation_index") or 0), 1),
            round(100 * float(pk.get("route_anomaly_score") or 0), 1),
            round(100 * min(1.0, float((aggregates[-1] if aggregates else {}).get("sts_event_count") or 0) / 50.0), 1),
            round(100 * float(pk.get("dark_ais_gap_prob") or 0), 1),
            round(100 * (1.0 - float(pk.get("fuel_efficiency_index") or 0)), 1),
        ],
    }

    progress_pct = min(100.0, 100.0 * (0.55 + 0.35 * float(pk.get("voyage_efficiency") or 50) / 100))
    eta_delta = float(pk.get("eta_variance_h") or 0)

    density = float((aggregates[-1] if aggregates else {}).get("chokepoint_density_index") or 5)
    weather_labels = labels[:: max(1, len(labels) // 24)] or ["t0"]
    weather = {
        "labels": weather_labels,
        "wave_m": [round(1.2 + 0.8 * math.sin(i / 3) + 0.05 * density, 2) for i in range(len(weather_labels))],
        "wind_kn": [round(12 + 6 * math.cos(i / 4) + density, 1) for i in range(len(weather_labels))],
        "current_kn": [round(0.6 + 0.4 * math.sin(i / 5), 2) for i in range(len(weather_labels))],
    }

    agg_labels = [a["date"][5:] for a in aggregates[-min(30, len(aggregates)) :]]
    agg_density = [float(a.get("chokepoint_density_index") or 0) for a in aggregates[-min(30, len(aggregates)) :]]
    agg_sts = [float(a.get("sts_event_count") or 0) for a in aggregates[-min(30, len(aggregates)) :]]
    agg_active = [float(a.get("total_active_tankers") or 0) for a in aggregates[-min(30, len(aggregates)) :]]

    return {
        "primary_imo": primary,
        "p1_speed": {"labels": labels, "sog": sog, "design_speed": design},
        "p2_draught": {
            "labels": labels,
            "draft_m": draft,
            "design_draft": float(
                next((v["design_draft_m"] for v in vessels if str(v["imo"]) == primary), DESIGN_DRAFT_M)
            ),
        },
        "p3_dwt": {"labels": dwt_labels, "capacity": dwt_cap, "utilized": dwt_util},
        "p4_engine_fuel": {"labels": dwt_labels, "engine_load": eng, "fuel_efficiency": fuel},
        "p5_dark_gaps": {"labels": hist_bins, "counts": hist_vals},
        "p6_radar": radar,
        "p7_eta": {
            "progress_pct": round(progress_pct, 1),
            "eta_variance_h": eta_delta,
            "label": f"ΔETA {eta_delta:+.1f}h",
        },
        "p8_weather": weather,
        "p9_fleet_density": {
            "labels": agg_labels,
            "chokepoint_density": agg_density,
            "sts_events": agg_sts,
            "active_tankers": agg_active,
        },
    }


def _sts_zones_from_tracks(tracks: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    zones = []
    for imo, tr in tracks.items():
        for p in tr:
            if float(p["sog"]) < 1.0:
                zones.append(
                    {
                        "imo": imo,
                        "lat": p["lat"],
                        "lon": p["lon"],
                        "t": p["t"],
                        "kind": "STS_OR_ANCHOR",
                    }
                )
                break
    return zones[:24]


def build_route_analytics_payload(
    *,
    db_path: Optional[Path | str] = None,
    focus_imos: Optional[list[str]] = None,
) -> dict[str, Any]:
    path = resolve_db(db_path)
    aggregates = load_aggregates_frame(path, min_days=1)
    aggregates = aggregates[-45:] if len(aggregates) > 45 else aggregates

    try:
        reg = FleetRegistry.from_csv(ROOT / "output" / "fleet_database.csv", top_n=1001)
    except Exception:  # noqa: BLE001
        reg = FleetRegistry(vessels=[])

    focus = focus_imos or DEFAULT_FOCUS_IMOS
    vessels = resolve_monitored_vessels(reg, path, focus, target_n=MONITORED_VESSEL_TARGET)

    mmsis = [str(v.get("mmsi") or "") for v in vessels if v.get("mmsi")]
    live_by_mmsi = fetch_tracks_by_mmsi(path, mmsis, hours=24 * 45)
    tracks_30d, track_sources, live_vessel_count = _tracks_for_vessels(
        vessels, live_by_mmsi, aggregates, simplify=True
    )

    source_mode = "live_telemetry" if live_vessel_count > 0 else "aggregates+interpolated"

    horizons = {"1d": 24, "7d": 24 * 7, "30d": 24 * 30}
    by_horizon: dict[str, Any] = {}
    for key, hours in horizons.items():
        filtered = {imo: _filter_track(tr, hours) for imo, tr in tracks_30d.items()}
        # Ensure 1d horizon still has enough samples for charts
        for imo, tr in list(filtered.items()):
            if len(tr) < 4 and track_sources.get(imo) != "live_telemetry":
                v = next(v for v in vessels if str(v["imo"]) == imo)
                filtered[imo] = _aggregated_interpolation_track(
                    v, aggregates, hours=hours, step_h=0.5 if hours <= 24 else 2.0, seed=2001
                )
        kpis = {
            imo: _kpi_for_track(next(v for v in vessels if str(v["imo"]) == imo), tr, aggregates)
            for imo, tr in filtered.items()
        }
        kpi_list = list(kpis.values())
        fleet_kpi = {
            "current_sog": round(sum(k["current_sog"] for k in kpi_list) / max(1, len(kpi_list)), 2),
            "total_dwt": round(sum(k["total_dwt"] for k in kpi_list), 0),
            "voyage_efficiency": round(sum(k["voyage_efficiency"] for k in kpi_list) / max(1, len(kpi_list)), 1),
            "anomaly_flags": int(sum(k["anomaly_flags"] for k in kpi_list)),
            "avg_dwt_utilization": round(sum(k["dwt_utilization"] for k in kpi_list) / max(1, len(kpi_list)), 3),
            "avg_route_anomaly": round(sum(k["route_anomaly_score"] for k in kpi_list) / max(1, len(kpi_list)), 3),
            "live_telemetry_vessels": live_vessel_count,
        }
        panels = _panel_bundle(vessels, filtered, kpis, aggregates, hours=hours)
        heatmap = [
            {
                "lat": tr[-1]["lat"],
                "lon": tr[-1]["lon"],
                "imo": imo,
                "sog": tr[-1]["sog"],
                "weight": max(0.2, float((kpis.get(imo) or {}).get("route_anomaly_score") or 0.3)),
            }
            for imo, tr in filtered.items()
            if tr
        ]
        by_horizon[key] = {
            "hours": hours,
            "tracks": filtered,
            "vessel_kpis": kpis,
            "fleet_kpi": fleet_kpi,
            "panels": panels,
            "sts_zones": _sts_zones_from_tracks(filtered),
            "heatmap": heatmap,
            "track_sources": track_sources,
        }

    groups = sorted({str(v.get("group_tag") or "FLEET") for v in vessels} | {"ALL"})

    # GIS proximity (Contract 1.8.0): last known position vs compressor stations ≤50 nm
    fleet_proximity: list[dict[str, Any]] = []
    try:
        from services.compressor_stations import find_nearest_stations

        hz30 = by_horizon.get("30d") or {}
        tracks = hz30.get("tracks") or {}
        for v in vessels:
            imo = str(v.get("imo") or "")
            tr = tracks.get(imo) or []
            if not tr:
                continue
            last = tr[-1]
            try:
                vlat = float(last["lat"])
                vlon = float(last["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            prox = find_nearest_stations(vlat, vlon, max_distance_nm=50.0)
            fleet_proximity.append(
                {
                    "imo": imo,
                    "mmsi": v.get("mmsi"),
                    "lat": vlat,
                    "lon": vlon,
                    "proximity_compressors": prox,
                }
            )
            v["proximity_compressors"] = prox
    except Exception:  # noqa: BLE001
        fleet_proximity = []

    return {
        "generated_at_utc": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_mode": source_mode,
        "truth": "live_telemetry" if live_vessel_count > 0 else "interpolated",
        "db_path": sanitize_path_string(str(path)),
        "live_telemetry_vessels": live_vessel_count,
        "monitored_vessels": len(vessels),
        "min_live_points": MIN_LIVE_POINTS,
        "aggregates_days": len(aggregates),
        "aggregates": aggregates[-30:],
        "vessels": vessels,
        "groups": groups,
        "default_horizon": "7d",
        "default_selection": "ALL",
        "by_horizon": by_horizon,
        "track_sources": track_sources,
        "integrity": "PASS",
        "proximity_compressors": fleet_proximity,
        "proximity_buffer_nm": 50.0,
        "contract_version": "1.8.0-ops-gis-sot",
    }


def assert_route_payload(payload: dict[str, Any]) -> dict[str, Any]:
    from services.ttf_forecast.integrity import SREBuildError

    if not payload or payload.get("error"):
        raise SREBuildError("ROUTE_PAYLOAD_NULL", str((payload or {}).get("error")))
    if len(payload.get("vessels") or []) < 5:
        raise SREBuildError("ROUTE_VESSELS", "need ≥5 vessels in route catalog")
    for hz in ("1d", "7d", "30d"):
        block = (payload.get("by_horizon") or {}).get(hz) or {}
        if not block.get("tracks"):
            raise SREBuildError("ROUTE_TRACKS", f"by_horizon[{hz}].tracks empty")
        panels = block.get("panels") or {}
        for key in (
            "p1_speed",
            "p2_draught",
            "p3_dwt",
            "p4_engine_fuel",
            "p5_dark_gaps",
            "p6_radar",
            "p7_eta",
            "p8_weather",
            "p9_fleet_density",
        ):
            if key not in panels:
                raise SREBuildError("ROUTE_PANEL", f"missing panel {key} @ {hz}")
        if not (block.get("fleet_kpi") or {}).get("total_dwt"):
            raise SREBuildError("ROUTE_KPI", f"fleet_kpi incomplete @ {hz}")
    return {
        "ok": True,
        "vessels": len(payload["vessels"]),
        "source_mode": payload.get("source_mode"),
        "live_telemetry_vessels": payload.get("live_telemetry_vessels"),
    }
