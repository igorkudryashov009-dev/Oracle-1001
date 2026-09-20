"""Unified API credentials registry — Contract 1.8.0-ops-gis-sot.

Loads secrets exclusively from process environment / ``.env`` (gitignored).
Never hardcodes API keys. Public status endpoints return masked values only.

Canonical env names (aliases in parentheses):
  NEWSAPI_KEY
  GIE_API_KEY
  AISSTREAM_API_KEY   (AISSTREAM_KEY)
  FIRMS_MAP_KEY
  EXCHANGERATE_KEY
  NASDAQ_DATA_KEY
  BREVO_API_KEY
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

CONTRACT_VERSION = "1.8.0-ops-gis-sot"
ROOT = Path(__file__).resolve().parents[1]

# Logical service id → ordered env var candidates (first non-empty wins).
API_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "NEWSAPI_KEY": ("NEWSAPI_KEY", "NEWS_API_KEY"),
    "GIE_API_KEY": ("GIE_API_KEY", "AGSI_API_KEY", "GIE_KEY"),
    "AISSTREAM_KEY": ("AISSTREAM_API_KEY", "AISSTREAM_KEY"),
    "FIRMS_MAP_KEY": ("FIRMS_MAP_KEY", "NASA_FIRMS_MAP_KEY", "FIRMS_API_KEY"),
    "EXCHANGERATE_KEY": ("EXCHANGERATE_KEY", "EXCHANGERATE_API_KEY", "EXCHANGE_RATE_API_KEY"),
    "NASDAQ_DATA_KEY": ("NASDAQ_DATA_KEY", "NASDAQ_API_KEY", "QUANDL_API_KEY"),
    "BREVO_API_KEY": ("BREVO_API_KEY", "SENDINBLUE_API_KEY"),
}

# Public registry surface (keys only — values never live in source).
API_REGISTRY_KEYS: tuple[str, ...] = tuple(API_ENV_ALIASES.keys())

PLACEHOLDER_MARKERS = (
    "YOUR_",
    "CHANGEME",
    "PLACEHOLDER",
    "EXAMPLE",
    "INSERT_",
    "REPLACE_ME",
    "xxx",
)


@dataclass(frozen=True)
class ResolvedCredential:
    logical_name: str
    env_name: str
    present: bool
    configured: bool
    masked: str
    length: int
    source: str = "env"

    def public_dict(self) -> dict[str, Any]:
        return {
            "logical_name": self.logical_name,
            "env_name": self.env_name,
            "present": self.present,
            "configured": self.configured,
            "masked": self.masked,
            "length": self.length,
            "source": self.source,
        }


def _load_dotenv(path: Path | None = None) -> None:
    """Best-effort load of ROOT/.env into os.environ (does not override)."""
    env_path = path or (ROOT / ".env")
    if not env_path.is_file():
        return
    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        name = name.strip()
        val = val.strip().strip("'").strip('"')
        if name and name not in os.environ:
            os.environ[name] = val


def mask_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return "••••"
    if len(k) <= 4:
        return "••••"
    return f"••••{k[-4:]}"


def _is_placeholder(val: str) -> bool:
    v = (val or "").strip()
    if not v or len(v) < 8:
        return True
    up = v.upper()
    return any(m.upper() in up for m in PLACEHOLDER_MARKERS)


def _first_env(names: Iterable[str]) -> tuple[str, str]:
    for name in names:
        raw = (os.getenv(name) or "").strip()
        if raw:
            return name, raw
    names_t = tuple(names)
    return (names_t[0] if names_t else "", "")


def get_key(logical_name: str, *, default: str = "") -> str:
    """Return raw secret for ``logical_name`` or ``default`` if missing/placeholder."""
    _load_dotenv()
    aliases = API_ENV_ALIASES.get(logical_name)
    if not aliases:
        # Allow direct env passthrough for unknown logical names.
        raw = (os.getenv(logical_name) or "").strip()
        return raw if raw and not _is_placeholder(raw) else default
    _, raw = _first_env(aliases)
    if not raw or _is_placeholder(raw):
        return default
    return raw


def require_key(logical_name: str) -> str:
    val = get_key(logical_name)
    if not val:
        raise RuntimeError(
            f"Missing API credential {logical_name!r} — set env "
            f"{API_ENV_ALIASES.get(logical_name, (logical_name,))[0]} in .env"
        )
    return val


def resolve_credential(logical_name: str) -> ResolvedCredential:
    _load_dotenv()
    aliases = API_ENV_ALIASES.get(logical_name, (logical_name,))
    env_name, raw = _first_env(aliases)
    configured = bool(raw) and not _is_placeholder(raw)
    return ResolvedCredential(
        logical_name=logical_name,
        env_name=env_name or aliases[0],
        present=bool(raw),
        configured=configured,
        masked=mask_key(raw) if configured else "••••",
        length=len(raw) if configured else 0,
        source="env",
    )


def registry_status(*, include_unconfigured: bool = True) -> dict[str, Any]:
    """Public SoT blob for health / HUD — never includes raw secrets."""
    rows = [resolve_credential(name).public_dict() for name in API_REGISTRY_KEYS]
    if not include_unconfigured:
        rows = [r for r in rows if r["configured"]]
    configured_n = sum(1 for r in rows if r["configured"])
    return {
        "contract_version": CONTRACT_VERSION,
        "configured": configured_n,
        "total": len(API_REGISTRY_KEYS),
        "keys": rows,
    }


def as_env_mapping() -> dict[str, str]:
    """Return ``{logical_name: raw_secret}`` for configured keys only."""
    out: dict[str, str] = {}
    for name in API_REGISTRY_KEYS:
        val = get_key(name)
        if val:
            out[name] = val
    return out


# Back-compat name used in mission brief — values are NEVER literals here.
API_REGISTRY: Mapping[str, str] = type(
    "_LazyRegistry",
    (),
    {
        "__getitem__": staticmethod(lambda name: get_key(str(name))),
        "get": staticmethod(lambda name, default="": get_key(str(name), default=default)),
        "keys": staticmethod(lambda: API_REGISTRY_KEYS),
        "__iter__": staticmethod(lambda: iter(API_REGISTRY_KEYS)),
        "__len__": staticmethod(lambda: len(API_REGISTRY_KEYS)),
        "__contains__": staticmethod(lambda name: str(name) in API_ENV_ALIASES),
    },
)()


__all__ = (
    "CONTRACT_VERSION",
    "API_ENV_ALIASES",
    "API_REGISTRY_KEYS",
    "API_REGISTRY",
    "ResolvedCredential",
    "mask_key",
    "get_key",
    "require_key",
    "resolve_credential",
    "registry_status",
    "as_env_mapping",
)
