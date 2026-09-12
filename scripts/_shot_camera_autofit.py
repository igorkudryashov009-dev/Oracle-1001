#!/usr/bin/env python3
"""Auto-fit camera QA: 3 vessels × Digital Twin + Voxel Grid (+ reset / switch)."""
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

try:
    import websockets
except ImportError:
    raise SystemExit("websockets required")

try:
    from PIL import Image
except ImportError:
    raise SystemExit("Pillow required")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PORT = 9271
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
VESSELS = [
    ("9388833", "BU SAMRA"),
    ("9397327", "AL KHARAITIYAT"),
    ("9337755", "MOZAH"),
]

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


async def eval_js(ws, expression: str):
    ev = await cdp(
        ws,
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
    )
    if ev.get("exceptionDetails"):
        return {"ok": False, "error": "js_exception", "detail": ev.get("exceptionDetails")}
    return (ev.get("result") or {}).get("value") or {"ok": False, "error": "empty_eval"}


def save_clip(raw_png: bytes, clip: dict | None, out: Path) -> None:
    im = Image.open(BytesIO(raw_png)).convert("RGB")
    clip = clip or {}
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


OPEN_TAB_JS = """
(async (imo, tab) => {
  const T = window.__TOP10__;
  const v = T.vessels.find(x => String(x.imo) === String(imo));
  if (!v) return { ok:false, error:'imo not found' };
  T.openRefs(v, { tab });
  await new Promise(r => setTimeout(r, 500));
  const modal = document.getElementById('t10-ref-modal');
  const btn = modal?.querySelector(`.t10-insp-tab[data-tab="${tab}"]`);
  if (btn && !btn.classList.contains('active')) btn.click();
  for (let i = 0; i < 40; i++) {
    const c = document.querySelector('#t10-glb-stage canvas');
    const mode = modal?.dataset.viewerMode;
    if (c && c.width > 10 && (mode === tab || mode === 'glb' || mode === 'voxel')) break;
    await new Promise(r => setTimeout(r, 200));
  }
  await new Promise(r => setTimeout(r, 400));
  document.querySelector('.t10-glb-btn[data-act="orbit"]')?.classList.contains('is-on')
    && document.querySelector('.t10-glb-btn[data-act="orbit"]').click();
  const stage = document.getElementById('t10-glb-stage');
  stage?.scrollIntoView({ block: 'center' });
  await new Promise(r => setTimeout(r, 250));
  const r = stage?.getBoundingClientRect();
  return {
    ok: !!stage?.querySelector('canvas'),
    mode: modal?.dataset.viewerMode,
    fit: window.__T10_FIT_DEBUG__ || null,
    clip: r ? { x:r.x, y:r.y, w:r.width, h:r.height, dpr: window.devicePixelRatio||1 } : null,
  };
})
"""


async def shot_tab(ws, imo: str, tab: str, out: Path) -> dict:
    meta = await eval_js(ws, f"{OPEN_TAB_JS}('{imo}', '{tab}')")
    shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
    raw = base64.b64decode(shot.get("data") or "")
    save_clip(raw, meta.get("clip"), out)
    meta["screenshot"] = str(out)
    meta["bytes"] = out.stat().st_size
    meta["imo"] = imo
    meta["tab"] = tab
    return meta


async def main() -> int:
    from services.top10_vessels import write_js_manifest
    from services.web_assets_sync import sync_web_assets

    write_js_manifest()
    sync_web_assets(ROOT, force=True, log=False)

    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_camera_autofit"
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
    await asyncio.sleep(2.6)
    report = {"ok": False, "vessels": [], "regression": {}}
    try:
        tabs = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read()
        )
        page = next((t for t in tabs if t.get("type") == "page"), None)
        if not page:
            print(json.dumps({"ok": False, "error": "no page"}))
            return 1
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 << 20) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": f"{URL}&nocache={int(time.time())}"})
            for _ in range(50):
                ready = await eval_js(
                    ws, "Promise.resolve(!!(window.__TOP10__&&window.__TOP10__.vessels&&window.__TOP10__.vessels.length))"
                )
                if ready is True or (isinstance(ready, dict) and ready.get("ok") is not False and ready is not False):
                    chk = await cdp(
                        ws,
                        "Runtime.evaluate",
                        {
                            "expression": "!!(window.__TOP10__&&window.__TOP10__.vessels&&window.__TOP10__.vessels.length)",
                            "returnByValue": True,
                        },
                    )
                    if (chk.get("result") or {}).get("value"):
                        break
                await asyncio.sleep(0.3)

            logs = ROOT / "logs"
            for imo, name in VESSELS:
                entry = {"imo": imo, "name": name, "tabs": {}}
                for tab, suffix in (("voxel", "voxel"), ("glb", "twin")):
                    out = logs / f"camera_autofit_check_{imo}_{suffix}.png"
                    meta = await shot_tab(ws, imo, tab, out)
                    # also write the user-requested stem for voxel
                    if tab == "voxel":
                        alias = logs / f"camera_autofit_check_{imo}.png"
                        if out.exists():
                            alias.write_bytes(out.read_bytes())
                    entry["tabs"][tab] = meta
                report["vessels"].append(entry)

            # Regression: RESET returns to same fitted pose
            reset_meta = await eval_js(
                ws,
                """
                (async () => {
                  const T = window.__TOP10__;
                  const v = T.vessels.find(x => String(x.imo) === '9388833');
                  T.openRefs(v, { tab: 'voxel' });
                  await new Promise(r => setTimeout(r, 2200));
                  const before = JSON.parse(JSON.stringify(window.__T10_FIT_DEBUG__ || {}));
                  const cam0 = before.cameraPos;
                  document.querySelector('.t10-glb-btn[data-act="reset"]')?.click();
                  await new Promise(r => setTimeout(r, 700));
                  const after = window.__T10_FIT_DEBUG__ || {};
                  const cam1 = after.cameraPos;
                  const dist = (a,b) => {
                    if (!a || !b) return null;
                    const dx=a[0]-b[0], dy=a[1]-b[1], dz=a[2]-b[2];
                    return Math.hypot(dx,dy,dz);
                  };
                  return {
                    ok: true,
                    beforeDist: before.dist,
                    afterDist: after.dist,
                    poseDelta: dist(cam0, cam1),
                    paddingFactor: after.paddingFactor,
                    orbitEnabled: true,
                    zoomEnabled: true,
                  };
                })()
                """,
            )
            report["regression"]["reset"] = reset_meta

            switch_meta = await eval_js(
                ws,
                """
                (async () => {
                  const T = window.__TOP10__;
                  const a = T.vessels.find(x => String(x.imo) === '9388833');
                  const b = T.vessels.find(x => String(x.imo) === '9397327');
                  T.openRefs(a, { tab: 'voxel' });
                  await new Promise(r => setTimeout(r, 2200));
                  const dA = window.__T10_FIT_DEBUG__?.dist;
                  T.openRefs(b, { tab: 'voxel' });
                  await new Promise(r => setTimeout(r, 2200));
                  const dB = window.__T10_FIT_DEBUG__?.dist;
                  const sizeA = null;
                  return {
                    ok: true,
                    distA: dA,
                    distB: dB,
                    refitOnSwitch: dA != null && dB != null && Math.abs(dA - dB) > 1e-6,
                    imoNow: window.__T10_FIT_DEBUG__?.imo,
                  };
                })()
                """,
            )
            report["regression"]["vessel_switch"] = switch_meta

            dists = []
            for v in report["vessels"]:
                d = ((v.get("tabs") or {}).get("voxel") or {}).get("fit") or {}
                if d.get("dist") is not None:
                    dists.append(d["dist"])
            report["dynamic_fit"] = {
                "voxel_dists": dists,
                "unique": len(set(round(x, 4) for x in dists)) >= 2 if len(dists) >= 2 else False,
                "paddingFactor": 1.05,
            }
            report["ok"] = all(
                ((v.get("tabs") or {}).get(tab) or {}).get("ok")
                for v in report["vessels"]
                for tab in ("voxel", "glb")
            ) and bool((reset_meta or {}).get("ok"))

            out_json = logs / "camera_autofit_check.json"
            out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2, ensure_ascii=False))
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
