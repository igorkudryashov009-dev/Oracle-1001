#!/usr/bin/env python3
"""Capture raw hull baseline viewer screenshot (Prompt 1 gate) via Edge CDP."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    import websockets
except ImportError:
    raise SystemExit("websockets required")

ROOT = Path(__file__).resolve().parents[1]
EDGE_CANDIDATES = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe",
]
PORT = 9262
URL = "http://127.0.0.1:8765/logs/raw_hull_baseline_viewer.html"
OUT = ROOT / "logs" / "raw_hull_baseline_9388833.png"


def find_edge() -> Path:
    for p in EDGE_CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit("msedge not found")


def kill_port(port: int) -> None:
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    except Exception:
        return
    for line in out.splitlines():
        if f":{port}" not in line or "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            subprocess.run(["taskkill", "/PID", parts[-1], "/F"], capture_output=True)


_n = 1


async def cdp(ws, method: str, params: dict | None = None):
    global _n
    msg = {"id": _n, "method": method, "params": params or {}}
    _n += 1
    await ws.send(json.dumps(msg))
    while True:
        raw = json.loads(await ws.recv())
        if raw.get("id") == msg["id"]:
            if "error" in raw:
                raise RuntimeError(f"{method}: {raw['error']}")
            return raw.get("result") or {}


async def capture(out_path: Path) -> dict:
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_raw_baseline"
    profile.mkdir(parents=True, exist_ok=True)
    edge = find_edge()
    proc = subprocess.Popen(
        [
            str(edge),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu-sandbox",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2.5)
    try:
        tabs = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read()
        )
        page = next(
            (t for t in tabs if t.get("type") == "page"),
            None,
        )
        if not page:
            return {"ok": False, "error": "no page"}
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": URL})
            ready_meta = None
            for _ in range(40):
                ready = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!(window.__RAW_BASELINE__ && window.__RAW_BASELINE__.size)",
                        "returnByValue": True,
                    },
                )
                if (ready.get("result") or {}).get("value"):
                    meta_ev = await cdp(
                        ws,
                        "Runtime.evaluate",
                        {"expression": "window.__RAW_BASELINE__", "returnByValue": True},
                    )
                    ready_meta = (meta_ev.get("result") or {}).get("value")
                    break
                await asyncio.sleep(0.35)
            await asyncio.sleep(1.0)
            shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(base64.b64decode(shot.get("data") or ""))
            return {
                "ok": True,
                "screenshot": str(out_path),
                "bytes": out_path.stat().st_size,
                "viewer_meta": ready_meta,
            }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    meta = asyncio.run(capture(out))
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0 if meta.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
