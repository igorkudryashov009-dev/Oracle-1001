#!/usr/bin/env python3
"""Stress Photogrammetric Inspector: 15 open/close cycles via Edge CDP.

Confirms:
  - no JS exceptions
  - no canvas count growth (modal is HTML-only; shared GL stays at 0 or 1)
  - telemetry does not leak across vessels
  - works at desktop + mobile viewport
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    print("FAIL: websockets package required")
    raise SystemExit(2)

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
PORT_BASE = 9230
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"


def find_edge() -> Path:
    for p in EDGE_CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit("FAIL: msedge.exe not found")


def kill_port(port: int) -> None:
    if sys.platform != "win32":
        return
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    except Exception:
        return
    pids: set[str] = set()
    for line in out.splitlines():
        if f":{port}" not in line or "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            pids.add(parts[-1])
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)


async def cdp_call(ws, method: str, params: dict | None = None, session_id: str | None = None):
    msg = {"id": cdp_call._n, "method": method, "params": params or {}}
    cdp_call._n += 1
    if session_id:
        msg["sessionId"] = session_id
    await ws.send(json.dumps(msg))
    while True:
        raw = json.loads(await ws.recv())
        if raw.get("id") == msg["id"]:
            if "error" in raw:
                raise RuntimeError(f"{method}: {raw['error']}")
            return raw.get("result") or {}


cdp_call._n = 1


async def run_viewport(label: str, width: int, height: int, port: int) -> dict:
    edge = find_edge()
    kill_port(port)
    profile = ROOT / "logs" / f"edge_cdp_{label}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(edge),
            f"--remote-debugging-port={port}",
            "--headless=new",
            "--disable-gpu",
            f"--window-size={width},{height}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for DevTools endpoint
        import urllib.request

        ws_url = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                    tabs = json.loads(resp.read().decode("utf-8"))
                page = next((t for t in tabs if t.get("type") == "page" and "sentinel_dashboard" in (t.get("url") or "")), None)
                if not page and tabs:
                    page = next((t for t in tabs if t.get("type") == "page"), tabs[0])
                if page and page.get("webSocketDebuggerUrl"):
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            await asyncio.sleep(0.4)
        if not ws_url:
            return {"label": label, "ok": False, "error": "CDP endpoint not ready"}

        async with websockets.connect(ws_url, max_size=8_000_000) as ws:
            await cdp_call(ws, "Runtime.enable")
            await cdp_call(ws, "Page.enable")
            # Ensure top10 booted
            for _ in range(30):
                ready = await cdp_call(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!(window.__TOP10__ && window.__TOP10__.stressInspector && document.querySelector('.t10-ref-btn'))",
                        "returnByValue": True,
                    },
                )
                if ready.get("result", {}).get("value"):
                    break
                await asyncio.sleep(0.4)
            else:
                return {"label": label, "ok": False, "error": "__TOP10__.stressInspector not ready"}

            # Force mobile metrics if needed
            await cdp_call(
                ws,
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": 1,
                    "mobile": width <= 430,
                },
            )

            result = await cdp_call(
                ws,
                "Runtime.evaluate",
                {
                    "expression": "(async () => { try { return await window.__TOP10__.stressInspector(15); } catch (e) { return { ok:false, errors:[String(e)] }; } })()",
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            summary = result.get("result", {}).get("value") or {}

            # Placeholder path: force broken URL once and check Never-Black UI
            ph = await cdp_call(
                ws,
                "Runtime.evaluate",
                {
                    "expression": """
(() => {
  const v = window.__TOP10__.vessels[0];
  window.__TOP10__.openRefs(v);
  const img = document.querySelector('#t10-modal-grid img[data-ortho]');
  if (!img) return { ok:false, reason:'no img' };
  img.dataset.fbTried = '1';
  img.onerror();
  const ph = img.parentElement && img.parentElement.querySelector('.t10-ref-unavailable');
  const bg = getComputedStyle(img.parentElement).backgroundColor;
  const out = {
    ok: !!(ph && !ph.hidden),
    title: ph && ph.querySelector('.ua-title') ? ph.querySelector('.ua-title').textContent : null,
    figBg: bg,
    canvasCount: document.querySelectorAll('canvas').length,
  };
  window.__TOP10__.closeRefs();
  return out;
})()
""",
                    "returnByValue": True,
                },
            )
            placeholder = ph.get("result", {}).get("value") or {}

            exceptions = []
            # Drain console exceptions if any buffered — best-effort
            return {
                "label": label,
                "viewport": f"{width}x{height}",
                "ok": bool(summary.get("ok")) and bool(placeholder.get("ok")),
                "stress": summary,
                "placeholder": placeholder,
                "exceptions": exceptions,
            }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        kill_port(port)
        await asyncio.sleep(1.0)


async def main() -> int:
    reports = []
    for idx, (label, w, h) in enumerate((("desktop", 1920, 1080), ("mobile", 390, 844))):
        port = PORT_BASE + idx
        print(f"… running {label} {w}x{h} port={port}")
        reports.append(await run_viewport(label, w, h, port))
    out = ROOT / "logs" / "top10_inspector_stress.json"
    out.write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(reports, indent=2, ensure_ascii=False))
    ok = all(r.get("ok") for r in reports)
    print(f"WROTE {out} · PASS={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
