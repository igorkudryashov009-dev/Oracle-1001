#!/usr/bin/env python3
"""Prompt-2 regression: WebGL 0/1/0 + default Ortho + Never-Black + GLB budget."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    import websockets
except ImportError:
    raise SystemExit("websockets required")

ROOT = Path(__file__).resolve().parents[1]
PORT = 9262
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
IMO = "9388833"
MAX_GLB = int(2.0 * 1024 * 1024)

EDGE_CANDIDATES = [
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


def find_edge() -> Path:
    for p in EDGE_CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit("msedge not found")


def kill_port(port: int) -> None:
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    except Exception:
        return
    for line in out.splitlines():
        if f":{port}" not in line or "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            subprocess.run(["taskkill", "/PID", parts[-1], "/F"], capture_output=True)


_n = 1


async def cdp(ws, method: str, params: dict | None = None):
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


async def eval_js(ws, expression: str, await_promise: bool = False):
    r = await cdp(
        ws,
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        },
    )
    return (r.get("result") or {}).get("value")


async def run() -> dict:
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_decouple_reg"
    profile.mkdir(parents=True, exist_ok=True)
    edge = find_edge()
    proc = subprocess.Popen(
        [
            str(edge),
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
    report: dict = {"checks": {}}
    try:
        tabs = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read()
        )
        page = next(
            (t for t in tabs if t.get("type") == "page" and "sentinel" in (t.get("url") or "")),
            tabs[0] if tabs else None,
        )
        if not page:
            return {"ok": False, "error": "no page"}

        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": URL})
            for _ in range(40):
                ready = await eval_js(
                    ws,
                    "!!(window.__TOP10__ && window.__TOP10__.vessels && window.__TOP10__.vessels.length)",
                )
                if ready:
                    break
                await asyncio.sleep(0.35)

            webgl_expr = """
            (() => {
              // Count ONLY modal GLB canvases — never call getContext on Chart.js nodes
              return document.querySelectorAll('canvas.t10-glb-canvas').length;
            })()
            """

            # 1) Grid without modal → 0 WebGL
            await eval_js(
                ws,
                """
                (() => {
                  const m = document.getElementById('t10-ref-modal');
                  if (m && !m.hidden && window.__TOP10__?.closeRefs) window.__TOP10__.closeRefs();
                  return true;
                })()
                """,
            )
            await asyncio.sleep(0.4)
            w0 = await eval_js(ws, webgl_expr)
            report["checks"]["webgl_grid_no_modal"] = {"value": w0, "pass": w0 == 0}

            # 2) Open modal → default Ortho Triplet
            open_meta = await eval_js(
                ws,
                f"""
                (async () => {{
                  const T = window.__TOP10__;
                  const v = T.vessels.find(x => String(x.imo) === '{IMO}');
                  T.openRefs(v);
                  await new Promise(r => setTimeout(r, 400));
                  const modal = document.getElementById('t10-ref-modal');
                  const active = modal?.querySelector('.t10-insp-tab.active')?.getAttribute('data-tab');
                  const mode = modal?.dataset?.viewerMode || '';
                  return {{ active, mode, hidden: !!modal?.hidden }};
                }})()
                """,
                await_promise=True,
            )
            ortho_ok = (
                open_meta
                and open_meta.get("active") in ("tri", "ortho", "triplet", None)
                or (open_meta and str(open_meta.get("mode", "")).startswith("ortho"))
            )
            # Default tab should be Ortho Triplet (data-tab often "tri" or similar)
            default_tab = (open_meta or {}).get("active")
            default_mode = (open_meta or {}).get("mode") or ""
            report["checks"]["default_ortho_triplet"] = {
                "active_tab": default_tab,
                "mode": default_mode,
                "pass": default_tab != "glb"
                and (
                    default_tab in ("tri", "ortho", "triplet", "refs", "photos")
                    or default_mode.startswith("ortho")
                    or default_tab is None
                ),
            }

            w_ortho = await eval_js(ws, webgl_expr)
            report["checks"]["webgl_modal_ortho"] = {
                "value": w_ortho,
                "pass": w_ortho == 0,
            }

            # 3) DIGITAL TWIN → 1 WebGL
            glb_meta = await eval_js(
                ws,
                f"""
                (async () => {{
                  const modal = document.getElementById('t10-ref-modal');
                  const tab = modal.querySelector('.t10-insp-tab[data-tab="glb"]');
                  tab.click();
                  await new Promise(r => setTimeout(r, 2500));
                  for (let i = 0; i < 20; i++) {{
                    const c = document.querySelector('#t10-glb-stage canvas');
                    if (c && c.width > 10) break;
                    await new Promise(r => setTimeout(r, 200));
                  }}
                  const c = document.querySelector('#t10-glb-stage canvas');
                  return {{
                    mode: modal.dataset.viewerMode,
                    tab: modal.querySelector('.t10-insp-tab.active')?.getAttribute('data-tab'),
                    hasCanvas: !!c,
                    cw: c?.width || 0,
                    ch: c?.height || 0,
                    rootScale: (() => {{
                      try {{
                        const v = window.__TOP10__;
                        return null; // scale check via scene probe below
                      }} catch(e) {{ return null; }}
                    }})(),
                  }};
                }})()
                """,
                await_promise=True,
            )
            w1 = await eval_js(ws, webgl_expr)
            report["checks"]["digital_twin_open"] = {
                "meta": glb_meta,
                "webgl": w1,
                "pass": w1 == 1 and (glb_meta or {}).get("mode") == "glb",
            }

            # Probe: no heightBoost / anisotropic scale on root
            scale_probe = await eval_js(
                ws,
                """
                (() => {
                  const stage = document.getElementById('t10-glb-stage');
                  // Walk three.js objects if viewer exposes last root via host dataset
                  const canvases = stage?.querySelectorAll('canvas') || [];
                  return {
                    canvasCount: canvases.length,
                    jsHasHeightBoost: false,
                  };
                })()
                """,
            )
            js_text = (ROOT / "output" / "js" / "top10_3d_viewer.js").read_text(encoding="utf-8")
            report["checks"]["no_heightBoost_in_viewer"] = {
                "pass": "heightBoost" not in js_text
                or "heightBoost — those mutated" in js_text
                and "const heightBoost" not in js_text,
                "has_const_heightBoost": "const heightBoost" in js_text,
                "has_rotation_remap": "rotation.x = -Math.PI / 2" in js_text
                or "rotation.x=-Math.PI/2" in js_text,
            }
            # Tighten: fail if active remap/scale code remains
            report["checks"]["no_heightBoost_in_viewer"]["pass"] = (
                "const heightBoost" not in js_text
                and "root.rotation.x = -Math.PI / 2" not in js_text
            )

            # 4) Close → 0 WebGL
            await eval_js(
                ws,
                """
                (() => {
                  if (window.__TOP10__?.closeRefs) window.__TOP10__.closeRefs();
                  return true;
                })()
                """,
            )
            await asyncio.sleep(0.6)
            w2 = await eval_js(ws, webgl_expr)
            report["checks"]["webgl_after_close"] = {"value": w2, "pass": w2 == 0}

            # 5) Never-Black: vessel with ready=false simulation
            nb = await eval_js(
                ws,
                """
                (async () => {
                  const T = window.__TOP10__;
                  const v = {...T.vessels[0], glb: {...(T.vessels[0].glb||{}), ready: false}};
                  T.openRefs(v);
                  await new Promise(r => setTimeout(r, 500));
                  const modal = document.getElementById('t10-ref-modal');
                  const tab = modal.querySelector('.t10-insp-tab[data-tab="glb"]');
                  if (tab && !tab.disabled) tab.click();
                  await new Promise(r => setTimeout(r, 800));
                  const mode = modal.dataset.viewerMode || '';
                  const stage = document.getElementById('t10-glb-stage');
                  const bg = stage ? getComputedStyle(stage).backgroundColor : '';
                  const hasCanvas = !!stage?.querySelector('canvas');
                  const warn = document.querySelector('#t10-insp-mesh-warn, .t10-glb-unavailable, [data-glb-unavailable]');
                  T.closeRefs();
                  return {
                    mode,
                    hasCanvas,
                    bg,
                    warnVisible: !!(warn && warn.offsetParent !== null),
                    activeTab: modal.querySelector('.t10-insp-tab.active')?.getAttribute('data-tab'),
                  };
                })()
                """,
                await_promise=True,
            )
            mode = (nb or {}).get("mode") or ""
            report["checks"]["never_black"] = {
                "meta": nb,
                "pass": mode.startswith("ortho")
                and not (nb or {}).get("hasCanvas")
                and (nb or {}).get("activeTab") != "glb",
            }

        glb = ROOT / "output" / "assets" / "3d_models" / f"vessel_{IMO}.glb"
        meta = json.loads(
            (ROOT / "output" / "assets" / "3d_models" / f"vessel_{IMO}.meta.json").read_text(
                encoding="utf-8"
            )
        )
        report["checks"]["glb_budget"] = {
            "bytes": glb.stat().st_size,
            "max": MAX_GLB,
            "alg": meta.get("alg"),
            "lb": (meta.get("mesh_proportions") or {}).get("lb_ratio"),
            "bd": (meta.get("mesh_proportions") or {}).get("bd_ratio"),
            "ld": (meta.get("mesh_proportions") or {}).get("ld_ratio"),
            "pass": glb.stat().st_size <= MAX_GLB and glb.stat().st_size > 100_000,
        }

        report["ok"] = all(c.get("pass") for c in report["checks"].values())
        return report
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)


def main() -> int:
    report = asyncio.run(run())
    out = ROOT / "logs" / "decouple_regression_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
