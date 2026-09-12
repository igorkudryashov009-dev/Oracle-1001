#!/usr/bin/env python3
"""Emergency DIGITAL TWIN GLB resolution + canvas probe."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
PORT = 9288
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
OUT = ROOT / "logs" / "glb_asset_resolution_qa.json"
EDGE = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe"
)
PROFILE = ROOT / "logs" / "edge_cdp_glb_asset"
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
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const cons = [];
  const pages = [];
  const glbNet = [];
  const _fetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const res = await _fetch(...args);
    const u = String(args[0] || '');
    if (u.includes('.glb') || u.includes('3d_models')) {
      glbNet.push({
        via: 'fetch',
        url: u,
        status: res.status,
        ct: res.headers.get('content-type'),
        ok: res.ok,
      });
    }
    return res;
  };
  const XO = XMLHttpRequest.prototype.open;
  const XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this.__u = String(url || '');
    return XO.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(...args) {
    this.addEventListener('loadend', () => {
      const u = this.__u || '';
      if (u.includes('.glb') || u.includes('3d_models')) {
        glbNet.push({
          via: 'xhr',
          url: u,
          status: this.status,
          ct: this.getResponseHeader('content-type'),
          ok: this.status >= 200 && this.status < 300,
        });
      }
    });
    return XS.apply(this, args);
  };
  console.error = (...a) => { cons.push(a.map(String).join(' ')); };
  window.addEventListener('error', (ev) => pages.push(String(ev.message || ev.error)));

  for (let i = 0; i < 60; i++) {
    if (window.__TOP10__?.vessels?.length) break;
    await wait(200);
  }
  if (!window.__TOP10__) return { ok: false, error: 'no __TOP10__', cons, pages };

  const v = window.__TOP10__.vessels[0];
  const url = v?.glb?.url || null;
  let head = null;
  try {
    const r = await fetch(url, { method: 'GET', cache: 'no-store' });
    const buf = await r.arrayBuffer();
    const magic = String.fromCharCode(...new Uint8Array(buf.slice(0, 4)));
    head = { status: r.status, ct: r.headers.get('content-type'), bytes: buf.byteLength, magic, url };
  } catch (e) {
    head = { error: String(e), url };
  }

  window.__TOP10__.boot({ force: true });
  await wait(600);
  document.querySelector('.t10-twin-btn:not([disabled])')?.click();
  for (let i = 0; i < 40; i++) {
    const st = document.querySelector('#t10-glb-stage');
    if (st?.classList.contains('is-glb-ready') || st?.classList.contains('is-glb-fallback')) break;
    await wait(200);
  }

  // Force one render before pixel probe (preserveDrawingBuffer path)
  try {
    const canv = document.querySelector('#t10-glb-stage canvas');
    // nudge rAF
    await wait(100);
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  } catch (_) {}

  const stage = document.querySelector('#t10-glb-stage');
  const canvas = stage?.querySelector('canvas');
  let pixels = null;
  if (canvas) {
    try {
      await new Promise((r) => requestAnimationFrame(r));
      const c2 = document.createElement('canvas');
      c2.width = Math.min(64, canvas.width || 64);
      c2.height = Math.min(64, canvas.height || 64);
      const ctx = c2.getContext('2d');
      ctx.drawImage(canvas, 0, 0, c2.width, c2.height);
      const data = ctx.getImageData(0, 0, c2.width, c2.height).data;
      let nonBlack = 0, sum = 0;
      for (let i = 0; i < data.length; i += 4) {
        const l = data[i] + data[i+1] + data[i+2];
        sum += l;
        if (l > 12) nonBlack += 1;
      }
      const n = data.length / 4;
      pixels = { nonBlackRatio: nonBlack / n, mean: sum / (n * 3), w: c2.width, h: c2.height, canvasW: canvas.width, canvasH: canvas.height };
    } catch (e) {
      pixels = { error: String(e) };
    }
  }

  const cs = stage ? getComputedStyle(stage) : null;
  return {
    ok: !!stage?.classList.contains('is-glb-ready') && !!canvas && (pixels?.nonBlackRatio || 0) > 0.01,
    vessel: { imo: v.imo, glb: v.glb },
    head,
    glbNet,
    stage: {
      className: stage?.className,
      display: cs?.display,
      visibility: cs?.visibility,
      hasCanvas: !!canvas,
      warn: document.querySelector('#t10-insp-mesh-warn')?.textContent || null,
      mode: document.getElementById('t10-ref-modal')?.dataset?.viewerMode,
    },
    pixels,
    cons: cons.slice(0, 30),
    pages: pages.slice(0, 20),
  };
})()
"""


async def main() -> int:
    import shutil

    kill_port(PORT)
    shutil.rmtree(PROFILE, ignore_errors=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result: dict = {"ok": False}
    try:
        await asyncio.sleep(2.5)
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next(t for t in tabs if t.get("type") == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as ws:
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.enable")
            await cdp(ws, "Network.setCacheDisabled", {"cacheDisabled": True})
            await cdp(ws, "Page.navigate", {"url": URL + "&_nocache=1"})
            for _ in range(50):
                ready = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!(window.__TOP10__&&window.__TOP10__.vessels)",
                        "returnByValue": True,
                    },
                )
                if (ready.get("result") or {}).get("value"):
                    break
                await asyncio.sleep(0.2)
            ev = await cdp(
                ws,
                "Runtime.evaluate",
                {"expression": JS, "awaitPromise": True, "returnByValue": True},
            )
            result = (ev.get("result") or {}).get("value") or {"ok": False, "error": "empty"}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except Exception:
            proc.kill()
        kill_port(PORT)

    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"wrote": str(OUT), "ok": result.get("ok"), "error": result.get("error")}, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
