#!/usr/bin/env python3
"""Commercial VesselFinder userkey auto-resolver (cascade + persist).

Never hardcodes secrets. Resolves keys from OS env, dotenv files, CLI argv,
and optional SQLite ``config_secrets``, then can auto-persist into local ``.env``
(gitignored).

Canonical write keys:
  VESSEL_FINDER_USERKEY
  VESSELFINDER_API_KEY   (compat)
  VESSEL_FINDER_PLAN
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.key_manager")

ENV_PATH = ROOT / ".env"
ENV_CANDIDATES = (
    ROOT / ".env",
    ROOT / ".env.local",
    ROOT / ".env.production",
)
TELEMETRY_DB = ROOT / "data" / "archive" / "vessel_telemetry_history.sqlite"

# Search order for env var names (OS + dotenv)
ENV_KEY_NAMES = (
    "VESSEL_FINDER_USERKEY",
    "VESSEL_FINDER_KEY",
    "COMMERCIAL_USERKEY",
    "VESSELFINDER_API_KEY",
    "VESSEL_TRACKING_API_KEY",
    "PROVIDER_API_KEY",
    "API_KEY",
)

PLACEHOLDER_MARKERS = (
    "YOUR_",
    "CHANGEME",
    "PLACEHOLDER",
    "EXAMPLE",
    "INSERT_",
    "xxx",
)

CONFIG_SECRETS_DDL = """
CREATE TABLE IF NOT EXISTS config_secrets (
    key_name TEXT PRIMARY KEY,
    key_value TEXT NOT NULL,
    plan TEXT,
    source TEXT,
    updated_at TEXT NOT NULL
)
"""


@dataclass
class ResolvedKey:
    key: str
    source: str
    name: str
    valid: Optional[bool] = None
    plan: str = "unknown"
    persisted: bool = False
    error: Optional[str] = None

    def masked(self) -> str:
        return mask_key(self.key)

    def public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.masked()
        d["key_len"] = len(self.key)
        return d


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


def _looks_like_key(val: str) -> bool:
    v = (val or "").strip()
    if _is_placeholder(v):
        return False
    # VesselFinder userkeys are typically 16–64 hex/alnum; RapidAPI similar
    if not re.fullmatch(r"[A-Za-z0-9_\-]{12,128}", v):
        return False
    return True


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        name, _, val = line.partition("=")
        name = name.strip()
        val = val.strip().strip("'").strip('"')
        if name:
            out[name] = val
    return out


def _read_os_env() -> Optional[tuple[str, str, str]]:
    for name in ENV_KEY_NAMES:
        val = (os.environ.get(name) or "").strip()
        if _looks_like_key(val):
            return val, f"os_env:{name}", name
    return None


def _read_dotenv_files() -> Optional[tuple[str, str, str]]:
    for path in ENV_CANDIDATES:
        data = _parse_env_file(path)
        for name in ENV_KEY_NAMES:
            val = (data.get(name) or "").strip()
            if _looks_like_key(val):
                return val, f"dotenv:{path.name}:{name}", name
    return None


def _read_cli_argv(argv: Optional[Iterable[str]] = None) -> Optional[tuple[str, str, str]]:
    args = list(argv if argv is not None else sys.argv[1:])
    # Support --userkey=X, --api-key=X, --userkey X, --api-key X
    for i, tok in enumerate(args):
        low = tok.lower()
        for flag in ("--userkey", "--api-key", "--apikey", "--vf-userkey"):
            if low == flag and i + 1 < len(args):
                val = args[i + 1].strip()
                if _looks_like_key(val):
                    return val, f"cli:{flag}", "VESSEL_FINDER_USERKEY"
            if low.startswith(flag + "="):
                val = tok.split("=", 1)[1].strip()
                if _looks_like_key(val):
                    return val, f"cli:{flag}", "VESSEL_FINDER_USERKEY"
    return None


def ensure_config_secrets_schema(db_path: Path = TELEMETRY_DB) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute(CONFIG_SECRETS_DDL)
        conn.commit()
    finally:
        conn.close()


def _read_sqlite_secret(db_path: Path = TELEMETRY_DB) -> Optional[tuple[str, str, str]]:
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn.execute(CONFIG_SECRETS_DDL)
            row = conn.execute(
                """
                SELECT key_name, key_value FROM config_secrets
                WHERE key_name IN (
                  'VESSEL_FINDER_USERKEY','VESSELFINDER_API_KEY','COMMERCIAL_USERKEY','API_KEY'
                )
                ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            name, val = str(row[0]), str(row[1] or "").strip()
            if _looks_like_key(val):
                return val, f"sqlite:config_secrets:{name}", name
        finally:
            conn.close()
    except sqlite3.Error as exc:
        LOG.debug("config_secrets read failed: %s", exc)
    return None


def persist_secret_sqlite(
    key: str,
    *,
    key_name: str = "VESSEL_FINDER_USERKEY",
    plan: str = "commercial_rest",
    source: str = "persist",
    db_path: Path = TELEMETRY_DB,
) -> None:
    from datetime import datetime, timezone

    ensure_config_secrets_schema(db_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO config_secrets (key_name, key_value, plan, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key_name, key, plan, source, now),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_dotenv(
    updates: dict[str, str],
    *,
    path: Path = ENV_PATH,
) -> bool:
    """Insert or replace KEY=VALUE lines in .env without dumping unrelated secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    lines = existing.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        name = stripped.split("=", 1)[0].strip()
        if name in updates:
            out.append(f"{name}={updates[name]}")
            seen.add(name)
        else:
            out.append(line)
    for name, val in updates.items():
        if name not in seen:
            out.append(f"{name}={val}")
    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    # Mirror into process env immediately (hot-swap)
    for name, val in updates.items():
        os.environ[name] = val
    return True


def persist_key_to_env(
    key: str,
    *,
    plan: str = "commercial_rest",
    also_sqlite: bool = True,
) -> None:
    upsert_dotenv(
        {
            "VESSEL_FINDER_USERKEY": key,
            "VESSELFINDER_API_KEY": key,
            "VESSEL_FINDER_PLAN": plan,
        }
    )
    if also_sqlite:
        try:
            persist_secret_sqlite(key, plan=plan, source="auto_persist")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("sqlite secret persist failed: %s", exc)


def resolve_commercial_key(
    *,
    argv: Optional[Iterable[str]] = None,
    explicit: Optional[str] = None,
    auto_persist: bool = True,
    validate: bool = False,
) -> Optional[ResolvedKey]:
    """Cascade resolve a commercial userkey. Optionally validate + persist."""
    found: Optional[tuple[str, str, str]] = None
    if explicit and _looks_like_key(explicit):
        found = (explicit.strip(), "explicit", "VESSEL_FINDER_USERKEY")
    if not found:
        found = _read_cli_argv(argv)
    if not found:
        found = _read_os_env()
    if not found:
        found = _read_dotenv_files()
    if not found:
        found = _read_sqlite_secret()

    if not found:
        return None

    key, source, name = found
    resolved = ResolvedKey(key=key, source=source, name=name, plan="unknown")

    # Always expose to process env for downstream consumers
    os.environ.setdefault("VESSEL_FINDER_USERKEY", key)
    os.environ.setdefault("VESSELFINDER_API_KEY", key)

    if validate:
        probe = validate_userkey(key)
        resolved.valid = bool(probe.get("ok"))
        resolved.error = probe.get("error")
        resolved.plan = "commercial_rest" if resolved.valid else "hybrid_local"
    else:
        resolved.plan = (os.getenv("VESSEL_FINDER_PLAN") or "commercial_rest").strip()

    # Auto-persist when missing from .env canonical keys
    env_data = _parse_env_file(ENV_PATH)
    env_has = any(_looks_like_key(env_data.get(n, "")) for n in (
        "VESSEL_FINDER_USERKEY",
        "VESSELFINDER_API_KEY",
    ))
    if auto_persist and not env_has:
        persist_key_to_env(key, plan=resolved.plan if resolved.valid is not False else "commercial_rest")
        resolved.persisted = True
        LOG.info("auto-persisted commercial key to .env from %s (%s)", source, mask_key(key))

    return resolved


def validate_userkey(key: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """Lightweight ListManager handshake — does not mutate fleet."""
    import requests

    key = (key or "").strip()
    if not _looks_like_key(key):
        return {"ok": False, "error": "key_format_invalid", "status_code": None}
    try:
        resp = requests.get(
            "https://api.vesselfinder.com/listmanager",
            params={"userkey": key},
            timeout=timeout,
            headers={"User-Agent": "Oracle-1001-Sentinel-KeyManager/1.0"},
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": f"network:{exc}", "status_code": None}

    text = (resp.text or "")[:400]
    if resp.status_code in (401, 403):
        return {"ok": False, "error": f"http_{resp.status_code}", "status_code": resp.status_code}
    # VesselFinder returns 200 + {"error":"Invalid Userkey!"} for bad keys
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": text}

    if isinstance(data, dict) and data.get("error"):
        return {
            "ok": False,
            "error": str(data.get("error")),
            "status_code": resp.status_code,
        }
    if resp.status_code >= 400:
        return {"ok": False, "error": f"http_{resp.status_code}:{text[:120]}", "status_code": resp.status_code}
    return {
        "ok": True,
        "error": None,
        "status_code": resp.status_code,
        "payload_type": type(data).__name__,
    }


def inject_and_activate(
    userkey: str,
    *,
    run_sync: bool = True,
    force: bool = True,
) -> dict[str, Any]:
    """Validate → persist → optional archive sync (hot-swap without process restart)."""
    probe = validate_userkey(userkey)
    if not probe.get("ok"):
        return {
            "ok": False,
            "valid": False,
            "error": probe.get("error"),
            "key": mask_key(userkey),
            "ingest_mode": "hybrid_local",
        }

    persist_key_to_env(userkey, plan="commercial_rest")
    result: dict[str, Any] = {
        "ok": True,
        "valid": True,
        "key": mask_key(userkey),
        "persisted": True,
        "ingest_mode": "commercial_rest",
        "plan": "COMMERCIAL REST API (LIVE)",
    }
    if run_sync:
        try:
            from services.archive_service import sync_archive

            sync_res = sync_archive(force=force, dry_run=False, refresh_snapshot=True)
            result["sync"] = {
                "ok_api": sync_res.get("ok_api"),
                "ingest_mode": sync_res.get("ingest_mode"),
                "positions": sync_res.get("positions"),
                "status": sync_res.get("status"),
            }
        except Exception as exc:  # noqa: BLE001
            result["sync_error"] = str(exc)[:400]
    return result


def update_userkey(new_key: str, *, run_sync: bool = True) -> tuple[bool, str]:
    """UI/server-friendly wrapper: validate + persist + optional sync.

    Returns (success, message). Never raises for invalid keys.
    """
    result = inject_and_activate(new_key, run_sync=run_sync, force=True)
    if result.get("ok"):
        msg = (
            f"Commercial userkey accepted ({result.get('key')}). "
            f"Mode={result.get('ingest_mode')}"
        )
        if result.get("sync"):
            msg += f" · sync positions={result['sync'].get('positions')}"
        return True, msg
    return False, str(result.get("error") or "invalid_userkey")


def key_status_public() -> dict[str, Any]:
    resolved = resolve_commercial_key(auto_persist=False, validate=False)
    if not resolved:
        return {
            "has_key": False,
            "key_masked": None,
            "source": None,
            "plan": "hybrid_local",
            "ui_api": "HYBRID LOCAL FALLBACK",
            "ui_status": "NOMINAL",
        }
    # Soft validate only if we have network? Keep last known from api_status if present
    return {
        "has_key": True,
        "key_masked": resolved.masked(),
        "source": resolved.source,
        "plan": resolved.plan,
        "name": resolved.name,
        "ui_api": "COMMERCIAL REST API",
        "ui_key": f"AUTO-RESOLVED ({resolved.masked()})",
        "ui_status": "ONLINE",
    }


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="VesselFinder commercial key auto-resolver")
    p.add_argument("--userkey", default=None, help="Explicit commercial userkey")
    p.add_argument("--api-key", dest="api_key", default=None, help="Alias for --userkey")
    p.add_argument("--validate", action="store_true", help="Ping ListManager handshake")
    p.add_argument("--persist", action="store_true", help="Force write resolved key into .env")
    p.add_argument("--status", action="store_true", help="Print public key status JSON")
    args = p.parse_args(argv)

    if args.status:
        import json

        print(json.dumps(key_status_public(), ensure_ascii=False, indent=2))
        return 0

    explicit = args.userkey or args.api_key
    resolved = resolve_commercial_key(
        argv=argv,
        explicit=explicit,
        auto_persist=True,
        validate=bool(args.validate),
    )
    import json

    if not resolved:
        print(json.dumps({"ok": False, "error": "no_key_found"}, ensure_ascii=False, indent=2))
        return 1
    if args.persist:
        persist_key_to_env(resolved.key, plan=resolved.plan or "commercial_rest")
        resolved.persisted = True
    print(json.dumps({"ok": True, **resolved.public_dict()}, ensure_ascii=False, indent=2))
    return 0 if (resolved.valid is not False) else 2


if __name__ == "__main__":
    raise SystemExit(main())
