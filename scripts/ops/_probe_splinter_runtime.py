#!/usr/bin/env python3
"""Probe DIGITAL TWIN runtime: JS version, wireframe, world ratios, screenshot."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
PORT = 9263
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
OUT = ROOT / "logs" / "digital_twin_splinter_probe.png"
EDGE = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / (
    "Microsoft/Edge/Application/msedge.exe"
)

PROBE_JS = r"""
(async () => {
  const T = window.__TOP10__;
  if (!T?.vessels) return { ok: false, error: 'no __TOP10__' };
  const v = T.vessels.find((x) => String(x.imo) === '9388833');
  if (!v) return { ok: false, error: 'imo missing' };
  T.openRefs(v);
  await new Promise((r) => setTimeout(r, 400));
  const tab = document.querySelector('.t10-insp-tab[data-tab="glb"]');
  if (!tab) return { ok: false, error: 'no glb tab' };
  tab.click();
  await new Promise((r) => setTimeout(r, 4500));

  const jsText = await fetch('/output/js/top10_3d_viewer.js', { cache: 'no-store' }).then((r) => r.text());
  const wireBtn = document.querySelector('[data-act="wire"]');
  const inst = typeof T.getGlbViewer === 'function' ? T.getGlbViewer() : null;
  const root = inst && inst._root ? inst._root : null;

  let pres = null, scale = null, rot = null;
  if (root) {
    pres = root.userData?.presWorld || null;
    scale = { x: root.scale.x, y: root.scale.y, z: root.scale.z };
    rot = { x: root.rotation.x, y: root.rotation.y, z: root.rotation.z };
  }

  if (inst?.controls) {
    inst.controls.autoRotate = false;
    if (typeof inst.resetCamera === 'function') inst.resetCamera();
    await new Promise((r) => setTimeout(r, 700));
  }

  return {
    ok: true,
    wireOn: !!(wireBtn && wireBtn.classList.contains('is-on')),
    mode: document.getElementById('t10-ref-modal')?.dataset?.viewerMode || null,
    hasCanvas: !!document.querySelector('#t10-glb-stage canvas'),
    hasInst: !!inst,
    viewerOk: inst ? !!inst.ok : null,
    fallback: !!(inst && inst.host && inst.host.classList.contains('is-glb-fallback')),
    js: {
      hasGuard: jsText.includes('splinter-world-lb'),
      hasBoost: jsText.includes('PRES_HEIGHT_BOOST'),
      has235: jsText.includes('2.35'),
      hasRemapFirst: jsText.includes('Remap FIRST'),
    },
    pres,
    scale,
    rot,
  };
})()
"""


def kill_port(port: int) -> None:
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    except Exception:
        return
    for line in out.splitlines():
        if f":{port}" not in line or "LISTENING" not in line.upper():
            continue
        pid = line.split()[-1]
        if pid.isdigit():
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)


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


async def main() -> int:
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_splinter_probe"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={str(profile)}",
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
        page = next(
            (t for t in tabs if t.get("type") == "page" and "sentinel" in (t.get("url") or "")),
            tabs[0] if tabs else None,
        )
        if not page:
            print(json.dumps({"ok": False, "error": "no page"}))
            return 1
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": URL})
            for _ in range(40):
                ready = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!(window.__TOP10__ && window.__TOP10__.vessels && window.__TOP10__.vessels.length)",
                        "returnByValue": True,
                    },
                )
                if (ready.get("result") or {}).get("value"):
                    break
                await asyncio.sleep(0.3)
            ev = await cdp(
                ws,
                "Runtime.evaluate",
                {"expression": PROBE_JS, "awaitPromise": True, "returnByValue": True},
            )
            meta = (ev.get("result") or {}).get("value") or {}
            shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
            OUT.write_bytes(base64.b64decode(shot.get("data") or ""))
            meta["screenshot"] = str(OUT)
            meta["bytes"] = OUT.stat().st_size
            print(json.dumps(meta, ensure_ascii=False, indent=2))
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
