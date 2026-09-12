#!/usr/bin/env python3
"""Sanity audit: critical Sentinel HTTP endpoints on DASHBOARD_PORT (default 8765).

Usage:
  python scripts/audit_endpoints.py
  python scripts/audit_endpoints.py --base http://127.0.0.1:8765
  python -m scripts.audit_endpoints --start-server
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _dashboard_port() -> int:
    raw = (os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or "8765").strip()
    try:
        return int(raw)
    except ValueError:
        return 8765


DEFAULT_PORT = _dashboard_port()
DEFAULT_BASE = f"http://127.0.0.1:{DEFAULT_PORT}"

CRITICAL_PATHS = (
    "/output/sentinel_dashboard.html",
    "/output/api/v1/health",
    "/output/js/top10_sheet.js",
    "/output/js/vessel_3d_reconstruction.js",
    "/output/js/sentinel_hud.css",
    "/assets/7000/1-1.jpg",
)


def _probe(url: str, *, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "sentinel-audit/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            chunk = resp.read(65536)
            length_hdr = resp.headers.get("Content-Length")
            if length_hdr is not None:
                content_length = int(length_hdr)
            else:
                rest = resp.read()
                content_length = len(chunk) + len(rest)
            status = int(getattr(resp, "status", 200) or 200)
            return {
                "url": url,
                "ok": 200 <= status < 300 and content_length > 0,
                "status": status,
                "content_length": content_length,
                "error": None,
            }
    except urllib.error.HTTPError as exc:
        length = 0
        try:
            length = len(exc.read() or b"")
        except Exception:
            pass
        return {
            "url": url,
            "ok": False,
            "status": int(exc.code),
            "content_length": length,
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "url": url,
            "ok": False,
            "status": 0,
            "content_length": 0,
            "error": str(exc),
        }


def _wait_ready(base: str, *, attempts: int = 30) -> bool:
    health = base.rstrip("/") + "/output/api/v1/health"
    for _ in range(attempts):
        r = _probe(health, timeout=2.0)
        if r["ok"]:
            return True
        time.sleep(0.4)
    return False


def main(argv: list[str] | None = None) -> int:
    port = _dashboard_port()
    parser = argparse.ArgumentParser(description=f"Audit Sentinel endpoints on :{port}")
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"Base URL (default http://127.0.0.1:{port})",
    )
    parser.add_argument(
        "--start-server",
        action="store_true",
        help=f"Spawn scripts/serve_dashboard.py --host 0.0.0.0 --port {port} if health is down",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON report")
    args = parser.parse_args(argv)
    base = args.base.rstrip("/")

    if args.start_server and not _wait_ready(base, attempts=3):
        py = ROOT / "venv" / "Scripts" / "python.exe"
        if not py.exists():
            py = Path(sys.executable)
        cmd = [
            str(py),
            str(ROOT / "scripts" / "serve_dashboard.py"),
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--sync-on-start",
        ]
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_ready(base, attempts=40):
            print("FAIL: server did not become ready on", base)
            return 2

    results = [_probe(base + path) for path in CRITICAL_PATHS]
    all_ok = all(r["ok"] for r in results)
    report = {
        "base": base,
        "ok": all_ok,
        "checked": len(results),
        "passed": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("=" * 72)
        print(f"SENTINEL ENDPOINT AUDIT  base={base}")
        print("=" * 72)
        for r in results:
            flag = "OK  " if r["ok"] else "FAIL"
            err = f"  ({r['error']})" if r["error"] and not r["ok"] else ""
            print(f"[{flag}] HTTP {r['status']:>3}  cl={r['content_length']:<8}  {r['url']}{err}")
        print("-" * 72)
        print(f"RESULT: {'PASS' if all_ok else 'FAIL'}  {report['passed']}/{report['checked']}")
        print("=" * 72)

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
