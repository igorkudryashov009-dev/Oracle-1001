"""Sales letter for a registered pilot. One send per registered status. No LLM digest."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from services.i18n_catalog import catalog_for, negotiate_lang
from services.pilot_register import append_event, find_by_email, has_event

ROOT = Path(__file__).resolve().parents[1]
AUDIT_URL = "https://audit.oracle1001.com"
OFFER_PATH = ROOT / "PILOT_OFFER.md"
OFFER_KEYS = (
    "offer.subject",
    "offer.lead",
    "offer.gis",
    "offer.archive",
    "offer.score",
    "offer.llm_later",
)
UNSUBSCRIBE = "<mailto:unsubscribe@oracle1001.com?subject=unsubscribe>"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _landing_path() -> Path:
    raw = (os.getenv("AUDIT_LANDING_PATH") or "").strip()
    if raw:
        return Path(raw)
    return ROOT / "data" / "archive" / "audit_landing.json"


def _hop(url: str, *, timeout: float = 12.0) -> tuple[int, str | None]:
    """One request. Redirects are not followed; Location is returned instead."""

    class _Stop(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None

    opener = urllib.request.build_opener(_Stop)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Oracle-1001-Sentinel/1.8.0", "Accept": "text/html"},
        method="GET",
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), None
    except urllib.error.HTTPError as exc:
        location = None
        if exc.headers is not None:
            location = exc.headers.get("Location")
        return int(exc.code), location


def check_audit_landing(
    *,
    url: str = AUDIT_URL,
    hop: Callable[[str], tuple[int, str | None]] | None = None,
) -> dict[str, Any]:
    """Require HTTPS, HTTP 200, and no redirect cycle."""
    fetch = hop or _hop
    seen: list[str] = []
    current = url
    status = 0
    for _ in range(6):
        if current in seen:
            return _store_landing(
                {
                    "url": url,
                    "ok": False,
                    "status": status,
                    "tls": current.startswith("https://"),
                    "redirect_loop": True,
                    "hops": len(seen),
                    "error": "redirect_loop",
                }
            )
        seen.append(current)
        if not current.startswith("https://"):
            return _store_landing(
                {
                    "url": url,
                    "ok": False,
                    "status": status,
                    "tls": False,
                    "redirect_loop": False,
                    "hops": len(seen) - 1,
                    "error": "tls_required",
                }
            )
        try:
            status, location = fetch(current)
        except Exception as exc:  # noqa: BLE001
            return _store_landing(
                {
                    "url": url,
                    "ok": False,
                    "status": 0,
                    "tls": True,
                    "redirect_loop": False,
                    "hops": len(seen) - 1,
                    "error": type(exc).__name__,
                }
            )
        if status in {301, 302, 303, 307, 308}:
            if not location:
                return _store_landing(
                    {
                        "url": url,
                        "ok": False,
                        "status": status,
                        "tls": True,
                        "redirect_loop": False,
                        "hops": len(seen) - 1,
                        "error": "redirect_without_location",
                    }
                )
            current = urljoin(current, location)
            continue
        ok = status == 200
        return _store_landing(
            {
                "url": url,
                "ok": ok,
                "status": status,
                "tls": True,
                "redirect_loop": False,
                "hops": len(seen) - 1,
                "error": None if ok else f"http_{status}",
            }
        )
    return _store_landing(
        {
            "url": url,
            "ok": False,
            "status": status,
            "tls": current.startswith("https://"),
            "redirect_loop": True,
            "hops": len(seen),
            "error": "redirect_loop",
        }
    )


def _store_landing(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result)
    result["checked_at"] = _utc_now()
    path = _landing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def landing_digest_line() -> str:
    path = _landing_path()
    if not path.is_file():
        return "- audit_landing: not_checked"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "- audit_landing: unreadable"
    return (
        "- audit_landing: "
        f"ok={bool(doc.get('ok'))} http={doc.get('status')} "
        f"tls={bool(doc.get('tls'))} loop={bool(doc.get('redirect_loop'))}"
    )


def render_offer_email(lang: str, *, company: str) -> tuple[str, str]:
    chosen = negotiate_lang(query=lang)
    strings = catalog_for(chosen).get("strings") or {}
    subject = strings.get("offer.subject") or "Sentinel pilot"
    parts = [strings.get(key) or "" for key in OFFER_KEYS if key != "offer.subject"]
    offer = OFFER_PATH.read_text(encoding="utf-8").strip()
    body = "\n\n".join([company, *[p for p in parts if p], offer]) + "\n"
    return subject, body


def _mailgun_transport(payload: dict[str, str]) -> None:
    key = (os.getenv("MAILGUN_API_KEY") or "").strip()
    domain = (os.getenv("MAILGUN_DOMAIN") or "").strip()
    if not key or not domain:
        raise RuntimeError("mailgun_not_configured")
    sender = (os.getenv("MAILGUN_FROM") or f"pilot@{domain}").strip()
    data = urllib.parse.urlencode(
        {
            "from": sender,
            "to": payload["to"],
            "subject": payload["subject"],
            "text": payload["text"],
            "h:List-Unsubscribe": payload["list_unsubscribe"],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.mailgun.net/v3/{domain}/messages",
        data=data,
        method="POST",
    )
    token = f"api:{key}".encode("utf-8")
    import base64

    req.add_header("Authorization", "Basic " + base64.b64encode(token).decode("ascii"))
    with urllib.request.urlopen(req, timeout=20) as resp:
        if int(resp.status) not in {200, 202}:
            raise RuntimeError(f"mailgun_http_{resp.status}")


def send_pilot_offer(
    email: str,
    lang: str,
    *,
    transport: Callable[[dict[str, str]], None] | None = None,
    hop: Callable[[str], tuple[int, str | None]] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Send one offer. Refuses unless landing is healthy and status is registered."""
    landing = check_audit_landing(hop=hop)
    if not landing.get("ok"):
        try:
            from services.alerts import emit_alert

            emit_alert(
                "audit_landing",
                "audit.oracle1001.com did not return HTTPS 200 without a redirect loop",
                severity="WARN",
                detail={"status": landing.get("status"), "error": landing.get("error")},
            )
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "audit_landing", "landing": landing}

    found = find_by_email(email, db_path=db_path)
    if found is None:
        return {"ok": False, "error": "missing_client"}
    if found.get("status") != "registered":
        return {"ok": False, "error": "not_registered", "status": found.get("status")}
    if has_event(str(found["email"]), "contacted", db_path=db_path):
        return {"ok": False, "error": "already_contacted"}

    subject, text = render_offer_email(lang, company=str(found.get("company") or ""))
    payload = {
        "to": str(found["email"]),
        "subject": subject,
        "text": text,
        "list_unsubscribe": UNSUBSCRIBE,
    }
    post = transport or _mailgun_transport
    try:
        post(payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "mailgun", "detail": type(exc).__name__}

    event = append_event(str(found["email"]), "contacted", "pilot_offer_sent", db_path=db_path)
    return {"ok": True, "event": event, "lang": negotiate_lang(query=lang)}


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--paid":
        from services.pilot_register import record_paid

        result = record_paid(args[1])
        print("paid" if result.get("ok") else f"refused {result.get('error')}")
        return 0 if result.get("ok") else 3
    if len(args) != 2:
        print("usage: scripts/pilot_send.sh <email> <lang>", file=sys.stderr)
        return 2
    result = send_pilot_offer(args[0], args[1])
    if result.get("ok"):
        print("sent contacted")
        return 0
    print(f"refused {result.get('error')}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
