#!/usr/bin/env python3
"""Headless: verify DIGITAL TWIN card button + modal tab visibility on :8765."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
PORT = 9272
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
OUT = ROOT / "logs" / "digital_twin_ui_qa.json"
EDGE = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe"
)
PROFILE = ROOT / "logs" / "edge_cdp_digital_twin_ui"
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
  for (let i = 0; i < 40; i++) {
    if (window.__TOP10__?.vessels?.length) break;
    await wait(250);
  }
  const T = window.__TOP10__;
  if (!T?.vessels) return { ok: false, error: 'no __TOP10__' };

  const cards = [...document.querySelectorAll('.t10-card')].map((el) => {
    const twin = el.querySelector('.t10-twin-btn, [data-open-glb]');
    const cs = twin ? getComputedStyle(twin) : null;
    const r = twin?.getBoundingClientRect();
    return {
      imo: el.dataset.imo,
      hasTwinBtn: !!twin,
      twinText: twin?.innerText?.trim() || null,
      display: cs?.display || null,
      visibility: cs?.visibility || null,
      opacity: cs?.opacity || null,
      disabled: twin ? !!(twin.disabled || twin.getAttribute('aria-disabled') === 'true') : null,
      w: r ? Math.round(r.width) : 0,
      h: r ? Math.round(r.height) : 0,
    };
  });

  const firstTwin = document.querySelector('.t10-twin-btn:not([disabled])');
  if (!firstTwin) return { ok: false, error: 'no enabled twin button', cards };

  firstTwin.click();
  await wait(2800);
  // Prefer ready stage; if still loading, allow another beat
  for (let i = 0; i < 20; i++) {
    const st = document.querySelector('#t10-glb-stage');
    if (st?.classList.contains('is-glb-ready') && st.querySelector('canvas')) break;
    await wait(200);
  }

  const modal = document.getElementById('t10-ref-modal');
  const glbTab = modal?.querySelector('.t10-insp-tab[data-tab="glb"]');
  const stage = modal?.querySelector('#t10-glb-stage');
  const canvas = stage?.querySelector('canvas');
  const gcs = glbTab ? getComputedStyle(glbTab) : null;
  const gr = glbTab?.getBoundingClientRect();

  return {
    ok: cards.every((c) => c.hasTwinBtn && c.display !== 'none' && c.w > 0 && c.h > 0 && !c.disabled)
      && !!modal && !modal.hidden
      && glbTab?.classList.contains('active')
      && stage?.style.display !== 'none'
      && !!canvas,
    cards,
    modal: {
      hidden: modal?.hidden ?? true,
      viewerMode: modal?.dataset?.viewerMode || null,
      glbReady: modal?.dataset?.glbReady || null,
    },
    glbTab: glbTab ? {
      text: glbTab.innerText.trim(),
      active: glbTab.classList.contains('active'),
      disabled: glbTab.getAttribute('aria-disabled') === 'true',
      display: gcs.display,
      visibility: gcs.visibility,
      opacity: gcs.opacity,
      w: Math.round(gr.width),
      h: Math.round(gr.height),
    } : null,
    stage: {
      display: stage?.style?.display || null,
      classes: stage?.className || null,
      hasCanvas: !!canvas,
      canvasClass: canvas?.className || null,
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
    result: dict = {"ok": False}
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next((t for t in tabs if t.get("type") == "page"), None)
        if not page:
            result = {"ok": False, "error": "no page"}
        else:
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
                # Force re-render so live HUD picks up synced top10_sheet.js
                await cdp(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "window.__TOP10__?.boot?.({force:true}); true",
                        "returnByValue": True,
                    },
                )
                await asyncio.sleep(0.6)
                ev = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {"expression": JS, "awaitPromise": True, "returnByValue": True},
                )
                result = (ev.get("result") or {}).get("value") or {"ok": False, "error": "empty"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        kill_port(PORT)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"wrote": str(OUT), "ok": bool(result.get("ok"))}, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
