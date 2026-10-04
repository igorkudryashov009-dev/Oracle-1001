#!/usr/bin/env python3
"""Pilot self-register — POST /api/v1/pilot/register (Contract 1.8.0-ops-gis-sot).

Public (no X-API-Key), IP rate-limited 10 req/min. Issues a readonly sk_sent_*
key once; stores SHA-256 hash + mask in pilot_clients (never plaintext).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.api_auth import generate_api_key

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_VERSION = "1.8.0-ops-gis-sot"
PILOT_RATE_LIMIT_PER_MIN = 10
PILOT_KEY_TIER = "readonly"
PILOT_KEY_RATE_LIMIT_LABEL = "60 req/min"

# Pragmatic RFC 5322 local-part / domain (no IP-literals, no comments)
_EMAIL_RE = re.compile(
    r"^(?=.{3,254}$)"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

PILOT_STATUSES = ("registered", "contacted", "onboarded", "active", "paid", "churned")
PILOT_KEY_STATUSES = ("registered", "contacted", "onboarded", "active", "paid")
FUNNEL_STATUSES = ("contacted", "onboarded", "active", "paid")
PILOT_DDL = """
CREATE TABLE IF NOT EXISTS pilot_clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    email TEXT NOT NULL COLLATE NOCASE,
    key_hash TEXT NOT NULL,
    key_mask TEXT NOT NULL,
    vessels TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'registered',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(email)
)
"""
PILOT_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS pilot_client_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
)
"""

_LOCK = threading.RLock()
_IP_HITS: dict[str, deque[float]] = defaultdict(deque)


def _default_db_path() -> Path:
    env = (os.getenv("PILOT_CLIENTS_DB") or "").strip()
    if env:
        return Path(env)
    return ROOT / "data" / "archive" / "pilot_clients.sqlite"


def mask_api_key(key: str) -> str:
    k = (key or "").strip()
    if len(k) <= 4:
        return "****"
    return f"****{k[-4:]}"


def hash_api_key(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(db_path: Path | None = None) -> Path:
    path = Path(db_path) if db_path is not None else _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            conn.execute(PILOT_DDL)
            conn.execute(PILOT_EVENT_DDL)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pilot_clients_key_hash "
                "ON pilot_clients(key_hash)"
            )
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(pilot_clients)")}
            if "lang" not in cols:
                conn.execute("ALTER TABLE pilot_clients ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'")
            conn.commit()
        finally:
            conn.close()
    return path


def validate_register_body(body: Any) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Return (normalized {company, email}, None) or (None, error_message)."""
    if not isinstance(body, dict):
        return None, "JSON body must be an object with company and email"
    company = body.get("company")
    email = body.get("email")
    if company is None or email is None:
        return None, "Required fields: company (str), email (str)"
    if not isinstance(company, str) or not isinstance(email, str):
        return None, "company and email must be strings"
    company_n = company.strip()
    email_n = email.strip()
    if len(company_n) < 2 or len(company_n) > 120:
        return None, "company must be 2..120 characters after trim"
    if not _EMAIL_RE.match(email_n):
        return None, "email failed RFC-pattern validation"
    return {"company": company_n, "email": email_n.lower()}, None


def check_ip_rate_limit(client_ip: str) -> dict[str, Any]:
    """Sliding 60s window — max PILOT_RATE_LIMIT_PER_MIN hits per IP."""
    now = time.monotonic()
    window = 60.0
    bucket = (client_ip or "unknown").strip() or "unknown"
    with _LOCK:
        q = _IP_HITS[bucket]
        while q and (now - q[0]) > window:
            q.popleft()
        if len(q) >= PILOT_RATE_LIMIT_PER_MIN:
            retry = max(1, int(window - (now - q[0])) + 1)
            return {"ok": False, "retry_after": retry}
        q.append(now)
        return {"ok": True, "retry_after": 0}


def reset_ip_rate_limits() -> None:
    """Test helper — clear in-memory IP buckets."""
    with _LOCK:
        _IP_HITS.clear()


def find_by_email(email: str, *, db_path: Path | None = None) -> Optional[dict[str, Any]]:
    path = ensure_schema(db_path)
    with _LOCK:
        conn = sqlite3.connect(str(path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, company, email, key_hash, key_mask, vessels, status, "
                "created_at, updated_at FROM pilot_clients WHERE email = ? COLLATE NOCASE",
                (email.strip().lower(),),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return dict(row)


def lookup_pilot_by_api_key(api_key: str, *, db_path: Path | None = None) -> Optional[dict[str, Any]]:
    """Resolve active pilot client by plaintext key → hash match (no plaintext stored)."""
    key = (api_key or "").strip()
    if not key.startswith("sk_sent_"):
        return None
    digest = hash_api_key(key)
    path = ensure_schema(db_path)
    with _LOCK:
        conn = sqlite3.connect(str(path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, company, email, key_mask, vessels, status, lang "
                "FROM pilot_clients WHERE key_hash = ? AND status IN "
                "('registered', 'contacted', 'onboarded', 'active', 'paid')",
                (digest,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return dict(row)


def append_event(
    email: str,
    to_status: str,
    reason: str,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Append-only transition. The status column is the projection of the last event."""
    if to_status not in PILOT_STATUSES:
        raise ValueError(f"unknown pilot status: {to_status}")
    path = ensure_schema(db_path)
    now = _utc_now()
    with _LOCK:
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            row = conn.execute(
                "SELECT status FROM pilot_clients WHERE email = ? COLLATE NOCASE",
                (email.strip().lower(),),
            ).fetchone()
            if row is None:
                raise ValueError("pilot client missing")
            previous = row[0]
            if previous == to_status:
                return {"email": email, "from_status": previous, "to_status": to_status, "noop": True}
            if previous == "churned":
                raise ValueError("churned clients do not transition")
            if to_status == "contacted" and previous != "registered":
                raise ValueError("contacted requires registered")
            if to_status == "paid" and previous != "active":
                raise ValueError("paid requires active")
            if to_status == "paid" and _event_exists(conn, email, "paid"):
                return {"email": email, "from_status": previous, "to_status": to_status, "noop": True}
            conn.execute(
                "INSERT INTO pilot_client_events (email, from_status, to_status, reason, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (email.strip().lower(), previous, to_status, reason, now),
            )
            conn.execute(
                "UPDATE pilot_clients SET status=?, updated_at=? WHERE email = ? COLLATE NOCASE",
                (to_status, now, email.strip().lower()),
            )
            conn.commit()
        finally:
            conn.close()
    return {"email": email.strip().lower(), "from_status": previous, "to_status": to_status, "at": now}


def count_clients(*, status: str = "active", db_path: Path | None = None) -> int:
    path = Path(db_path) if db_path is not None else _default_db_path()
    if not path.is_file():
        return 0
    with _LOCK:
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM pilot_clients WHERE status=?",
                (status,),
            ).fetchone()
        finally:
            conn.close()
    return int(row[0] if row else 0)


def _event_exists(conn: sqlite3.Connection, email: str, to_status: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pilot_client_events WHERE email = ? COLLATE NOCASE AND to_status = ? LIMIT 1",
        (email.strip().lower(), to_status),
    ).fetchone()
    return row is not None


def has_event(email: str, to_status: str, *, db_path: Path | None = None) -> bool:
    path = ensure_schema(db_path)
    with _LOCK:
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            return _event_exists(conn, email, to_status)
        finally:
            conn.close()


def record_paid(email: str, *, reason: str = "first_payment", db_path: Path | None = None) -> dict[str, Any]:
    """Append the first paid event. A second call does not write another row."""
    path = ensure_schema(db_path)
    normalized = email.strip().lower()
    with _LOCK:
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            if _event_exists(conn, normalized, "paid"):
                return {"ok": False, "error": "already_paid", "email": normalized}
            row = conn.execute(
                "SELECT status FROM pilot_clients WHERE email = ? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return {"ok": False, "error": "missing_client", "email": normalized}
    if row[0] != "active":
        return {"ok": False, "error": "paid_requires_active", "status": row[0], "email": normalized}
    event = append_event(normalized, "paid", reason, db_path=path)
    event["ok"] = True
    event["score_trigger"] = "first_paid"
    return event


def funnel_from_events(*, db_path: Path | None = None) -> dict[str, Any]:
    """Count distinct emails per funnel stage from the append-only event log."""
    counts = {name: 0 for name in FUNNEL_STATUSES}
    path = Path(db_path) if db_path is not None else _default_db_path()
    if not path.is_file():
        return {
            "contacted": 0,
            "onboarded": 0,
            "active": 0,
            "paid": 0,
            "source": "pilot_client_events",
            "paid_score_trigger": False,
        }
    ensure_schema(path)
    with _LOCK:
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            rows = conn.execute(
                "SELECT to_status, COUNT(DISTINCT email) FROM pilot_client_events "
                "WHERE to_status IN ('contacted', 'onboarded', 'active', 'paid') "
                "GROUP BY to_status"
            ).fetchall()
        finally:
            conn.close()
    for status, n in rows:
        if status in counts:
            counts[str(status)] = int(n)
    return {
        **counts,
        "source": "pilot_client_events",
        "paid_score_trigger": counts["paid"] > 0,
    }


def note_first_use(api_key: str, *, db_path: Path | None = None) -> None:
    """Advance one funnel step on an authenticated request. Further calls stay quiet.

    registered or contacted → onboarded. onboarded → active.
    paid and churned do not move.
    """
    found = lookup_pilot_by_api_key(api_key, db_path=db_path)
    if not found:
        return
    status = found.get("status")
    if status in {"active", "paid", "churned"}:
        return
    email = str(found["email"])
    if status == "onboarded":
        append_event(email, "active", "first_authenticated_request", db_path=db_path)
    elif status in {"registered", "contacted"}:
        append_event(email, "onboarded", "first_authenticated_request", db_path=db_path)


def _insert_client(
    *,
    company: str,
    email: str,
    key_hash: str,
    key_mask: str,
    db_path: Path,
) -> None:
    now = _utc_now()
    with _LOCK:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        try:
            conn.execute(
                "INSERT INTO pilot_clients "
                "(company, email, key_hash, key_mask, vessels, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'registered', ?, ?)",
                (company, email, key_hash, key_mask, "[]", now, now),
            )
            conn.execute(
                "INSERT INTO pilot_client_events (email, from_status, to_status, reason, at) "
                "VALUES (?, NULL, 'registered', 'register', ?)",
                (email, now),
            )
            conn.commit()
        finally:
            conn.close()


def _welcome(lang: str, *, company: str) -> tuple[str, str]:
    from services.i18n_catalog import catalog_for, negotiate_lang

    chosen = negotiate_lang(query=lang)
    cat = catalog_for(chosen)
    strings = cat.get("strings") or {}
    title = strings.get("welcome.title") or "Pilot"
    body = strings.get("welcome.body") or ""
    text = f"# {title}\n\n{company}\n\n{body}\n"
    return chosen, text


def handle_pilot_register(
    body: Any,
    *,
    client_ip: str,
    db_path: Path | None = None,
    apply_rate_limit: bool = True,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Core register handler.

    Returns (http_status, json_payload, extra_headers).
    """
    headers: dict[str, str] = {"X-Contract-Version": CONTRACT_VERSION}

    if apply_rate_limit:
        rl = check_ip_rate_limit(client_ip)
        if not rl["ok"]:
            headers["Retry-After"] = str(int(rl["retry_after"]))
            return (
                429,
                {
                    "ok": False,
                    "error": "rate_limited",
                    "detail": f"Max {PILOT_RATE_LIMIT_PER_MIN} req/min per IP",
                    "retry_after": int(rl["retry_after"]),
                },
                headers,
            )

    normalized, err = validate_register_body(body)
    if err or normalized is None:
        return (
            400,
            {"ok": False, "error": "validation_error", "detail": err or "invalid_input"},
            headers,
        )

    path = ensure_schema(db_path)
    existing = find_by_email(normalized["email"], db_path=path)
    if existing is not None:
        return (
            200,
            {"ok": True, "existing": True},
            headers,
        )

    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_mask = mask_api_key(api_key)
    try:
        _insert_client(
            company=normalized["company"],
            email=normalized["email"],
            key_hash=key_hash,
            key_mask=key_mask,
            db_path=path,
        )
    except sqlite3.IntegrityError:
        # Race: concurrent register for same email
        return (200, {"ok": True, "existing": True}, headers)

    lang_raw = ""
    if isinstance(body, dict) and isinstance(body.get("lang"), str):
        lang_raw = body.get("lang") or ""
    lang, welcome = _welcome(lang_raw, company=normalized["company"])
    base = (os.getenv("SENTINEL_PUBLIC_BASE") or "http://45.8.230.214:8765").rstrip("/")
    hud_url = (
        f"{base}/output/sentinel_dashboard.html?lang={lang}&key={api_key}"
    )
    return (
        200,
        {
            "ok": True,
            "api_key": api_key,
            "tier": PILOT_KEY_TIER,
            "rate_limit": PILOT_KEY_RATE_LIMIT_LABEL,
            "status": "registered",
            "lang": lang,
            "welcome_md": welcome,
            "hud_url": hud_url,
            "key_mask": key_mask,
        },
        headers,
    )


__all__ = [
    "CONTRACT_VERSION",
    "PILOT_RATE_LIMIT_PER_MIN",
    "check_ip_rate_limit",
    "ensure_schema",
    "find_by_email",
    "funnel_from_events",
    "handle_pilot_register",
    "record_paid",
    "hash_api_key",
    "lookup_pilot_by_api_key",
    "mask_api_key",
    "reset_ip_rate_limits",
    "validate_register_body",
]
