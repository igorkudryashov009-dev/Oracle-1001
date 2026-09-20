"""Brevo urgent alert dispatcher — Contract 1.8.0-ops-gis-sot."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_keys import CONTRACT_VERSION, get_key

LOG = logging.getLogger("sentinel.notify_service")
ROOT = Path(__file__).resolve().parents[1]
ALERTS_LOG = ROOT / "output" / "archive" / "alerts.log"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_log(entry: dict[str, Any]) -> None:
    try:
        ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ALERTS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("alerts.log write failed: %s", exc)


def validate_alert_payload(body: dict[str, Any] | None) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(body, dict):
        return False, "body_must_be_object", {}
    subject = str(body.get("subject") or "").strip()
    message = str(body.get("message") or body.get("htmlContent") or body.get("textContent") or "").strip()
    to_raw = body.get("to") or body.get("recipients") or []
    if isinstance(to_raw, str):
        to_list = [{"email": to_raw}]
    elif isinstance(to_raw, list):
        to_list = []
        for item in to_raw:
            if isinstance(item, str) and "@" in item:
                to_list.append({"email": item})
            elif isinstance(item, dict) and item.get("email"):
                to_list.append({"email": str(item["email"]), "name": item.get("name")})
    else:
        to_list = []
    if not subject or len(subject) > 200:
        return False, "invalid_subject", {}
    if not message or len(message) > 20000:
        return False, "invalid_message", {}
    if not to_list:
        return False, "missing_recipients", {}
    severity = str(body.get("severity") or "critical").lower()
    return True, "ok", {
        "subject": subject,
        "message": message,
        "to": to_list,
        "severity": severity,
        "tags": body.get("tags") or ["sentinel", "pipeline"],
    }


def dispatch_alert(body: dict[str, Any] | None) -> dict[str, Any]:
    """Validate + send via Brevo; always append delivery status to alerts.log."""
    ok, reason, payload = validate_alert_payload(body)
    base = {
        "contract_version": CONTRACT_VERSION,
        "ts": _now_iso(),
    }
    if not ok:
        out = {**base, "ok": False, "delivered": False, "error": reason}
        _append_log(out)
        return out

    api_key = get_key("BREVO_API_KEY")
    sender_email = (os.getenv("BREVO_SENDER_EMAIL") or "").strip()
    sender_name = (os.getenv("BREVO_SENDER_NAME") or "Oracle-1001 Sentinel").strip()

    if not api_key:
        out = {
            **base,
            "ok": True,
            "delivered": False,
            "dry_run": True,
            "error": "BREVO_API_KEY_missing",
            "subject": payload["subject"],
            "recipients": [t["email"] for t in payload["to"]],
        }
        _append_log(out)
        return out

    if not sender_email or "@" not in sender_email:
        out = {
            **base,
            "ok": True,
            "delivered": False,
            "dry_run": True,
            "error": "BREVO_SENDER_EMAIL_missing",
            "subject": payload["subject"],
            "recipients": [t["email"] for t in payload["to"]],
            "note": "Set BREVO_SENDER_EMAIL in .env to enable live dispatch",
        }
        _append_log(out)
        return out

    brevo_body = {
        "sender": {"email": sender_email, "name": sender_name},
        "to": payload["to"],
        "subject": f"[Sentinel/{payload['severity']}] {payload['subject']}",
        "htmlContent": f"<p>{payload['message']}</p>",
        "tags": payload["tags"],
    }
    req = urllib.request.Request(
        BREVO_URL,
        data=json.dumps(brevo_body).encode("utf-8"),
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Oracle-1001-Sentinel/1.8.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
        out = {
            **base,
            "ok": True,
            "delivered": True,
            "http_status": status,
            "subject": payload["subject"],
            "recipients": [t["email"] for t in payload["to"]],
            "provider_response": raw[:500],
        }
        _append_log(out)
        return out
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        out = {
            **base,
            "ok": False,
            "delivered": False,
            "http_status": exc.code,
            "error": detail or str(exc),
            "subject": payload["subject"],
            "recipients": [t["email"] for t in payload["to"]],
        }
        _append_log(out)
        return out
    except (urllib.error.URLError, TimeoutError) as exc:
        out = {
            **base,
            "ok": False,
            "delivered": False,
            "error": str(exc)[:240],
            "subject": payload["subject"],
            "recipients": [t["email"] for t in payload["to"]],
        }
        _append_log(out)
        return out


__all__ = ("dispatch_alert", "validate_alert_payload", "ALERTS_LOG")
