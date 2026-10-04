#!/usr/bin/env python3
"""LLM daily brief router — Anthropic Haiku, read-only narrative (never scores/positions).

Contract: brief lands in llm_daily_brief only. Dual Gate / archive / quant untouched.
Fallback without key or on 429 → llm_status=degraded, pipeline stays NOMINAL.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
_ENV_FILE = ROOT / ".env"
_DOTENV_LLM_NAMES = ("HTTPS_PROXY", "https_proxy", "LLM_RUNNER")
LOG = logging.getLogger("sentinel.llm_router")
BUDGET_PATH = ROOT / "data" / "archive" / "llm_budget.json"
MONTHLY_USD_CAP = 60.0
MAX_OUT_TOKENS = 2000
MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_ORIGIN_DEFAULT = "https://api.anthropic.com"
HTTP_TIMEOUT_SEC = 30.0
NETWORK_RETRIES = 2

_LOCK = threading.RLock()

# Published list rates for claude-haiku-4-5 (USD per million tokens).
# The billed counter is these rates times Messages `usage` token counts.
# A missing usage object is not replaced with a character-length guess.
INPUT_USD_PER_MTOK = 1.0
OUTPUT_USD_PER_MTOK = 5.0
LANG_NAMES = {
    "en": "English",
    "ru": "Russian",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
    "zh": "Chinese",
    "ja": "Japanese",
    "ar": "Arabic",
    "ko": "Korean",
}

LLM_BRIEF_DDL = """
CREATE TABLE IF NOT EXISTS llm_daily_brief (
    brief_date TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'en',
    markdown TEXT NOT NULL,
    source_rows INTEGER NOT NULL DEFAULT 0,
    prompt_hash TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (brief_date, lang)
)
"""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_key(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def resolve_anthropic_key() -> str:
    try:
        from services.runtime_env import getenv_secret

        v = getenv_secret("ANTHROPIC_API_KEY")
        if v:
            return v.strip()
    except Exception:  # noqa: BLE001
        pass
    return (os.getenv("ANTHROPIC_API_KEY") or "").strip()


def _apply_llm_dotenv() -> None:
    """Fill HTTPS_PROXY and LLM_RUNNER from .env. Does not override the process."""
    path = _ENV_FILE
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        name = name.strip()
        if name in _DOTENV_LLM_NAMES:
            found[name] = val.strip().strip('"').strip("'")
    for name in _DOTENV_LLM_NAMES:
        if name in found and not (os.getenv(name) or "").strip():
            os.environ[name] = found[name]


def llm_runner_mode() -> str:
    """direct calls Anthropic from this process. remote skips the Korolev timer."""
    _apply_llm_dotenv()
    raw = (os.getenv("LLM_RUNNER") or "").strip().lower()
    return "remote" if raw == "remote" else "direct"


def anthropic_origin() -> str:
    raw = (os.getenv("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")
    return raw or ANTHROPIC_ORIGIN_DEFAULT


def health_status_for_error(err: str) -> str:
    """Health status. A geo 403 is egress_blocked, not a bad key and not a code bug."""
    code = (err or "").strip()
    if code == "invalid_key":
        return "invalid_key"
    if code in {"request_not_allowed", "egress_blocked"}:
        return "egress_blocked"
    return "degraded"


def _build_opener():
    """Use HTTPS_PROXY from the process or from .env. Otherwise open direct."""
    import urllib.request

    _apply_llm_dotenv()
    proxy = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "").strip()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def _open(req, timeout: float):
    return _build_opener().open(req, timeout=timeout)


def _is_timeout(exc: BaseException) -> bool:
    import socket
    import urllib.error

    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError) and "timed out" in str(reason or exc).lower():
        return True
    return "timed out" in str(exc).lower()


def _alert_network(err: str) -> None:
    try:
        from services.alerts import emit_alert

        if err in {"request_not_allowed", "egress_blocked"}:
            emit_alert(
                "llm_network",
                "Anthropic egress from this node is geo-blocked (HTTP 403 request_not_allowed). "
                "Bypass: set HTTPS_PROXY=http://user:pass@vps:port, or set LLM_RUNNER=remote "
                "and run scripts/llm_brief_runner.py on the MSI.",
                severity="WARN",
                detail={"error": "egress_blocked", "bypass": ["HTTPS_PROXY", "LLM_RUNNER=remote"]},
            )
            return
        emit_alert(
            "llm_network",
            "Anthropic request failed after retries",
            severity="WARN",
            detail={"error": err, "retries": NETWORK_RETRIES},
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("llm network alert failed: %s", type(exc).__name__)


def anthropic_request(
    method: str,
    path: str,
    *,
    key: str,
    payload: dict[str, Any] | None = None,
    alert: bool = True,
) -> dict[str, Any]:
    """Call Anthropic. Network errors retry twice, then degraded + alert.

    The key and any proxy password stay out of the returned dict.
    """
    import urllib.error
    import urllib.request

    url = anthropic_origin() + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    last_error = "network"
    attempts = 1 + NETWORK_RETRIES
    for _attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _open(req, HTTP_TIMEOUT_SEC) as resp:
                raw = resp.read()
                if int(resp.status) != 200:
                    err = f"http_{int(resp.status)}"
                    return {"ok": False, "error": err, "status": health_status_for_error(err)}
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    parsed = {}
                return {"ok": True, "json": parsed if isinstance(parsed, dict) else {}, "status": "ok"}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            err = classify_anthropic_error(int(exc.code), body)
            if alert and err == "request_not_allowed":
                _alert_network(err)
            return {"ok": False, "error": err, "status": health_status_for_error(err)}
        except Exception as exc:  # noqa: BLE001
            last_error = "timeout" if _is_timeout(exc) else "network"
            LOG.warning("anthropic %s attempt failed: %s", method, last_error)
            continue
    if alert:
        _alert_network(last_error)
    return {"ok": False, "error": last_error, "status": "degraded"}


def _load_budget() -> dict[str, Any]:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if BUDGET_PATH.is_file():
        try:
            data = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("month") == month:
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return _empty_budget(month)


def _save_budget(bud: dict[str, Any]) -> None:
    BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    used = float(bud.get("used") or 0.0)
    cap = float(bud.get("monthly_usd_cap") or MONTHLY_USD_CAP)
    bud["used"] = round(used, 4)
    bud["remaining"] = round(max(0.0, cap - used), 4)
    bud["monthly_usd_cap"] = cap
    tmp = BUDGET_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(bud, indent=2), encoding="utf-8")
    tmp.replace(BUDGET_PATH)


def _db_path() -> Path:
    from services.storage import DEFAULT_DB

    return Path(os.environ.get("SENTINEL_DB_PATH") or DEFAULT_DB)


def ensure_brief_schema(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if own:
        conn = sqlite3.connect(str(_db_path()), timeout=30)
    assert conn is not None
    try:
        conn.execute(LLM_BRIEF_DDL)
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(llm_daily_brief)")}
        if cols and "lang" not in cols:
            conn.execute(
                """
                CREATE TABLE llm_daily_brief_v2 (
                    brief_date TEXT NOT NULL,
                    lang TEXT NOT NULL DEFAULT 'en',
                    markdown TEXT NOT NULL,
                    source_rows INTEGER NOT NULL DEFAULT 0,
                    prompt_hash TEXT NOT NULL,
                    model TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (brief_date, lang)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO llm_daily_brief_v2
                    (brief_date, lang, markdown, source_rows, prompt_hash, model, created_at)
                SELECT brief_date, 'en', markdown, source_rows, prompt_hash, model, created_at
                FROM llm_daily_brief
                """
            )
            conn.execute("DROP TABLE llm_daily_brief")
            conn.execute("ALTER TABLE llm_daily_brief_v2 RENAME TO llm_daily_brief")
        conn.commit()
    finally:
        if own:
            conn.close()


def _gather_digest(*, day: str) -> tuple[str, int]:
    """Build compact gap digest from archive + gfw events (no secrets)."""
    db = _db_path()
    if not db.is_file():
        return f"No DB for {day}.", 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows_n = 0
    lines: list[str] = [f"Date UTC: {day}", "Gap / GFW digest (read-only):"]
    try:
        ensure_brief_schema()
        cur = conn.execute(
            """
            SELECT imo, source, gap_hours, COALESCE(gfw_verified,0), COALESCE(gfw_events_n,0)
            FROM vessel_daily_archive
            WHERE snapshot_date = ?
              AND gap_hours IS NOT NULL AND gap_hours > 48
            ORDER BY gap_hours DESC
            LIMIT 40
            """,
            (day,),
        )
        gap_rows = cur.fetchall()
        rows_n += len(gap_rows)
        lines.append(f"gap_48h_n={len(gap_rows)} (showing top {min(40, len(gap_rows))})")
        for imo, source, gap, gv, ge in gap_rows[:25]:
            lines.append(
                f"- IMO {imo} source={source} gap_h={gap} gfw_ver={gv} gfw_ev={ge}"
            )
        try:
            ev = conn.execute(
                """
                SELECT imo, event_type, COUNT(*)
                FROM vessel_gfw_events
                WHERE date(start_utc) >= date(?, '-1 day')
                GROUP BY imo, event_type
                ORDER BY COUNT(*) DESC
                LIMIT 30
                """,
                (day,),
            ).fetchall()
            rows_n += len(ev)
            lines.append(f"gfw_event_groups={len(ev)}")
            for imo, et, n in ev[:20]:
                lines.append(f"- IMO {imo} type={et} n={n}")
        except sqlite3.Error:
            lines.append("gfw_events: table unavailable")
    finally:
        conn.close()
    text = "\n".join(lines)
    if len(text) > 12000:
        text = text[:12000] + "\n…truncated"
    return text, rows_n


def _cost_usd(*, input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1e6) * INPUT_USD_PER_MTOK + (output_tokens / 1e6) * OUTPUT_USD_PER_MTOK


def _empty_budget(month: str | None = None) -> dict[str, Any]:
    month_s = month or datetime.now(timezone.utc).strftime("%Y-%m")
    return {
        "month": month_s,
        "monthly_usd_cap": MONTHLY_USD_CAP,
        "used": 0.0,
        "remaining": MONTHLY_USD_CAP,
        "input_tokens": 0,
        "output_tokens": 0,
        "days": {},
        "cost_source": "messages.usage",
        "alert_80": False,
    }


def brief_row_count() -> int:
    db = _db_path()
    if not db.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(llm_daily_brief)")}
            if "markdown" not in cols:
                return 0
            return int(conn.execute("SELECT COUNT(*) FROM llm_daily_brief").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def llm_health_block() -> dict[str, Any]:
    key = resolve_anthropic_key()
    bud = _load_budget()
    rows = brief_row_count()
    runner = llm_runner_mode()
    if runner == "remote":
        if rows > 0:
            return {
                "status": "ok",
                "configured": True,
                "rows": rows,
                "runner": "remote",
                "model": MODEL,
                "note": "brief pushed by the MSI runner",
                "budget": bud,
            }
        return {
            "status": "degraded",
            "configured": bool(key),
            "rows": 0,
            "runner": "remote",
            "note": "LLM_RUNNER=remote; waiting for the MSI brief",
            "budget": bud,
        }
    if not key:
        return {
            "status": "degraded",
            "configured": False,
            "rows": rows,
            "runner": "direct",
            "note": "ANTHROPIC_API_KEY missing — install_key.sh ANTHROPIC",
            "budget": bud,
        }
    if float(bud.get("remaining") or 0) <= 0:
        return {
            "status": "degraded",
            "configured": True,
            "rows": rows,
            "note": "monthly_usd_cap exhausted",
            "budget": bud,
        }
    try:
        from services.key_activation import provider_state

        prov = provider_state("anthropic")
    except Exception:  # noqa: BLE001
        prov = {}
    if prov.get("last_ok") is not True and prov.get("last_error"):
        err = str(prov.get("last_error"))
        return {
            "status": health_status_for_error(err),
            "configured": True,
            "rows": rows,
            "runner": "direct",
            "model": MODEL,
            "note": err,
            "budget": bud,
        }
    if rows > 0:
        return {
            "status": "ok",
            "configured": True,
            "rows": rows,
            "runner": "direct",
            "model": MODEL,
            "note": "llm_daily_brief has rows; daily job only",
            "budget": bud,
        }
    return {
        "status": "armed",
        "configured": True,
        "rows": 0,
        "runner": "direct",
        "model": MODEL,
        "note": "key present; llm_daily_brief empty",
        "budget": bud,
    }


def normalize_lang(lang: str | None) -> str:
    code = (lang or "en").strip().lower()[:8]
    return code if code in LANG_NAMES else "en"


def _scrub(text: str) -> str:
    """Drop key-shaped tokens before a brief is stored or returned."""
    import re

    cleaned = re.sub(r"sk-ant-[A-Za-z0-9_\-]{8,}|sk_sent_[A-Za-z0-9]+", "****", text)
    return re.sub(r"(https?://)[^/\s:@]+:[^@\s/]+@", r"\1****@", cleaned)


def _budget_ratio(bud: dict[str, Any]) -> float:
    cap = float(bud.get("monthly_usd_cap") or MONTHLY_USD_CAP)
    if cap <= 0:
        return 1.0
    return float(bud.get("used") or 0.0) / cap


def _note_budget_skip(day_s: str, bud: dict[str, Any]) -> None:
    """Append-only job_log row. Does not rewrite earlier rows."""
    try:
        from services.job_log import log_job_finish, log_job_start

        row_id = log_job_start("llm_daily_brief")
        log_job_finish(
            row_id,
            status="skipped",
            rows_affected=0,
            error="budget_cap_100",
            detail={
                "reason": "monthly_usd_cap_100_skip_until_month_end",
                "month": bud.get("month"),
                "used": bud.get("used"),
                "cap": bud.get("monthly_usd_cap"),
                "day": day_s,
                "cost_source": bud.get("cost_source"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("budget skip log failed: %s", type(exc).__name__)


def _maybe_alert_80(bud: dict[str, Any]) -> dict[str, Any]:
    if _budget_ratio(bud) < 0.8 or bud.get("alert_80"):
        return bud
    try:
        from services.alerts import emit_alert

        emit_alert(
            "llm_budget_80",
            "Anthropic monthly spend reached 80% of the $60 cap",
            severity="WARN",
            detail={
                "month": bud.get("month"),
                "used": bud.get("used"),
                "cap": bud.get("monthly_usd_cap"),
                "cost_source": bud.get("cost_source"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("budget alert failed: %s", type(exc).__name__)
    bud["alert_80"] = True
    _save_budget(bud)
    return bud


def apply_messages_usage(usage: dict[str, Any] | None, *, day: str | None = None) -> dict[str, Any]:
    """Add one Messages `usage` object to the month/day ledger.

    Refuses to invent tokens when the API omitted `usage`.
    """
    if not isinstance(usage, dict):
        raise ValueError("usage_absent")
    if "input_tokens" not in usage and "output_tokens" not in usage:
        raise ValueError("usage_absent")
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    cost = _cost_usd(input_tokens=in_tok, output_tokens=out_tok)
    day_s = day or _day_key()
    with _LOCK:
        bud = _load_budget()
        days = dict(bud.get("days") or {})
        slot = dict(days.get(day_s) or {})
        slot["input_tokens"] = int(slot.get("input_tokens") or 0) + in_tok
        slot["output_tokens"] = int(slot.get("output_tokens") or 0) + out_tok
        slot["usd"] = round(float(slot.get("usd") or 0.0) + cost, 6)
        slot["requests"] = int(slot.get("requests") or 0) + 1
        days[day_s] = slot
        bud["days"] = days
        bud["input_tokens"] = int(bud.get("input_tokens") or 0) + in_tok
        bud["output_tokens"] = int(bud.get("output_tokens") or 0) + out_tok
        bud["used"] = round(float(bud.get("used") or 0.0) + cost, 6)
        bud["cost_source"] = "messages.usage"
        _save_budget(bud)
        return _maybe_alert_80(bud)


def refresh_cost_report(key: str) -> dict[str, Any] | None:
    """Org cost report when the key is an admin key. Standard keys get None."""
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    path = (
        "/v1/organizations/cost_report"
        f"?starting_at={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&ending_at={now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        "&bucket_width=1d"
    )
    result = anthropic_request("GET", path, key=key, alert=False)
    if not result.get("ok"):
        LOG.info("cost_report unavailable: %s", result.get("error"))
        return None
    payload = result.get("json") or {}
    total = 0.0
    found = False
    for bucket in payload.get("data") or []:
        if not isinstance(bucket, dict):
            continue
        for row in bucket.get("results") or []:
            if not isinstance(row, dict):
                continue
            currency = str(row.get("currency") or "USD").upper()
            if currency != "USD":
                continue
            try:
                total += float(row.get("amount") or 0.0)
                found = True
            except (TypeError, ValueError):
                continue
    if not found:
        return None
    with _LOCK:
        bud = _load_budget()
        # Keep the higher figure so a delayed report cannot erase tokens already billed.
        bud["used"] = round(max(float(bud.get("used") or 0.0), total), 6)
        bud["cost_source"] = "cost_report"
        _save_budget(bud)
        return _maybe_alert_80(bud)


def classify_anthropic_error(code: int, body: str = "") -> str:
    """Map an Anthropic HTTP failure. 403 from a blocked egress is not an invalid key."""
    text = (body or "").lower()
    if code == 401 or ("invalid" in text and "key" in text):
        return "invalid_key"
    if code == 403:
        return "request_not_allowed"
    return f"http_{code}"


def brief_http_status(payload: dict[str, Any]) -> int:
    if payload.get("ok"):
        return 200
    err = str(payload.get("error") or "")
    if err in {"brief_unavailable", "not_configured"}:
        return 404
    if err == "budget_cap_100":
        return 429
    return 502


def anthropic_models_probe(key: str) -> dict[str, Any]:
    """Real HTTP probe. Presence of a string is not activation."""
    result = anthropic_request("GET", "/v1/models?limit=1", key=key, alert=True)
    if result.get("ok"):
        return {"ok": True}
    err = str(result.get("error") or "network")
    return {"ok": False, "error": err}


def run_daily_brief(*, day: str | None = None, force: bool = False, lang: str | None = None) -> dict[str, Any]:
    """Generate gap digest brief. Never writes scores/positions/strategies."""
    day_s = day or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    lang_s = normalize_lang(lang)
    key = resolve_anthropic_key()
    out: dict[str, Any] = {
        "ok": False,
        "day": day_s,
        "lang": lang_s,
        "configured": bool(key),
        "model": MODEL,
    }
    if not key:
        out["error"] = "not_configured"
        out["status"] = "degraded"
        return out

    dig, source_rows = _gather_digest(day=day_s)
    language = LANG_NAMES[lang_s]
    prompt = (
        "You are Sentinel OSINT analyst. Summarize AIS gap / GFW events for LNG carriers. "
        f"Write the brief in {language}. "
        "Do NOT invent positions, scores, or trading strategies. Markdown only. "
        "Max ~400 words.\n\n" + dig
    )
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]

    db = _db_path()
    conn = sqlite3.connect(str(db), timeout=30)
    try:
        ensure_brief_schema(conn)
        if not force:
            existing = conn.execute(
                "SELECT prompt_hash FROM llm_daily_brief WHERE brief_date=? AND lang=?",
                (day_s, lang_s),
            ).fetchone()
            if existing and existing[0] == prompt_hash:
                out.update({"ok": True, "skipped": "unchanged", "source_rows": source_rows})
                return out

        bud = _load_budget()
        if _budget_ratio(bud) >= 1.0:
            _note_budget_skip(day_s, bud)
            out["error"] = "budget_cap_100"
            out["status"] = "degraded"
            out["skipped"] = "monthly_usd_cap"
            return out
        if _budget_ratio(bud) >= 0.8:
            _maybe_alert_80(bud)

        body = {
            "model": MODEL,
            "max_tokens": MAX_OUT_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        result = anthropic_request("POST", "/v1/messages", key=key, payload=body, alert=True)
        if not result.get("ok"):
            out["error"] = str(result.get("error") or "network")
            out["status"] = health_status_for_error(out["error"])
            LOG.warning("daily_brief failed: %s", out["error"])
            return out
        payload = result.get("json") or {}

        blocks = payload.get("content") or []
        md_parts = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                md_parts.append(str(b.get("text") or ""))
        markdown = _scrub("\n".join(md_parts).strip() or "_empty brief_")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        try:
            bud = apply_messages_usage(usage, day=day_s)
            cost = _cost_usd(
                input_tokens=int((usage or {}).get("input_tokens") or 0),
                output_tokens=int((usage or {}).get("output_tokens") or 0),
            )
        except ValueError:
            out["error"] = "usage_absent"
            out["status"] = "degraded"
            LOG.warning("daily_brief response had no usage object; not billed and not stored")
            return out
        try:
            refresh_cost_report(key)
        except Exception as exc:  # noqa: BLE001
            LOG.info("cost_report skipped: %s", type(exc).__name__)

        conn.execute(
            """
            INSERT INTO llm_daily_brief
                (brief_date, lang, markdown, source_rows, prompt_hash, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(brief_date, lang) DO UPDATE SET
              markdown=excluded.markdown,
              source_rows=excluded.source_rows,
              prompt_hash=excluded.prompt_hash,
              model=excluded.model,
              created_at=excluded.created_at
            """,
            (day_s, lang_s, markdown, int(source_rows), prompt_hash, MODEL, _utc_iso()),
        )
        conn.commit()
        out.update(
            {
                "ok": True,
                "status": "ok",
                "source_rows": source_rows,
                "prompt_hash": prompt_hash,
                "chars": len(markdown),
                "cost_usd": round(cost, 6),
                "cost_source": (bud or {}).get("cost_source"),
                "rows": 1,
            }
        )
        return out
    finally:
        conn.close()


def store_pushed_brief(
    *,
    markdown: str,
    lang: str | None = None,
    day: str | None = None,
    source_rows: int = 0,
    model: str | None = None,
) -> dict[str, Any]:
    """Store a brief produced off-node. Does not call Anthropic and does not keep the prompt."""
    lang_s = normalize_lang(lang)
    day_s = day or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    text = _scrub(markdown or "").strip()
    if not text:
        return {"ok": False, "error": "empty_brief"}
    if len(text) > 20000:
        text = text[:20000]
    prompt_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=30)
    try:
        ensure_brief_schema(conn)
        conn.execute(
            """
            INSERT INTO llm_daily_brief
                (brief_date, lang, markdown, source_rows, prompt_hash, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(brief_date, lang) DO UPDATE SET
              markdown=excluded.markdown,
              source_rows=excluded.source_rows,
              prompt_hash=excluded.prompt_hash,
              model=excluded.model,
              created_at=excluded.created_at
            """,
            (
                day_s,
                lang_s,
                text,
                int(source_rows or 0),
                prompt_hash,
                (model or MODEL)[:80],
                _utc_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "status": "ok",
        "brief_date": day_s,
        "lang": lang_s,
        "chars": len(text),
        "source_rows": int(source_rows or 0),
    }


def digest_for_brief(*, day: str | None = None) -> dict[str, Any]:
    """Gap text for a remote runner. No key, no prompt."""
    day_s = day or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    text, rows = _gather_digest(day=day_s)
    return {"ok": True, "day": day_s, "digest": _scrub(text), "source_rows": int(rows)}


def latest_brief(*, lang: str | None = None, generate: bool = True) -> dict[str, Any]:
    """Newest brief for a language. Does not return the prompt or the key."""
    lang_s = normalize_lang(lang)
    day_s = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        ensure_brief_schema(conn)
        row = conn.execute(
            """
            SELECT brief_date, lang, markdown, source_rows, prompt_hash, model, created_at
            FROM llm_daily_brief
            WHERE lang=?
            ORDER BY brief_date DESC
            LIMIT 1
            """,
            (lang_s,),
        ).fetchone()
    finally:
        conn.close()
    if (
        (row is None or str(row["brief_date"]) != day_s)
        and generate
        and llm_runner_mode() != "remote"
    ):
        generated = run_daily_brief(day=day_s, lang=lang_s)
        if not generated.get("ok"):
            if row is None:
                return {
                    "ok": False,
                    "lang": lang_s,
                    "error": generated.get("error") or "brief_unavailable",
                }
        else:
            conn = sqlite3.connect(str(db), timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    """
                    SELECT brief_date, lang, markdown, source_rows, prompt_hash, model, created_at
                    FROM llm_daily_brief
                    WHERE brief_date=? AND lang=?
                    """,
                    (day_s, lang_s),
                ).fetchone()
            finally:
                conn.close()
    if row is None:
        return {"ok": False, "lang": lang_s, "error": "brief_unavailable"}
    return {
        "ok": True,
        "brief_date": row["brief_date"],
        "lang": row["lang"],
        "markdown": _scrub(str(row["markdown"] or "")),
        "source_rows": int(row["source_rows"] or 0),
        "prompt_hash": row["prompt_hash"],
        "model": row["model"],
        "created_at": row["created_at"],
    }


__all__ = [
    "anthropic_models_probe",
    "apply_messages_usage",
    "brief_http_status",
    "classify_anthropic_error",
    "ensure_brief_schema",
    "anthropic_origin",
    "anthropic_request",
    "digest_for_brief",
    "health_status_for_error",
    "latest_brief",
    "llm_health_block",
    "llm_runner_mode",
    "normalize_lang",
    "resolve_anthropic_key",
    "run_daily_brief",
    "store_pushed_brief",
]
