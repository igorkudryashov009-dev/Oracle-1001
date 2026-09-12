#!/usr/bin/env python3
"""Regression: Ortho default + WebGL 0/1/0 for GLB XOR voxel + DT badge intact."""
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
PORT = 9272
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
OUT = ROOT / "logs" / "voxel_cube_regression.json"


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


async def eval_js(ws, expression, await_promise=False):
    r = await cdp(
        ws,
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": await_promise, "returnByValue": True},
    )
    return (r.get("result") or {}).get("value")


async def main() -> int:
    from services.top10_vessels import write_js_manifest
    from services.web_assets_sync import sync_web_assets

    write_js_manifest()
    sync_web_assets(ROOT, force=True, log=False)
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_voxel_reg"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "--no-first-run", URL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2.5)
    report = {"checks": {}}
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next(t for t in tabs if t.get("type") == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 << 20) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": f"{URL}&t={int(time.time())}"})
            for _ in range(50):
                if await eval_js(ws, "!!(window.__TOP10__&&window.__TOP10__.vessels&&window.__TOP10__.vessels.length)"):
                    break
                await asyncio.sleep(0.3)

            webgl = "document.querySelectorAll('canvas.t10-glb-canvas').length"

            await eval_js(ws, "window.__TOP10__?.closeRefs?.(); true")
            await asyncio.sleep(0.3)
            w0 = await eval_js(ws, webgl)
            report["checks"]["webgl_grid"] = {"value": w0, "pass": w0 == 0}

            open_m = await eval_js(
                ws,
                f"""(async()=>{{
                  const v=window.__TOP10__.vessels.find(x=>String(x.imo)==='{IMO}');
                  window.__TOP10__.openRefs(v);
                  await new Promise(r=>setTimeout(r,400));
                  const m=document.getElementById('t10-ref-modal');
                  return {{tab:m.querySelector('.t10-insp-tab.active')?.dataset.tab, mode:m.dataset.viewerMode}};
                }})()""",
                await_promise=True,
            )
            report["checks"]["default_ortho"] = {
                "meta": open_m,
                "pass": (open_m or {}).get("tab") == "all",
            }
            w_o = await eval_js(ws, webgl)
            report["checks"]["webgl_ortho"] = {"value": w_o, "pass": w_o == 0}

            glb = await eval_js(
                ws,
                """(async()=>{
                  const m=document.getElementById('t10-ref-modal');
                  m.querySelector('.t10-insp-tab[data-tab="glb"]').click();
                  await new Promise(r=>setTimeout(r,2600));
                  const fid=document.querySelector('.t10-glb-fidelity')?.textContent||'';
                  return {mode:m.dataset.viewerMode, webgl:document.querySelectorAll('canvas.t10-glb-canvas').length,
                    fidelity:fid, hasNotVerified:fid.includes('NOT VERIFIED STRUCTURAL MODEL')};
                })()""",
                await_promise=True,
            )
            report["checks"]["digital_twin"] = {
                "meta": glb,
                "pass": (glb or {}).get("webgl") == 1 and (glb or {}).get("hasNotVerified") is True,
            }

            vox = await eval_js(
                ws,
                """(async()=>{
                  const m=document.getElementById('t10-ref-modal');
                  m.querySelector('.t10-insp-tab[data-tab="voxel"]').click();
                  await new Promise(r=>setTimeout(r,2600));
                  const fid=document.querySelector('.t10-glb-fidelity')?.textContent||'';
                  return {mode:m.dataset.viewerMode, webgl:document.querySelectorAll('canvas.t10-glb-canvas').length,
                    fidelity:fid, hasVoxelBadge:fid.includes('VOXEL GRID RECONSTRUCTION') && fid.includes('NOT A CONTINUOUS SURFACE MODEL') && fid.includes('COLOR PALETTE')};
                })()""",
                await_promise=True,
            )
            report["checks"]["voxel_tab"] = {
                "meta": vox,
                "pass": (vox or {}).get("webgl") == 1 and (vox or {}).get("hasVoxelBadge") is True,
            }

            oh = await eval_js(
                ws,
                """(async()=>{
                  const m=document.getElementById('t10-ref-modal');
                  m.querySelector('.t10-insp-tab[data-tab="overhead"]').click();
                  await new Promise(r=>setTimeout(r,2800));
                  return {
                    mode:m.dataset.viewerMode,
                    webgl:document.querySelectorAll('canvas.t10-glb-canvas').length,
                    isOrtho: !!(window.__TOP10__ && document.querySelector('#t10-glb-stage canvas')),
                    fidelity: document.querySelector('.t10-glb-fidelity')?.textContent||'',
                    gridHidden: !!m.querySelector('#t10-modal-grid')?.hidden,
                  };
                })()""",
                await_promise=True,
            )
            report["checks"]["overhead_3d"] = {
                "meta": oh,
                "pass": (oh or {}).get("webgl") == 1 and str((oh or {}).get("mode") or "").startswith("overhead"),
            }

            lat = await eval_js(
                ws,
                """(async()=>{
                  const m=document.getElementById('t10-ref-modal');
                  m.querySelector('.t10-insp-tab[data-tab="side"]').click();
                  await new Promise(r=>setTimeout(r,400));
                  const vis=[...m.querySelectorAll('.t10-ref-fig')].filter(f=>f.style.display!=='none').map(f=>f.getAttribute('data-view'));
                  return {mode:m.dataset.viewerMode, webgl:document.querySelectorAll('canvas.t10-glb-canvas').length, vis};
                })()""",
                await_promise=True,
            )
            report["checks"]["lateral_photo"] = {
                "meta": lat,
                "pass": (lat or {}).get("webgl") == 0 and (lat or {}).get("mode") == "ortho:side",
            }

            bow = await eval_js(
                ws,
                """(async()=>{
                  const m=document.getElementById('t10-ref-modal');
                  m.querySelector('.t10-insp-tab[data-tab="bow"]').click();
                  await new Promise(r=>setTimeout(r,400));
                  const vis=[...m.querySelectorAll('.t10-ref-fig')].filter(f=>f.style.display!=='none').map(f=>f.getAttribute('data-view'));
                  return {mode:m.dataset.viewerMode, webgl:document.querySelectorAll('canvas.t10-glb-canvas').length, vis};
                })()""",
                await_promise=True,
            )
            report["checks"]["bow_photo"] = {
                "meta": bow,
                "pass": (bow or {}).get("webgl") == 0 and (bow or {}).get("mode") == "ortho:bow",
            }

            # Switch back to GLB — still single canvas
            glb2 = await eval_js(
                ws,
                """(async()=>{
                  const m=document.getElementById('t10-ref-modal');
                  m.querySelector('.t10-insp-tab[data-tab="glb"]').click();
                  await new Promise(r=>setTimeout(r,2200));
                  return {webgl:document.querySelectorAll('canvas.t10-glb-canvas').length, mode:m.dataset.viewerMode};
                })()""",
                await_promise=True,
            )
            report["checks"]["glb_after_voxel"] = {
                "meta": glb2,
                "pass": (glb2 or {}).get("webgl") == 1,
            }

            await eval_js(ws, "window.__TOP10__.closeRefs(); true")
            await asyncio.sleep(0.5)
            w_close = await eval_js(ws, webgl)
            report["checks"]["webgl_after_close"] = {"value": w_close, "pass": w_close == 0}

        # sizes
        from services.top10_vessels import TOP10_VESSELS

        sizes = []
        for v in TOP10_VESSELS:
            imo = str(v["imo"])
            glb_p = ROOT / "output" / "assets" / "3d_models" / f"vessel_{imo}.glb"
            vox_p = ROOT / "output" / "assets" / "3d_models" / f"vessel_{imo}_voxels.json"
            gb = glb_p.stat().st_size if glb_p.exists() else 0
            vb = vox_p.stat().st_size if vox_p.exists() else 0
            sizes.append({"imo": imo, "glb": gb, "voxel_json": vb, "sum": gb + vb})
        report["sizes"] = {
            "per_vessel": sizes,
            "max_voxel_json": max(s["voxel_json"] for s in sizes),
            "max_sum_glb_plus_voxel": max(s["sum"] for s in sizes),
        }
        report["ok"] = all(c.get("pass") for c in report["checks"].values())
        OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
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
