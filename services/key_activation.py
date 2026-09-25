#!/usr/bin/env python3
"""Auto-activation / pause for VF + GFW provider keys (env-only secrets)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.key_activation")
STATE_PATH = ROOT / "data" / "archive" / "provider_activation.json"

PROVIDERS = ("vesselfinder", "gfw")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict[str, Any]:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.is_file():
        return {"providers": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"providers": {}}
    except (OSError, json.JSONDecodeError):
        return {"providers": {}}


def _save(st: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def is_provider_paused(name: str) -> bool:
    st = _load()
    p = (st.get("providers") or {}).get(name) or {}
    return bool(p.get("paused"))


def provider_state(name: str) -> dict[str, Any]:
    return dict((_load().get("providers") or {}).get(name) or {})


def _set_provider(name: str, **fields: Any) -> dict[str, Any]:
    st = _load()
    prov = dict(st.get("providers") or {})
    cur = dict(prov.get(name) or {})
    cur.update(fields)
    cur["updated_at"] = _utc_iso()
    prov[name] = cur
    st["providers"] = prov
    _save(st)
    return cur


def record_provider_error(name: str, error: str) -> dict[str, Any]:
    """After 3 consecutive provider errors → pause + alert."""
    from services.alerts import emit_alert

    cur = provider_state(name)
    n = int(cur.get("consecutive_errors") or 0) + 1
    fields: dict[str, Any] = {
        "consecutive_errors": n,
        "last_error": str(error)[:200],
        "last_probe_at": _utc_iso(),
        "last_ok": False,
    }
    if n >= 3:
        fields["paused"] = True
        fields["paused_reason"] = "consecutive_provider_errors"
        emit_alert(
            f"provider_paused_{name}",
            f"{name} paused after {n} consecutive errors",
            severity="WARN",
            detail={"error": str(error)[:120]},
        )
    return _set_provider(name, **fields)


def record_provider_ok(name: str) -> dict[str, Any]:
    from services.alerts import clear_alert

    clear_alert(f"provider_paused_{name}")
    return _set_provider(
        name,
        consecutive_errors=0,
        last_ok=True,
        paused=False,
        paused_reason=None,
        last_error=None,
        last_probe_at=_utc_iso(),
        activated_at=_utc_iso(),
    )


def probe_vesselfinder() -> dict[str, Any]:
    """One cheap AIS-only GET; never prints key. Bills 0 on Invalid Userkey."""
    import requests
    from services.key_manager import mask_key
    from services.vesselfinder_client import VESSELS_URL, resolve_userkey

    key = resolve_userkey()
    if not key:
        return {"ok": False, "configured": False, "error": "no_key"}
    if is_provider_paused("vesselfinder"):
        return {"ok": False, "configured": True, "error": "paused", "paused": True}

    # Prefer a known Q-Flex IMO for probe
    imo = "9388819"
    try:
        resp = requests.get(
            VESSELS_URL,
            params={"userkey": key, "imo": imo, "format": "json"},
            timeout=25,
            headers={"User-Agent": "Oracle-1001-Sentinel-KeyActivation/1.0"},
        )
        data = None
        try:
            data = resp.json()
        except ValueError:
            data = None
        err = None
        if isinstance(data, dict) and data.get("error"):
            err = str(data.get("error"))
        invalid = bool(err and "Invalid Userkey" in err) or resp.status_code in (401, 403)
        if invalid:
            record_provider_error("vesselfinder", err or f"http_{resp.status_code}")
            return {
                "ok": False,
                "configured": True,
                "error": err or "invalid_key",
                "http_status": resp.status_code,
                "key_masked": mask_key(key),
            }
        if resp.status_code == 200 and not err:
            record_provider_ok("vesselfinder")
            # Soft-touch budget last_ok without spend when empty — use record_spend only on real fetch
            return {
                "ok": True,
                "configured": True,
                "http_status": 200,
                "key_masked": mask_key(key),
            }
        record_provider_error("vesselfinder", err or f"http_{resp.status_code}")
        return {"ok": False, "configured": True, "error": err or f"http_{resp.status_code}"}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if key:
            msg = msg.replace(key, "***")
        record_provider_error("vesselfinder", msg)
        return {"ok": False, "configured": True, "error": msg[:160]}


def probe_gfw() -> dict[str, Any]:
    from services.gfw_events import resolve_token, fetch_events_for_imo

    tok = resolve_token()
    if not tok:
        return {"ok": False, "configured": False, "error": "no_token"}
    if is_provider_paused("gfw"):
        return {"ok": False, "configured": True, "error": "paused", "paused": True}
    try:
        # Dry identity/events path for a single IMO
        out = fetch_events_for_imo("9388819", use_cache=False)
        if out.get("ok"):
            record_provider_ok("gfw")
            return {"ok": True, "configured": True, "events_n": out.get("events_n") or 0}
        err = str(out.get("error") or "gfw_probe_failed")
        if err == "not_configured":
            return {"ok": False, "configured": False, "error": err}
        record_provider_error("gfw", err)
        return {"ok": False, "configured": True, "error": err}
    except Exception as exc:  # noqa: BLE001
        record_provider_error("gfw", str(exc))
        return {"ok": False, "configured": True, "error": str(exc)[:160]}


def run_key_activation_cycle() -> dict[str, Any]:
    """Called from watchdog every 15 min — probe only when key present and not paused with fresh ok."""
    result: dict[str, Any] = {"vf": None, "gfw": None}

    vf_key = (
        os.getenv("VESSELFINDER_API_KEY")
        or os.getenv("VESSEL_FINDER_USERKEY")
        or ""
    ).strip()
    vf_st = provider_state("vesselfinder")
    if vf_key and not vf_st.get("paused"):
        # Re-probe if never ok or last_ok False
        if vf_st.get("last_ok") is not True:
            result["vf"] = probe_vesselfinder()
        else:
            result["vf"] = {"ok": True, "skipped": "already_ok"}
    elif not vf_key:
        result["vf"] = {"ok": False, "configured": False}
    else:
        result["vf"] = {"ok": False, "paused": True}

    from services.gfw_events import resolve_token

    gfw_tok = resolve_token()
    gfw_st = provider_state("gfw")
    if gfw_tok and not gfw_st.get("paused"):
        if gfw_st.get("last_ok") is not True:
            result["gfw"] = probe_gfw()
        else:
            result["gfw"] = {"ok": True, "skipped": "already_ok"}
    elif not gfw_tok:
        result["gfw"] = {"ok": False, "configured": False}
    else:
        result["gfw"] = {"ok": False, "paused": True}

    return result


__all__ = (
    "is_provider_paused",
    "probe_gfw",
    "probe_vesselfinder",
    "provider_state",
    "record_provider_error",
    "record_provider_ok",
    "run_key_activation_cycle",
)
