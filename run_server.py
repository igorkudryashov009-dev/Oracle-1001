"""Local HTTP server with bind-retry (frees port 8765 if stale)."""

from __future__ import annotations

import argparse
import socket
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

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
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Kill existing listener on --port before bind (Windows)",
    )
    args = parser.parse_args()

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
