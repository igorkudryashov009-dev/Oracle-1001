#!/usr/bin/env python3
"""Server-side map tile proxy — key never leaves the process.

Route contract (served by scripts/serve_dashboard.py):
  GET /api/tiles/{provider}/{z}/{x}/{y}.png

Security:
  - provider whitelist only (no client-supplied upstream URL → no SSRF relay)
  - Origin/Referer allow-list (loopback exempt for ops curl)
  - per-IP rate limit

Resilience (Never-Black for maps):
  - disk cache under output/tile_cache/
  - paid upstream only when MAPTILES_PROVIDER_KEY is set
  - on paid miss/error/budget → Esri World Dark Gray fallback (public, no key)
  - cache hits never burn paid budget
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.maptiles_proxy")

CACHE_ROOT = ROOT / "output" / "tile_cache"
CACHE_TTL_SEC = int(os.getenv("MAPTILES_CACHE_TTL_SEC", str(14 * 24 * 3600)))  # 14d
FALLBACK_CACHE_TTL_SEC = int(os.getenv("MAPTILES_FALLBACK_CACHE_TTL_SEC", str(24 * 3600)))
UPSTREAM_TIMEOUT_SEC = float(os.getenv("MAPTILES_UPSTREAM_TIMEOUT_SEC", "8"))

# Leaflet z/x/y → Esri MapServer uses z/y/x
ESRI_DARK_GRAY = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
    "World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"
)

PROVIDERS: dict[str, dict[str, Any]] = {
    "mapbox": {
        "paid": True,
        # Override with MAPTILES_MAPBOX_STYLE_URL (must contain {z}/{x}/{y})
        "url_env": "MAPTILES_MAPBOX_STYLE_URL",
        "default_url": (
            "https://api.mapbox.com/styles/v1/mapbox/dark-v11/tiles/256/{z}/{x}/{y}"
            "?access_token={KEY}"
        ),
        "content_type": "image/png",
        "ext": "png",
    },
    "carto": {
        "paid": True,
        "url_env": "MAPTILES_CARTO_STYLE_URL",
        # Placeholder template — requires MAPTILES_CARTO_STYLE_URL when using a keyed plan.
        "default_url": (
            "https://{user}.carto.com/api/v1/map/named/{tpl}/tile/{z}/{x}/{y}.png?api_key={KEY}"
        ),
        "content_type": "image/png",
        "ext": "png",
    },
    "esri": {
        "paid": False,
        "url_env": "",
        "default_url": ESRI_DARK_GRAY,
        "content_type": "image/jpeg",
        "ext": "jpg",
    },
}

_PATH_RE = re.compile(
    r"^/api/tiles/(?P<provider>[a-z0-9_-]+)/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.(?P<ext>png|jpg|jpeg)$",
    re.IGNORECASE,
)

_rate_lock = threading.Lock()
_rate_buckets: dict[str, list[float]] = {}


@dataclass
class TileResult:
    body: bytes
    content_type: str
    source: str  # cache | paid | esri_fallback | esri
    cache_hit: bool
    status: int = 200
    error_code: str | None = None


def provider_key() -> str:
    return (os.getenv("MAPTILES_PROVIDER_KEY") or os.getenv("MAPBOX_ACCESS_TOKEN") or "").strip()


def default_provider() -> str:
    p = (os.getenv("MAPTILES_PROVIDER") or "mapbox").strip().lower()
    return p if p in PROVIDERS else "mapbox"


def parse_tile_path(path: str) -> Optional[dict[str, Any]]:
    m = _PATH_RE.match(path or "")
    if not m:
        return None
    provider = m.group("provider").lower()
    if provider not in PROVIDERS:
        return None
    z, x, y = int(m.group("z")), int(m.group("x")), int(m.group("y"))
    if z < 0 or z > 22 or x < 0 or y < 0:
        return None
    max_idx = (1 << z) if z < 31 else 0
    if max_idx and (x >= max_idx or y >= max_idx):
        return None
    return {"provider": provider, "z": z, "x": x, "y": y, "ext": m.group("ext").lower()}


def _allowed_origins() -> list[str]:
    raw = (os.getenv("MAPTILES_ALLOWED_ORIGINS") or "").strip()
    defaults = [
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://45.8.230.214:8765",
        "https://45.8.230.214",
    ]
    public = (os.getenv("SENTINEL_PUBLIC_ORIGIN") or "").strip().rstrip("/")
    if public:
        defaults.append(public)
    extras = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    return list(dict.fromkeys(defaults + extras))


def origin_allowed(*, origin: str, referer: str, client_ip: str) -> bool:
    """Basic hygiene — not a hard security boundary."""
    if (os.getenv("MAPTILES_RELAX_ORIGIN") or "").strip() in {"1", "true", "yes"}:
        return True
    if client_ip in {"127.0.0.1", "::1", "localhost"}:
        return True
    allowed = _allowed_origins()
    o = (origin or "").strip().rstrip("/")
    if o and o in allowed:
        return True
    ref = (referer or "").strip()
    if ref:
        try:
            p = urlparse(ref)
            base = f"{p.scheme}://{p.netloc}".rstrip("/")
            if base in allowed:
                return True
        except Exception:  # noqa: BLE001
            pass
    # No Origin/Referer from non-loopback → reject (blocks casual hotlink abuse)
    if not o and not ref:
        return False
    return False


def rate_limit_ok(client_ip: str) -> bool:
    """Token-ish sliding window: MAPTILES_RATE_PER_SEC (default 40) over 1s."""
    try:
        limit = max(5, int(os.getenv("MAPTILES_RATE_PER_SEC", "40")))
    except ValueError:
        limit = 40
    now = time.monotonic()
    ip = client_ip or "unknown"
    with _rate_lock:
        bucket = _rate_buckets.setdefault(ip, [])
        cutoff = now - 1.0
        bucket[:] = [t for t in bucket if t >= cutoff]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        # prune idle IPs occasionally
        if len(_rate_buckets) > 2000:
            dead = [k for k, v in _rate_buckets.items() if not v or v[-1] < now - 60]
            for k in dead[:500]:
                _rate_buckets.pop(k, None)
        return True


def _cache_paths(provider: str, z: int, x: int, y: int, ext: str) -> tuple[Path, Path]:
    folder = CACHE_ROOT / provider / str(z) / str(x)
    body = folder / f"{y}.{ext}"
    meta = folder / f"{y}.meta.json"
    return body, meta


def _read_cache(provider: str, z: int, x: int, y: int, ext: str) -> Optional[TileResult]:
    body_path, meta_path = _cache_paths(provider, z, x, y, ext)
    if not body_path.is_file():
        # try alternate ext from meta providers
        for alt in ("png", "jpg", "jpeg"):
            if alt == ext:
                continue
            bp, mp = _cache_paths(provider, z, x, y, alt)
            if bp.is_file():
                body_path, meta_path = bp, mp
                ext = alt
                break
        else:
            return None
    try:
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        saved_at = float(meta.get("saved_at_epoch") or body_path.stat().st_mtime)
        source = str(meta.get("source") or "cache")
        ttl = FALLBACK_CACHE_TTL_SEC if source.startswith("esri") else CACHE_TTL_SEC
        if time.time() - saved_at > ttl:
            return None
        data = body_path.read_bytes()
        if not data:
            return None
        ctype = str(meta.get("content_type") or ("image/jpeg" if ext in {"jpg", "jpeg"} else "image/png"))
        return TileResult(body=data, content_type=ctype, source=f"cache:{source}", cache_hit=True)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _write_cache(
    provider: str,
    z: int,
    x: int,
    y: int,
    *,
    body: bytes,
    content_type: str,
    source: str,
    ext: str,
) -> None:
    body_path, meta_path = _cache_paths(provider, z, x, y, ext)
    try:
        body_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = body_path.with_suffix(body_path.suffix + ".tmp")
        tmp.write_bytes(body)
        tmp.replace(body_path)
        meta = {
            "saved_at_epoch": time.time(),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": source,
            "content_type": content_type,
            "bytes": len(body),
            "provider": provider,
            "z": z,
            "x": x,
            "y": y,
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:
        LOG.warning("tile cache write failed %s/%s/%s/%s: %s", provider, z, x, y, exc)


def _build_paid_url(provider: str, z: int, x: int, y: int) -> Optional[str]:
    cfg = PROVIDERS[provider]
    if not cfg.get("paid"):
        return None
    key = provider_key()
    if not key:
        return None
    env_name = str(cfg.get("url_env") or "")
    template = (os.getenv(env_name) or "").strip() if env_name else ""
    if not template:
        template = str(cfg.get("default_url") or "")
    if "{z}" not in template or "{x}" not in template or "{y}" not in template:
        LOG.error("invalid tile template for provider=%s (missing z/x/y)", provider)
        return None
    if "{KEY}" not in template and "access_token=" not in template and "api_key=" not in template:
        # Append key safely for Mapbox-style URLs
        if provider == "mapbox":
            sep = "&" if "?" in template else "?"
            template = f"{template}{sep}access_token={{KEY}}"
        else:
            LOG.error("provider=%s template has no {KEY} placeholder", provider)
            return None
    # CARTO default may need user/tpl — require explicit env URL if placeholders remain
    if "{user}" in template or "{tpl}" in template:
        LOG.error("provider=carto requires MAPTILES_CARTO_STYLE_URL with concrete host/template")
        return None
    return (
        template.replace("{z}", str(z))
        .replace("{x}", str(x))
        .replace("{y}", str(y))
        .replace("{KEY}", key)
    )


def _fetch_bytes(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Oracle-1001-Sentinel-MapTiles/1.0",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_SEC) as resp:  # noqa: S310
        ctype = (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
        data = resp.read()
    if not data:
        raise RuntimeError("empty_tile_body")
    if "image/" not in ctype and not data.startswith(b"\x89PNG") and data[:2] != b"\xff\xd8":
        # Likely JSON error page from provider
        snippet = data[:120].decode("utf-8", errors="replace")
        raise RuntimeError(f"non_image_response ctype={ctype} body={snippet!r}")
    if "image/" not in ctype:
        ctype = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return data, ctype


def _fetch_esri(z: int, x: int, y: int) -> TileResult:
    url = ESRI_DARK_GRAY.format(z=z, x=x, y=y)
    data, ctype = _fetch_bytes(url)
    return TileResult(body=data, content_type=ctype, source="esri_fallback", cache_hit=False)


def fetch_tile(provider: str, z: int, x: int, y: int, *, prefer_ext: str = "png") -> TileResult:
    """Resolve tile with cache → paid → Esri fallback. Never returns a key in errors."""
    from services.maptiles_budget import (
        MaptilesBudgetExhausted,
        record_result,
        reserve_or_raise,
    )

    cached = _read_cache(provider, z, x, y, prefer_ext)
    if cached:
        LOG.info("tile cache_hit provider=%s z=%s x=%s y=%s source=%s", provider, z, x, y, cached.source)
        return cached

    # Direct Esri provider (no key, no budget)
    if provider == "esri" or not PROVIDERS[provider].get("paid"):
        try:
            result = _fetch_esri(z, x, y)
            result = TileResult(
                body=result.body,
                content_type=result.content_type,
                source="esri",
                cache_hit=False,
            )
            _write_cache(
                provider, z, x, y,
                body=result.body,
                content_type=result.content_type,
                source="esri",
                ext="jpg" if "jpeg" in result.content_type else "png",
            )
            LOG.info("tile esri_ok z=%s x=%s y=%s", z, x, y)
            return result
        except Exception as exc:  # noqa: BLE001
            LOG.error("tile esri_fail z=%s x=%s y=%s err=%s", z, x, y, type(exc).__name__)
            return TileResult(
                body=b"",
                content_type="application/json",
                source="error",
                cache_hit=False,
                status=502,
                error_code="esri_upstream_failed",
            )

    key = provider_key()
    if not key:
        LOG.warning(
            "tile paid_key_missing provider=%s → esri_fallback z=%s x=%s y=%s",
            provider, z, x, y,
        )
        try:
            result = _fetch_esri(z, x, y)
            _write_cache(
                provider, z, x, y,
                body=result.body,
                content_type=result.content_type,
                source="esri_fallback_no_key",
                ext="jpg" if "jpeg" in result.content_type else "png",
            )
            return TileResult(
                body=result.body,
                content_type=result.content_type,
                source="esri_fallback_no_key",
                cache_hit=False,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.error("tile fallback_fail (no key) err=%s", type(exc).__name__)
            return TileResult(
                body=b"",
                content_type="application/json",
                source="error",
                cache_hit=False,
                status=503,
                error_code="maptiles_key_missing_and_esri_failed",
            )

    paid_url = _build_paid_url(provider, z, x, y)
    if not paid_url:
        LOG.warning("tile paid_url_unconfigured provider=%s → esri_fallback", provider)
        result = _fetch_esri(z, x, y)
        _write_cache(
            provider, z, x, y,
            body=result.body,
            content_type=result.content_type,
            source="esri_fallback_unconfigured",
            ext="jpg" if "jpeg" in result.content_type else "png",
        )
        return TileResult(
            body=result.body,
            content_type=result.content_type,
            source="esri_fallback_unconfigured",
            cache_hit=False,
        )

    # Budget gate before paid upstream
    try:
        reserve_or_raise(1)
    except MaptilesBudgetExhausted:
        LOG.warning("tile budget_exhausted provider=%s → esri_fallback z=%s/%s/%s", provider, z, x, y)
        result = _fetch_esri(z, x, y)
        _write_cache(
            provider, z, x, y,
            body=result.body,
            content_type=result.content_type,
            source="esri_fallback_budget",
            ext="jpg" if "jpeg" in result.content_type else "png",
        )
        return TileResult(
            body=result.body,
            content_type=result.content_type,
            source="esri_fallback_budget",
            cache_hit=False,
        )

    try:
        data, ctype = _fetch_bytes(paid_url)
        record_result(provider=provider, ok=True, detail=f"{z}/{x}/{y}")
        ext = "png" if "png" in ctype or data.startswith(b"\x89PNG") else "jpg"
        _write_cache(
            provider, z, x, y,
            body=data,
            content_type=ctype,
            source="paid",
            ext=ext,
        )
        LOG.info("tile paid_ok provider=%s z=%s x=%s y=%s bytes=%s", provider, z, x, y, len(data))
        return TileResult(body=data, content_type=ctype, source="paid", cache_hit=False)
    except urllib.error.HTTPError as exc:
        # Do not leak provider response body (may include account hints)
        record_result(provider=provider, ok=False, detail=f"http_{exc.code}", refund=True)
        LOG.error(
            "tile paid_http_error provider=%s code=%s z=%s/%s/%s → esri_fallback",
            provider, exc.code, z, x, y,
        )
    except Exception as exc:  # noqa: BLE001
        record_result(provider=provider, ok=False, detail=type(exc).__name__, refund=True)
        LOG.error(
            "tile paid_error provider=%s err=%s z=%s/%s/%s → esri_fallback",
            provider, type(exc).__name__, z, x, y,
        )

    try:
        result = _fetch_esri(z, x, y)
        _write_cache(
            provider, z, x, y,
            body=result.body,
            content_type=result.content_type,
            source="esri_fallback_paid_error",
            ext="jpg" if "jpeg" in result.content_type else "png",
        )
        LOG.warning(
            "tile esri_fallback_engaged provider=%s z=%s x=%s y=%s",
            provider, z, x, y,
        )
        return TileResult(
            body=result.body,
            content_type=result.content_type,
            source="esri_fallback_paid_error",
            cache_hit=False,
        )
    except Exception as exc:  # noqa: BLE001
        LOG.error("tile total_fail provider=%s err=%s", provider, type(exc).__name__)
        return TileResult(
            body=b"",
            content_type="application/json",
            source="error",
            cache_hit=False,
            status=502,
            error_code="upstream_and_esri_failed",
        )


def public_status() -> dict[str, Any]:
    from services.maptiles_budget import get_budget_status

    key = provider_key()
    return {
        "provider_default": default_provider(),
        "providers_whitelist": sorted(PROVIDERS.keys()),
        "key_configured": bool(key),
        "key_preview": None,  # never expose
        "cache_root": str(CACHE_ROOT.relative_to(ROOT)).replace("\\", "/"),
        "cache_ttl_sec": CACHE_TTL_SEC,
        "fallback": "esri_world_dark_gray",
        "budget": get_budget_status(),
    }
