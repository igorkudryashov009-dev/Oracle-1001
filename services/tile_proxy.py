#!/usr/bin/env python3
"""Zero-cost GIS tile proxy — Contract 1.8.0-ops-gis-sot.

Route (edge ``serve_dashboard`` + optional FastAPI):
  GET /api/v1/gis/tiles/{provider}/{z}/{x}/{y}.png

Providers: maptiler | mapbox | esri_satellite | osm_standard

Rules:
  - Keys never leave the process (MAPTILES_KEY / MAPBOX_TOKEN / legacy aliases).
  - Missing key → Esri World Imagery (no exception).
  - Disk cache under data/tile_cache (30d TTL); X-Cache HIT|MISS.
  - Monthly SQLite counters: maptiler hard-stop ≥90k, mapbox ≥40k → force free fallback.
  - Cache hits never increment paid counters.

Sync implementation (compatible with BaseHTTPRequestHandler; no aiofiles/httpx).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.tile_proxy")

CONTRACT_VERSION = "1.8.0-ops-gis-sot"

CACHE_ROOT = Path(
    os.getenv("SENTINEL_TILE_CACHE_DIR")
    or os.getenv("TILE_CACHE_DIR")
    or (ROOT / "data" / "tile_cache")
)
CACHE_TTL_SEC = int(os.getenv("GIS_TILE_CACHE_TTL_SEC", str(30 * 24 * 3600)))  # 30d
UPSTREAM_TIMEOUT_SEC = float(os.getenv("GIS_TILE_UPSTREAM_TIMEOUT_SEC", "8"))
USAGE_DB = Path(
    os.getenv("SENTINEL_TILE_USAGE_DB") or (ROOT / "data" / "archive" / "tile_usage_counter.sqlite")
)

# Free-tier hard stops (requests / calendar month UTC)
MAPTILER_HARD_STOP = int(os.getenv("MAPTILER_MONTHLY_HARD_STOP", "90000"))
MAPBOX_HARD_STOP = int(os.getenv("MAPBOX_MONTHLY_HARD_STOP", "40000"))

USER_AGENT = "Sentinel-GIS-Proxy/1.8.0 (Oracle-1001; contact=ops)"

_PATH_RE = re.compile(
    r"^/api/v1/gis/tiles/(?P<provider>[a-z0-9_-]+)/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\."
    r"(?P<ext>png|jpg|jpeg|pbf)$",
    re.IGNORECASE,
)

_LOCK = threading.RLock()
_DB_INIT = False


def maptiler_key() -> str:
    return (
        os.getenv("MAPTILES_KEY")
        or os.getenv("MAPTILER_KEY")
        or os.getenv("MAPTILES_PROVIDER_KEY")
        or ""
    ).strip()


def mapbox_token() -> str:
    return (
        os.getenv("MAPBOX_TOKEN")
        or os.getenv("MAPBOX_ACCESS_TOKEN")
        or os.getenv("MAPTILES_PROVIDER_KEY")
        or ""
    ).strip()


PROVIDERS: dict[str, dict[str, Any]] = {
    "maptiler": {
        "paid": True,
        "content_type": "image/jpeg",
        "ext": "jpg",
        "url": (
            "https://api.maptiler.com/maps/satellite/{z}/{x}/{y}.jpg?key={KEY}"
        ),
    },
    "mapbox": {
        "paid": True,
        "content_type": "image/jpeg",
        "ext": "jpg",
        "url": (
            "https://api.mapbox.com/styles/v1/mapbox/satellite-v9/tiles/256/{z}/{x}/{y}"
            "?access_token={KEY}"
        ),
    },
    "esri_satellite": {
        "paid": False,
        "content_type": "image/jpeg",
        "ext": "jpg",
        # Esri MapServer uses z/y/x
        "url": (
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
    },
    "osm_standard": {
        "paid": False,
        "content_type": "image/png",
        "ext": "png",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    },
}

FREE_FALLBACK_ORDER = ("esri_satellite", "osm_standard")


@dataclass
class GisTileResult:
    body: bytes
    content_type: str
    provider: str
    requested_provider: str
    cache_hit: bool
    status: int = 200
    error_code: str | None = None
    fallback_reason: str | None = None

    def headers(self) -> dict[str, str]:
        h = {
            "X-Cache": "HIT" if self.cache_hit else "MISS",
            "X-Contract-Version": CONTRACT_VERSION,
            "X-Tile-Provider": self.provider,
            "X-Tile-Requested-Provider": self.requested_provider,
            "Cache-Control": "public, max-age=86400",
        }
        if self.fallback_reason:
            h["X-Tile-Fallback-Reason"] = self.fallback_reason
        return h


def parse_gis_tile_path(path: str) -> Optional[dict[str, Any]]:
    m = _PATH_RE.match((path or "").split("?", 1)[0])
    if not m:
        return None
    provider = m.group("provider").lower()
    if provider not in PROVIDERS:
        return None
    z, x, y = int(m.group("z")), int(m.group("x")), int(m.group("y"))
    if z < 0 or z > 22 or x < 0 or y < 0:
        return None
    max_idx = 1 << z
    if x >= max_idx or y >= max_idx:
        return None
    ext = m.group("ext").lower()
    if ext == "pbf":
        # Raster-only proxy for v1.8.0 — vector tiles not wired
        return None
    return {"provider": provider, "z": z, "x": x, "y": y, "ext": ext}


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _ensure_db() -> None:
    global _DB_INIT
    with _LOCK:
        if _DB_INIT and USAGE_DB.is_file():
            return
        USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(USAGE_DB))
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS tile_usage_counter (
                    month TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (month, provider)
                )
                """
            )
            con.commit()
        finally:
            con.close()
        _DB_INIT = True


def get_usage(provider: str, *, month: Optional[str] = None) -> int:
    _ensure_db()
    m = month or _month_key()
    with _LOCK:
        con = sqlite3.connect(str(USAGE_DB))
        try:
            row = con.execute(
                "SELECT used FROM tile_usage_counter WHERE month=? AND provider=?",
                (m, provider),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()


def _increment_usage(provider: str) -> int:
    """Increment paid upstream counter; return new used count."""
    _ensure_db()
    m = _month_key()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _LOCK:
        con = sqlite3.connect(str(USAGE_DB))
        try:
            con.execute(
                """
                INSERT INTO tile_usage_counter(month, provider, used, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(month, provider) DO UPDATE SET
                    used = used + 1,
                    updated_at = excluded.updated_at
                """,
                (m, provider, now),
            )
            con.commit()
            row = con.execute(
                "SELECT used FROM tile_usage_counter WHERE month=? AND provider=?",
                (m, provider),
            ).fetchone()
            return int(row[0]) if row else 1
        finally:
            con.close()


def usage_snapshot() -> dict[str, Any]:
    _ensure_db()
    m = _month_key()
    return {
        "month": m,
        "maptiler_used": get_usage("maptiler", month=m),
        "mapbox_used": get_usage("mapbox", month=m),
        "maptiler_hard_stop": MAPTILER_HARD_STOP,
        "mapbox_hard_stop": MAPBOX_HARD_STOP,
        "contract_version": CONTRACT_VERSION,
    }


def _hard_stop_exceeded(provider: str) -> bool:
    used = get_usage(provider)
    if provider == "maptiler":
        return used >= MAPTILER_HARD_STOP
    if provider == "mapbox":
        return used >= MAPBOX_HARD_STOP
    return False


def resolve_provider(requested: str) -> tuple[str, Optional[str]]:
    """Return (actual_provider, fallback_reason). Never raises."""
    p = (requested or "").strip().lower()
    if p not in PROVIDERS:
        return "esri_satellite", "unknown_provider"
    if p == "maptiler":
        if not maptiler_key():
            return "esri_satellite", "maptiler_key_missing"
        if _hard_stop_exceeded("maptiler"):
            return "esri_satellite", "maptiler_hard_stop"
        return "maptiler", None
    if p == "mapbox":
        if not mapbox_token():
            return "esri_satellite", "mapbox_token_missing"
        if _hard_stop_exceeded("mapbox"):
            return "esri_satellite", "mapbox_hard_stop"
        return "mapbox", None
    return p, None


def _cache_path(provider: str, z: int, x: int, y: int, ext: str) -> Path:
    return CACHE_ROOT / provider / str(z) / str(x) / f"{y}.{ext}"


def _read_cache(provider: str, z: int, x: int, y: int) -> Optional[tuple[bytes, str, str]]:
    cfg = PROVIDERS[provider]
    preferred = str(cfg.get("ext") or "png")
    for ext in (preferred, "png", "jpg", "jpeg", "img"):
        path = _cache_path(provider, z, x, y, ext)
        if not path.is_file():
            continue
        try:
            age = time.time() - path.stat().st_mtime
            if age > CACHE_TTL_SEC:
                continue
            data = path.read_bytes()
            if not data:
                continue
            ctype = "image/png" if ext == "png" else str(cfg.get("content_type") or "image/jpeg")
            return data, ctype, ext
        except OSError:
            continue
    return None


def _write_cache(provider: str, z: int, x: int, y: int, body: bytes, ext: str) -> None:
    path = _cache_path(provider, z, x, y, ext)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)
        meta = path.with_suffix(path.suffix + ".meta.json")
        meta.write_text(
            json.dumps(
                {
                    "saved_at_epoch": time.time(),
                    "provider": provider,
                    "z": z,
                    "x": x,
                    "y": y,
                    "bytes": len(body),
                    "contract_version": CONTRACT_VERSION,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        LOG.warning("gis tile cache write failed: %s", exc)


def _build_url(provider: str, z: int, x: int, y: int) -> Optional[str]:
    cfg = PROVIDERS.get(provider) or PROVIDERS["esri_satellite"]
    template = str(cfg["url"])
    key = ""
    if provider == "maptiler":
        key = maptiler_key()
    elif provider == "mapbox":
        key = mapbox_token()
    if "{KEY}" in template and not key:
        return None
    return template.format(z=z, x=x, y=y, KEY=key)


def _http_get(url: str) -> tuple[int, bytes, str]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_SEC) as resp:
        ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        return int(resp.status), resp.read(), ctype


def fetch_gis_tile(
    *,
    provider: str,
    z: int,
    x: int,
    y: int,
) -> GisTileResult:
    """Fetch one tile with cache + free-tier hard-stop + Esri/OSM fallback."""
    requested = (provider or "esri_satellite").lower()
    actual, reason = resolve_provider(requested)

    cached = _read_cache(actual, z, x, y)
    if cached is not None:
        body, ctype, _ext = cached
        return GisTileResult(
            body=body,
            content_type=ctype,
            provider=actual,
            requested_provider=requested,
            cache_hit=True,
            fallback_reason=reason,
        )

    # Upstream fetch with cascade: actual → esri → osm
    cascade = [actual]
    for fb in FREE_FALLBACK_ORDER:
        if fb not in cascade:
            cascade.append(fb)

    last_err: str | None = None
    for cand in cascade:
        url = _build_url(cand, z, x, y)
        if not url:
            last_err = f"{cand}_url_unavailable"
            continue
        try:
            status, body, ctype = _http_get(url)
            if status != 200 or not body:
                last_err = f"{cand}_http_{status}"
                continue
            cfg = PROVIDERS[cand]
            ext = str(cfg.get("ext") or "jpg")
            if "png" in ctype:
                ext = "png"
                ctype = "image/png"
            elif "jpeg" in ctype or "jpg" in ctype:
                ext = "jpg"
                ctype = "image/jpeg"
            # Paid counter only on real upstream miss for paid providers
            if cand in {"maptiler", "mapbox"} and PROVIDERS[cand].get("paid"):
                _increment_usage(cand)
            _write_cache(cand, z, x, y, body, ext)
            fb_reason = reason
            if cand != actual:
                fb_reason = fb_reason or f"upstream_fallback_to_{cand}"
            elif cand != requested:
                fb_reason = reason
            return GisTileResult(
                body=body,
                content_type=ctype,
                provider=cand,
                requested_provider=requested,
                cache_hit=False,
                fallback_reason=fb_reason,
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_err = f"{cand}_{exc.__class__.__name__}"
            LOG.info("gis tile upstream fail provider=%s: %s", cand, exc)
            continue

    return GisTileResult(
        body=b"",
        content_type="application/json",
        provider=actual,
        requested_provider=requested,
        cache_hit=False,
        status=502,
        error_code=last_err or "tile_unavailable",
        fallback_reason=reason,
    )


def public_status() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "cache_root": str(CACHE_ROOT).replace("\\", "/"),
        "cache_ttl_sec": CACHE_TTL_SEC,
        "providers": list(PROVIDERS.keys()),
        "maptiler_key_configured": bool(maptiler_key()),
        "mapbox_token_configured": bool(mapbox_token()),
        "usage": usage_snapshot(),
        "route": "/api/v1/gis/tiles/{provider}/{z}/{x}/{y}.png",
    }


# ---------------------------------------------------------------------------
# Optional FastAPI router (api_server mount). Edge uses sync fetch_gis_tile.
# ---------------------------------------------------------------------------
try:
    from fastapi import APIRouter, HTTPException, Response as FastAPIResponse

    router = APIRouter(prefix="/api/v1/gis/tiles", tags=["GIS Tiles"])

    @router.get("/status")
    async def gis_tiles_status() -> dict[str, Any]:
        return {"ok": True, **public_status()}

    @router.get("/{provider}/{z}/{x}/{y}.{ext}")
    async def gis_tile(
        provider: str, z: int, x: int, y: int, ext: str
    ) -> FastAPIResponse:
        provider_l = (provider or "").lower()
        ext_l = (ext or "").lower()
        if provider_l not in PROVIDERS:
            raise HTTPException(status_code=404, detail="unknown_provider")
        if ext_l == "pbf":
            raise HTTPException(status_code=501, detail="vector_pbf_not_wired")
        if ext_l not in {"png", "jpg", "jpeg"}:
            raise HTTPException(status_code=404, detail="unsupported_ext")
        result = fetch_gis_tile(provider=provider_l, z=z, x=x, y=y)
        if result.status != 200 or not result.body:
            return FastAPIResponse(
                content=json.dumps(
                    {
                        "ok": False,
                        "error": result.error_code or "tile_unavailable",
                        "contract_version": CONTRACT_VERSION,
                    }
                ),
                status_code=int(result.status or 502),
                media_type="application/json",
                headers={"X-Contract-Version": CONTRACT_VERSION},
            )
        return FastAPIResponse(
            content=result.body,
            status_code=200,
            media_type=result.content_type,
            headers=result.headers(),
        )

except ImportError:  # pragma: no cover — edge may run without FastAPI
    router = None  # type: ignore[assignment]


__all__ = (
    "CONTRACT_VERSION",
    "GisTileResult",
    "PROVIDERS",
    "parse_gis_tile_path",
    "fetch_gis_tile",
    "public_status",
    "usage_snapshot",
    "get_usage",
    "resolve_provider",
    "router",
)
