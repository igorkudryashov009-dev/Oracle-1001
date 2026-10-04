"""LLM brief: real usage ledger, cap, language, endpoint payload."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


def test_usage_ledger_rejects_a_guess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services import llm_router

    monkeypatch.setattr(llm_router, "BUDGET_PATH", tmp_path / "llm_budget.json")
    with pytest.raises(ValueError, match="usage_absent"):
        llm_router.apply_messages_usage(None)
    with pytest.raises(ValueError, match="usage_absent"):
        llm_router.apply_messages_usage({})
    bud = llm_router.apply_messages_usage({"input_tokens": 1000, "output_tokens": 500}, day="2026-10-03")
    assert bud["cost_source"] == "messages.usage"
    assert bud["input_tokens"] == 1000
    assert bud["output_tokens"] == 500
    assert bud["days"]["2026-10-03"]["requests"] == 1
    assert bud["used"] == pytest.approx(1000 / 1e6 * 1.0 + 500 / 1e6 * 5.0)
    assert llm_router._load_budget()["used"] == bud["used"]


def test_cap_alerts_at_80_and_skips_at_100(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services import llm_router

    monkeypatch.setattr(llm_router, "BUDGET_PATH", tmp_path / "llm_budget.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-testkey1234")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    alerts: list[str] = []
    monkeypatch.setattr(
        "services.alerts.emit_alert",
        lambda kind, message, **kwargs: alerts.append(kind) or {"kind": kind},
    )
    bud = llm_router._empty_budget("2026-10")
    bud["used"] = 48.0
    llm_router._save_budget(bud)
    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("HTTP must not run at 100%")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    llm_router._maybe_alert_80(llm_router._load_budget())
    assert alerts == ["llm_budget_80"]
    assert llm_router._load_budget()["alert_80"] is True

    bud = llm_router._load_budget()
    bud["used"] = 60.0
    llm_router._save_budget(bud)
    out = llm_router.run_daily_brief(day="2026-10-03", lang="en")
    assert out["error"] == "budget_cap_100"
    assert called["n"] == 0
    conn = sqlite3.connect(str(tmp_path / "sentinel.db"))
    row = conn.execute(
        "SELECT status, error, detail_json FROM job_log WHERE job_name='llm_daily_brief'"
    ).fetchone()
    conn.close()
    assert row[0] == "skipped"
    assert row[1] == "budget_cap_100"
    detail = json.loads(row[2])
    assert detail["reason"] == "monthly_usd_cap_100_skip_until_month_end"
    assert "sk-ant" not in row[2]


def test_latest_brief_uses_pilot_lang_and_hides_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services import llm_router
    from services.pilot_register import ensure_schema, hash_api_key

    monkeypatch.setattr(llm_router, "BUDGET_PATH", tmp_path / "llm_budget.json")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    db = ensure_schema(tmp_path / "pilot.sqlite")
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO pilot_clients
            (company, email, key_hash, key_mask, vessels, status, created_at, updated_at, lang)
        VALUES ('A', 'a@b.c', ?, '****1234', '[]', 'active', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', 'ru')
        """,
        (hash_api_key("sk_sent_demo1234"),),
    )
    conn.commit()
    conn.close()

    def _fake_run(*, day=None, force=False, lang=None):
        path = tmp_path / "sentinel.db"
        c = sqlite3.connect(str(path))
        llm_router.ensure_brief_schema(c)
        c.execute(
            """
            INSERT INTO llm_daily_brief
                (brief_date, lang, markdown, source_rows, prompt_hash, model, created_at)
            VALUES (?, ?, 'сводка', 3, 'abc123', 'claude-haiku-4-5-20251001', '2026-10-03T00:00:00Z')
            """,
            (day, lang),
        )
        c.commit()
        c.close()
        return {"ok": True, "chars": 6}

    monkeypatch.setattr(llm_router, "run_daily_brief", _fake_run)
    payload = llm_router.latest_brief(lang="ru", generate=True)
    assert payload["ok"] is True
    assert payload["lang"] == "ru"
    assert payload["source_rows"] == 3
    assert payload["prompt_hash"] == "abc123"
    assert "prompt" not in payload
    assert "sk-ant" not in json.dumps(payload)
    from services.llm_router import normalize_lang

    assert normalize_lang(None) == "en"
    assert normalize_lang("zz") == "en"


def test_invalid_key_probe_does_not_mark_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.key_activation.STATE_PATH", tmp_path / "prov.json")
    monkeypatch.setattr("services.runtime_env.RUNTIME_ENV_PATH", tmp_path / "runtime_env.json")
    monkeypatch.setattr("services.runtime_env.SIGNAL_PATH", tmp_path / "signal")
    monkeypatch.delenv("VESSELFINDER_API_KEY", raising=False)
    monkeypatch.delenv("VESSEL_FINDER_USERKEY", raising=False)
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    monkeypatch.delenv("GFW_API_KEY", raising=False)
    monkeypatch.delenv("GLOBAL_FISHING_WATCH_TOKEN", raising=False)
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-badkey9999")
    monkeypatch.setattr(
        "services.llm_router.anthropic_models_probe",
        lambda key: {"ok": False, "error": "invalid_key"},
    )
    from services.key_activation import provider_state, run_key_activation_cycle

    out = run_key_activation_cycle(force_providers=["anthropic"])
    assert out["anthropic"]["ok"] is False
    assert out["anthropic"]["error"] == "invalid_key"
    assert out["anthropic"]["masked"] == "****9999"
    assert "sk-ant-badkey9999" not in json.dumps(out)
    assert provider_state("anthropic").get("last_ok") is False
    assert out["immediate_runs"] == []
    conn = sqlite3.connect(tmp_path / "sentinel.db")
    try:
        row = conn.execute(
            "SELECT status, error, detail_json FROM job_log WHERE job_name='key_activation_anthropic'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "error"
    assert row[1] == "invalid_key"
    assert "sk-ant-badkey9999" not in (row[2] or "")
    assert "probe" in (row[2] or "")


def test_anthropic_403_is_not_an_invalid_key() -> None:
    from services.llm_router import brief_http_status, classify_anthropic_error, health_status_for_error

    assert classify_anthropic_error(401, "API key is invalid") == "invalid_key"
    assert classify_anthropic_error(403, "Request not allowed") == "request_not_allowed"
    assert health_status_for_error("invalid_key") == "invalid_key"
    assert health_status_for_error("request_not_allowed") == "egress_blocked"
    assert health_status_for_error("timeout") == "degraded"
    assert brief_http_status({"ok": False, "error": "request_not_allowed"}) == 502
    assert brief_http_status({"ok": False, "error": "brief_unavailable"}) == 404
    assert brief_http_status({"ok": False, "error": "budget_cap_100"}) == 429
    assert brief_http_status({"ok": True}) == 200


def test_health_distinguishes_key_geo_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import llm_router

    monkeypatch.delenv("LLM_RUNNER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-exampleKEY1")
    monkeypatch.setattr(llm_router, "brief_row_count", lambda: 0)
    monkeypatch.setattr(llm_router, "_load_budget", lambda: {"remaining": 60, "used": 0})
    expected = {
        "invalid_key": "invalid_key",
        "request_not_allowed": "egress_blocked",
        "timeout": "degraded",
    }
    for err, status in expected.items():
        monkeypatch.setattr(
            "services.key_activation.provider_state",
            lambda name, e=err: {"last_ok": False, "last_error": e},
        )
        block = llm_router.llm_health_block()
        assert block["status"] == status
        assert block["note"] == err
        assert "exampleKEY1" not in json.dumps(block)


def test_proxy_base_url_and_probe_hide_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error
    import urllib.request
    from email.message import Message

    from services import llm_router

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://llm.example.test")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:s3cret@127.0.0.1:9")
    seen: dict[str, object] = {}

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return b'{"data":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

    def _open(req, timeout):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(llm_router, "_open", _open)
    out = llm_router.anthropic_models_probe("sk-ant-supersecretKEY")
    assert out == {"ok": True}
    assert str(seen["url"]).startswith("https://llm.example.test/v1/models")
    assert seen["timeout"] == 30.0
    opener = llm_router._build_opener()
    proxies = [
        h.proxies for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies
    ]
    assert proxies and "127.0.0.1:9" in proxies[0].get("https", "")
    assert "s3cret" not in json.dumps(out)
    assert "supersecretKEY" not in json.dumps(out)

    calls = {"n": 0}

    def _denied(req, timeout):
        calls["n"] += 1
        body = b'{"error":{"message":"API key is invalid sk-ant-supersecretKEY"}}'
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", Message(), io.BytesIO(body))

    monkeypatch.setattr(llm_router, "_open", _denied)
    denied = llm_router.anthropic_models_probe("sk-ant-supersecretKEY")
    assert calls["n"] == 1
    assert denied["error"] == "invalid_key"
    assert "supersecretKEY" not in json.dumps(denied)


def test_network_error_retries_twice_then_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import llm_router

    calls = {"n": 0}
    alerts: list[tuple] = []

    def _boom(req, timeout):
        calls["n"] += 1
        raise TimeoutError("timed out sk-ant-supersecretKEY")

    monkeypatch.setattr(llm_router, "_open", _boom)
    monkeypatch.setattr(
        "services.alerts.emit_alert",
        lambda kind, message, **kwargs: alerts.append((kind, message, kwargs.get("detail"))),
    )
    out = llm_router.anthropic_models_probe("sk-ant-supersecretKEY")
    assert calls["n"] == 3
    assert out == {"ok": False, "error": "timeout"}
    assert alerts and alerts[0][0] == "llm_network"
    assert "supersecretKEY" not in json.dumps(alerts)
    assert "s3cret" not in json.dumps(alerts)


def test_remote_mode_skips_korolev_timer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_RUNNER", "remote")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))

    def _no(*args, **kwargs):
        raise AssertionError("Korolev must not call Anthropic in remote mode")

    monkeypatch.setattr("services.llm_router.run_daily_brief", _no)
    from services.scheduler import run_job

    out = run_job("daily_brief", force=True)
    assert out["status"] == "skipped"
    assert out["detail"]["reason"] == "llm_runner_remote"
    conn = sqlite3.connect(str(tmp_path / "sentinel.db"))
    row = conn.execute(
        "SELECT status, error, detail_json FROM job_log WHERE job_name='daily_brief'"
    ).fetchone()
    conn.close()
    assert row[0] == "skipped"
    assert row[1] == "llm_runner_remote"
    assert "sk-ant" not in (row[2] or "")


def test_remote_health_ok_only_after_a_pushed_brief(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services import llm_router

    monkeypatch.setenv("LLM_RUNNER", "remote")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setattr(llm_router, "BUDGET_PATH", tmp_path / "llm_budget.json")
    waiting = llm_router.llm_health_block()
    assert waiting["status"] == "degraded"
    assert waiting["runner"] == "remote"
    stored = llm_router.store_pushed_brief(
        markdown="Gap note sk-ant-supersecretKEY stays out",
        lang="en",
        day="2026-10-03",
        source_rows=4,
    )
    assert stored["ok"] is True
    assert "supersecretKEY" not in stored["markdown"] if "markdown" in stored else True
    block = llm_router.llm_health_block()
    assert block["status"] == "ok"
    assert block["rows"] == 1
    conn = sqlite3.connect(str(tmp_path / "sentinel.db"))
    md = conn.execute("SELECT markdown FROM llm_daily_brief").fetchone()[0]
    conn.close()
    assert "supersecretKEY" not in md
    assert "****" in md


def test_llm_status_ok_requires_a_stored_brief(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services import llm_router

    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setattr(llm_router, "BUDGET_PATH", tmp_path / "llm_budget.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-exampleKEY1")
    monkeypatch.setattr(
        "services.key_activation.provider_state",
        lambda name: {"last_ok": True, "last_error": ""},
    )
    monkeypatch.delenv("LLM_RUNNER", raising=False)
    direct = llm_router.llm_health_block()
    assert direct["status"] != "ok"
    monkeypatch.setenv("LLM_RUNNER", "remote")
    monkeypatch.setattr(
        "services.key_activation.provider_state",
        lambda name: {"last_ok": False, "last_error": "request_not_allowed"},
    )
    remote = llm_router.llm_health_block()
    assert remote["status"] != "ok"
    llm_router.store_pushed_brief(markdown="gap digest", lang="en", day="2026-10-03", source_rows=2)
    ready = llm_router.llm_health_block()
    assert ready["status"] == "ok"
    assert ready["rows"] == 1
    assert "exampleKEY1" not in json.dumps(ready)


def test_egress_blocked_alert_names_both_bypasses(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error
    from email.message import Message

    from services import llm_router

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("LLM_RUNNER", raising=False)
    alerts: list[tuple] = []

    def _denied(req, timeout):
        body = b'{"error":{"message":"Request not allowed"}}'
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", Message(), io.BytesIO(body))

    monkeypatch.setattr(llm_router, "_open", _denied)
    monkeypatch.setattr(
        "services.alerts.emit_alert",
        lambda kind, message, **kwargs: alerts.append((kind, message, kwargs.get("detail"))),
    )
    out = llm_router.anthropic_models_probe("sk-ant-exampleKEY1")
    assert out["error"] == "request_not_allowed"
    assert alerts
    kind, message, detail = alerts[0]
    assert kind == "llm_network"
    assert "geo-blocked" in message
    assert "HTTPS_PROXY" in message
    assert "LLM_RUNNER=remote" in message
    assert "llm_brief_runner.py" in message
    assert detail["error"] == "egress_blocked"
    blob = json.dumps({"message": message, "detail": detail, "probe": out})
    assert "exampleKEY1" not in blob


def test_remote_runner_pushes_without_leaking_the_key() -> None:
    from scripts.llm_brief_runner import run_remote

    secret = "sk-ant-supersecretKEY"
    pushed: dict = {}

    def digest_get():
        return {"ok": True, "digest": f"gaps include {secret}", "source_rows": 2, "day": "2026-10-03"}

    def complete(text: str):
        assert secret not in text
        return {"ok": True, "markdown": f"summary {secret}", "model": "claude-haiku-4-5-20251001"}

    def push(body: dict):
        pushed.update(body)
        return {"ok": True, "brief_date": "2026-10-03"}

    out = run_remote(digest_get=digest_get, complete=complete, push=push)
    assert out["ok"] is True
    assert secret not in pushed["markdown"]
    assert secret not in json.dumps(out)
    assert "****" in pushed["markdown"]


def test_probe_and_daily_brief_read_https_proxy_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    from services import llm_router

    env_file = tmp_path / ".env"
    env_file.write_text(
        "HTTPS_PROXY=http://user:s3cret@127.0.0.1:9\nLLM_RUNNER=direct\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(llm_router, "_ENV_FILE", env_file)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("LLM_RUNNER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-exampleKEY1")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setattr(llm_router, "BUDGET_PATH", tmp_path / "llm_budget.json")
    assert llm_router.HTTP_TIMEOUT_SEC == 30.0
    assert llm_router.NETWORK_RETRIES == 2

    seen: dict[str, object] = {}

    class _Resp:
        status = 200

        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

    def _capture(self, req, timeout=None):
        seen["timeout"] = timeout
        proxies = [
            h.proxies
            for h in self.handlers
            if isinstance(h, urllib.request.ProxyHandler) and h.proxies
        ]
        seen["proxies"] = proxies
        path = str(getattr(req, "full_url", ""))
        if path.endswith("/v1/messages") or "/v1/messages" in path:
            body = (
                b'{"content":[{"type":"text","text":"gap note"}],'
                b'"usage":{"input_tokens":10,"output_tokens":5}}'
            )
        else:
            body = b'{"data":[]}'
        return _Resp(body)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _capture)
    probe = llm_router.anthropic_models_probe("sk-ant-exampleKEY1")
    assert probe == {"ok": True}
    assert seen["timeout"] == 30.0
    assert seen["proxies"] and "127.0.0.1:9" in seen["proxies"][0].get("https", "")
    assert "s3cret" not in json.dumps(probe)

    seen.clear()
    brief = llm_router.run_daily_brief(day="2026-10-03", force=True, lang="en")
    assert brief.get("ok") is True
    assert seen["timeout"] == 30.0
    assert seen["proxies"] and "127.0.0.1:9" in seen["proxies"][0].get("https", "")
    assert "s3cret" not in json.dumps(brief)
    assert "exampleKEY1" not in json.dumps(brief)


def test_remote_runner_pushes_via_admin_api_and_health_ok_only_after_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from services import llm_router

    monkeypatch.setenv("LLM_RUNNER", "remote")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setattr(llm_router, "BUDGET_PATH", tmp_path / "llm_budget.json")
    monkeypatch.setattr(llm_router, "_ENV_FILE", tmp_path / "missing.env")
    waiting = llm_router.llm_health_block()
    assert waiting["status"] == "degraded"
    assert waiting["rows"] == 0

    admin = "admin-test-key-9f3a"
    secret = "sk-ant-supersecretKEY"

    class _Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _admin(self) -> bool:
            return self.headers.get("X-API-Key") == admin

        def do_GET(self) -> None:  # noqa: N802
            if not self.path.startswith("/api/v1/llm/digest"):
                self._json(404, {"ok": False})
                return
            if not self._admin():
                self._json(403, {"ok": False, "error": "admin_required"})
                return
            self._json(
                200,
                {
                    "ok": True,
                    "digest": f"gaps include {secret}",
                    "source_rows": 2,
                    "day": "2026-10-03",
                },
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/api/v1/llm/brief":
                self._json(404, {"ok": False})
                return
            if not self._admin():
                self._json(403, {"ok": False, "error": "admin_required"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            result = llm_router.store_pushed_brief(
                markdown=str(body.get("markdown") or ""),
                lang=str(body.get("lang") or "en"),
                day=str(body.get("brief_date") or "") or None,
                source_rows=int(body.get("source_rows") or 0),
                model=str(body.get("model") or "") or None,
            )
            self._json(200 if result.get("ok") else 400, result)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from scripts.llm_brief_runner import _request, run_remote

        base = f"http://127.0.0.1:{port}"

        def complete(text: str) -> dict:
            assert secret not in text
            return {"ok": True, "markdown": f"summary {secret}", "model": "claude-haiku-4-5-20251001"}

        out = run_remote(
            digest_get=lambda: _request("GET", f"{base}/api/v1/llm/digest", api_key=admin),
            complete=complete,
            push=lambda body: _request(
                "POST", f"{base}/api/v1/llm/brief", api_key=admin, payload=body
            ),
        )
        assert out["ok"] is True
        assert secret not in json.dumps(out)
        ready = llm_router.llm_health_block()
        assert ready["status"] == "ok"
        assert ready["rows"] == 1
        assert ready["runner"] == "remote"
        conn = sqlite3.connect(str(tmp_path / "sentinel.db"))
        md = conn.execute("SELECT markdown FROM llm_daily_brief").fetchone()[0]
        conn.close()
        assert secret not in md
        assert "****" in md
    finally:
        server.shutdown()
        server.server_close()
