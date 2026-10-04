#!/usr/bin/env python3
"""Runtime overlay for secrets under bind-mounted data/ (no web restart required).

install_key.sh writes host .env (persistence) AND data/archive/runtime_env.json
(immediate). Resolvers call apply_runtime_env() before getenv.
Never log values — only masked last4 via key_manager.mask_key.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ENV_PATH = ROOT / "data" / "archive" / "runtime_env.json"
SIGNAL_PATH = ROOT / "data" / "archive" / "key_install_signal"

KNOWN_KEYS = (
    "GFW_API_TOKEN",
    "GFW_API_KEY",
    "VESSELFINDER_API_KEY",
    "VESSEL_FINDER_USERKEY",
    "ANTHROPIC_API_KEY",
    "ALERT_WEBHOOK_URL",
    "SATELLITE_API_KEY",
    "SAT_PROVIDER",
    "SAT_BASE_URL",
    "SAT_DAILY_CAP",
)

_LOCK = threading.RLock()


def _load_file() -> dict[str, str]:
    if not RUNTIME_ENV_PATH.is_file():
        return {}
    try:
        data = json.loads(RUNTIME_ENV_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        if k in KNOWN_KEYS and isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def write_runtime_key(name: str, value: str) -> None:
    """Upsert one key into runtime_env.json (chmod 600 best-effort)."""
    name = str(name).strip()
    value = str(value).strip()
    if name not in KNOWN_KEYS or not value:
        raise ValueError(f"unsupported or empty key: {name}")
    with _LOCK:
        RUNTIME_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        cur = _load_file()
        cur[name] = value
        if name == "GFW_API_TOKEN":
            cur["GFW_API_KEY"] = value
        if name == "VESSELFINDER_API_KEY":
            cur["VESSEL_FINDER_USERKEY"] = value
        tmp = RUNTIME_ENV_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(RUNTIME_ENV_PATH)
        try:
            os.chmod(RUNTIME_ENV_PATH, 0o600)
        except OSError:
            pass
        os.environ[name] = value
        if name == "GFW_API_TOKEN":
            os.environ["GFW_API_KEY"] = value
        if name == "VESSELFINDER_API_KEY":
            os.environ["VESSEL_FINDER_USERKEY"] = value


def apply_runtime_env() -> dict[str, bool]:
    """Overlay runtime keys into os.environ. Returns {name: present}."""
    present: dict[str, bool] = {}
    with _LOCK:
        data = _load_file()
        for k in KNOWN_KEYS:
            if k in data:
                os.environ[k] = data[k]
                present[k] = True
            else:
                present[k] = bool((os.getenv(k) or "").strip())
    return present


def write_install_signal(provider: str) -> Path:
    """Host/container signal: key_installed:<provider>."""
    provider = str(provider).strip().lower()
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_PATH.write_text(f"key_installed:{provider}\n", encoding="utf-8")
    try:
        os.chmod(SIGNAL_PATH, 0o600)
    except OSError:
        pass
    return SIGNAL_PATH


def consume_install_signal() -> str | None:
    """Return provider name if signal present; delete file. Else None."""
    if not SIGNAL_PATH.is_file():
        return None
    try:
        raw = SIGNAL_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        SIGNAL_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    if raw.startswith("key_installed:"):
        return raw.split(":", 1)[1].strip().lower() or None
    return raw.lower() or None


def getenv_secret(name: str, *aliases: str) -> str:
    """apply_runtime_env then getenv chain."""
    apply_runtime_env()
    for n in (name, *aliases):
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return ""


__all__ = (
    "KNOWN_KEYS",
    "RUNTIME_ENV_PATH",
    "SIGNAL_PATH",
    "apply_runtime_env",
    "consume_install_signal",
    "getenv_secret",
    "write_install_signal",
    "write_runtime_key",
)
