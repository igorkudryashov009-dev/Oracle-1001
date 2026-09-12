#!/usr/bin/env python3
"""OVERHEAD 3D ortho vs Satellite photo + silhouette mask (3 vessels)."""
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

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    import websockets
except ImportError:
    raise SystemExit("websockets required")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = 9291
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
IMOS = ("9388833", "9337755", "9372731")  # BU SAMRA, MOZAH, UMM SLAL
LOGS = ROOT / "logs"
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


def crop_clip(raw: bytes, clip: dict | None) -> Image.Image:
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
    return im


def sat_assets(imo: str):
    from services.top10_3d_mesh import extract_silhouette, resolve_ortho_paths
    from services.top10_vessels import REF_BASE, TOP10_VESSELS

    v = next(x for x in TOP10_VESSELS if str(x["imo"]) == str(imo))
    paths = resolve_ortho_paths(v, REF_BASE)
    photo = Image.open(paths["sat"]).convert("RGB")
    mask = extract_silhouette(paths["sat"])
    return v, photo, mask


def render_mask(mask: np.ndarray, size: tuple[int, int]) -> Image.Image:
    m = (np.asarray(mask) > 0).astype(np.uint8) * 255
    im = Image.fromarray(m, mode="L").convert("RGB")
    return ImageOps.contain(im, size)


def iou_silhouettes(render: Image.Image, mask: np.ndarray) -> float:
    bg = np.array([11, 18, 32], dtype=np.int16)
    arr = np.asarray(render.convert("RGB"))
    dist = np.abs(arr.astype(np.int16) - bg).sum(axis=2)
    sil = dist > 40
    m = np.asarray(mask) > 0
    m_im = Image.fromarray(m.astype(np.uint8) * 255, mode="L").resize(
        (sil.shape[1], sil.shape[0]), Image.Resampling.NEAREST
    )
    mb = np.asarray(m_im) > 127
    inter = np.logical_and(sil, mb).sum()
    union = np.logical_or(sil, mb).sum()
    return float(inter / union) if union else 0.0


def compose(imo: str, name: str, sat: Image.Image, mask: np.ndarray, render: Image.Image, meta: dict) -> Image.Image:
    cell = (480, 280)
    sat_c = ImageOps.contain(sat, cell)
    mask_c = render_mask(mask, cell)
    rnd_c = ImageOps.contain(render, cell)
    pad, label_h, header = 16, 28, 48
    W = pad * 4 + cell[0] * 3
    H = header + pad + cell[1] + label_h + pad
    sheet = Image.new("RGB", (W, H), (11, 18, 32))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 14)
        font_t = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font = font_t = ImageFont.load_default()
    is_ortho = bool((meta.get("view") or {}).get("isOrthographic")) or str(meta.get("mode") or "").startswith("overhead")
    cam = "ORTHO" if is_ortho else "PERS"
    draw.text(
        (pad, 12),
        f"OVERHEAD rootcause · {name} · IMO {imo} · {cam} · {meta.get('mode')} · sil-IoU {meta.get('iou', 0):.3f}",
        fill=(224, 247, 252),
        font=font_t,
    )
    labels = ("SAT photo (truth)", "SAT silhouette mask", "3D voxel ortho top-down")
    for i, (img, lab) in enumerate(((sat_c, labels[0]), (mask_c, labels[1]), (rnd_c, labels[2]))):
        x = pad + i * (cell[0] + pad)
        y = header
        sheet.paste(img, (x + (cell[0] - img.width) // 2, y + (cell[1] - img.height) // 2))
        draw.text((x, y + cell[1] + 6), lab, fill=(148, 163, 184), font=font)
    return sheet


async def shot_overhead(ws, imo: str) -> dict:
    js = f"""
    (async () => {{
      const wait = async (ms) => new Promise(r => setTimeout(r, ms));
      for (let i=0;i<40;i++) {{
        if (window.__TOP10__?.vessels?.length) break;
        await wait(200);
      }}
      const T = window.__TOP10__;
      if (!T?.vessels) return {{ ok:false, error:'no_top10' }};
      const v = T.vessels.find(x => String(x.imo) === '{imo}');
      if (!v) return {{ ok:false, error:'imo not found' }};
      T.openRefs(v, {{ tab: 'overhead' }});
      await wait(400);
      const modal = document.getElementById('t10-ref-modal');
      const tab = modal.querySelector('.t10-insp-tab[data-tab="overhead"]');
      if (tab && !tab.classList.contains('active')) tab.click();
      await wait(3200);
      for (let i=0;i<40;i++) {{
        const c = document.querySelector('#t10-glb-stage canvas');
        if (c && c.width>10) break;
        await wait(200);
      }}
      const stage = document.getElementById('t10-glb-stage');
      stage?.scrollIntoView({{block:'center'}});
      await wait(250);
      const r = stage?.getBoundingClientRect();
      return {{
        ok: !!stage?.querySelector('canvas'),
        mode: modal.dataset.viewerMode,
        fidelity: document.querySelector('.t10-glb-fidelity')?.textContent || null,
        view: window.__T10_VIEW__ || null,
        name: v.name,
        clip: r ? {{x:r.x,y:r.y,w:r.width,h:r.height,dpr:window.devicePixelRatio||1}} : null,
      }};
    }})()
    """
    meta = await eval_js(ws, js)
    if not isinstance(meta, dict):
        meta = {"ok": False, "error": meta}
    shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
    raw = base64.b64decode(shot.get("data") or "")
    render = crop_clip(raw, (meta or {}).get("clip"))
    await cdp(ws, "Runtime.evaluate", {"expression": "window.__TOP10__?.closeRefs?.()", "returnByValue": True})
    await asyncio.sleep(0.2)
    return meta or {}, render


async def main() -> int:
    from services.top10_vessels import write_js_manifest
    from services.web_assets_sync import sync_web_assets

    write_js_manifest()
    sync_web_assets(ROOT, force=True, log=False)
    kill_port(PORT)
    profile = LOGS / "edge_cdp_voxel_reg"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "--no-first-run", URL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2.5)
    rows = {}
    regression = {}
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next((t for t in tabs if t.get("type") == "page" and "sentinel_dashboard" in (t.get("url") or "")), None)
        if not page:
            page = next(t for t in tabs if t.get("type") == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 << 20) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": f"{URL}&nocache={int(time.time())}"})
            await asyncio.sleep(1.5)
            ready = False
            for _ in range(80):
                r = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!(window.__TOP10__&&window.__TOP10__.vessels&&window.__TOP10__.vessels.length)",
                        "returnByValue": True,
                    },
                )
                if (r.get("result") or {}).get("value"):
                    ready = True
                    break
                await asyncio.sleep(0.3)
            if not ready:
                loc = await cdp(ws, "Runtime.evaluate", {"expression": "location.href", "returnByValue": True})
                raise RuntimeError(f"__TOP10__ never booted href={(loc.get('result') or {}).get('value')}")

            for imo in IMOS:
                meta, render = await shot_overhead(ws, imo)
                v, sat, mask = sat_assets(imo)
                iou = iou_silhouettes(render, mask)
                meta["iou"] = round(iou, 4)
                meta["isOrtho"] = str(meta.get("mode") or "").startswith("overhead")
                sheet = compose(imo, v["name"], sat, mask, render, meta)
                out = LOGS / f"overhead_rootcause_fix_{imo}.png"
                sheet.save(out)
                render.save(LOGS / f"overhead_rootcause_fix_{imo}_3d.png")
                meta["screenshot"] = str(out)
                rows[imo] = meta
                print(json.dumps({"imo": imo, "ok": meta.get("ok"), "mode": meta.get("mode"), "error": meta.get("error"), "iou": iou, "view": meta.get("view"), "fidelity": meta.get("fidelity")}, ensure_ascii=False))

            # LATERAL / BOW stay photos — do not remount WebGL
            reg_js = """
            (async () => {
              const wait = async (ms) => new Promise(r => setTimeout(r, ms));
              const T = window.__TOP10__;
              const v = T.vessels.find(x => String(x.imo) === '9388833');
              const out = {};
              for (const tab of ['lateral', 'bow']) {
                T.openRefs(v, { tab });
                await wait(800);
                const modal = document.getElementById('t10-ref-modal');
                out[tab] = {
                  mode: modal?.dataset?.viewerMode || null,
                  webgl: document.querySelectorAll('#t10-glb-stage canvas').length,
                };
                T.closeRefs?.();
                await wait(200);
              }
              return out;
            })()
            """
            regression = await eval_js(ws, reg_js)
        report = {"ok": all(r.get("ok") for r in rows.values()), "vessels": rows, "lateral_bow": regression}
        (LOGS / "overhead_rootcause_fix_shots.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"ok": report["ok"], "n": len(rows)}, indent=2))
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
