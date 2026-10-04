#!/usr/bin/env python3
"""Generate the daily brief on this machine and push it to Korolev.

Used when LLM_RUNNER=remote. The Anthropic key stays in the local .env.
The push uses an admin API key. Neither value is printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_BASE = "http://45.8.230.214:8765"


def load_env_file(path: Path) -> None:
    """Fill os.environ from .env. Does not override variables already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, _, value = text.partition("=")
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip('"').strip("'")


def admin_api_key() -> str:
    raw = (os.getenv("API_KEYS_JSON") or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    for item, meta in data.items():
        tier = str(meta.get("tier") or "").lower() if isinstance(meta, dict) else ""
        if tier == "admin" and str(item).strip():
            return str(item).strip()
    return ""


def _request(method: str, url: str, *, api_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body if isinstance(body, dict) else {"ok": False, "error": "bad_json"}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            parsed.setdefault("ok", False)
            parsed.setdefault("error", f"http_{exc.code}")
            return parsed
        return {"ok": False, "error": f"http_{exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def run_remote(
    *,
    digest_get: Callable[[], dict[str, Any]],
    complete: Callable[[str], dict[str, Any]],
    push: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Orchestrate digest → local completion → push. Callers inject I/O."""
    from services.llm_router import _scrub

    digest = digest_get()
    if not digest.get("ok") and not digest.get("digest"):
        return {"ok": False, "error": str(digest.get("error") or "digest_unavailable")}
    text = _scrub(str(digest.get("digest") or ""))
    completed = complete(text)
    if not completed.get("ok"):
        return {"ok": False, "error": str(completed.get("error") or "complete_failed")}
    markdown = _scrub(str(completed.get("markdown") or ""))
    if not markdown.strip():
        return {"ok": False, "error": "empty_brief"}
    pushed = push(
        {
            "markdown": markdown,
            "lang": "en",
            "brief_date": digest.get("day"),
            "source_rows": int(digest.get("source_rows") or 0),
            "model": completed.get("model"),
        }
    )
    if not pushed.get("ok"):
        return {"ok": False, "error": str(pushed.get("error") or "push_failed")}
    return {
        "ok": True,
        "chars": len(markdown),
        "brief_date": pushed.get("brief_date") or digest.get("day"),
        "error": "",
    }


def complete_with_anthropic(digest: str) -> dict[str, Any]:
    from services.llm_router import MODEL, anthropic_request, resolve_anthropic_key

    key = resolve_anthropic_key()
    if not key:
        return {"ok": False, "error": "not_configured"}
    prompt = (
        "You are Sentinel OSINT analyst. Summarize AIS gap / GFW events for LNG carriers. "
        "Write the brief in English. "
        "Do NOT invent positions, scores, or trading strategies. Markdown only. "
        "Max ~400 words.\n\n" + digest
    )
    result = anthropic_request(
        "POST",
        "/v1/messages",
        key=key,
        payload={
            "model": MODEL,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
        alert=True,
    )
    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("error") or "network")}
    payload = result.get("json") or {}
    parts = []
    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return {"ok": True, "markdown": "\n".join(parts).strip(), "model": MODEL}


def main(argv: list[str] | None = None) -> int:
    del argv
    load_env_file(ROOT / ".env")
    from services.llm_router import _scrub

    base = (os.getenv("SENTINEL_BASE_URL") or DEFAULT_BASE).rstrip("/")
    api_key = admin_api_key()
    if not api_key:
        print("missing admin api key")
        return 2

    def digest_get() -> dict[str, Any]:
        return _request("GET", f"{base}/api/v1/llm/digest", api_key=api_key)

    def push(body: dict[str, Any]) -> dict[str, Any]:
        return _request("POST", f"{base}/api/v1/llm/brief", api_key=api_key, payload=body)

    out = run_remote(digest_get=digest_get, complete=complete_with_anthropic, push=push)
    err = _scrub(str(out.get("error") or ""))
    if out.get("ok"):
        print("pushed", out.get("brief_date"), "chars", out.get("chars"))
        return 0
    print("failed", err or "remote_brief_failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
