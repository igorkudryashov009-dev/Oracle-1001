#!/usr/bin/env python3
"""Screenshot Z-up probe viewer + production DIGITAL TWIN for compare."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
PORT = 9264
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
                raise RuntimeError(raw["error"])
            return raw.get("result") or {}


async def shot(url: str, out: Path, settle: float = 3.0, prep_js: str | None = None) -> dict:
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_dual_shot"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2.2)
    try:
        tabs = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read()
        )
        page = next((t for t in tabs if t.get("type") == "page"), None)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 << 20) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": url})
            await asyncio.sleep(settle)
            meta = {}
            if prep_js:
                ev = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {"expression": prep_js, "awaitPromise": True, "returnByValue": True},
                )
                meta = (ev.get("result") or {}).get("value") or {}
            else:
                ev = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {"expression": "window.__PROBE__ || null", "returnByValue": True},
                )
                meta = (ev.get("result") or {}).get("value") or {}
            shot_r = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(shot_r.get("data") or ""))
            meta["screenshot"] = str(out)
            meta["bytes"] = out.stat().st_size
            return meta
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)


async def main():
    probe_url = "http://127.0.0.1:8765/logs/decouple_zup_probe_viewer.html"
    prod_url = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
    prep = """
    (async () => {
      const T = window.__TOP10__;
      if (!T) return {ok:false,error:'no TOP10'};
      const v = T.vessels.find(x => String(x.imo)==='9388833');
      T.openRefs(v);
      await new Promise(r => setTimeout(r, 400));
      const modal = document.getElementById('t10-ref-modal');
      const tab = modal.querySelector('.t10-insp-tab[data-tab="glb"]');
      tab.click();
      await new Promise(r => setTimeout(r, 3200));
      const stage = document.getElementById('t10-glb-stage');
      if (stage) {
        stage.scrollIntoView({block:'center', inline:'nearest'});
        // Hide ortho grid while capturing DT (visual gate focuses on mesh)
        const grid = modal.querySelector('.t10-insp-grid, #t10-ortho-grid, .t10-tri-grid');
        if (grid) grid.style.display = 'none';
      }
      await new Promise(r => setTimeout(r, 400));
      const c = stage?.querySelector('canvas');
      return {
        ok: true,
        mode: modal?.dataset?.viewerMode,
        tab: modal?.querySelector('.t10-insp-tab.active')?.getAttribute('data-tab'),
        cw: c?.width, ch: c?.height,
        debug: window.__T10_GLB_DEBUG__ || null,
      };
    })()
    """
    a = await shot(probe_url, ROOT / "logs" / "digital_twin_zup_probe.png", settle=3.5)
    b = await shot(
        prod_url,
        ROOT / "logs" / "digital_twin_decoupled_pipeline.png",
        settle=1.0,
        prep_js=prep,
    )
    print(json.dumps({"probe": a, "prod": b}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
