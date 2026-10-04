"""In-memory HUD catalogs. Disk is read once per process."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "web" / "locales"
LANGS = ("en", "ru", "de", "fr", "es", "pt", "zh", "ja", "ar", "ko")

_CACHE: dict[str, dict[str, str]] | None = None
_BODY: dict[str, bytes] = {}
_GZIP: dict[str, bytes] = {}


def load_catalogs() -> dict[str, dict[str, str]]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    loaded: dict[str, dict[str, str]] = {}
    for lang in LANGS:
        path = LOCALES / f"{lang}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"locale {lang} is not an object")
        loaded[lang] = {str(k): str(v) for k, v in raw.items()}
    _CACHE = loaded
    _BODY.clear()
    _GZIP.clear()
    for lang in loaded:
        _remember(lang)
    return loaded


def reset_cache() -> None:
    global _CACHE
    _CACHE = None
    _BODY.clear()
    _GZIP.clear()


def _remember(lang: str) -> bytes:
    cats = _CACHE or {}
    chosen = lang if lang in cats else "en"
    hit = _BODY.get(chosen)
    if hit is not None:
        return hit
    body = {"lang": chosen, "dir": "rtl" if chosen == "ar" else "ltr", "strings": cats[chosen]}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _BODY[chosen] = raw
    _GZIP[chosen] = gzip.compress(raw, compresslevel=6)
    return raw


def catalog_body(lang: str) -> tuple[bytes, bytes]:
    """Serialized catalog and gzip. Disk is not touched after the first load."""
    load_catalogs()
    chosen = lang if lang in (_CACHE or {}) else "en"
    raw = _remember(chosen)
    return raw, _GZIP[chosen]


def negotiate_lang(
    *,
    query: str | None = None,
    cookie: str | None = None,
    accept_language: str | None = None,
) -> str:
    if query and query.lower() in LANGS:
        return query.lower()
    if cookie:
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("sentinel_lang="):
                value = part.split("=", 1)[1].strip().lower()
                if value in LANGS:
                    return value
    if accept_language:
        for bit in accept_language.split(","):
            tag = bit.split(";", 1)[0].strip().lower().replace("_", "-")
            primary = tag.split("-", 1)[0]
            if primary in LANGS:
                return primary
    return "en"


def catalog_for(lang: str) -> dict[str, Any]:
    cats = load_catalogs()
    chosen = lang if lang in cats else "en"
    return {"lang": chosen, "dir": "rtl" if chosen == "ar" else "ltr", "strings": cats[chosen]}
