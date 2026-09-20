#!/usr/bin/env python3
"""Capture GLB canvas pixels + mesh bbox after presentation fix."""
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
PORT = 9267
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
EDGE = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe"
)
PROFILE = ROOT / "logs" / "edge_cdp_dt_canvas"
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
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
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
              await new Promise(r => setTimeout(r, 3000));
              const stage = document.getElementById('t10-glb-stage');
              stage.scrollIntoView({block:'center'});
              await new Promise(r => setTimeout(r, 200));
              const c = stage.querySelector('canvas');
              // Force a render tick if viewer exposes nothing — just wait
              await new Promise(r => setTimeout(r, 200));
              const dataUrl = c ? c.toDataURL('image/png') : null;
              // Probe scene via Three if attached on canvas
              let bbox = null;
              try {
                // Heuristic from known viewer globals — none. Measure from canvas pixels instead.
                const ctx = document.createElement('canvas');
                ctx.width = c.width; ctx.height = c.height;
                const g = ctx.getContext('2d');
                const img = new Image();
                await new Promise((res, rej) => { img.onload=res; img.onerror=rej; img.src=dataUrl; });
                g.drawImage(img, 0, 0);
                const px = g.getImageData(0, 0, c.width, c.height).data;
                let nonDark = 0, sum=0;
                for (let i=0;i<px.length;i+=16) {
                  const r=px[i], gv=px[i+1], b=px[i+2], a=px[i+3];
                  sum++;
                  if (a>10 && (r+gv+b) > 40) nonDark++;
                }
                bbox = { nonDarkFrac: nonDark/sum, samples: sum, cw:c.width, ch:c.height };
              } catch (e) { bbox = { err: String(e) }; }
              return {
                mode: modal.dataset.viewerMode,
                gridHidden: document.getElementById('t10-modal-grid')?.hidden,
                stageDisplay: getComputedStyle(stage).display,
                dataUrl,
                bbox,
              };
            })()
            """
            ev = await cdp(
                ws,
                "Runtime.evaluate",
                {"expression": js, "awaitPromise": True, "returnByValue": True},
            )
            val = (ev.get("result") or {}).get("value") or {}
            data_url = val.pop("dataUrl", None)
            print(json.dumps(val, indent=2))
            if data_url and data_url.startswith("data:image/png;base64,"):
                raw = base64.b64decode(data_url.split(",", 1)[1])
                out = ROOT / "logs" / "digital_twin_canvas_only.png"
                out.write_bytes(raw)
                print("CANVAS", out, len(raw))
            # Full page after scrollIntoView
            shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
            out2 = ROOT / "logs" / "digital_twin_after_fix.png"
            out2.write_bytes(base64.b64decode(shot.get("data") or ""))
            print("PAGE", out2, out2.stat().st_size)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)


if __name__ == "__main__":
    asyncio.run(main())
