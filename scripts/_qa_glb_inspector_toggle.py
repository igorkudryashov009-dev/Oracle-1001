#!/usr/bin/env python3
"""QA: Ortho Triplet default + lazy 3D View + Never-Black + dispose (Edge CDP)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import websockets
except ImportError:
    print("FAIL: websockets required")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[1]
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
PORT = 9245
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"


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


JS_QA = r"""
(async () => {
  const T = window.__TOP10__;
  const vessels = T.vessels.slice(0, 6);
  const steps = [];
  const beforeCanvas = document.querySelectorAll("canvas").length;
  const sharedBefore = !!document.getElementById("t10-shared-webgl");
  const hintSample = document.querySelector(".t10-vp-hint")?.textContent || "";
  steps.push({
    step: "grid_preflight",
    sharedCardWebgl: sharedBefore,
    canvasCount: beforeCanvas,
    hint: hintSample,
    photoOnlyHint: /ORTHO|PHOTO/i.test(hintSample) && !/PBR\s*·\s*STANDBY/i.test(hintSample),
  });

  T.openRefs(vessels[0]);
  await new Promise((r) => setTimeout(r, 100));
  const modal = document.getElementById("t10-ref-modal");
  steps.push({
    step: "default",
    mode: modal?.dataset?.viewerMode,
    activeTab: modal?.querySelector(".t10-insp-tab.active")?.getAttribute("data-tab"),
    stageDisplay: getComputedStyle(document.getElementById("t10-glb-stage")).display,
    gridHidden: !!document.getElementById("t10-modal-grid")?.hidden,
  });
  T.closeRefs();
  await new Promise((r) => setTimeout(r, 50));

  for (let i = 0; i < 5; i++) {
    const v = vessels[i];
    T.openRefs(v);
    await new Promise((r) => setTimeout(r, 60));
    const defMode = modal.dataset.viewerMode;
    modal.querySelector('.t10-insp-tab[data-tab="glb"]').click();
    await new Promise((r) => setTimeout(r, 1200));
    const row = {
      imo: String(v.imo),
      ready: !!(v.glb && v.glb.ready === true),
      defMode,
      mode: modal.dataset.viewerMode,
      activeTab: modal.querySelector(".t10-insp-tab.active")?.getAttribute("data-tab"),
      hasCanvas: !!document.querySelector("#t10-glb-stage canvas"),
      fidelity: document.querySelector(".t10-glb-fidelity")?.textContent || null,
      sharedCardWebgl: !!document.getElementById("t10-shared-webgl"),
      webglContextsApprox: (() => {
        // Heuristic: count canvases that look like WebGL hosts
        let n = 0;
        document.querySelectorAll("canvas").forEach((c) => {
          if (c.id === "t10-shared-webgl") n += 1;
          if (c.classList.contains("t10-glb-canvas")) n += 1;
        });
        return n;
      })(),
      warn: document.querySelector("#t10-insp-mesh-warn")?.hidden === false
        ? document.querySelector("#t10-insp-mesh-warn").textContent
        : null,
      canvasCount: document.querySelectorAll("canvas").length,
    };
    modal.querySelector('.t10-insp-tab[data-tab="all"]').click();
    await new Promise((r) => setTimeout(r, 100));
    row.backMode = modal.dataset.viewerMode;
    row.canvasAfterOrtho = document.querySelectorAll("canvas").length;
    T.closeRefs();
    await new Promise((r) => setTimeout(r, 80));
    row.canvasAfterClose = document.querySelectorAll("canvas").length;
    steps.push(row);
  }

  const fake = Object.assign({}, vessels[0], { glb: { ready: false, url: "/missing.glb" } });
  T.openRefs(fake);
  await new Promise((r) => setTimeout(r, 80));
  const glbTab = modal.querySelector('.t10-insp-tab[data-tab="glb"]');
  const disabled =
    glbTab.getAttribute("aria-disabled") === "true" || glbTab.classList.contains("is-disabled");
  glbTab.click();
  await new Promise((r) => setTimeout(r, 250));
  const fakeReadyFalse = {
    disabled,
    mode: modal.dataset.viewerMode,
    activeTab: modal.querySelector(".t10-insp-tab.active")?.getAttribute("data-tab"),
    warnVisible: document.querySelector("#t10-insp-mesh-warn")?.hidden === false,
    stageDisplay: getComputedStyle(document.getElementById("t10-glb-stage")).display,
    hasBlackRisk: !!(
      document.querySelector("#t10-glb-stage canvas") &&
      document.getElementById("t10-modal-grid")?.hidden
    ),
  };
  T.closeRefs();
  await new Promise((r) => setTimeout(r, 40));

  const stress = await T.stressInspector(6);

  return {
    beforeCanvas,
    afterCanvas: document.querySelectorAll("canvas").length,
    steps,
    fakeReadyFalse,
    stress,
  };
})()
"""


async def main() -> int:
    edge = find_edge()
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_glb_qa"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(edge),
            f"--remote-debugging-port={PORT}",
            "--headless=new",
            "--disable-gpu",
            "--window-size=1600,1000",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import urllib.request

        ws_url = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as resp:
                    tabs = json.loads(resp.read().decode("utf-8"))
                page = next((t for t in tabs if t.get("type") == "page"), None)
                if page and page.get("webSocketDebuggerUrl"):
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            await asyncio.sleep(0.35)
        if not ws_url:
            print(json.dumps({"ok": False, "error": "CDP not ready"}))
            return 1

        async with websockets.connect(ws_url, max_size=8_000_000) as ws:
            await cdp(ws, "Runtime.enable")
            for _ in range(40):
                ready = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!(window.__TOP10__ && document.querySelector('.t10-ref-btn'))",
                        "returnByValue": True,
                    },
                )
                if ready.get("result", {}).get("value"):
                    break
                await asyncio.sleep(0.4)
            else:
                print(json.dumps({"ok": False, "error": "TOP10 not ready"}))
                return 1

            result = await cdp(
                ws,
                "Runtime.evaluate",
                {"expression": JS_QA, "awaitPromise": True, "returnByValue": True},
            )
            val = result.get("result", {}).get("value")
            out = ROOT / "logs" / "glb_integration_qa.json"
            out.write_text(json.dumps(val, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps(val, indent=2, ensure_ascii=False))

            ok = True
            fails = []
            if not val:
                fails.append("empty result")
                ok = False
            else:
                d0 = val["steps"][0]
                if d0.get("step") == "grid_preflight":
                    if d0.get("sharedCardWebgl"):
                        fails.append("shared card WebGL (#t10-shared-webgl) present on grid")
                        ok = False
                    if d0.get("photoOnlyHint") is False:
                        fails.append(f"card hint still PBR/STANDBY-like: {d0.get('hint')}")
                        ok = False
                    d0 = val["steps"][1] if len(val["steps"]) > 1 else {}
                if d0.get("mode") != "ortho:triplet" or d0.get("activeTab") != "all":
                    fails.append(f"default not ortho: {d0}")
                    ok = False
                if d0.get("gridHidden") is True:
                    fails.append("ortho grid hidden on default")
                    ok = False
                for s in val["steps"]:
                    if s.get("step") in ("default", "grid_preflight"):
                        continue
                    if s.get("defMode") != "ortho:triplet":
                        fails.append(f"defMode {s.get('imo')}: {s.get('defMode')}")
                        ok = False
                    if s.get("ready"):
                        mode = str(s.get("mode") or "")
                        if not (mode.startswith("glb") or mode.startswith("ortho")):
                            fails.append(f"bad mode {s}")
                            ok = False
                        if mode.startswith("glb") and not s.get("fidelity"):
                            fails.append(f"missing fidelity badge {s.get('imo')}")
                            ok = False
                    if int(s.get("canvasAfterClose") or 0) > int(val["beforeCanvas"]) + 2:
                        fails.append(f"canvas leak {s}")
                        ok = False
                    if s.get("sharedCardWebgl"):
                        fails.append(f"shared card webgl leaked during cycle {s.get('imo')}")
                        ok = False
                fr = val["fakeReadyFalse"]
                if not fr.get("disabled"):
                    fails.append("ready=false tab not disabled")
                    ok = False
                if fr.get("activeTab") != "all":
                    fails.append(f"ready=false activeTab={fr.get('activeTab')}")
                    ok = False
                if fr.get("hasBlackRisk"):
                    fails.append("black viewport risk on ready=false")
                    ok = False
                stress = val.get("stress") or {}
                if stress.get("errors"):
                    fails.append(f"stress errors: {stress['errors']}")
                    ok = False

            print("FAILS", fails)
            print("OVERALL", "PASS" if ok else "FAIL")
            print(f"WROTE {out}")
            return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        kill_port(PORT)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
