#!/usr/bin/env python3
"""Capture DIGITAL TWIN screenshot for a given IMO (Edge CDP)."""
from __future__ import annotations

import asyncio
import base64
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
EDGE_CANDIDATES = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Microsoft" / "Edge" / "Application" / "msedge.exe",
]
PORT = 9261
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"


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


async def capture(imo: str, out_path: Path, settle_ms: int = 2000) -> dict:
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_dt_shot"
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
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next(
            (t for t in tabs if t.get("type") == "page" and "sentinel" in (t.get("url") or "")),
            tabs[0] if tabs else None,
        )
        if not page:
            return {"ok": False, "error": "no page"}
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            bust = URL + ("&" if "?" in URL else "?") + "nocache=" + str(int(__import__("time").time()))
            await cdp(ws, "Page.navigate", {"url": bust})
            # Wait for sheet module to bind window.__TOP10__
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
                await asyncio.sleep(0.35)
            await asyncio.sleep(0.5)
            js = f"""
            (async () => {{
              const T = window.__TOP10__;
              if (!T) return {{ ok: false, error: 'no __TOP10__' }};
              const v = T.vessels.find(x => String(x.imo) === '{imo}');
              if (!v) return {{ ok: false, error: 'imo not found' }};
              T.openRefs(v);
              await new Promise(r => setTimeout(r, 300));
              const modal = document.getElementById('t10-ref-modal');
              if (!modal) return {{ ok: false, error: 'no modal' }};
              const tab = modal.querySelector('.t10-insp-tab[data-tab="glb"]');
              if (!tab) return {{ ok: false, error: 'no glb tab' }};
              tab.click();
              await new Promise(r => setTimeout(r, {settle_ms}));
              // Extra settle for GLTF load
              for (let i = 0; i < 20; i++) {{
                const c = document.querySelector('#t10-glb-stage canvas');
                if (c && c.width > 10) break;
                await new Promise(r => setTimeout(r, 200));
              }}
              const stage = document.getElementById('t10-glb-stage');
              if (stage) stage.scrollIntoView({{ block: 'center', inline: 'nearest' }});
              await new Promise(r => setTimeout(r, 400));
              // Ensure default textured view — no Spec Plate / Wireframe toggles
              const measureBtn = document.querySelector('.t10-glb-btn[data-act="measure"]');
              const wireBtn = document.querySelector('.t10-glb-btn[data-act="wire"]');
              const orbitBtn = document.querySelector('.t10-glb-btn[data-act="orbit"]');
              const c = document.querySelector('#t10-glb-stage canvas');
              return {{
                ok: true,
                mode: modal.dataset.viewerMode,
                tab: modal.querySelector('.t10-insp-tab.active')?.getAttribute('data-tab'),
                hasCanvas: !!c,
                cw: c?.width, ch: c?.height,
                measureOn: measureBtn?.classList?.contains('is-on') || false,
                wireOn: wireBtn?.classList?.contains('is-on') || false,
                orbitOn: orbitBtn?.classList?.contains('is-on') || false,
                debug: window.__T10_GLB_DEBUG__ || null,
                fidelity: document.querySelector('.t10-glb-fidelity')?.textContent || null,
                warn: document.querySelector('#t10-insp-mesh-warn')?.hidden === false
                  ? document.querySelector('#t10-insp-mesh-warn').textContent : null
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
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(base64.b64decode(shot.get("data") or ""))
            meta["screenshot"] = str(out_path)
            meta["bytes"] = out_path.stat().st_size
            return meta
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)


def main() -> int:
    imo = sys.argv[1] if len(sys.argv) > 1 else "9388833"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "logs" / "digital_twin_current_state.png"
    meta = asyncio.run(capture(imo, out))
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0 if meta.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
