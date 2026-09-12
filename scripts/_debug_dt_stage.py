#!/usr/bin/env python3
"""Debug DIGITAL TWIN stage after presentation fixes."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
PORT = 9266
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
EDGE = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe"
)
PROFILE = ROOT / "logs" / "edge_cdp_dt_debug"
_n = 1


async def cdp(ws, method, params=None):
    global _n
    msg = {"id": _n, "method": method, "params": params or {}}
    _n += 1
    await ws.send(json.dumps(msg))
    while True:
        raw = json.loads(await ws.recv())
        if raw.get("id") == msg["id"]:
            if "error" in raw:
                raise RuntimeError(raw["error"])
            return raw.get("result") or {}


def kill_port(port: int) -> None:
    out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            subprocess.run(["taskkill", "/PID", line.split()[-1], "/F"], capture_output=True)


async def main() -> None:
    kill_port(PORT)
    PROFILE.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(3)
    try:
        tabs = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read()
        )
        page = next(t for t in tabs if t.get("type") == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.enable")
            await cdp(ws, "Page.navigate", {"url": URL})
            for _ in range(40):
                r = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!(window.__TOP10__&&window.__TOP10__.vessels)",
                        "returnByValue": True,
                    },
                )
                if (r.get("result") or {}).get("value"):
                    break
                await asyncio.sleep(0.3)
            js = r"""
            (async () => {
              const T = window.__TOP10__;
              const v = T.vessels.find(x => String(x.imo)==='9388833');
              T.openRefs(v);
              await new Promise(r => setTimeout(r, 400));
              const modal = document.getElementById('t10-ref-modal');
              modal.querySelector('.t10-insp-tab[data-tab="glb"]').click();
              await new Promise(r => setTimeout(r, 2800));
              const stage = document.getElementById('t10-glb-stage');
              const c = stage && stage.querySelector('canvas');
              const cs = stage ? getComputedStyle(stage) : null;
              let pixel = null;
              if (c) {
                try {
                  pixel = {
                    w: c.width, h: c.height, cls: c.className,
                    dataUrlLen: (c.toDataURL('image/png') || '').length,
                    hostFallback: stage.classList.contains('is-glb-fallback'),
                    hostReady: stage.classList.contains('is-glb-ready'),
                  };
                } catch (e) { pixel = { err: String(e) }; }
              }
              return {
                mode: modal.dataset.viewerMode,
                stageDisplay: cs && cs.display,
                stageVisibility: cs && cs.visibility,
                stageH: stage && stage.clientHeight,
                stageW: stage && stage.clientWidth,
                stageClass: stage && stage.className,
                hasCanvas: !!c,
                pixel,
                warn: document.querySelector('#t10-insp-mesh-warn')?.hidden === false
                  ? document.querySelector('#t10-insp-mesh-warn').textContent : null,
                gridHidden: !!document.getElementById('t10-modal-grid')?.hidden,
              };
            })()
            """
            ev = await cdp(
                ws,
                "Runtime.evaluate",
                {"expression": js, "awaitPromise": True, "returnByValue": True},
            )
            val = (ev.get("result") or {}).get("value")
            print(json.dumps(val, indent=2, ensure_ascii=False))
            shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
            out = ROOT / "logs" / "digital_twin_after_debug.png"
            out.write_bytes(base64.b64decode(shot.get("data") or ""))
            print("SHOT", out, out.stat().st_size)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)


if __name__ == "__main__":
    asyncio.run(main())
