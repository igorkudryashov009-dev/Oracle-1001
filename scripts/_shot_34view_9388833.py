#!/usr/bin/env python3
"""BU SAMRA 3/4 default camera confirmation shot."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
PORT = 9266
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
OUT = ROOT / "logs" / "digital_twin_34view_9388833.png"
IMO = "9388833"
EDGE = next(
    p
    for p in [
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
    if p.is_file()
)
_n = 1


def kill_port(port: int) -> None:
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    except Exception:
        return
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            pid = line.split()[-1]
            if pid.isdigit():
                subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)


async def cdp(ws, method, params=None):
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


async def main() -> int:
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_34view"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2.4)
    try:
        tabs = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read()
        )
        page = next((t for t in tabs if t.get("type") == "page"), None)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 << 20) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": f"{URL}&nocache={int(time.time())}"})
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
            js = f"""
            (async () => {{
              const T = window.__TOP10__;
              const v = T.vessels.find(x => String(x.imo)==='{IMO}');
              T.openRefs(v);
              await new Promise(r => setTimeout(r, 400));
              const modal = document.getElementById('t10-ref-modal');
              modal.querySelector('.t10-insp-tab[data-tab="glb"]').click();
              await new Promise(r => setTimeout(r, 2800));
              for (let i=0;i<25;i++) {{
                const c=document.querySelector('#t10-glb-stage canvas');
                if (c && c.width>10) break;
                await new Promise(r=>setTimeout(r,200));
              }}
              const measure=document.querySelector('.t10-glb-btn[data-act="measure"]');
              const wire=document.querySelector('.t10-glb-btn[data-act="wire"]');
              const orbit=document.querySelector('.t10-glb-btn[data-act="orbit"]');
              if (measure?.classList.contains('is-on')) measure.click();
              if (wire?.classList.contains('is-on')) wire.click();
              if (orbit?.classList.contains('is-on')) orbit.click();
              document.querySelector('.t10-glb-btn[data-act="reset"]').click();
              await new Promise(r => setTimeout(r, 750));
              if (orbit?.classList.contains('is-on')) orbit.click();
              document.getElementById('t10-glb-stage')?.scrollIntoView({{block:'center'}});
              await new Promise(r => setTimeout(r, 350));
              const c=document.querySelector('#t10-glb-stage canvas');
              const d=window.__T10_GLB_DEBUG__||null;
              return {{
                ok:true,
                measureOn:!!measure?.classList.contains('is-on'),
                wireOn:!!wire?.classList.contains('is-on'),
                orbitOn:!!orbit?.classList.contains('is-on'),
                cameraPos:d?.cameraPos||null,
                scale:d?.scale||null,
                rotation:d?.rotation||null,
                cw:c?.width, ch:c?.height,
              }};
            }})()
            """
            ev = await cdp(
                ws,
                "Runtime.evaluate",
                {"expression": js, "awaitPromise": True, "returnByValue": True},
            )
            meta = (ev.get("result") or {}).get("value") or {}
            shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
            OUT.write_bytes(base64.b64decode(shot.get("data") or ""))
            meta["screenshot"] = str(OUT)
            meta["bytes"] = OUT.stat().st_size
            print(json.dumps(meta, indent=2))
            return 0 if meta.get("ok") else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
