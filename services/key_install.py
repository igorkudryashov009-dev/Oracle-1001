"""Install a provider secret from a file into .env, runtime_env, and the watcher signal.

The shell and the bake hook call this. Nothing here prints the secret.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from services.key_manager import _looks_like_key
from services.runtime_env import write_install_signal, write_runtime_key

SAT_VENDORS = ("spire", "unseenlabs", "iceye")


def mask_last4(value: str) -> str:
    text = value or ""
    if len(text) <= 4:
        return "****"
    return "****" + text[-4:]


def read_secret_file(path: Path) -> str:
    """Read, drop a UTF-8 BOM and CR, then trim surrounding whitespace."""
    text = path.read_text(encoding="utf-8-sig")
    return text.replace("\r", "").strip()


def validate_provider_secret(env_key: str, value: str, *, provider: str | None = None) -> str:
    """Return the trimmed secret or raise ValueError. The message never contains the value."""
    secret = (value or "").strip()
    if env_key in ("VESSELFINDER_API_KEY", "VESSEL_FINDER_USERKEY"):
        if not _looks_like_key(secret):
            raise ValueError("invalid_vesselfinder_key_format")
        return secret
    if env_key == "SATELLITE_API_KEY":
        if not re.fullmatch(r"\S{8,256}", secret):
            raise ValueError("invalid_satellite_key_format")
        name = (provider or "").strip().lower()
        if name not in SAT_VENDORS:
            raise ValueError("sat_provider_required")
        return secret
    if not secret:
        raise ValueError("empty_key")
    return secret


def upsert_env_line(env_path: Path, key: str, value: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    kept: list[str] = []
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            if raw.startswith(f"{key}="):
                continue
            kept.append(raw)
    kept.append(f"{key}={value}")
    env_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def install_local(
    env_key: str,
    value: str,
    *,
    signal: str,
    env_path: Path,
    extra: dict[str, str] | None = None,
) -> str:
    """Write .env, runtime_env, and the watcher signal. Returns the mask only."""
    secret = value.strip()
    upsert_env_line(env_path, env_key, secret)
    write_runtime_key(env_key, secret)
    if env_key == "VESSELFINDER_API_KEY":
        upsert_env_line(env_path, "VESSEL_FINDER_USERKEY", secret)
    for name, extra_value in (extra or {}).items():
        upsert_env_line(env_path, name, extra_value)
        write_runtime_key(name, extra_value)
    write_install_signal(signal)
    return mask_last4(secret)


def install_from_file(
    provider: str,
    path: Path,
    *,
    env_path: Path,
    sat_provider: str | None = None,
    stamp_path: Path | None = None,
    stamp_digest: str | None = None,
) -> str:
    """Validate a key file and run the local half of the install pipeline."""
    name = (provider or "").strip().upper().replace("-", "_")
    raw = read_secret_file(path)
    if name in ("VF", "VESSELFINDER", "VESSELFINDER_API_KEY", "VESSEL_FINDER", "VESSEL_FINDER_USERKEY"):
        secret = validate_provider_secret("VESSELFINDER_API_KEY", raw)
        mask = install_local(
            "VESSELFINDER_API_KEY",
            secret,
            signal="vesselfinder",
            env_path=env_path,
        )
    elif name in ("SATELLITE", "SAT", "SATELLITE_API_KEY"):
        secret = validate_provider_secret("SATELLITE_API_KEY", raw, provider=sat_provider)
        vendor = (sat_provider or "").strip().lower()
        mask = install_local(
            "SATELLITE_API_KEY",
            secret,
            signal="satellite",
            env_path=env_path,
            extra={"SAT_PROVIDER": vendor},
        )
    else:
        raise ValueError("unsupported_provider")
    if stamp_path is not None and stamp_digest:
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(stamp_digest + "\n", encoding="utf-8")
    return mask


def vessel_finder_autoinstall(key_path: Path, stamp_path: Path, *, env_path: Path) -> str:
    """Install when the key file exists and its sha256 changed. Returns installed|skip|absent|refused."""
    if not key_path.is_file():
        return "absent"
    digest = hashlib.sha256(key_path.read_bytes()).hexdigest()
    prev = stamp_path.read_text(encoding="utf-8").strip() if stamp_path.is_file() else ""
    if prev == digest:
        return "skip"
    try:
        install_from_file(
            "VESSEL_FINDER",
            key_path,
            env_path=env_path,
            stamp_path=stamp_path,
            stamp_digest=digest,
        )
    except ValueError:
        return "refused"
    return "installed"
