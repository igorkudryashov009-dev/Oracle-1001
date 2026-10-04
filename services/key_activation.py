#!/usr/bin/env python3
"""Auto-activation / pause for VF + GFW provider keys (env-only secrets).

On install_key signal or key fingerprint change: force probe even if paused.
Success → immediate first job run + today resnapshot (≤30 min path).
Provider errors: pause after 3; re-probe paused keys at most 1/6h.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.key_activation")
STATE_PATH = ROOT / "data" / "archive" / "provider_activation.json"

PROVIDERS = ("vesselfinder", "gfw")
REPROBE_COOLDOWN_SEC = 6 * 3600


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        raw = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


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
    """After 3 consecutive provider errors → pause + alert (deduped)."""
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
            detail={"error": str(error)[:120], "provider": name},
        )
    return _set_provider(name, **fields)


def record_provider_ok(name: str) -> dict[str, Any]:
    from services.alerts import resolve_alert

    resolve_alert(f"provider_paused_{name}", reason="probe_ok")
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


def _maybe_reset_for_new_key(name: str, key_value: str) -> bool:
    """If fingerprint changed vs stored → clear pause and allow probe. Return True if new."""
    fp = _fingerprint(key_value)
    cur = provider_state(name)
    old_fp = cur.get("key_fp")
    if old_fp and old_fp == fp:
        return False
    _set_provider(
        name,
        key_fp=fp,
        paused=False,
        paused_reason=None,
        consecutive_errors=0,
        last_ok=False,
        last_error=None,
        key_changed_at=_utc_iso(),
    )
    return True


def _cooldown_blocks_probe(name: str) -> bool:
    """Paused providers: at most one re-probe / 6h (unless new key)."""
    cur = provider_state(name)
    if not cur.get("paused"):
        return False
    last = _parse_iso(cur.get("last_probe_at"))
    if last is None:
        return False
    return (_utc_now() - last).total_seconds() < REPROBE_COOLDOWN_SEC


def probe_vesselfinder(*, force: bool = False) -> dict[str, Any]:
    """One cheap AIS-only GET; never prints key."""
    import requests
    from services.runtime_env import apply_runtime_env
    from services.vesselfinder_client import VESSELS_URL, resolve_userkey

    apply_runtime_env()
    key = resolve_userkey()
    if not key:
        return {"ok": False, "configured": False, "error": "no_key"}
    new_key = _maybe_reset_for_new_key("vesselfinder", key)
    if (is_provider_paused("vesselfinder") and not force and not new_key) and _cooldown_blocks_probe(
        "vesselfinder"
    ):
        return {
            "ok": False,
            "configured": True,
            "error": "paused_cooldown",
            "paused": True,
            "cooldown_sec": REPROBE_COOLDOWN_SEC,
        }

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
        masked = _mask_last4(key)
        if invalid:
            record_provider_error("vesselfinder", "invalid_key")
            _alert_vf_invalid_key(masked)
            return {
                "ok": False,
                "configured": True,
                "status": "invalid_key",
                "error": "invalid_key",
                "http_status": resp.status_code,
                "key_masked": masked,
                "new_key": new_key,
            }
        if resp.status_code == 200 and not err:
            record_provider_ok("vesselfinder")
            _set_provider("vesselfinder", key_fp=_fingerprint(key))
            _clear_vf_invalid_alert()
            return {
                "ok": True,
                "configured": True,
                "status": "active",
                "http_status": 200,
                "key_masked": masked,
                "new_key": new_key,
            }
        record_provider_error("vesselfinder", err or f"http_{resp.status_code}")
        return {"ok": False, "configured": True, "error": err or f"http_{resp.status_code}"}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if key:
            msg = msg.replace(key, "***")
        record_provider_error("vesselfinder", msg)
        return {"ok": False, "configured": True, "error": msg[:160]}


def probe_gfw(*, force: bool = False) -> dict[str, Any]:
    from services.gfw_events import fetch_events_for_imo, resolve_token
    from services.runtime_env import apply_runtime_env

    apply_runtime_env()
    tok = resolve_token()
    if not tok:
        return {"ok": False, "configured": False, "error": "no_token"}
    new_key = _maybe_reset_for_new_key("gfw", tok)
    if (is_provider_paused("gfw") and not force and not new_key) and _cooldown_blocks_probe("gfw"):
        return {
            "ok": False,
            "configured": True,
            "error": "paused_cooldown",
            "paused": True,
            "cooldown_sec": REPROBE_COOLDOWN_SEC,
        }
    try:
        out = fetch_events_for_imo("9388819", use_cache=False)
        if out.get("auth_failed") or str(out.get("error") or "").startswith("auth_http_"):
            err = str(out.get("error") or "auth_failed")
            record_provider_error("gfw", err)
            return {"ok": False, "configured": True, "error": err, "auth_failed": True}
        if out.get("ok") and out.get("verify") is True:
            record_provider_ok("gfw")
            _set_provider("gfw", key_fp=_fingerprint(tok))
            return {
                "ok": True,
                "configured": True,
                "events_n": out.get("events_n") or 0,
                "new_key": new_key,
            }
        # vessel_not_found is not auth success for activation
        if out.get("ok") and out.get("note") == "vessel_not_found_in_gfw_identity":
            # Token accepted enough to search, but probe IMO unknown — soft ok only if no auth
            record_provider_ok("gfw")
            _set_provider("gfw", key_fp=_fingerprint(tok))
            return {
                "ok": True,
                "configured": True,
                "events_n": 0,
                "new_key": new_key,
                "note": "probe_imo_not_in_gfw_identity",
            }
        err = str(out.get("error") or "gfw_probe_failed")
        if err == "not_configured":
            return {"ok": False, "configured": False, "error": err}
        record_provider_error("gfw", err)
        return {"ok": False, "configured": True, "error": err}
    except Exception as exc:  # noqa: BLE001
        record_provider_error("gfw", str(exc))
        return {"ok": False, "configured": True, "error": str(exc)[:160]}


def _alert_vf_invalid_key(masked: str) -> None:
    """Deduped alert. The message carries the mask only."""
    from services.alerts import emit_alert

    emit_alert(
        "vf_invalid_key",
        f"VesselFinder invalid_key {masked}",
        severity="WARN",
        detail={"provider": "vesselfinder", "status": "invalid_key", "key_masked": masked},
    )


def _clear_vf_invalid_alert() -> None:
    from services.alerts import resolve_alert

    resolve_alert("vf_invalid_key", reason="probe_ok")


def vf_health_block() -> dict[str, Any]:
    """Watcher-facing VF channel. No raw key."""
    from services.vesselfinder_client import resolve_userkey

    key = (resolve_userkey() or "").strip()
    st = provider_state("vesselfinder")
    err = str(st.get("last_error") or "")
    if st.get("last_ok") is True and not st.get("paused"):
        status = "active"
    elif not key:
        status = "parked"
    elif err == "invalid_key" or "Invalid Userkey" in err:
        status = "invalid_key"
    elif st.get("paused"):
        status = "paused"
    else:
        status = "parked"
    masked = _mask_last4(key) if key else None
    note = {
        "active": "active",
        "invalid_key": "Invalid Userkey",
        "paused": "paused",
    }.get(status, "awaiting new key — function covered by GFW")
    return {
        "status": status,
        "configured": bool(key),
        "paused": bool(st.get("paused")),
        "last_ok": st.get("last_ok"),
        "key_masked": masked,
        "note": note,
    }


def _mask_last4(secret: str) -> str:
    text = (secret or "").strip()
    if len(text) < 4:
        return "****"
    return "****" + text[-4:]


def probe_anthropic(*, force: bool = False) -> dict[str, Any]:
    """HTTP probe against Anthropic. A stored string alone is not activation."""
    from services.llm_router import anthropic_models_probe
    from services.runtime_env import getenv_secret

    key = getenv_secret("ANTHROPIC_API_KEY")
    if not key:
        return {"ok": False, "configured": False}
    masked = _mask_last4(key)
    needs = force or provider_state("anthropic").get("last_ok") is not True or _maybe_reset_for_new_key("anthropic", key)
    if not needs:
        return {"ok": True, "configured": True, "masked": masked, "llm_unlocked": True, "skipped": "already_ok"}
    probed = anthropic_models_probe(key)
    if not probed.get("ok"):
        err = str(probed.get("error") or "invalid_key")
        record_provider_error("anthropic", err)
        return {"ok": False, "configured": True, "error": err, "masked": masked}
    record_provider_ok("anthropic")
    return {"ok": True, "configured": True, "masked": masked, "llm_unlocked": True}


def _force_today_resnapshot() -> dict[str, Any]:
    from services.archive_snapshot_worker import take_daily_snapshot

    today = _utc_now().strftime("%Y-%m-%d")
    r = take_daily_snapshot(snapshot_date=today, export=False)
    return {
        "ok": bool(r.get("ok")),
        "stored": r.get("stored_for_date"),
        "overlay": r.get("live_ais_overlay"),
        "date": today,
    }


def _log_anthropic_probe(probed: dict[str, Any]) -> None:
    """Append-only probe failure. The key itself stays out of job_log."""
    from services.job_log import log_job_finish, log_job_start

    row_id = log_job_start("key_activation_anthropic")
    log_job_finish(
        row_id,
        status="error",
        rows_affected=0,
        error=str(probed.get("error") or "probe_failed")[:120],
        detail={
            "provider": "anthropic",
            "phase": "probe",
            "masked": probed.get("masked"),
        },
    )


def _immediate_first_run(provider: str) -> dict[str, Any]:
    """After successful probe: run the matching job + today resnapshot + acceptance."""
    out: dict[str, Any] = {"provider": provider}
    if provider in {"gfw", "gfw_api_token"}:
        from services.gfw_events import run_daily_gfw_batch

        out["gfw_poll"] = run_daily_gfw_batch(limit=20, dry_run=False)
        out["resnapshot"] = _force_today_resnapshot()
        try:
            from services.acceptance import maybe_rerun_acceptance_after_verification

            verified = int(
                (out["gfw_poll"] or {}).get("flagged")
                or (out["gfw_poll"] or {}).get("events_total")
                or 0
            )
            # Never treat fetched/planned as verified (401-as-empty bug class)
            if verified > 0 and not (out["gfw_poll"] or {}).get("auth_failed"):
                acc = maybe_rerun_acceptance_after_verification(
                    channel="gfw",
                    verified_hint=verified,
                    job_detail=out["gfw_poll"] or {},
                )
                if acc:
                    out["acceptance"] = {
                        "status": acc.get("status"),
                        "transition": acc.get("transition"),
                        "fully_commissioned_at": acc.get("fully_commissioned_at"),
                    }
        except Exception:  # noqa: BLE001
            pass
    elif provider in {"vesselfinder", "vf", "vesselfinder_api_key"}:
        from services.vf_allocator_runner import run_vf_allocator_live

        out["vf_allocator"] = run_vf_allocator_live(dry_run=False)
        out["resnapshot"] = _force_today_resnapshot()
        try:
            from services.acceptance import maybe_rerun_acceptance_after_verification

            billed = int((out["vf_allocator"] or {}).get("billed") or 0)
            if billed > 0:
                acc = maybe_rerun_acceptance_after_verification(
                    channel="vf", verified_hint=billed, job_detail=out["vf_allocator"]
                )
                if acc:
                    out["acceptance"] = {
                        "status": acc.get("status"),
                        "transition": acc.get("transition"),
                        "fully_commissioned_at": acc.get("fully_commissioned_at"),
                    }
        except Exception:  # noqa: BLE001
            pass
    elif provider in {"anthropic", "anthropic_api_key"}:
        from services.job_log import log_job_finish, log_job_start
        from services.llm_router import run_daily_brief

        row_id = log_job_start("key_activation_anthropic")
        brief = run_daily_brief()
        chars = int(brief.get("chars") or 0)
        if not chars:
            chars = len(str(brief.get("text") or brief.get("markdown") or ""))
        out["daily_brief"] = {
            "ok": bool(brief.get("ok")),
            "status": brief.get("status"),
            "configured": bool(brief.get("configured")),
            "chars": chars,
            "prompt_hash": brief.get("prompt_hash"),
            "source_rows": brief.get("source_rows"),
        }
        out["llm_unlocked"] = True
        out["resnapshot"] = _force_today_resnapshot()
        log_job_finish(
            row_id,
            status="ok" if brief.get("ok") else "error",
            rows_affected=1 if chars else 0,
            detail={"provider": "anthropic", "masked": probe_anthropic().get("masked")},
        )
        if brief.get("ok") and chars:
            try:
                from services.acceptance import maybe_rerun_acceptance_after_verification

                acc = maybe_rerun_acceptance_after_verification(
                    channel="anthropic", verified_hint=1, job_detail={"verified_n": 1}
                )
                if acc:
                    out["acceptance"] = {
                        "status": acc.get("status"),
                        "transition": acc.get("transition"),
                        "fully_commissioned_at": acc.get("fully_commissioned_at"),
                        "trigger": acc.get("trigger"),
                    }
            except Exception:  # noqa: BLE001
                pass
    return out


def run_key_activation_cycle(*, force_providers: list[str] | None = None) -> dict[str, Any]:
    """Watchdog / signal path — probe when key present; immediate run on success."""
    from services.runtime_env import apply_runtime_env, consume_install_signal

    apply_runtime_env()
    signal = consume_install_signal()
    forced = set(force_providers or [])
    if signal:
        forced.add(signal)
        # Normalize aliases
        if signal in {"vf", "vesselfinder_api_key"}:
            forced.add("vesselfinder")
        if signal in {"gfw_api_token"}:
            forced.add("gfw")
        if signal in {"satellite", "sat", "satellite_api_key"}:
            forced.add("satellite")

    result: dict[str, Any] = {
        "vf": None,
        "gfw": None,
        "anthropic": None,
        "signal": signal,
        "immediate_runs": [],
    }

    vf_key = (
        os.getenv("VESSELFINDER_API_KEY")
        or os.getenv("VESSEL_FINDER_USERKEY")
        or ""
    ).strip()
    vf_st = provider_state("vesselfinder")
    force_vf = "vesselfinder" in forced or "vf" in forced
    if vf_key and (force_vf or not vf_st.get("paused") or not _cooldown_blocks_probe("vesselfinder")):
        if force_vf or vf_st.get("last_ok") is not True or _maybe_reset_for_new_key("vesselfinder", vf_key):
            # Re-check fingerprint may have already updated state
            result["vf"] = probe_vesselfinder(force=force_vf)
            if result["vf"].get("ok"):
                run = _immediate_first_run("vesselfinder")
                result["immediate_runs"].append(run)
        else:
            result["vf"] = {"ok": True, "skipped": "already_ok"}
    elif not vf_key:
        result["vf"] = {"ok": False, "configured": False}
    else:
        result["vf"] = {"ok": False, "paused": True, "cooldown": _cooldown_blocks_probe("vesselfinder")}

    from services.gfw_events import resolve_token

    gfw_tok = resolve_token()
    gfw_st = provider_state("gfw")
    force_gfw = "gfw" in forced
    if gfw_tok and (force_gfw or not gfw_st.get("paused") or not _cooldown_blocks_probe("gfw")):
        if force_gfw or gfw_st.get("last_ok") is not True or _maybe_reset_for_new_key("gfw", gfw_tok):
            result["gfw"] = probe_gfw(force=force_gfw)
            if result["gfw"].get("ok"):
                run = _immediate_first_run("gfw")
                result["immediate_runs"].append(run)
        else:
            result["gfw"] = {"ok": True, "skipped": "already_ok"}
    elif not gfw_tok:
        result["gfw"] = {"ok": False, "configured": False}
    else:
        result["gfw"] = {"ok": False, "paused": True, "cooldown": _cooldown_blocks_probe("gfw")}

    anth_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    anth_st = provider_state("anthropic")
    force_anth = "anthropic" in forced or "anthropic_api_key" in forced
    if anth_key and (force_anth or anth_st.get("last_ok") is not True or _maybe_reset_for_new_key("anthropic", anth_key)):
        result["anthropic"] = probe_anthropic(force=force_anth)
        if result["anthropic"].get("ok"):
            result["immediate_runs"].append(_immediate_first_run("anthropic"))
        else:
            _log_anthropic_probe(result["anthropic"])
    elif anth_key:
        result["anthropic"] = {"ok": True, "skipped": "already_ok", "llm_unlocked": True, "masked": _mask_last4(anth_key)}
    else:
        result["anthropic"] = {"ok": False, "configured": False}

    from services.satellite_adapter import armed, probe as probe_satellite

    force_sat = "satellite" in forced
    if armed() and (force_sat or provider_state("satellite").get("last_ok") is not True):
        result["satellite"] = probe_satellite()
        if result["satellite"].get("status") == "active":
            record_provider_ok("satellite")
    else:
        result["satellite"] = {"status": "parked", "requests": 0}

    return result


__all__ = (
    "is_provider_paused",
    "probe_anthropic",
    "probe_gfw",
    "probe_vesselfinder",
    "provider_state",
    "vf_health_block",
    "record_provider_error",
    "record_provider_ok",
    "run_key_activation_cycle",
)
