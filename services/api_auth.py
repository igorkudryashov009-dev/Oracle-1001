#!/usr/bin/env python3
"""Inbound API auth — X-API-Key + rate limit + health sanitization.

Contract 1.8.0-ops-gis-sot · Sprint-1DAY.
- /api/v1/health is public (slim without key); other /api/v1/* require key
  or legacy X-Contract-Version (readonly, 24h deprecation).
- Tiers: admin (full) | readonly (no budget/env/diagnostics).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_VERSION = "1.8.0-ops-gis-sot"
RATE_LIMIT_PER_MIN = 60

# Fields stripped for readonly / legacy Contract-Version callers
_ADMIN_ONLY_TOP = frozenset(
    {
        "gfw_budget",
        "gfw_budget_warn",
        "gfw_budget_warn_active",
        "vesselfinder_budget",
        "maptiles_budget",
        "maptiles_budget_remaining",
        "llm_budget",
        "oob",
        "scheduler_warn",
    }
)
_ADMIN_ONLY_NESTED = frozenset(
    {
        "budget",
        "vf_budget",
        "token_masked",
        "key_fp",
        "db_path",
        "path",
        "env",
        "providers_masked",
    }
)

_LOCK = threading.RLock()
_HITS: dict[str, deque[float]] = defaultdict(deque)


def _load_keys_raw() -> dict[str, Any]:
    raw = (os.getenv("API_KEYS_JSON") or "").strip()
    if not raw:
        # Optional file path for larger maps
        path = (os.getenv("API_KEYS_FILE") or "").strip()
        if path and Path(path).is_file():
            raw = Path(path).read_text(encoding="utf-8")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def list_api_keys() -> dict[str, dict[str, Any]]:
    """Return key → {name, tier} (never log key values)."""
    out: dict[str, dict[str, Any]] = {}
    for k, meta in _load_keys_raw().items():
        key = str(k).strip()
        if not key:
            continue
        if isinstance(meta, dict):
            out[key] = {
                "name": str(meta.get("name") or "unnamed")[:64],
                "tier": str(meta.get("tier") or "readonly").lower(),
            }
        elif isinstance(meta, str):
            out[key] = {"name": meta[:64], "tier": "readonly"}
    return out


def generate_api_key() -> str:
    return "sk_sent_" + secrets.token_urlsafe(24)


def fingerprint_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def resolve_auth(
    *,
    api_key: str | None,
    contract_version: str | None,
    path: str,
) -> dict[str, Any]:
    """Resolve caller tier for a request path.

    Returns dict: ok, tier (admin|readonly|public|denied), name, status (401|429|200),
    retry_after, reason.
    """
    path_n = (path or "").split("?", 1)[0]
    is_health = path_n.rstrip("/").endswith("/api/v1/health") or path_n.endswith(
        "/api/v1/health.json"
    )
    # Also match /output/api/v1/health
    is_health = is_health or "/api/v1/health" in path_n
    # Pilot self-register is public (IP rate-limit enforced in handler)
    is_pilot_register = "/api/v1/pilot/register" in path_n

    keys = list_api_keys()
    key = (api_key or "").strip()
    cv = (contract_version or "").strip()

    if is_pilot_register:
        return {
            "ok": True,
            "tier": "public",
            "status": 200,
            "name": "pilot_register",
            "reason": "public_pilot_register",
        }

    # Bootstrap / pytest: no keys configured yet → readonly open (install gen_api_key to lock)
    if not keys and not key:
        if is_health:
            return {
                "ok": True,
                "tier": "public",
                "status": 200,
                "name": "anonymous",
                "reason": "public_health_no_keys",
            }
        if "/api/v1/" in path_n:
            return {
                "ok": True,
                "tier": "readonly",
                "status": 200,
                "name": "bootstrap_open",
                "reason": "api_keys_not_configured",
            }

    if key:
        meta = keys.get(key)
        if not meta:
            # Pilot clients store SHA-256 only — resolve via hash lookup
            try:
                from services.pilot_register import lookup_pilot_by_api_key

                pilot = lookup_pilot_by_api_key(key)
            except Exception:  # noqa: BLE001
                pilot = None
            if pilot:
                meta = {
                    "name": str(pilot.get("company") or pilot.get("email") or "pilot")[:64],
                    "tier": "readonly",
                }
            else:
                return {
                    "ok": False,
                    "tier": "denied",
                    "status": 401,
                    "reason": "invalid_api_key",
                    "name": None,
                }
        rl = _rate_limit_check(fingerprint_key(key))
        if not rl["ok"]:
            return {
                "ok": False,
                "tier": meta["tier"],
                "status": 429,
                "retry_after": rl["retry_after"],
                "reason": "rate_limited",
                "name": meta["name"],
            }
        tier = meta["tier"] if meta["tier"] in {"admin", "readonly"} else "readonly"
        return {
            "ok": True,
            "tier": tier,
            "status": 200,
            "name": meta["name"],
            "reason": "api_key",
        }

    # Legacy: Contract-Version header ⇒ readonly (deprecation window)
    if cv:
        rl = _rate_limit_check("legacy:" + cv[:32])
        if not rl["ok"]:
            return {
                "ok": False,
                "tier": "readonly",
                "status": 429,
                "retry_after": rl["retry_after"],
                "reason": "rate_limited",
                "name": "legacy_contract",
            }
        return {
            "ok": True,
            "tier": "readonly",
            "status": 200,
            "name": "legacy_contract",
            "reason": "legacy_contract_version",
        }

    if is_health:
        return {
            "ok": True,
            "tier": "public",
            "status": 200,
            "name": "anonymous",
            "reason": "public_health",
        }

    # Non-health API without key (keys ARE configured)
    if "/api/v1/" in path_n:
        return {
            "ok": False,
            "tier": "denied",
            "status": 401,
            "reason": "missing_api_key",
            "name": None,
        }

    # Static / non-API
    return {
        "ok": True,
        "tier": "public",
        "status": 200,
        "name": "static",
        "reason": "non_api",
    }


def _rate_limit_check(bucket: str) -> dict[str, Any]:
    now = time.monotonic()
    window = 60.0
    with _LOCK:
        q = _HITS[bucket]
        while q and (now - q[0]) > window:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            retry = max(1, int(window - (now - q[0])) + 1)
            return {"ok": False, "retry_after": retry}
        q.append(now)
        return {"ok": True, "retry_after": 0}


def _redact_internal_paths(obj: Any) -> Any:
    """Recursively redact container/host absolute paths for non-admin callers."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl.endswith("_path") or kl.endswith("_file") or "path" in kl or kl.endswith("_report"):
                if isinstance(v, str) and (
                    "/app/" in v
                    or v.startswith("/")
                    or (len(v) > 2 and v[1] == ":" and v[0].isalpha())
                ):
                    out[k] = "[redacted]"
                    continue
            out[k] = _redact_internal_paths(v)
        return out
    if isinstance(obj, list):
        return [_redact_internal_paths(v) for v in obj]
    if isinstance(obj, str):
        if "/app/" in obj or (len(obj) > 3 and obj[1] == ":" and obj[0].isalpha() and ("\\" in obj or "/" in obj)):
            return "[redacted]"
        return obj
    return obj


def sanitize_health(doc: dict[str, Any], *, tier: str) -> dict[str, Any]:
    """Return health payload appropriate for caller tier."""
    if tier == "admin":
        return doc
    if tier == "public":
        acc = doc.get("acceptance") or {}
        return {
            "status": doc.get("pipeline_health_status")
            or doc.get("operational_status")
            or doc.get("status")
            or "UNKNOWN",
            "acceptance": {"status": acc.get("status")},
            "commissioned": bool(acc.get("fully_commissioned_at")),
            "fully_commissioned_at": acc.get("fully_commissioned_at"),
            "http": "200",
            "note": "slim health — provide X-API-Key or X-Contract-Version for readonly+",
        }

    # readonly
    out = dict(doc)
    for k in _ADMIN_ONLY_TOP:
        out.pop(k, None)
    # Nested scrub
    fa = out.get("fleet_archive")
    if isinstance(fa, dict):
        fa2 = {k: v for k, v in fa.items() if k not in _ADMIN_ONLY_NESTED}
        out["fleet_archive"] = fa2
    gfw = out.get("gfw_status")
    if isinstance(gfw, dict):
        gfw2 = {
            k: v
            for k, v in gfw.items()
            if k not in {"token_masked", "budget"} and "path" not in k.lower()
        }
        out["gfw_status"] = gfw2
    mt = out.get("maptiles")
    if isinstance(mt, dict):
        out["maptiles"] = {
            k: v for k, v in mt.items() if k not in {"budget", "key_preview"}
        }
    # Strip absolute paths in replica
    rep = out.get("replica")
    if isinstance(rep, dict) and "db_path" in rep:
        rep = dict(rep)
        rep["db_path"] = "[redacted]"
        out["replica"] = rep
    # llm_budget admin-only; keep llm_status
    out.pop("llm_budget", None)
    # acceptance.commissioning_report and any leftover /app/ paths
    return _redact_internal_paths(out)


def upsert_key_into_env_map(
    existing_json: str, *, key: str, name: str, tier: str
) -> str:
    try:
        data = json.loads(existing_json) if existing_json.strip() else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = {"name": name, "tier": tier}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "CONTRACT_VERSION",
    "RATE_LIMIT_PER_MIN",
    "fingerprint_key",
    "generate_api_key",
    "list_api_keys",
    "resolve_auth",
    "sanitize_health",
    "upsert_key_into_env_map",
]
