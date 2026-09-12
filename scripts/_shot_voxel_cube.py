#!/usr/bin/env python3
"""Screenshot VOXEL GRID tab for one IMO (default BU SAMRA)."""
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

try:
    import websockets
except ImportError:
    raise SystemExit("websockets required")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = 9268
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
IMO = sys.argv[1] if len(sys.argv) > 1 else "9388833"
OUT = ROOT / "logs" / f"voxel_cube_reconstruction_{IMO}.png"

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
    from services.top10_vessels import write_js_manifest
    from services.web_assets_sync import sync_web_assets

    write_js_manifest()
    sync_web_assets(ROOT, force=True, log=False)

    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_voxel_cubes"
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
    await asyncio.sleep(2.5)
    try:
        tabs = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read()
        )
        page = next((t for t in tabs if t.get("type") == "page"), None)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 << 20) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": f"{URL}&nocache={int(time.time())}"})
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
              if (!v) return {{ ok:false, error:'imo not found' }};
              T.openRefs(v);
              await new Promise(r => setTimeout(r, 400));
              const modal = document.getElementById('t10-ref-modal');
              const tab = modal.querySelector('.t10-insp-tab[data-tab="voxel"]');
              if (!tab) return {{ ok:false, error:'no voxel tab' }};
              if (tab.classList.contains('is-disabled') || tab.getAttribute('aria-disabled')==='true') {{
                return {{ ok:false, error:'voxel tab disabled', ready:v.voxel_cubes }};
              }}
              tab.click();
              await new Promise(r => setTimeout(r, 2800));
              for (let i = 0; i < 30; i++) {{
                const c = document.querySelector('#t10-glb-stage canvas');
                if (c && c.width > 10) break;
                await new Promise(r => setTimeout(r, 200));
              }}
              document.querySelector('.t10-glb-btn[data-act="reset"]')?.click();
              await new Promise(r => setTimeout(r, 600));
              const orbit = document.querySelector('.t10-glb-btn[data-act="orbit"]');
              if (orbit?.classList.contains('is-on')) orbit.click();
              const stage = document.getElementById('t10-glb-stage');
              stage?.scrollIntoView({{ block: 'center' }});
              await new Promise(r => setTimeout(r, 300));
              const r = stage?.getBoundingClientRect();
              const fid = document.querySelector('.t10-glb-fidelity')?.textContent || null;
              return {{
                ok: !!stage?.querySelector('canvas'),
                mode: modal.dataset.viewerMode,
                fidelity: fid,
                n: v.voxel_cubes?.n_occupied,
                clip: r ? {{ x:r.x, y:r.y, w:r.width, h:r.height, dpr: window.devicePixelRatio||1 }} : null,
              }};
            }})()
            """
            ev = await cdp(
                ws,
                "Runtime.evaluate",
                {"expression": js, "awaitPromise": True, "returnByValue": True},
            )
            if ev.get("exceptionDetails"):
                meta = {"ok": False, "error": "js_exception", "detail": ev.get("exceptionDetails")}
            else:
                meta = (ev.get("result") or {}).get("value") or {"ok": False, "error": "empty_eval", "raw": ev}
            shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
            raw = base64.b64decode(shot.get("data") or "")
            from io import BytesIO
            from PIL import Image

            im = Image.open(BytesIO(raw)).convert("RGB")
            clip = meta.get("clip") or {}
            if clip.get("w") and clip.get("h"):
                dpr = float(clip.get("dpr") or 1)
                x0 = max(0, int(clip["x"] * dpr))
                y0 = max(0, int(clip["y"] * dpr))
                x1 = min(im.width, int((clip["x"] + clip["w"]) * dpr))
                y1 = min(im.height, int((clip["y"] + clip["h"]) * dpr))
                if x1 > x0 + 40 and y1 > y0 + 40:
                    im = im.crop((x0, y0, x1, y1))
            im.save(OUT)
            meta["screenshot"] = str(OUT)
            meta["bytes"] = OUT.stat().st_size
            (ROOT / "logs" / f"voxel_cube_shot_{IMO}.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )
            print(json.dumps(meta, indent=2, ensure_ascii=False))
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
