#!/usr/bin/env python3
"""Ortho Triplet layout check — full modal, no photo crop."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image

try:
    import websockets
except ImportError:
    raise SystemExit("websockets required")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = 9278
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
OUT = ROOT / "logs" / "ortho_triplet_layout_fix_check.png"
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


async def eval_js(ws, expression: str):
    ev = await cdp(
        ws,
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
    )
    if ev.get("exceptionDetails"):
        return {"ok": False, "error": ev.get("exceptionDetails")}
    return (ev.get("result") or {}).get("value")


def save_clip(raw: bytes, clip: dict | None, path: Path) -> None:
    im = Image.open(BytesIO(raw)).convert("RGB")
    clip = clip or {}
    if clip.get("w") and clip.get("h"):
        dpr = float(clip.get("dpr") or 1)
        x0 = max(0, int(clip["x"] * dpr))
        y0 = max(0, int(clip["y"] * dpr))
        x1 = min(im.width, int((clip["x"] + clip["w"]) * dpr))
        y1 = min(im.height, int((clip["y"] + clip["h"]) * dpr))
        if x1 > x0 + 40 and y1 > y0 + 40:
            im = im.crop((x0, y0, x1, y1))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


async def inspect_vessel(ws, imo: str) -> dict:
    js = f"""
    (async () => {{
      const T = window.__TOP10__;
      const v = T.vessels.find(x => String(x.imo) === '{imo}');
      T.openRefs(v);
      await new Promise(r => setTimeout(r, 700));
      const modal = document.getElementById('t10-ref-modal');
      const panel = modal.querySelector('.t10-modal-panel');
      const tabs = [...modal.querySelectorAll('.t10-insp-tab')].map(t => t.getAttribute('data-tab'));
      const figs = [...modal.querySelectorAll('.t10-ref-fig img')].map(img => {{
        const cs = getComputedStyle(img);
        const fig = img.closest('.t10-ref-fig');
        const fr = fig.getBoundingClientRect();
        const ir = img.getBoundingClientRect();
        const expectedH = img.naturalWidth ? ir.width * (img.naturalHeight / img.naturalWidth) : 0;
        return {{
          view: fig.getAttribute('data-view'),
          objectFit: cs.objectFit,
          nw: img.naturalWidth, nh: img.naturalHeight,
          w: ir.width, h: ir.height,
          figH: fr.height,
          overflowHidden: getComputedStyle(fig).overflow === 'hidden',
          croppedVsAspect: expectedH > 0 ? (ir.height + 1.5) < expectedH : null,
        }};
      }});
      const pcs = getComputedStyle(panel);
      const pr = panel.getBoundingClientRect();
      panel.scrollTop = 0;
      await new Promise(r => setTimeout(r, 80));
      return {{
        ok: figs.length === 3 && figs.every(f => f.objectFit === 'contain' && f.croppedVsAspect === false),
        imo: '{imo}',
        name: v.name,
        tabs,
        tabWrap: getComputedStyle(modal.querySelector('.t10-insp-tabs')).flexWrap,
        panel: {{
          maxHeight: pcs.maxHeight,
          overflow: pcs.overflow,
          display: pcs.display,
          h: pr.height,
          scrollH: panel.scrollHeight,
          canScroll: panel.scrollHeight > panel.clientHeight + 2,
        }},
        gridFlex: getComputedStyle(document.getElementById('t10-modal-grid')).flex,
        figs,
        clip: (() => {{
          const r = panel.getBoundingClientRect();
          return {{ x:r.x, y:r.y, w:r.width, h:r.height, dpr: window.devicePixelRatio||1 }};
        }})(),
      }};
    }})()
    """
    return await eval_js(ws, js)


async def main() -> int:
    from services.top10_vessels import write_js_manifest
    from services.web_assets_sync import sync_web_assets

    write_js_manifest()
    sync_web_assets(ROOT, force=True, log=False)
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_ortho_layout"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            URL,
        ],
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

            checks = []
            for imo in ("9388833", "9397327", "9337755"):
                meta = await inspect_vessel(ws, imo)
                checks.append(meta)
                if imo == "9388833":
                    shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
                    save_clip(base64.b64decode(shot.get("data") or ""), meta.get("clip"), OUT)
                    meta["screenshot"] = str(OUT)
            report = {
                "ok": all(c.get("ok") for c in checks),
                "checks": checks,
                "screenshot": str(OUT),
            }
            (ROOT / "logs" / "ortho_triplet_layout_fix.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            print(json.dumps(report, indent=2, default=str)[:4000])
            return 0 if report["ok"] else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
