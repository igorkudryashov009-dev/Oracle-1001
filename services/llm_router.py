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
LOG = logging.getLogger("sentinel.llm_router")
BUDGET_PATH = ROOT / "data" / "archive" / "llm_budget.json"
MONTHLY_USD_CAP = 60.0
MAX_OUT_TOKENS = 2000
MODEL = "claude-haiku-4-5-20251001"

_LOCK = threading.RLock()

LLM_BRIEF_DDL = """
CREATE TABLE IF NOT EXISTS llm_daily_brief (
    brief_date TEXT PRIMARY KEY,
    markdown TEXT NOT NULL,
    source_rows INTEGER NOT NULL DEFAULT 0,
    prompt_hash TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL
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


def _load_budget() -> dict[str, Any]:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if BUDGET_PATH.is_file():
        try:
            data = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("month") == month:
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "month": month,
        "monthly_usd_cap": MONTHLY_USD_CAP,
        "used": 0.0,
        "remaining": MONTHLY_USD_CAP,
    }


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


def _estimate_cost_usd(*, input_tokens: int, output_tokens: int) -> float:
    # Approximate Haiku list prices (USD / MTok) — envelope only
    return (input_tokens / 1e6) * 0.80 + (output_tokens / 1e6) * 4.00


def llm_health_block() -> dict[str, Any]:
    key = resolve_anthropic_key()
    bud = _load_budget()
    if not key:
        return {
            "status": "degraded",
            "configured": False,
            "note": "ANTHROPIC_API_KEY missing — install_key.sh ANTHROPIC",
            "budget": bud,
        }
    if float(bud.get("remaining") or 0) <= 0:
        return {
            "status": "degraded",
            "configured": True,
            "note": "monthly_usd_cap exhausted",
            "budget": bud,
        }
    return {
        "status": "armed",
        "configured": True,
        "model": MODEL,
        "note": "daily_brief job writes llm_daily_brief only",
        "budget": bud,
    }


def run_daily_brief(*, day: str | None = None, force: bool = False) -> dict[str, Any]:
    """Generate gap digest brief. Never writes scores/positions/strategies."""
    day_s = day or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    key = resolve_anthropic_key()
    out: dict[str, Any] = {
        "ok": False,
        "day": day_s,
        "configured": bool(key),
        "model": MODEL,
    }
    if not key:
        out["error"] = "not_configured"
        out["status"] = "degraded"
        return out

    dig, source_rows = _gather_digest(day=day_s)
    prompt = (
        "You are Sentinel OSINT analyst. Summarize AIS gap / GFW events for LNG carriers. "
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
                "SELECT prompt_hash FROM llm_daily_brief WHERE brief_date=?",
                (day_s,),
            ).fetchone()
            if existing and existing[0] == prompt_hash:
                out.update({"ok": True, "skipped": "unchanged", "source_rows": source_rows})
                return out

        bud = _load_budget()
        if float(bud.get("remaining") or 0) <= 0:
            out["error"] = "budget_exhausted"
            out["status"] = "degraded"
            return out

        try:
            import urllib.request

            body = {
                "model": MODEL,
                "max_tokens": MAX_OUT_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            }
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            status = "degraded"
            if "429" in err:
                status = "degraded"
                out["error"] = "rate_limited_429"
            else:
                out["error"] = f"llm_http:{type(exc).__name__}"
            out["status"] = status
            LOG.warning("daily_brief failed: %s", out["error"])
            return out

        blocks = payload.get("content") or []
        md_parts = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                md_parts.append(str(b.get("text") or ""))
        markdown = "\n".join(md_parts).strip() or "_empty brief_"
        usage = payload.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or max(1, len(prompt) // 4))
        out_tok = int(usage.get("output_tokens") or max(1, len(markdown) // 4))
        cost = _estimate_cost_usd(input_tokens=in_tok, output_tokens=out_tok)
        with _LOCK:
            bud = _load_budget()
            bud["used"] = float(bud.get("used") or 0.0) + cost
            _save_budget(bud)

        conn.execute(
            """
            INSERT INTO llm_daily_brief (brief_date, markdown, source_rows, prompt_hash, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(brief_date) DO UPDATE SET
              markdown=excluded.markdown,
              source_rows=excluded.source_rows,
              prompt_hash=excluded.prompt_hash,
              model=excluded.model,
              created_at=excluded.created_at
            """,
            (day_s, markdown, int(source_rows), prompt_hash, MODEL, _utc_iso()),
        )
        conn.commit()
        out.update(
            {
                "ok": True,
                "status": "ok",
                "source_rows": source_rows,
                "prompt_hash": prompt_hash,
                "cost_usd": round(cost, 5),
                "rows": 1,
            }
        )
        return out
    finally:
        conn.close()


__all__ = [
    "ensure_brief_schema",
    "llm_health_block",
    "resolve_anthropic_key",
    "run_daily_brief",
]
