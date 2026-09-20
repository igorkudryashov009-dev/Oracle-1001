"""NASA FIRMS thermal anomalies near compressor stations — Contract 1.8.0-ops-gis-sot."""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_keys import CONTRACT_VERSION, get_key
from services.spatial_index import get_spatial_index

LOG = logging.getLogger("sentinel.firms_service")
ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "output" / "cache" / "firms_anomalies.json"

# VIIRS SNPP NRT — MAP_KEY area CSV API
FIRMS_AREA_TMPL = (
    "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
    "{key}/VIIRS_SNPP_NRT/{west},{south},{east},{north}/{days}"
)
DEFAULT_BUFFER_NM = 50.0
NM_PER_DEG_LAT = 60.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r_km * c * 0.539957


def _station_bbox_pad(pad_deg: float = 2.0) -> tuple[float, float, float, float]:
    idx = get_spatial_index()
    lons: list[float] = []
    lats: list[float] = []
    for st in idx.stations():
        lons.extend([st.lon_min, st.lon_max])
        lats.extend([st.lat_min, st.lat_max])
    if not lons:
        return (-20.0, 20.0, 160.0, 75.0)
    return (
        max(-180.0, min(lons) - pad_deg),
        max(-90.0, min(lats) - pad_deg),
        min(180.0, max(lons) + pad_deg),
        min(90.0, max(lats) + pad_deg),
    )


def _load_cache() -> dict[str, Any] | None:
    if not CACHE_PATH.is_file():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _save_cache(payload: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("firms cache write failed: %s", exc)


def _fetch_firms_csv(*, days: int = 1) -> list[dict[str, Any]]:
    key = get_key("FIRMS_MAP_KEY")
    if not key:
        return []
    west, south, east, north = _station_bbox_pad()
    url = FIRMS_AREA_TMPL.format(
        key=key,
        west=f"{west:.3f}",
        south=f"{south:.3f}",
        east=f"{east:.3f}",
        north=f"{north:.3f}",
        days=max(1, min(int(days), 10)),
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Oracle-1001-Sentinel/1.8.0", "Accept": "text/csv"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    if "Invalid" in text[:200] or text.strip().startswith("<"):
        raise RuntimeError(f"FIRMS upstream error: {text[:160]}")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, Any]] = []
    for row in reader:
        try:
            lat = float(row.get("latitude") or row.get("Latitude") or "")
            lon = float(row.get("longitude") or row.get("Longitude") or "")
        except ValueError:
            continue
        rows.append(
            {
                "lat": lat,
                "lon": lon,
                "brightness": row.get("bright_ti4") or row.get("brightness"),
                "frp": row.get("frp"),
                "acq_date": row.get("acq_date"),
                "acq_time": row.get("acq_time"),
                "satellite": row.get("satellite") or "VIIRS",
                "confidence": row.get("confidence"),
            }
        )
    return rows


def _attach_proximity(
    fires: list[dict[str, Any]],
    *,
    max_distance_nm: float,
) -> list[dict[str, Any]]:
    idx = get_spatial_index()
    # Degree window for candidate prune
    dlat = max_distance_nm / NM_PER_DEG_LAT
    out: list[dict[str, Any]] = []
    for fire in fires:
        lat, lon = fire["lat"], fire["lon"]
        cos_lat = max(0.05, abs(math.cos(math.radians(lat))))
        dlon = dlat / cos_lat
        candidates = idx.query_bbox(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        nearest = None
        nearest_nm = None
        for st in candidates:
            clat, clon = st.centroid
            dist = _haversine_nm(lat, lon, clat, clon)
            if dist <= max_distance_nm and (nearest_nm is None or dist < nearest_nm):
                nearest_nm = dist
                nearest = st
        if nearest is None:
            continue
        out.append(
            {
                **fire,
                "nearest_station": nearest.name,
                "nearest_cluster": nearest.cluster,
                "distance_nm": round(float(nearest_nm or 0.0), 2),
            }
        )
    out.sort(key=lambda r: r.get("distance_nm", 1e9))
    return out


def fetch_firms_anomalies(
    *,
    days: int = 1,
    max_distance_nm: float = DEFAULT_BUFFER_NM,
    use_cache: bool = True,
) -> dict[str, Any]:
    """GeoJSON FeatureCollection of FIRMS hits within ``max_distance_nm`` of CS (SWR)."""

    def _fresh() -> dict[str, Any]:
        fires = _fetch_firms_csv(days=days)
        near = _attach_proximity(fires, max_distance_nm=max_distance_nm)
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {k: v for k, v in r.items() if k not in ("lat", "lon")},
            }
            for r in near
        ]
        return {
            "ok": True,
            "contract_version": CONTRACT_VERSION,
            "fetched_at": _now_iso(),
            "type": "FeatureCollection",
            "features": features,
            "count": len(features),
            "raw_fire_count": len(fires),
            "proximity_buffer_nm": max_distance_nm,
            "is_cached": False,
            "errors": [],
        }

    if not use_cache:
        try:
            return _fresh()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": True,
                "contract_version": CONTRACT_VERSION,
                "fetched_at": _now_iso(),
                "type": "FeatureCollection",
                "features": [],
                "count": 0,
                "raw_fire_count": 0,
                "proximity_buffer_nm": max_distance_nm,
                "is_cached": False,
                "errors": [str(exc)[:240]],
            }

    from services.cache_swr import swr_fetch

    try:
        return swr_fetch(
            cache_path=CACHE_PATH,
            fresh_fetch=_fresh,
            cache_key=f"firms_{days}_{max_distance_nm}",
        )
    except Exception as exc:  # noqa: BLE001
        cached = _load_cache()
        if cached:
            cached = dict(cached)
            cached["ok"] = True
            cached["is_cached"] = True
            cached["cache_fallback"] = True
            cached["errors"] = list(cached.get("errors") or []) + [str(exc)[:160]]
            cached["contract_version"] = CONTRACT_VERSION
            return cached
        return {
            "ok": True,
            "contract_version": CONTRACT_VERSION,
            "fetched_at": _now_iso(),
            "type": "FeatureCollection",
            "features": [],
            "count": 0,
            "raw_fire_count": 0,
            "proximity_buffer_nm": max_distance_nm,
            "is_cached": False,
            "errors": [str(exc)[:240]],
            "note": "Configure FIRMS_MAP_KEY in .env",
        }


__all__ = ("fetch_firms_anomalies", "DEFAULT_BUFFER_NM", "CACHE_PATH")
