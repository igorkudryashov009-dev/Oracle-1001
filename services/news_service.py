"""News + GIE announcements adapter — Contract 1.8.0-ops-gis-sot.

Ingests NewsAPI and GIE AGSI headlines filtered to LNG / pipeline topics.
Falls back to a local JSON cache when upstream is offline or unkeyed.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_keys import CONTRACT_VERSION, get_key

LOG = logging.getLogger("sentinel.news_service")
ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "output" / "cache" / "news_latest.json"

TOPIC_TERMS = ("LNG", "Nord Stream", "Gas Pipeline", "Baltic", "TTF")
NEWSAPI_URL = "https://newsapi.org/v2/everything"
GIE_URL = "https://agsi.gie.eu/api"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 12.0,
) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Oracle-1001-Sentinel/1.8.0",
            "Accept": "application/json",
            **(headers or {}),
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _topic_query() -> str:
    parts = [f'"{t}"' if " " in t else t for t in TOPIC_TERMS]
    return " OR ".join(parts)


def _matches_topics(text: str) -> bool:
    up = (text or "").upper()
    return any(term.upper() in up for term in TOPIC_TERMS)


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
        LOG.warning("news cache write failed: %s", exc)


def _fetch_newsapi(limit: int = 20) -> list[dict[str, Any]]:
    key = get_key("NEWSAPI_KEY")
    if not key:
        return []
    q = urllib.parse.urlencode(
        {
            "q": _topic_query(),
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": str(min(limit, 50)),
            "apiKey": key,
        }
    )
    data = _http_json(f"{NEWSAPI_URL}?{q}")
    out: list[dict[str, Any]] = []
    for art in data.get("articles") or []:
        title = str(art.get("title") or "")
        desc = str(art.get("description") or "")
        if not _matches_topics(f"{title} {desc}"):
            continue
        out.append(
            {
                "source": "newsapi",
                "title": title,
                "description": desc,
                "url": art.get("url"),
                "published_at": art.get("publishedAt"),
                "provider": (art.get("source") or {}).get("name"),
            }
        )
    return out


def _fetch_gie() -> list[dict[str, Any]]:
    key = get_key("GIE_API_KEY")
    if not key:
        return []
    # EU aggregate snapshot — surface as storage announcement proxy.
    data = _http_json(GIE_URL, headers={"x-key": key})
    out: list[dict[str, Any]] = []
    if isinstance(data, dict):
        gas_day = data.get("gasDayStart") or data.get("gas_day") or ""
        info = data.get("info") or data.get("name") or "GIE AGSI+ EU storage"
        note = (
            f"GIE AGSI+ update gasDay={gas_day} · "
            f"full={data.get('full')}% · injection={data.get('injection')} · "
            f"withdrawal={data.get('withdrawal')}"
        )
        if _matches_topics(note) or True:
            out.append(
                {
                    "source": "gie_agsi",
                    "title": f"GIE AGSI+ · {info}",
                    "description": note,
                    "url": "https://agsi.gie.eu/",
                    "published_at": gas_day or _now_iso(),
                    "provider": "GIE",
                    "raw_metrics": {
                        "full": data.get("full"),
                        "injection": data.get("injection"),
                        "withdrawal": data.get("withdrawal"),
                        "gasDayStart": gas_day,
                    },
                }
            )
    elif isinstance(data, list):
        for row in data[:10]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("code") or "GIE facility")
            out.append(
                {
                    "source": "gie_agsi",
                    "title": f"GIE · {name}",
                    "description": (
                        f"full={row.get('full')}% injection={row.get('injection')} "
                        f"withdrawal={row.get('withdrawal')}"
                    ),
                    "url": "https://agsi.gie.eu/",
                    "published_at": row.get("gasDayStart") or _now_iso(),
                    "provider": "GIE",
                }
            )
    return out


def fetch_latest_news(*, limit: int = 25, use_cache: bool = True) -> dict[str, Any]:
    """Unified NewsAPI + GIE feed with offline cache fallback."""
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    sources_ok: list[str] = []

    try:
        news = _fetch_newsapi(limit=limit)
        if news:
            items.extend(news)
            sources_ok.append("newsapi")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        errors.append(f"newsapi:{exc}")
        LOG.warning("NewsAPI fetch failed: %s", exc)

    try:
        gie = _fetch_gie()
        if gie:
            items.extend(gie)
            sources_ok.append("gie")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        errors.append(f"gie:{exc}")
        LOG.warning("GIE fetch failed: %s", exc)

    items = items[:limit]
    if items:
        payload = {
            "ok": True,
            "contract_version": CONTRACT_VERSION,
            "fetched_at": _now_iso(),
            "topics": list(TOPIC_TERMS),
            "sources_ok": sources_ok,
            "count": len(items),
            "items": items,
            "is_cached": False,
            "errors": errors,
        }
        _save_cache(payload)
        return payload

    cached = _load_cache() if use_cache else None
    if cached:
        cached = dict(cached)
        cached["ok"] = True
        cached["is_cached"] = True
        cached["cache_fallback"] = True
        cached["errors"] = errors or cached.get("errors") or ["upstream_unavailable"]
        cached["contract_version"] = CONTRACT_VERSION
        return cached

    return {
        "ok": True,
        "contract_version": CONTRACT_VERSION,
        "fetched_at": _now_iso(),
        "topics": list(TOPIC_TERMS),
        "sources_ok": [],
        "count": 0,
        "items": [],
        "is_cached": False,
        "errors": errors or ["no_keys_or_empty_feed"],
        "note": "Configure NEWSAPI_KEY / GIE_API_KEY in .env",
    }


__all__ = ("TOPIC_TERMS", "fetch_latest_news", "CACHE_PATH")
