#!/usr/bin/env python3
"""DIAG-ONLY: what '3D broken' means — symptom + console + network + files. No product fixes."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import websockets
except ImportError:
    print("FAIL: websockets required")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logs" / "glb_diag_3d_symptom.json"
SHOT_DIR = ROOT / "logs" / "glb_diag_shots"
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
PORT = 9255
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
PROFILE = ROOT / "logs" / "edge_cdp_glb_diag"


def find_edge() -> Path:
    for p in EDGE_CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit("FAIL: msedge.exe not found")


def kill_port(port: int) -> None:
    if sys.platform != "win32":
        return
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


async def cdp_eval(ws, expression: str, await_promise: bool = True):
    return await cdp(
        ws,
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        },
    )


JS_DIAG = r"""
(async () => {
  const T = window.__TOP10__;
  if (!T || !T.vessels) return { fatal: "window.__TOP10__ missing" };

  const consoleBuf = [];
  const _err = console.error.bind(console);
  const _warn = console.warn.bind(console);
  console.error = (...a) => { consoleBuf.push({level:'error', text: a.map(String).join(' ')}); _err(...a); };
  console.warn = (...a) => { consoleBuf.push({level:'warn', text: a.map(String).join(' ').slice(0,300)}); _warn(...a); };
  window.__diagPageErrors = window.__diagPageErrors || [];
  if (!window.__diagHooked) {
    window.addEventListener('error', (e) => {
      window.__diagPageErrors.push({type:'error', msg: e.message, stack: e.error && e.error.stack});
    });
    window.addEventListener('unhandledrejection', (e) => {
      window.__diagPageErrors.push({type:'rejection', msg: String(e.reason)});
    });
    window.__diagHooked = true;
  }

  const vessels = T.vessels.slice(0, 5);
  const rows = [];

  function classify(row) {
    if (row.glbTabDisabled) return 'a_tab_disabled';
    if (row.warnVisible && /unavailable|fail|error|missing/i.test(row.warnText || '')) return 'c_ui_error';
    if (!row.hasCanvas && row.mode === 'glb') return 'b_empty_no_canvas';
    if (row.hasCanvas && row.mode === 'glb') {
      if (row.canvasSample && row.canvasSample.nearBlack) return 'b_black_viewport';
      if (row.canvasSample && row.canvasSample.nearWhite) return 'b_white_viewport';
      if (row.canvasSample && row.canvasSample.hasPixels) return 'd_or_ok_something_rendered';
      return 'b_canvas_no_pixels';
    }
    return 'unknown';
  }

  async function sampleCanvas() {
    const c = document.querySelector('#t10-glb-stage canvas');
    if (!c) return null;
    try {
      const gl = c.getContext('webgl2') || c.getContext('webgl');
      // Cannot read WebGL pixels easily without preserveDrawingBuffer; use 2d copy via draw?
      // Fallback: size + visibility only + toDataURL may be blank for WebGL
      let dataUrl = null;
      try { dataUrl = c.toDataURL('image/png'); } catch (e) { dataUrl = 'toDataURL_fail:' + e; }
      const nearBlank = typeof dataUrl === 'string' && dataUrl.length < 100;
      return {
        w: c.width, h: c.height,
        className: c.className,
        display: getComputedStyle(c).display,
        visibility: getComputedStyle(c).visibility,
        dataUrlLen: typeof dataUrl === 'string' ? dataUrl.length : 0,
        toDataURL_blankish: nearBlank,
        note: 'WebGL toDataURL often blank without preserveDrawingBuffer — treat size/mode as primary'
      };
    } catch (e) {
      return { error: String(e) };
    }
  }

  async function fetchGlb(url) {
    try {
      const r = await fetch(url, { method: 'GET', cache: 'no-store' });
      const buf = await r.arrayBuffer();
      const u8 = new Uint8Array(buf.slice(0, 4));
      const magic = String.fromCharCode(u8[0], u8[1], u8[2], u8[3]);
      return {
        url,
        status: r.status,
        bytes: buf.byteLength,
        contentType: r.headers.get('content-type'),
        magic,
        looksGlb: magic === 'glTF'
      };
    } catch (e) {
      return { url, error: String(e) };
    }
  }

  // Grid preflight
  const preflight = {
    sharedCardWebgl: !!document.getElementById('t10-shared-webgl'),
    canvasCount: document.querySelectorAll('canvas').length,
    hint: document.querySelector('.t10-vp-hint')?.textContent || null
  };

  for (const v of vessels) {
    T.openRefs(v);
    await new Promise((r) => setTimeout(r, 80));
    const modal = document.getElementById('t10-ref-modal');
    const glbTab = modal.querySelector('.t10-insp-tab[data-tab="glb"]');
    const tabDisabled =
      !glbTab ||
      glbTab.getAttribute('aria-disabled') === 'true' ||
      glbTab.classList.contains('is-disabled') ||
      glbTab.disabled === true;

    const beforeClick = {
      mode: modal?.dataset?.viewerMode,
      activeTab: modal?.querySelector('.t10-insp-tab.active')?.getAttribute('data-tab'),
      stageDisplay: getComputedStyle(document.getElementById('t10-glb-stage')).display,
      glbTabDisabled: tabDisabled,
      glbTabText: glbTab ? glbTab.textContent.trim() : null
    };

    if (glbTab && !tabDisabled) glbTab.click();
    await new Promise((r) => setTimeout(r, 1500));

    const warnEl = document.querySelector('#t10-insp-mesh-warn');
    const net = await fetchGlb(v.glb && v.glb.url ? v.glb.url : `/output/assets/3d_models/vessel_${v.imo}.glb`);
    const canvasSample = await sampleCanvas();

    let webglN = 0;
    document.querySelectorAll('canvas').forEach((c) => {
      if (c.id === 't10-shared-webgl') webglN += 1;
      if (c.classList.contains('t10-glb-canvas')) webglN += 1;
    });

    const row = {
      imo: String(v.imo),
      name: v.name || null,
      manifest_glb: v.glb || null,
      ...beforeClick,
      after: {
        mode: modal.dataset.viewerMode,
        activeTab: modal.querySelector('.t10-insp-tab.active')?.getAttribute('data-tab'),
        stageDisplay: getComputedStyle(document.getElementById('t10-glb-stage')).display,
      },
      hasCanvas: !!document.querySelector('#t10-glb-stage canvas'),
      fidelity: document.querySelector('.t10-glb-fidelity')?.textContent || null,
      warnVisible: warnEl ? warnEl.hidden === false : false,
      warnText: warnEl && warnEl.hidden === false ? warnEl.textContent.trim() : null,
      webglContextsApprox: webglN,
      canvasCount: document.querySelectorAll('canvas').length,
      canvasSample,
      network: net,
      console_during: consoleBuf.slice(),
      page_errors: (window.__diagPageErrors || []).slice()
    };
    row.symptom_class = classify({
      glbTabDisabled: tabDisabled,
      warnVisible: row.warnVisible,
      warnText: row.warnText,
      hasCanvas: row.hasCanvas,
      mode: row.after.mode,
      canvasSample: {
        nearBlack: false,
        nearWhite: false,
        hasPixels: row.hasCanvas && row.after.mode === 'glb' && !row.warnVisible
      }
    });
    rows.push(row);
    consoleBuf.length = 0;

    T.closeRefs();
    await new Promise((r) => setTimeout(r, 100));
  }

  return {
    preflight,
    vessels: rows,
    console_residual: consoleBuf,
    page_errors: window.__diagPageErrors || []
  };
})()
"""


def file_manifest_audit() -> dict:
    """Disk vs manifest consistency — no code changes."""
    man_path = ROOT / "web" / "js" / "top10_vessels_manifest.js"
    text = man_path.read_text(encoding="utf-8")
    # Extract JSON array after export
    start = text.find("[")
    end = text.rfind("]")
    vessels = json.loads(text[start : end + 1]) if start >= 0 else []
    rows = []
    for v in vessels:
        imo = str(v.get("imo"))
        glb = v.get("glb") or {}
        url = glb.get("url") or ""
        # url like /output/assets/3d_models/vessel_X.glb
        rel = url.lstrip("/").replace("/", os.sep)
        out_p = ROOT / rel if rel else None
        web_p = ROOT / "web" / "assets" / "3d_models" / f"vessel_{imo}.glb"
        out_alt = ROOT / "output" / "assets" / "3d_models" / f"vessel_{imo}.glb"
        meta_p = out_alt.with_suffix(".meta.json")
        meta = {}
        if meta_p.exists():
            meta = json.loads(meta_p.read_text(encoding="utf-8"))

        def info(p: Path | None) -> dict | None:
            if not p or not p.exists():
                return {"exists": False, "path": str(p) if p else None}
            st = p.stat()
            return {
                "exists": True,
                "path": str(p),
                "bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            }

        out_i = info(out_alt)
        web_i = info(web_p)
        rows.append(
            {
                "imo": imo,
                "name": v.get("name"),
                "manifest": {
                    "url": url,
                    "ready": glb.get("ready"),
                    "bytes": glb.get("bytes"),
                    "path": glb.get("path"),
                },
                "disk_output": out_i,
                "disk_web": web_i,
                "meta_alg": meta.get("alg"),
                "meta_bytes": meta.get("bytes"),
                "bytes_match_manifest_vs_output": (
                    out_i.get("bytes") == glb.get("bytes") if out_i and out_i.get("exists") else False
                ),
                "output_eq_web_bytes": (
                    out_i.get("bytes") == web_i.get("bytes")
                    if out_i and web_i and out_i.get("exists") and web_i.get("exists")
                    else False
                ),
            }
        )
    return {"manifest_path": str(man_path), "vessels": rows}


async def run() -> dict:
    kill_port(PORT)
    PROFILE.mkdir(parents=True, exist_ok=True)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    edge = find_edge()
    proc = subprocess.Popen(
        [
            str(edge),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2.5)
    try:
        import urllib.request

        ver = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5).read())
        ws_url = ver["webSocketDebuggerUrl"]
        # Prefer page target
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next((t for t in tabs if t.get("type") == "page" and "sentinel_dashboard" in (t.get("url") or "")), None)
        if page and page.get("webSocketDebuggerUrl"):
            ws_url = page["webSocketDebuggerUrl"]

        async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
            await cdp(ws, "Network.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.enable")
            await asyncio.sleep(2.0)
            # Navigate fresh
            await cdp(ws, "Page.navigate", {"url": URL})
            await asyncio.sleep(3.0)
            result = await cdp_eval(ws, JS_DIAG, await_promise=True)
            val = (result.get("result") or {}).get("value")
            if val is None:
                val = {"cdp_raw": result}
            # Screenshot
            shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
            import base64

            png = base64.b64decode(shot.get("data") or "")
            shot_path = SHOT_DIR / "digital_twin_last.png"
            shot_path.write_bytes(png)
            return {"browser": val, "screenshot": str(shot_path)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(PORT)


def main() -> int:
    server_ok = False
    server_err = None
    try:
        import urllib.request

        r = urllib.request.urlopen(
            "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10", timeout=5
        )
        server_ok = r.status == 200
    except Exception as e:
        server_err = str(e)

    files = file_manifest_audit()
    browser = {}
    if server_ok:
        browser = asyncio.run(run())
    else:
        browser = {"skipped": True, "reason": server_err}

    report = {
        "diag_only": True,
        "no_code_changes": True,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "server": {"ok": server_ok, "url": URL, "error": server_err},
        "step3_files_manifest": files,
        "step1_2_4_browser": browser,
    }
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"WROTE {OUT}")
    return 0 if server_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
