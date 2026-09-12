#!/usr/bin/env python3
"""Headless: load all 10 TOP-10 GLBs via DIGITAL TWIN path; report mesh + WebGL errors."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
PORT = 9271
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
OUT = ROOT / "logs" / "glb_render_pipeline_qa.json"
EDGE = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe"
)
PROFILE = ROOT / "logs" / "edge_cdp_glb_render"
_n = 1


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


def kill_port(port: int) -> None:
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    except Exception:
        return
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts[-1].isdigit():
                subprocess.run(["taskkill", "/PID", parts[-1], "/F"], capture_output=True)


JS = r"""
(async () => {
  const T = window.__TOP10__;
  if (!T?.vessels) return { ok: false, error: 'no __TOP10__' };
  const consoleErr = [];
  const pageErr = [];
  const _e = console.error.bind(console);
  console.error = (...a) => { consoleErr.push(a.map(String).join(' ')); _e(...a); };
  window.addEventListener('error', (ev) => pageErr.push(String(ev.message||ev.error)));
  window.addEventListener('unhandledrejection', (ev) => pageErr.push('rejection:'+String(ev.reason)));

  const rows = [];
  for (const v of T.vessels) {
    const url = v.glb?.url || `/output/assets/3d_models/vessel_${v.imo}.glb`;
    let net = null;
    try {
      const r = await fetch(url, { cache: 'no-store' });
      const buf = await r.arrayBuffer();
      const u8 = new Uint8Array(buf.slice(0, 4));
      const magic = String.fromCharCode(...u8);
      net = {
        status: r.status,
        bytes: buf.byteLength,
        contentType: r.headers.get('content-type'),
        magic,
        looksGlb: magic === 'glTF',
      };
    } catch (e) {
      net = { error: String(e) };
    }

    T.openRefs(v);
    await new Promise((r) => setTimeout(r, 120));
    const modal = document.getElementById('t10-ref-modal');
    const tab = modal.querySelector('.t10-insp-tab[data-tab="glb"]');
    const disabled = tab?.getAttribute('aria-disabled') === 'true' || tab?.classList.contains('is-disabled');
    if (tab && !disabled) tab.click();
    await new Promise((r) => setTimeout(r, 1800));

    const stage = document.getElementById('t10-glb-stage');
    const canvas = stage?.querySelector('canvas.t10-glb-canvas');
    const warn = document.querySelector('#t10-insp-mesh-warn');
    rows.push({
      imo: String(v.imo),
      name: v.name,
      manifest_ready: !!(v.glb && v.glb.ready === true),
      manifest_url: v.glb?.url || null,
      manifest_bytes: v.glb?.bytes ?? null,
      network: net,
      glbTabDisabled: !!disabled,
      mode: modal?.dataset?.viewerMode,
      stageDisplay: stage ? getComputedStyle(stage).display : null,
      hasCanvas: !!canvas,
      canvasClass: canvas?.className || null,
      canvasWH: canvas ? [canvas.width, canvas.height] : null,
      stageReady: !!stage?.classList.contains('is-glb-ready'),
      stageFallback: !!stage?.classList.contains('is-glb-fallback'),
      warnVisible: warn ? warn.hidden === false : false,
      warnText: warn && warn.hidden === false ? (warn.textContent||'').trim().slice(0,160) : null,
      fidelity: document.querySelector('.t10-glb-fidelity')?.textContent || null,
      sharedCardWebgl: !!document.getElementById('t10-shared-webgl'),
    });
    T.closeRefs();
    await new Promise((r) => setTimeout(r, 80));
  }

  return {
    ok: true,
    consoleErrors: consoleErr,
    pageErrors: pageErr,
    vessels: rows,
    preflight: {
      sharedCardWebgl: !!document.getElementById('t10-shared-webgl'),
      cardHint: document.querySelector('.t10-vp-hint')?.textContent || null,
    },
  };
})()
"""


async def main() -> int:
    kill_port(PORT)
    PROFILE.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2.5)
    report: dict = {"url": URL}
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next((t for t in tabs if t.get("type") == "page"), None)
        if not page:
            report["ok"] = False
            report["error"] = "no page"
            OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return 1
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as ws:
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.enable")
            await cdp(ws, "Page.navigate", {"url": URL})
            for _ in range(50):
                ready = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!(window.__TOP10__&&window.__TOP10__.vessels&&window.__TOP10__.vessels.length)",
                        "returnByValue": True,
                    },
                )
                if (ready.get("result") or {}).get("value"):
                    break
                await asyncio.sleep(0.25)
            ev = await cdp(
                ws,
                "Runtime.evaluate",
                {"expression": JS, "awaitPromise": True, "returnByValue": True},
            )
            report["browser"] = (ev.get("result") or {}).get("value")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)

    # Mesh topology via trimesh (server-side)
    try:
        import trimesh

        mesh_rows = []
        for v in (report.get("browser") or {}).get("vessels") or []:
            imo = v["imo"]
            glb = ROOT / "output" / "assets" / "3d_models" / f"vessel_{imo}.glb"
            if not glb.exists():
                mesh_rows.append({"imo": imo, "error": "missing"})
                continue
            m = trimesh.load(str(glb), force="mesh")
            mesh_rows.append(
                {
                    "imo": imo,
                    "vertices": int(len(m.vertices)),
                    "faces": int(len(m.faces)),
                    "extents": [float(x) for x in m.extents],
                    "is_volume": bool(getattr(m, "is_volume", False)),
                    "has_texture": bool(
                        getattr(getattr(m, "visual", None), "material", None)
                        and (
                            getattr(m.visual.material, "baseColorTexture", None) is not None
                            or getattr(m.visual.material, "image", None) is not None
                        )
                    ),
                }
            )
        report["mesh_topology"] = mesh_rows
    except Exception as exc:
        report["mesh_topology_error"] = str(exc)

    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({"wrote": str(OUT), "ok": (report.get("browser") or {}).get("ok")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
