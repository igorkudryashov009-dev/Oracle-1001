#!/usr/bin/env python3
"""Final pipeline stability + DIGITAL TWIN render audit on :8765."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
PORT = 9274
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
OUT = ROOT / "logs" / "pipeline_stability_qa.json"
EDGE = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe"
)
PROFILE = ROOT / "logs" / "edge_cdp_stability"
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
  const _e = console.error.bind(console);
  console.error = (...a) => { cons.push(a.map(String).join(' ')); _e(...a); };
  window.addEventListener('error', (ev) => pages.push(String(ev.message || ev.error)));
  window.addEventListener('unhandledrejection', (ev) => pages.push('rej:' + String(ev.reason)));

  for (let i = 0; i < 100; i++) {
    if (window.__TOP10__?.vessels?.length) break;
    await wait(200);
  }
  if (!window.__TOP10__?.boot) {
    return {
      ok: false,
      error: 'no __TOP10__',
      href: location.href,
      sheet: document.documentElement.getAttribute('data-sheet'),
      cons: cons.slice(0, 30),
      pages: pages.slice(0, 30),
      scripts: [...document.scripts].map((s) => s.src).filter(Boolean).slice(-15),
    };
  }

  window.__TOP10__.boot({ force: true });
  await wait(1000);

  const cards = document.querySelectorAll('.t10-card').length;
  const twins = document.querySelectorAll('.t10-twin-btn:not([disabled])').length;
  const btn = document.querySelector('.t10-twin-btn:not([disabled])');
  if (!btn) {
    return { ok: false, error: 'no twin btn', cards, twins, cons: cons.slice(0, 20), pages: pages.slice(0, 20) };
  }

  btn.click();
  for (let i = 0; i < 50; i++) {
    const st = document.querySelector('#t10-glb-stage');
    if (st?.classList.contains('is-glb-ready') && st.querySelector('canvas')) break;
    if (st?.classList.contains('is-glb-fallback')) break;
    await wait(200);
  }

  const modal = document.getElementById('t10-ref-modal');
  const stage = document.querySelector('#t10-glb-stage');
  const canvas = stage?.querySelector('canvas');
  const rect = canvas?.getBoundingClientRect?.();
  const nanHits = cons.filter((x) => /NaN|\bnan\b|degenerate|CONTEXT_LOST|WebGL/i.test(x));
  const black =
    !!canvas &&
    (!rect || rect.width < 8 || rect.height < 8 || getComputedStyle(canvas).display === 'none');

  // Scale finiteness probe: walk Three scene if attached on stage
  let scaleProbe = { ok: true, detail: 'n/a' };
  try {
    const threeRoot = stage?.__t10Root || null;
    if (threeRoot?.scale) {
      const s = threeRoot.scale;
      const vals = [s.x, s.y, s.z];
      const finite = vals.every((v) => Number.isFinite(v) && v > 0);
      scaleProbe = { ok: finite, detail: vals };
    }
  } catch (err) {
    scaleProbe = { ok: true, detail: 'probe-skip:' + String(err) };
  }

  const ok =
    !modal?.hidden &&
    stage?.classList.contains('is-glb-ready') &&
    !!canvas &&
    !black &&
    cards === 10 &&
    twins === 10 &&
    nanHits.length === 0 &&
    pages.length === 0 &&
    scaleProbe.ok;

  return {
    ok,
    cards,
    twins,
    blackScreen: black,
    modal: {
      hidden: !!modal?.hidden,
      mode: modal?.dataset?.viewerMode || null,
      glbReady: modal?.dataset?.glbReady || null,
    },
    stage: {
      classes: stage?.className || null,
      hasCanvas: !!canvas,
      canvasClass: canvas?.className || null,
      wh: rect ? [Math.round(rect.width), Math.round(rect.height)] : null,
    },
    glbTabActive: !!modal?.querySelector('.t10-insp-tab[data-tab="glb"].active'),
    scaleProbe,
    consoleErrors: cons.slice(0, 25),
    pageErrors: pages.slice(0, 25),
    nanHits,
  };
})()
"""


async def _browser_probe() -> dict:
    kill_port(PORT)
    if PROFILE.exists():
        # Fresh profile avoids sticky blank tab / service-worker stalls
        import shutil

        shutil.rmtree(PROFILE, ignore_errors=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-popup-blocking",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        tabs = None
        for _ in range(40):
            try:
                tabs = json.loads(
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{PORT}/json/list", timeout=2
                    ).read()
                )
                if tabs:
                    break
            except Exception:
                await asyncio.sleep(0.25)
        page = next((t for t in (tabs or []) if t.get("type") == "page"), None)
        if not page:
            return {"ok": False, "error": "no page"}
        async with websockets.connect(
            page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024, open_timeout=10
        ) as ws:
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.enable")
            await cdp(ws, "Page.navigate", {"url": URL})
            await asyncio.sleep(1.5)
            for _ in range(60):
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
                await asyncio.sleep(0.25)
            diag = await cdp(
                ws,
                "Runtime.evaluate",
                {
                    "expression": "({href:location.href, sheet:document.documentElement.getAttribute('data-sheet'), hasTop10:!!window.__TOP10__, scripts:[...document.scripts].map(s=>s.src).filter(Boolean).slice(-8)})",
                    "returnByValue": True,
                },
            )
            ev = await cdp(
                ws,
                "Runtime.evaluate",
                {"expression": JS, "awaitPromise": True, "returnByValue": True},
            )
            result = (ev.get("result") or {}).get("value") or {"ok": False, "error": "empty"}
            result["diag"] = (diag.get("result") or {}).get("value")
            return result
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        kill_port(PORT)


async def main() -> int:
    try:
        result = await asyncio.wait_for(_browser_probe(), timeout=75)
    except Exception as exc:
        result = {"ok": False, "error": f"browser_timeout_or_fail:{exc}"}

    # Disk aspect ratios for all 10 GLBs
    try:
        import numpy as np
        import trimesh

        rows = []
        for p in sorted((ROOT / "output" / "assets" / "3d_models").glob("vessel_*.glb")):
            m = trimesh.load(str(p), force="mesh")
            e = np.asarray(m.extents, dtype=float)
            meta = json.loads(p.with_suffix(".meta.json").read_text(encoding="utf-8"))
            rows.append(
                {
                    "file": p.name,
                    "bytes": p.stat().st_size,
                    "L_D": round(float(e[0] / max(e[2], 1e-9)), 2),
                    "B_D": round(float(e[1] / max(e[2], 1e-9)), 2),
                    "L_B": round(float(e[0] / max(e[1], 1e-9)), 2),
                    "alg": meta.get("alg"),
                }
            )
        result["glb_aspect"] = rows
        result["glb_aspect_ok"] = all(
            8.0 <= r["L_D"] <= 14.0 and 1.2 <= r["B_D"] <= 2.5 for r in rows
        ) and len(rows) == 10
    except Exception as exc:
        result["glb_aspect_error"] = str(exc)
        result["glb_aspect_ok"] = False

    result["overall_ok"] = bool(result.get("ok")) and bool(result.get("glb_aspect_ok"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        json.dumps(
            {
                "wrote": str(OUT),
                "overall_ok": result.get("overall_ok"),
                "browser_ok": result.get("ok"),
                "glb_aspect_ok": result.get("glb_aspect_ok"),
                "error": result.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.get("overall_ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
