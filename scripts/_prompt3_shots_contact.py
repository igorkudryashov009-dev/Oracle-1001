#!/usr/bin/env python3
"""Prompt-3 final: screenshot all TOP-10 DIGITAL TWIN (3/4 default) + contact sheet."""
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

from PIL import Image, ImageDraw, ImageFont

try:
    import websockets
except ImportError:
    raise SystemExit("websockets required")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = 9267
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
LOGS = ROOT / "logs"
CONTACT = LOGS / "digital_twin_all10_final_contact_sheet.png"

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


async def shot_one(ws, imo: str) -> dict:
    js = f"""
    (async () => {{
      const T = window.__TOP10__;
      const v = T.vessels.find(x => String(x.imo) === '{imo}');
      if (!v) return {{ ok:false, error:'imo not found' }};
      T.openRefs(v);
      await new Promise(r => setTimeout(r, 350));
      const modal = document.getElementById('t10-ref-modal');
      modal.querySelector('.t10-insp-tab[data-tab="glb"]').click();
      await new Promise(r => setTimeout(r, 2600));
      for (let i = 0; i < 25; i++) {{
        const c = document.querySelector('#t10-glb-stage canvas');
        if (c && c.width > 10) break;
        await new Promise(r => setTimeout(r, 180));
      }}
      const measure = document.querySelector('.t10-glb-btn[data-act="measure"]');
      const wire = document.querySelector('.t10-glb-btn[data-act="wire"]');
      const orbit = document.querySelector('.t10-glb-btn[data-act="orbit"]');
      if (measure?.classList.contains('is-on')) measure.click();
      if (wire?.classList.contains('is-on')) wire.click();
      if (orbit?.classList.contains('is-on')) orbit.click();
      document.querySelector('.t10-glb-btn[data-act="reset"]')?.click();
      await new Promise(r => setTimeout(r, 700));
      if (orbit?.classList.contains('is-on')) orbit.click();
      const stage = document.getElementById('t10-glb-stage');
      stage?.scrollIntoView({{ block: 'center' }});
      await new Promise(r => setTimeout(r, 300));
      // Prefer canvas-only crop via element bounds
      const c = stage?.querySelector('canvas');
      const r = stage?.getBoundingClientRect();
      return {{
        ok: !!c,
        mode: modal.dataset.viewerMode,
        measureOn: !!measure?.classList.contains('is-on'),
        wireOn: !!wire?.classList.contains('is-on'),
        orbitOn: !!orbit?.classList.contains('is-on'),
        fidelity: document.querySelector('.t10-glb-fidelity')?.textContent || null,
        clip: r ? {{ x: r.x, y: r.y, w: r.width, h: r.height, dpr: window.devicePixelRatio || 1 }} : null,
        name: v.name, rank: v.rank,
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
    raw = base64.b64decode(shot.get("data") or "")
    out = LOGS / f"digital_twin_final_{imo}.png"
    # Crop to GLB stage if possible
    try:
        from io import BytesIO

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
        im.save(out)
    except Exception:
        out.write_bytes(raw)
    meta["screenshot"] = str(out)
    meta["bytes"] = out.stat().st_size
    # close modal for next
    await cdp(
        ws,
        "Runtime.evaluate",
        {"expression": "window.__TOP10__?.closeRefs?.()", "returnByValue": True},
    )
    await asyncio.sleep(0.35)
    return meta


def build_contact_sheet(vessels: list[dict], shot_metas: dict) -> Path:
    # 2 rows x 5 cols
    cell_w, cell_h = 480, 280
    pad = 12
    label_h = 28
    cols, rows = 5, 2
    W = cols * cell_w + (cols + 1) * pad
    H = rows * (cell_h + label_h) + (rows + 1) * pad + 36
    sheet = Image.new("RGB", (W, H), (11, 18, 32))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 14)
        font_t = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font = ImageFont.load_default()
        font_t = font
    draw.text((pad, 10), "DIGITAL TWIN final — TOP-10 · 3/4 camera · honest envelope", fill=(224, 247, 252), font=font_t)

    for i, v in enumerate(vessels[:10]):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = 36 + pad + r * (cell_h + label_h + pad)
        imo = str(v["imo"])
        path = LOGS / f"digital_twin_final_{imo}.png"
        if path.exists():
            img = Image.open(path).convert("RGB")
            img.thumbnail((cell_w, cell_h))
            ox = x + (cell_w - img.width) // 2
            oy = y + (cell_h - img.height) // 2
            sheet.paste(img, (ox, oy))
        label = f"#{v.get('rank')} {v.get('name')} · IMO {imo}"
        draw.text((x, y + cell_h + 4), label, fill=(148, 163, 184), font=font)
    sheet.save(CONTACT)
    return CONTACT


async def main() -> int:
    from services.top10_vessels import TOP10_VESSELS, write_js_manifest
    from services.web_assets_sync import sync_web_assets

    write_js_manifest()
    sync_web_assets(ROOT, force=True, log=False)

    kill_port(PORT)
    profile = LOGS / "edge_cdp_prompt3_final"
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
    results = {}
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

            for v in TOP10_VESSELS:
                imo = str(v["imo"])
                meta = await shot_one(ws, imo)
                results[imo] = meta
                print(json.dumps({"imo": imo, "ok": meta.get("ok"), "mode": meta.get("mode"), "fidelity_ok": "NOT VERIFIED" in (meta.get("fidelity") or "")}, ensure_ascii=False))

        contact = build_contact_sheet(TOP10_VESSELS, results)
        report = {"shots": results, "contact_sheet": str(contact)}
        (LOGS / "prompt3_final_shots.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"contact_sheet": str(contact), "n": len(results)}, indent=2))
        return 0 if all(r.get("ok") for r in results.values()) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
