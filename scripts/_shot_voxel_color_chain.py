#!/usr/bin/env python3
"""Screenshot VOXEL GRID for one/all vessels with a filename prefix + contact sheet."""
from __future__ import annotations

import argparse
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

from PIL import Image, ImageDraw, ImageFont

try:
    import websockets
except ImportError:
    raise SystemExit("websockets required")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
LOGS = ROOT / "logs"
EDGE = next(
    p
    for p in [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\\Program Files"))
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


async def shot_one(ws, imo: str, out: Path) -> dict:
    js = f"""
    (async () => {{
      const T = window.__TOP10__;
      const v = T.vessels.find(x => String(x.imo) === '{imo}');
      if (!v) return {{ ok:false, error:'imo not found' }};
      T.openRefs(v, {{ tab: 'voxel' }});
      await new Promise(r => setTimeout(r, 400));
      const modal = document.getElementById('t10-ref-modal');
      const tab = modal.querySelector('.t10-insp-tab[data-tab="voxel"]');
      if (tab && !tab.classList.contains('active')) tab.click();
      await new Promise(r => setTimeout(r, 2400));
      for (let i=0;i<25;i++) {{
        const c = document.querySelector('#t10-glb-stage canvas');
        if (c && c.width>10) break;
        await new Promise(r => setTimeout(r, 160));
      }}
      document.querySelector('.t10-glb-btn[data-act="reset"]')?.click();
      await new Promise(r => setTimeout(r, 500));
      const orbit = document.querySelector('.t10-glb-btn[data-act="orbit"]');
      if (orbit && !orbit.classList.contains('is-on')) orbit.click();
      const fps = await new Promise((resolve) => {{
        const samples = [];
        let last = performance.now();
        let n = 0;
        const done = () => {{
          const mid = samples.slice(Math.min(5, Math.max(0, samples.length - 1)));
          if (!mid.length) {{
            resolve({{ frames: 0, avg_ms: null, fps: null, max_ms: null, smooth: false, error: 'no_frames' }});
            return;
          }}
          const avg = mid.reduce((a,b)=>a+b,0) / mid.length;
          resolve({{
            frames: mid.length,
            avg_ms: +avg.toFixed(2),
            fps: +(1000 / avg).toFixed(1),
            max_ms: +Math.max(...mid).toFixed(2),
            smooth: (1000 / avg) >= 45 && Math.max(...mid) < 40,
          }});
        }};
        const timer = setTimeout(done, 2500);
        const tick = (t) => {{
          samples.push(t - last);
          last = t;
          n += 1;
          if (n < 90) requestAnimationFrame(tick);
          else {{ clearTimeout(timer); done(); }}
        }};
        requestAnimationFrame(tick);
      }});
      if (orbit?.classList.contains('is-on')) orbit.click();
      const stage = document.getElementById('t10-glb-stage');
      stage?.scrollIntoView({{block:'center'}});
      await new Promise(r => setTimeout(r, 250));
      const r = stage?.getBoundingClientRect();
      return {{
        ok: !!stage?.querySelector('canvas'),
        mode: modal.dataset.viewerMode,
        fidelity: document.querySelector('.t10-glb-fidelity')?.textContent || null,
        n: v.voxel_cubes?.n_occupied,
        k: v.voxel_cubes?.palette_k || (v.voxel_cubes?.palette_hex||[]).length,
        name: v.name, rank: v.rank,
        fps,
        clip: r ? {{x:r.x,y:r.y,w:r.width,h:r.height,dpr:window.devicePixelRatio||1}} : null,
      }};
    }})()
    """
    ev = await cdp(ws, "Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True})
    if ev.get("exceptionDetails"):
        return {"ok": False, "error": "js_exception", "detail": ev["exceptionDetails"]}
    meta = (ev.get("result") or {}).get("value") or {}
    shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
    raw = base64.b64decode(shot.get("data") or "")
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
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    meta["screenshot"] = str(out)
    await cdp(ws, "Runtime.evaluate", {"expression": "window.__TOP10__?.closeRefs?.()", "returnByValue": True})
    await asyncio.sleep(0.25)
    return meta


def build_contact(vessels, prefix: str, title: str, contact: Path) -> Path:
    cell_w, cell_h, pad, label_h = 480, 280, 12, 28
    cols, rows = 5, 2
    W = cols * cell_w + (cols + 1) * pad
    H = rows * (cell_h + label_h) + (rows + 1) * pad + 36
    sheet = Image.new("RGB", (W, H), (11, 18, 32))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 14)
        font_t = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font = font_t = ImageFont.load_default()
    draw.text((pad, 10), title, fill=(224, 247, 252), font=font_t)
    for i, v in enumerate(vessels[:10]):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = 36 + pad + r * (cell_h + label_h + pad)
        imo = str(v["imo"])
        path = LOGS / f"{prefix}_{imo}.png"
        if path.exists():
            img = Image.open(path).convert("RGB")
            img.thumbnail((cell_w, cell_h))
            sheet.paste(img, (x + (cell_w - img.width) // 2, y + (cell_h - img.height) // 2))
        draw.text(
            (x, y + cell_h + 4),
            f"#{v.get('rank')} {v.get('name')} · IMO {imo}",
            fill=(148, 163, 184),
            font=font,
        )
    contact.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(contact)
    return contact


async def main() -> int:
    from services.top10_vessels import TOP10_VESSELS, write_js_manifest
    from services.web_assets_sync import sync_web_assets

    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="voxel_color_final")
    ap.add_argument("--title", default="VOXEL GRID · k≥21 · multi-sample · TOP-10")
    ap.add_argument("--imo", default="")
    ap.add_argument("--port", type=int, default=9274)
    args = ap.parse_args()

    write_js_manifest()
    sync_web_assets(ROOT, force=True, log=False)

    vessels = TOP10_VESSELS
    if args.imo:
        vessels = [v for v in TOP10_VESSELS if str(v["imo"]) == str(args.imo)]
        if not vessels:
            print(json.dumps({"ok": False, "error": "imo not found"}))
            return 1

    kill_port(args.port)
    profile = LOGS / f"edge_cdp_{args.prefix}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={args.port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2.5)
    results = {}
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json/list", timeout=5).read())
        page = next(t for t in tabs if t.get("type") == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 << 20) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": f"{URL}&nocache={int(time.time())}"})
            for _ in range(60):
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
            for v in vessels:
                imo = str(v["imo"])
                out = LOGS / f"{args.prefix}_{imo}.png"
                meta = await shot_one(ws, imo, out)
                results[imo] = meta
                print(json.dumps({"imo": imo, "ok": meta.get("ok"), "k": meta.get("k"), "n": meta.get("n")}, ensure_ascii=False))
        contact = None
        if len(vessels) > 1:
            contact = build_contact(vessels, args.prefix, args.title, LOGS / f"{args.prefix}_all10_contact_sheet.png")
        report = {"shots": results, "contact_sheet": str(contact) if contact else None, "prefix": args.prefix}
        (LOGS / f"{args.prefix}_shots.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"contact": report["contact_sheet"], "n_ok": sum(1 for r in results.values() if r.get("ok"))}, indent=2))
        return 0 if all(r.get("ok") for r in results.values()) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(args.port)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
