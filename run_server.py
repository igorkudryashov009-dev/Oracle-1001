"""Local HTTP server with bind-retry (frees DASHBOARD_PORT if stale)."""

from __future__ import annotations

import argparse
import mimetypes
import os
import socket
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent

# Ensure .glb is never served as text/plain (breaks GLTFLoader on some clients)
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("model/gltf+json", ".gltf")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")


def _default_port() -> int:
    from services.utils.canonical_port import assert_canonical_dashboard_port, resolve_dashboard_port

    return assert_canonical_dashboard_port(resolve_dashboard_port(), context="run_server")


def _content_type(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".glb":
        return "model/gltf-binary"
    if suf == ".gltf":
        return "model/gltf+json"
    if suf in {".js", ".mjs"}:
        return "application/javascript; charset=utf-8"
    if suf == ".css":
        return "text/css; charset=utf-8"
    if suf == ".json":
        return "application/json; charset=utf-8"
    if suf == ".html":
        return "text/html; charset=utf-8"
    guessed = mimetypes.guess_type(str(path))[0]
    return guessed or "application/octet-stream"


def _cache_control_no_store() -> str:
    return "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0"


def _no_store(path: Path) -> bool:
    """Bust stale module / GLB caches that caused empty DIGITAL TWIN canvases."""
    return path.suffix.lower() in {".js", ".mjs", ".css", ".glb", ".gltf", ".html", ".json"}


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **getattr(SimpleHTTPRequestHandler, "extensions_map", {}),
        ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json",
        ".js": "application/javascript",
        ".mjs": "application/javascript",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Range")
        # Path may include ?query — strip for suffix checks
        rel = unquote(urlparse(self.path).path or "").lstrip("/")
        target = (ROOT / rel).resolve() if rel else None
        try:
            if target and target.is_file() and _no_store(target):
                self.send_header("Cache-Control", _cache_control_no_store())
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
        except Exception:
            pass
        super().end_headers()

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        # Enterprise health alias → static JSON produced by builders / healthcheck
        if self.path in ("/api/v1/health", "/api/v1/health/"):
            for cand in (
                ROOT / "output" / "api" / "v1" / "health",
                ROOT / "output" / "api" / "v1" / "health.json",
            ):
                if cand.exists():
                    self.path = "/" + cand.relative_to(ROOT).as_posix()
                    break
        return super().do_GET()

    def guess_type(self, path):
        p = Path(path)
        if p.suffix.lower() in {".glb", ".gltf", ".js", ".mjs", ".css", ".json", ".html"}:
            return _content_type(p)
        return super().guess_type(path)

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _try_free_port(port: int) -> None:
    """Best-effort free of LISTENING PIDs on Windows (no-op elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import subprocess

        out = subprocess.check_output(
            ["netstat", "-ano"],
            text=True,
            errors="replace",
        )
        pids: set[str] = set()
        for line in out.splitlines():
            upper = line.upper()
            if "LISTENING" not in upper:
                continue
            if f":{port}" not in line:
                continue
            parts = line.split()
            if parts and parts[-1].isdigit() and parts[-1] != "0":
                pids.add(parts[-1])
        for pid in sorted(pids):
            subprocess.run(
                ["taskkill", "/PID", pid, "/F"],
                check=False,
                capture_output=True,
            )
            print(f"Freed port {port}: killed PID {pid}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: could not auto-free port {port}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=_default_port())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Kill existing listener on --port before bind (Windows)",
    )
    args = parser.parse_args()
    from services.utils.canonical_port import assert_canonical_dashboard_port

    args.port = assert_canonical_dashboard_port(args.port, context="run_server")

    if args.force or _port_in_use(args.host, args.port):
        _try_free_port(args.port)
        time.sleep(0.6)

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in (98, 48):
            print(f"Address already in use on {args.host}:{args.port} — retrying with --force")
            _try_free_port(args.port)
            time.sleep(0.8)
            server = ThreadingHTTPServer((args.host, args.port), Handler)
        else:
            raise

    print(f"Serving {ROOT}")
    print(f"Open: http://{args.host}:{args.port}/output/osint_layers.html")
    print(f"Also: http://{args.host}:{args.port}/output/dashboard.html")
    print("Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
