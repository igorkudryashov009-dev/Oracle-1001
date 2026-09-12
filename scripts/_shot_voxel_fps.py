#!/usr/bin/env python3
"""Measure InstancedMesh orbit FPS for one voxel grid (wall-clock 2s)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    import websockets
except ImportError:
    raise SystemExit("websockets required")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = 9283
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
IMO = "9388833"
EDGE = next(
    p
    for p in [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft/Edge/Application/msedge.exe",
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
    from services.web_assets_sync import sync_web_assets

    sync_web_assets(ROOT, force=True, log=False)
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_voxel_fps"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "--no-first-run", URL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2.5)
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next(t for t in tabs if t.get("type") == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 << 20) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": f"{URL}&t={int(time.time())}"})
            for _ in range(50):
                r = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!(window.__TOP10__&&window.__TOP10__.vessels&&window.__TOP10__.vessels.length)",
                        "returnByValue": True,
                    },
                )
                if (r.get("result") or {}).get("value"):
                    break
                await asyncio.sleep(0.3)
            js = f"""
            (async () => {{
              const T = window.__TOP10__;
              const v = T.vessels.find(x => String(x.imo) === '{IMO}');
              T.openRefs(v, {{ tab: 'voxel' }});
              await new Promise(r => setTimeout(r, 500));
              document.querySelector('.t10-insp-tab[data-tab="voxel"]')?.click();
              for (let i=0;i<50;i++) {{
                const c = document.querySelector('#t10-glb-stage canvas');
                if (c && c.width>10) break;
                await new Promise(r => setTimeout(r, 200));
              }}
              await new Promise(r => setTimeout(r, 1200));
              const orbit = document.querySelector('.t10-glb-btn[data-act="orbit"]');
              if (orbit && !orbit.classList.contains('is-on')) orbit.click();
              const t0 = performance.now();
              let frames = 0;
              let maxDt = 0;
              let last = t0;
              await new Promise((resolve) => {{
                const tick = (t) => {{
                  frames += 1;
                  maxDt = Math.max(maxDt, t - last);
                  last = t;
                  if (t - t0 >= 2000) resolve();
                  else requestAnimationFrame(tick);
                }};
                requestAnimationFrame(tick);
              }});
              const elapsed = performance.now() - t0;
              const fps = frames / (elapsed / 1000);
              if (orbit?.classList.contains('is-on')) orbit.click();
              return {{
                ok: frames > 20,
                imo: '{IMO}',
                n: v.voxel_cubes?.n_occupied,
                frames,
                elapsed_ms: +elapsed.toFixed(1),
                fps: +fps.toFixed(1),
                max_frame_ms: +maxDt.toFixed(2),
                smooth: fps >= 45 && maxDt < 50,
              }};
            }})()
            """
            ev = await cdp(
                ws,
                "Runtime.evaluate",
                {"expression": js, "awaitPromise": True, "returnByValue": True},
            )
            meta = (ev.get("result") or {}).get("value") or {"ok": False, "error": ev}
            (ROOT / "logs" / "voxel_v3_fps_9388833.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )
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
