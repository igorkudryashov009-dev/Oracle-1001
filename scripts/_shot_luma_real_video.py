#!/usr/bin/env python3
"""Capture REAL VIDEO tab (LUMA.ai, FRAIJAH IMO 9372743) + isolation checks."""
from __future__ import annotations

import asyncio
import base64
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
PORT = 9274
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
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


async def eval_js(ws, expression: str):
    ev = await cdp(
        ws,
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
    )
    if ev.get("exceptionDetails"):
        return {"ok": False, "error": ev.get("exceptionDetails")}
    return (ev.get("result") or {}).get("value")


async def main() -> int:
    kill_port(PORT)
    profile = ROOT / "logs" / "edge_cdp_luma_video"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
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
    out_shot = ROOT / "logs" / "luma_real_video_9372743.png"
    report: dict = {}
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next(
            (t for t in tabs if t.get("type") == "page" and "sentinel_dashboard" in (t.get("url") or "")),
            None,
        )
        if not page:
            page = next((t for t in tabs if t.get("type") == "page"), None)
        if not page:
            print(json.dumps({"ok": False, "error": "no page"}))
            return 1
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": f"{URL}&nocache={int(time.time())}"})
            ready = False
            for _ in range(80):
                r = await cdp(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!(window.__TOP10__ && window.__TOP10__.vessels && window.__TOP10__.vessels.length)",
                        "returnByValue": True,
                    },
                )
                if (r.get("result") or {}).get("value"):
                    ready = True
                    break
                await asyncio.sleep(0.25)
            if not ready:
                print(json.dumps({"ok": False, "error": "no __TOP10__"}))
                return 1

            lazy = await eval_js(
                ws,
                """
                (async () => {
                  const wait = (ms) => new Promise(r => setTimeout(r, ms));
                  const T = window.__TOP10__;
                  const v = T.vessels.find(x => String(x.imo) === '9372743');
                  T.openRefs(v, { tab: 'all' });
                  await wait(400);
                  const modal = document.getElementById('t10-ref-modal');
                  const tab = modal.querySelector('.t10-insp-tab[data-tab="video"]');
                  const vid = modal.querySelector('#t10-video-stage video');
                  return {
                    ok: true,
                    defaultMode: modal.dataset.viewerMode,
                    videoTabHidden: tab?.hidden === true || tab?.hasAttribute('hidden'),
                    videoTabLabel: (tab?.textContent || '').replace(/\\s+/g, ' ').trim(),
                    videoElBeforeClick: !!vid,
                    videoSrcBeforeClick: vid?.getAttribute('src') || vid?.src || null,
                    lumaReady: !!(v.luma_video && v.luma_video.ready),
                    lumaUrl: v.luma_video?.url || null,
                  };
                })()
                """,
            )

            video_meta = await eval_js(
                ws,
                """
                (async () => {
                  const wait = (ms) => new Promise(r => setTimeout(r, ms));
                  const modal = document.getElementById('t10-ref-modal');
                  const tab = modal.querySelector('.t10-insp-tab[data-tab="video"]');
                  if (!tab || tab.hidden) return { ok: false, error: 'video tab hidden on FRAIJAH' };
                  tab.click();
                  await wait(200);
                  const tabs = modal.querySelector('.t10-insp-tabs');
                  tabs?.scrollIntoView({ block: 'start' });
                  let vid = modal.querySelector('#t10-video-stage video');
                  for (let i = 0; i < 40; i++) {
                    vid = modal.querySelector('#t10-video-stage video');
                    if (vid && (vid.readyState >= 2 || vid.currentTime > 0.05)) break;
                    await wait(150);
                  }
                  await wait(600);
                  const stage = document.getElementById('t10-video-stage');
                  stage?.scrollIntoView({ block: 'center' });
                  const r = stage?.getBoundingClientRect();
                  const badge = document.querySelector('.t10-video-fidelity')?.textContent || null;
                  return {
                    ok: !!(vid && vid.src),
                    mode: modal.dataset.viewerMode,
                    activeTab: modal.querySelector('.t10-insp-tab.active')?.getAttribute('data-tab'),
                    src: vid?.currentSrc || vid?.src || null,
                    preload: vid?.preload || null,
                    paused: vid?.paused ?? null,
                    muted: vid?.muted ?? null,
                    loop: vid?.loop ?? null,
                    readyState: vid?.readyState ?? null,
                    currentTime: vid?.currentTime ?? null,
                    badge,
                    clip: r ? { x: r.x, y: r.y, w: r.width, h: r.height, dpr: window.devicePixelRatio || 1 } : null,
                  };
                })()
                """,
            )

            shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
            out_shot.write_bytes(base64.b64decode(shot.get("data") or ""))

            teardown = await eval_js(
                ws,
                """
                (async () => {
                  const wait = (ms) => new Promise(r => setTimeout(r, ms));
                  const modal = document.getElementById('t10-ref-modal');
                  const ortho = modal.querySelector('.t10-insp-tab[data-tab="all"]');
                  ortho?.click();
                  await wait(200);
                  const afterSwitch = {
                    mode: modal.dataset.viewerMode,
                    videoEl: !!modal.querySelector('#t10-video-stage video'),
                    videoSrc: modal.querySelector('#t10-video-stage video')?.src || null,
                    stageHidden: modal.querySelector('#t10-video-stage')?.hidden === true,
                  };
                  window.__TOP10__.closeRefs();
                  await wait(150);
                  const afterClose = {
                    modalHidden: modal.hidden === true,
                    videoEl: !!document.querySelector('#t10-video-stage video'),
                    videoSrc: document.querySelector('#t10-video-stage video')?.src || null,
                  };
                  return { afterSwitch, afterClose };
                })()
                """,
            )

            isolation = await eval_js(
                ws,
                """
                (async () => {
                  const wait = (ms) => new Promise(r => setTimeout(r, ms));
                  const T = window.__TOP10__;
                  const others = T.vessels.filter(x => String(x.imo) !== '9372743');
                  const withLuma = T.vessels.filter(x => x.luma_video).map(x => x.imo);
                  const v = T.vessels.find(x => String(x.imo) === '9388833');
                  T.openRefs(v, { tab: 'all' });
                  await wait(300);
                  const modal = document.getElementById('t10-ref-modal');
                  const tab = modal.querySelector('.t10-insp-tab[data-tab="video"]');
                  const styleHidden = tab ? getComputedStyle(tab).display === 'none' : true;
                  const clickedAnyway = (() => {
                    try { tab?.click(); } catch (e) { return String(e); }
                    return tab?.classList.contains('active') || false;
                  })();
                  await wait(150);
                  const out = {
                    otherCount: others.length,
                    vesselsWithLumaField: withLuma,
                    busamraName: v?.name,
                    videoTabHidden: tab?.hidden === true || tab?.hasAttribute('hidden'),
                    videoTabDisplayNone: styleHidden,
                    videoTabActiveAfterClick: !!clickedAnyway,
                    videoEl: !!modal.querySelector('#t10-video-stage video'),
                    mode: modal.dataset.viewerMode,
                    gridVisible: modal.querySelector('#t10-modal-grid')?.hidden === false,
                  };
                  T.closeRefs();
                  return out;
                })()
                """,
            )

            report = {
                "ok": bool(video_meta and video_meta.get("ok")),
                "screenshot": str(out_shot),
                "screenshot_bytes": out_shot.stat().st_size,
                "lazy": lazy,
                "video": video_meta,
                "teardown": teardown,
                "isolation": isolation,
            }
            (ROOT / "logs" / "luma_real_video_verify.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps(report, ensure_ascii=True, indent=2))
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
