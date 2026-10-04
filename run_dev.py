#!/usr/bin/env python3
"""Zero-touch local boot for Sentinel edge (:8765) — Contract 1.8.0-ops-gis-sot.

Always resolves the Oracle-1001 / Sentinel project root (never C:\\Users\\MSI),
liberates :8765 from non-Sentinel hijackers (plain ``python -m http.server``),
then starts ``scripts/serve_dashboard.py`` — the canonical public edge.

Internal micro-API ``api_server.py`` stays on :8766 (optional ``--with-api``).

Usage (from any cwd)::

    python C:\\Users\\MSI\\Oracle-1001\\7000\\run_dev.py
    .\\venv\\Scripts\\python.exe run_dev.py --force
"""

from __future__ import annotations

# Unbuffered logs when launched from PowerShell / Cursor background shells
import os

os.environ.setdefault("PYTHONUNBUFFERED", "1")

import argparse
import json
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONTRACT_VERSION = "1.8.0-ops-gis-sot"
EDGE_PORT = 8765
API_PORT = 8766
SENTINEL_MARKERS = (
    "serve_dashboard.py",
    "run_dev.py",
    "sentinel-web",
    "scripts\\serve_dashboard",
    "scripts/serve_dashboard",
)


def _log(msg: str) -> None:
    """Non-recursive safe logger (never wraps itself)."""
    text = msg if msg.startswith("[RUNNER]") or msg.startswith("[CHECK]") else f"[RUNNER] {msg}"
    sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))
    sys.stdout.flush()


def get_project_root() -> Path:
    """Locate repo root that contains api_server.py + services/."""
    here = Path(__file__).resolve().parent
    candidates = [here, *here.parents]
    # Also honour cwd if user launched from a clone with different layout
    try:
        candidates.insert(0, Path.cwd().resolve())
    except OSError:
        pass
    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if (cand / "api_server.py").is_file() and (cand / "services" / "tile_proxy.py").is_file():
            return cand
        if (cand / "scripts" / "serve_dashboard.py").is_file() and (
            cand / "services" / "compressor_stations.py"
        ).is_file():
            return cand
    return here


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _listening_pids_windows(port: int) -> list[int]:
    pids: list[int] = []
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.CalledProcessError):
        return pids
    needle = f":{port}"
    for line in out.splitlines():
        if "LISTENING" not in line.upper():
            continue
        if needle not in line:
            continue
        # Local address column must end with :port (avoid :87650 false hits)
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[1] if parts[0].upper().startswith("TCP") else parts[0]
        if not local.endswith(needle) and f"]{needle}" not in local:
            # IPv4 0.0.0.0:8765 or 127.0.0.1:8765
            if not local.endswith(f":{port}"):
                continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def _listening_pids_posix(port: int) -> list[int]:
    pids: list[int] = []
    # Prefer lsof; fall back to fuser parse
    try:
        out = subprocess.check_output(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            text=True,
            errors="replace",
        )
        for tok in out.split():
            try:
                pid = int(tok.strip())
            except ValueError:
                continue
            if pid > 0 and pid not in pids:
                pids.append(pid)
        return pids
    except (OSError, subprocess.CalledProcessError):
        pass
    try:
        out = subprocess.check_output(
            ["fuser", f"{port}/tcp"],
            text=True,
            errors="replace",
            stderr=subprocess.STDOUT,
        )
        for tok in out.replace(":", " ").split():
            try:
                pid = int(tok.strip())
            except ValueError:
                continue
            if pid > 0 and pid not in pids:
                pids.append(pid)
    except (OSError, subprocess.CalledProcessError):
        pass
    return pids


def listening_pids(port: int) -> list[int]:
    if os.name == "nt":
        return _listening_pids_windows(port)
    return _listening_pids_posix(port)


def _cmdline_for_pid(pid: int) -> str:
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine",
                ],
                text=True,
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return (out or "").strip()
        except (OSError, subprocess.CalledProcessError):
            return ""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""


def is_sentinel_process(pid: int) -> bool:
    cmd = _cmdline_for_pid(pid).lower()
    if not cmd:
        return False
    return any(m.lower() in cmd for m in SENTINEL_MARKERS)


def kill_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def liberate_port(port: int, *, force: bool = False) -> None:
    """Kill listeners on port unless they are already Sentinel (unless --force)."""
    pids = listening_pids(port)
    if not pids:
        return
    for pid in pids:
        sentinel = is_sentinel_process(pid)
        if sentinel and not force:
            _log(f"[RUNNER] Port {port} owned by Sentinel PID {pid} — leaving it.")
            continue
        label = "Sentinel" if sentinel else "non-Sentinel"
        _log(f"[RUNNER] Killing {label} PID {pid} on :{port}...")
        kill_pid(pid)
    time.sleep(0.8)


def kill_port_owner(port: int) -> None:
    """Directive alias: force-release :port (hijack / stale listener)."""
    liberate_port(port, force=True)


def http_probe(url: str, timeout: float = 2.0) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Sentinel-run_dev/1.8.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), headers, resp.read()
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return int(exc.code), headers, exc.read() if exc.fp else b""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}, b""


def probe_sentinel_edge(port: int = EDGE_PORT) -> dict:
    """Return diagnostics for an already-listening edge."""
    base = f"http://127.0.0.1:{port}"
    st, hdrs, _body = http_probe(f"{base}/", timeout=1.5)
    server = hdrs.get("server", "")
    st_cs, hdr_cs, body_cs = http_probe(f"{base}/api/v1/gis/compressor-stations", timeout=3.0)
    st_tile, hdr_tile, body_tile = http_probe(
        f"{base}/api/v1/gis/tiles/status", timeout=3.0
    )
    count = None
    contract = hdr_cs.get("x-contract-version") or hdr_tile.get("x-contract-version") or ""
    try:
        if body_cs:
            payload = json.loads(body_cs.decode("utf-8", errors="replace"))
            # SoT field is ``count``; ``total_count`` is an orchestrator alias
            count = payload.get("count")
            if count is None:
                count = payload.get("total_count")
            contract = contract or str(payload.get("contract_version") or "")
    except (json.JSONDecodeError, UnicodeError):
        pass
    # NOTE: serve_dashboard subclasses SimpleHTTPRequestHandler, so Server:
    # "SimpleHTTP/0.6 ..." is NORMAL for Sentinel — do NOT treat that alone
    # as a hijack. Hijack = GIS contract endpoints missing/wrong.
    gis_ok = st_cs == 200 and count == 185 and CONTRACT_VERSION in (contract or "")
    tiles_ok = False
    if st_tile == 200:
        cv = hdr_tile.get("x-contract-version") or ""
        if CONTRACT_VERSION in cv:
            tiles_ok = True
        elif body_tile:
            try:
                tp = json.loads(body_tile.decode("utf-8", errors="replace"))
                tiles_ok = tp.get("contract_version") == CONTRACT_VERSION
            except (json.JSONDecodeError, UnicodeError):
                pass
    is_sentinel = bool(gis_ok or tiles_ok)
    # Plain python -m http.server hijack: answers / but not GIS contract routes
    is_plain_static = (not is_sentinel) and st in {200, 301, 302, 403, 404}
    return {
        "port_open": True,
        "http_root_status": st,
        "server": server,
        "is_simplehttp": is_plain_static,  # legacy name = non-Sentinel static hijack
        "compressor_status": st_cs,
        "compressor_count": count,
        "tiles_status": st_tile,
        "contract": contract,
        "is_sentinel": is_sentinel,
        "gis_ok": gis_ok,
        "tiles_ok": tiles_ok,
    }


def wait_until_alive(port: int, timeout_sec: float = 25.0) -> dict:
    deadline = time.time() + timeout_sec
    last: dict = {}
    while time.time() < deadline:
        if is_port_open(port):
            last = probe_sentinel_edge(port)
            if last.get("gis_ok") or last.get("tiles_ok") or last.get("is_sentinel"):
                return last
            # Port open but not GIS-ready yet — keep waiting briefly
            if last.get("is_simplehttp"):
                return last
        time.sleep(0.4)
    return last or {"port_open": is_port_open(port), "is_sentinel": False}


def resolve_python(root: Path) -> str:
    if os.name == "nt":
        venv_py = root / "venv" / "Scripts" / "python.exe"
    else:
        venv_py = root / "venv" / "bin" / "python"
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable


def main() -> int:
    parser = argparse.ArgumentParser(description="Sentinel clean boot on :8765")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Kill even an existing Sentinel listener on :8765 before restart",
    )
    parser.add_argument(
        "--with-api",
        action="store_true",
        help="Also start api_server.py on :8766 (internal micro-API)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Probe :8765 GIS mounts and exit (no start)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=EDGE_PORT)
    args = parser.parse_args()

    root = get_project_root()
    os.chdir(root)
    # Ensure imports resolve when launching children
    os.environ.setdefault("PYTHONPATH", str(root))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    required = [
        root / "api_server.py",
        root / "scripts" / "serve_dashboard.py",
        root / "services" / "tile_proxy.py",
        root / "services" / "compressor_stations.py",
    ]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        _log("[RUNNER] FATAL: missing required files:")
        for m in missing:
            _log(f"  - {m}")
        return 2

    py = resolve_python(root)
    _log(f"[RUNNER] Working directory: {root}")
    _log(f"[RUNNER] Contract Version:  {CONTRACT_VERSION}")
    _log(f"[RUNNER] Python:            {py}")

    if args.check_only:
        if not is_port_open(args.port):
            _log(f"UNHEALTHY | Edge :{args.port} not responding.")
            return 1
        info = probe_sentinel_edge(args.port)
        if info.get("gis_ok") and info.get("tiles_ok"):
            _log(
                f"HEALTHY | Edge :{args.port} | CS Count: {info.get('compressor_count')} "
                f"| Contract: {info.get('contract') or CONTRACT_VERSION}"
            )
            _log(json.dumps(info, indent=2))
            return 0
        _log(f"UNHEALTHY | Edge :{args.port} GIS/tiles probe failed.")
        _log(json.dumps(info, indent=2))
        return 1

    # --- Port hygiene ---
    if is_port_open(args.port):
        info = probe_sentinel_edge(args.port)
        if info.get("is_simplehttp") or not info.get("is_sentinel"):
            _log(
                f"[RUNNER] Port {args.port} hijacked "
                f"(server={info.get('server')!r}). Liberating..."
            )
            liberate_port(args.port, force=True)
        elif args.force:
            _log(f"[RUNNER] --force: restarting Sentinel on :{args.port}")
            liberate_port(args.port, force=True)
        else:
            _log(f"[RUNNER] Sentinel already healthy on :{args.port}")
            _print_ready(args.port, info)
            return 0

    # --- Launch edge (canonical public gateway) ---
    edge_script = root / "scripts" / "serve_dashboard.py"
    _log(f"[RUNNER] Launching edge: {edge_script.name} on http://{args.host}:{args.port}")
    edge_cmd = [
        py,
        str(edge_script),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--force",
    ]
    edge_proc = subprocess.Popen(
        edge_cmd,
        cwd=str(root),
        env={**os.environ, "PYTHONPATH": str(root)},
    )

    api_proc = None
    if args.with_api:
        if is_port_open(API_PORT):
            _log(f"[RUNNER] :{API_PORT} already open — skipping api_server")
        else:
            _log(f"[RUNNER] Launching api_server.py on 127.0.0.1:{API_PORT}")
            api_env = {**os.environ, "PYTHONPATH": str(root), "API_SERVER_PORT": str(API_PORT)}
            api_proc = subprocess.Popen(
                [py, str(root / "api_server.py")],
                cwd=str(root),
                env=api_env,
            )

    info = wait_until_alive(args.port, timeout_sec=30.0)
    if not info.get("port_open"):
        _log("[RUNNER] FATAL: edge did not bind :8765")
        edge_proc.terminate()
        if api_proc:
            api_proc.terminate()
        return 1
    if info.get("is_simplehttp"):
        _log("[RUNNER] FATAL: SimpleHTTP still owns the port after launch")
        return 1

    _print_ready(args.port, info)

    try:
        return int(edge_proc.wait())
    except KeyboardInterrupt:
        _log("\n[RUNNER] Shutting down Sentinel engine...")
        edge_proc.terminate()
        if api_proc and api_proc.poll() is None:
            api_proc.terminate()
        return 0


def _print_ready(port: int, info: dict) -> None:
    base = f"http://127.0.0.1:{port}"
    _log("[RUNNER] ========================================")
    _log(f"[RUNNER] ALIVE  {base}")
    _log(f"[RUNNER] HUD    {base}/output/sentinel_dashboard.html?sheet=top10")
    _log(f"[RUNNER] GIS    {base}/api/v1/gis/compressor-stations  count={info.get('compressor_count')}")
    _log(f"[RUNNER] TILES  {base}/api/v1/gis/tiles/status")
    _log(f"[RUNNER] TILE   {base}/api/v1/gis/tiles/esri_satellite/2/1/1.png")
    _log(f"[RUNNER] Contract: {info.get('contract') or CONTRACT_VERSION}")
    _log(f"[RUNNER] GIS ok={info.get('gis_ok')}  tiles ok={info.get('tiles_ok')}")
    _log("[RUNNER] ========================================")


if __name__ == "__main__":
    raise SystemExit(main())
