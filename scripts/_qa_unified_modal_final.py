#!/usr/bin/env python3
"""Unified TOP-10 modal QA: WebGL/video isolation × 3 vessels × all tabs + contact sheet."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    import websockets
except ImportError:
    raise SystemExit("websockets required")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CDP_PORT = 9317
HUD = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
LOGS = ROOT / "logs"
CONTACT = LOGS / "unified_final_contact_sheet.png"
REPORT = LOGS / "unified_final_modal_verify.json"

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

VESSELS = (
    {"imo": "9388833", "name": "BU SAMRA", "rank": 1, "has_video": False},
    {"imo": "9372743", "name": "FRAIJAH", "rank": 7, "has_video": True},
    {"imo": "9337755", "name": "MOZAH", "rank": 5, "has_video": False},
)
TABS = (
    ("all", "Ortho Triplet"),
    ("glb", "Digital Twin"),
    ("voxel", "Voxel Grid"),
    ("overhead", "OVERHEAD"),
    ("video", "REAL VIDEO"),
)
_n = 1

JS_SNAP = r"""
(() => {
  // Scope: TOP-10 modal only. Never getContext() Chart.js canvases on other sheets —
  // that converts 2D contexts and burns the 16-slot WebGL budget.
  const canvases = [...document.querySelectorAll("#t10-glb-stage canvas, canvas.t10-glb-canvas, #t10-shared-webgl")];
  let webglLive = 0;
  const canvasInfo = canvases.map((c) => {
    let lost = true;
    try {
      const gl = c.getContext("webgl2") || c.getContext("webgl");
      lost = !gl || gl.isContextLost();
      if (gl && !lost) webglLive += 1;
    } catch (e) {}
    return { id: c.id || "", cls: c.className || "", w: c.width, h: c.height, lost };
  });
  const videos = [...document.querySelectorAll("#t10-video-stage video, video.t10-luma-video")].map((v) => ({
    src: String(v.currentSrc || v.getAttribute("src") || v.src || ""),
    paused: !!v.paused,
    muted: !!v.muted,
    loop: !!v.loop,
    readyState: v.readyState,
    inStage: !!v.closest("#t10-video-stage"),
  }));
  const modal = document.getElementById("t10-ref-modal");
  const videoTab = modal?.querySelector('.t10-insp-tab[data-tab="video"]');
  const active = modal?.querySelector(".t10-insp-tab.active");
  const badge = document.querySelector(".t10-glb-fidelity, .t10-video-fidelity")?.textContent || null;
  const panel = document.querySelector(".t10-modal-panel");
  const r = panel?.getBoundingClientRect();
  const glbCanvas = !!document.querySelector("#t10-glb-stage canvas.t10-glb-canvas, #t10-glb-stage canvas");
  const videoEl = !!document.querySelector("#t10-video-stage video");
  return {
    canvasCount: canvases.length,
    webglLive,
    videoCount: videos.length,
    videoPlaying: videos.filter((v) => !v.paused && v.inStage).length,
    sharedCard: !!document.getElementById("t10-shared-webgl"),
    glbCanvas,
    videoEl,
    bothModalLive: !!(glbCanvas && videoEl),
    videoStageHidden: modal?.querySelector("#t10-video-stage")?.hidden === true,
    modalHidden: modal ? modal.hidden === true : true,
    mode: modal?.dataset?.viewerMode || null,
    activeTab: active?.getAttribute("data-tab") || null,
    videoTabHidden: !videoTab || videoTab.hidden === true || videoTab.hasAttribute("hidden"),
    defaultOrtho: (active?.getAttribute("data-tab") === "all") && String(modal?.dataset?.viewerMode || "").startsWith("ortho"),
    badge,
    clip: r ? { x: r.x, y: r.y, w: r.width, h: r.height, dpr: window.devicePixelRatio || 1 } : null,
    canvasInfo,
    videos,
  };
})()
"""


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


def docker_push_live_js() -> list[str]:
    """HUD serves Docker volume, not host ./output — push current web/ artifacts."""
    pairs = [
        (ROOT / "web" / "js" / "top10_sheet.js", "/app/output/js/top10_sheet.js"),
        (ROOT / "web" / "js" / "top10_sheet.js", "/app/web/js/top10_sheet.js"),
        (ROOT / "web" / "js" / "top10_3d_viewer.js", "/app/output/js/top10_3d_viewer.js"),
        (ROOT / "web" / "js" / "top10_3d_viewer.js", "/app/web/js/top10_3d_viewer.js"),
        (ROOT / "web" / "js" / "top10_vessels_manifest.js", "/app/output/js/top10_vessels_manifest.js"),
        (ROOT / "web" / "js" / "top10_vessels_manifest.js", "/app/web/js/top10_vessels_manifest.js"),
        (ROOT / "web" / "css" / "sentinel_hud.css", "/app/output/js/sentinel_hud.css"),
        (ROOT / "web" / "css" / "sentinel_hud.css", "/app/output/css/sentinel_hud.css"),
        (ROOT / "web" / "css" / "sentinel_hud.css", "/app/web/css/sentinel_hud.css"),
    ]
    log: list[str] = []
    for src, dst in pairs:
        if not src.is_file():
            log.append(f"MISSING {src}")
            continue
        r = subprocess.run(
            ["docker", "cp", str(src), f"sentinel-web:{dst}"],
            capture_output=True,
            text=True,
        )
        log.append(f"{'OK' if r.returncode == 0 else 'FAIL'} {src.name} -> {dst}")
    return log


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


async def snap(ws) -> dict:
    v = await eval_js(ws, JS_SNAP)
    return v if isinstance(v, dict) else {"ok": False, "error": v}


def expect_res(s: dict, *, webgl: int, video: int) -> bool:
    return int(s.get("webglLive") or 0) == webgl and int(s.get("videoCount") or 0) == video and not s.get("sharedCard")


def judge_res(s: dict, webgl: int, video: int) -> str:
    return "PASS" if expect_res(s, webgl=webgl, video=video) else "FAIL"


async def wait_tab_ready(ws, tab: str, timeout_s: float = 8.0) -> dict:
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_s:
        last = await snap(ws)
        if tab in ("glb", "voxel", "overhead"):
            if last.get("glbCanvas") and last.get("webglLive") == 1 and last.get("videoCount") == 0:
                return last
        elif tab == "video":
            vids = last.get("videos") or []
            rs = int((vids[0] or {}).get("readyState") or 0) if vids else 0
            t = float((vids[0] or {}).get("currentTime") or 0) if vids else 0
            if last.get("videoEl") and last.get("webglLive") == 0 and (rs >= 2 or t > 0.05):
                return last
        elif tab == "all":
            if last.get("activeTab") == "all" and last.get("webglLive") == 0 and last.get("videoCount") == 0:
                return last
        await asyncio.sleep(0.2)
    return last


async def click_tab(ws, tab: str) -> None:
    await eval_js(
        ws,
        f"""
        (async () => {{
          const modal = document.getElementById("t10-ref-modal");
          const t = modal?.querySelector('.t10-insp-tab[data-tab="{tab}"]');
          if (t && !t.hidden && !t.hasAttribute("hidden")) t.click();
          await new Promise(r => setTimeout(r, 80));
          return true;
        }})()
        """,
    )


async def open_vessel(ws, imo: str, tab: str = "all") -> dict:
    return await eval_js(
        ws,
        f"""
        (async () => {{
          const T = window.__TOP10__;
          const v = T.vessels.find(x => String(x.imo) === "{imo}");
          if (!v) return {{ ok: false, error: "imo not found" }};
          T.openRefs(v, {{ tab: "{tab}" }});
          await new Promise(r => setTimeout(r, 350));
          return {{ ok: true, name: v.name, luma: !!(v.luma_video && v.luma_video.ready) }};
        }})()
        """,
    )


async def close_modal(ws) -> None:
    await eval_js(ws, "window.__TOP10__?.closeRefs?.()")
    await asyncio.sleep(0.25)


def crop_clip(raw: bytes, clip: dict | None) -> Image.Image:
    im = Image.open(BytesIO(raw)).convert("RGB")
    if not clip or not clip.get("w") or not clip.get("h"):
        return im
    dpr = float(clip.get("dpr") or 1)
    x0 = max(0, int(clip["x"] * dpr))
    y0 = max(0, int(clip["y"] * dpr))
    x1 = min(im.width, int((clip["x"] + clip["w"]) * dpr))
    y1 = min(im.height, int((clip["y"] + clip["h"]) * dpr))
    if x1 > x0 + 40 and y1 > y0 + 40:
        return im.crop((x0, y0, x1, y1))
    return im


async def capture_panel(ws, path: Path) -> dict:
    meta = await snap(ws)
    shot = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
    raw = base64.b64decode(shot.get("data") or "")
    img = crop_clip(raw, meta.get("clip"))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    meta["screenshot"] = str(path)
    meta["bytes"] = path.stat().st_size
    return meta


def na_tile(w: int, h: int, title: str) -> Image.Image:
    im = Image.new("RGB", (w, h), (11, 18, 32))
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font = ImageFont.load_default()
    msg = "N/A — LUMA clip not shipped"
    d.text((16, h // 2 - 24), title, fill=(148, 163, 184), font=font)
    d.text((16, h // 2 + 4), msg, fill=(100, 116, 139), font=font)
    return im


def build_contact_sheet(cells: dict[tuple[str, str], Path], matrix: list[dict]) -> Path:
    cell_w, cell_h = 420, 250
    pad = 10
    label_h = 22
    header = 42
    cols, rows = 5, 3
    W = cols * cell_w + (cols + 1) * pad
    H = header + rows * (cell_h + label_h) + (rows + 1) * pad
    sheet = Image.new("RGB", (W, H), (8, 14, 26))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
        font_t = ImageFont.truetype("arial.ttf", 18)
        font_s = ImageFont.truetype("arial.ttf", 11)
    except Exception:
        font = ImageFont.load_default()
        font_t = font
        font_s = font
    draw.text(
        (pad, 10),
        "UNIFIED FINAL  ·  TOP-10 modal  ·  vessel × tab  ·  Ortho / Twin / Voxel / OVERHEAD / REAL VIDEO",
        fill=(224, 247, 252),
        font=font_t,
    )
    status_by = {(r["imo"], r["tab"]): r.get("status") for r in matrix}
    for r, v in enumerate(VESSELS):
        for c, (tab, label) in enumerate(TABS):
            x = pad + c * (cell_w + pad)
            y = header + pad + r * (cell_h + label_h + pad)
            key = (v["imo"], tab)
            path = cells.get(key)
            if tab == "video" and not v["has_video"]:
                tile = na_tile(cell_w, cell_h, f"{v['name']} · REAL VIDEO")
                sheet.paste(tile, (x, y))
                st = "N/A"
            elif path and path.exists():
                img = Image.open(path).convert("RGB")
                img.thumbnail((cell_w, cell_h))
                ox = x + (cell_w - img.width) // 2
                oy = y + (cell_h - img.height) // 2
                sheet.paste(img, (ox, oy))
                st = status_by.get(key, "?")
            else:
                tile = na_tile(cell_w, cell_h, "missing shot")
                sheet.paste(tile, (x, y))
                st = "FAIL"
            draw.text(
                (x, y + cell_h + 3),
                f"{v['name']} · {label} · {st}",
                fill=(148, 163, 184) if st != "FAIL" else (248, 113, 113),
                font=font_s,
            )
    LOGS.mkdir(parents=True, exist_ok=True)
    sheet.save(CONTACT)
    return CONTACT


def file_bytes(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def payload_table() -> dict:
    rows = []
    for v in VESSELS:
        imo = v["imo"]
        rank = v["rank"]
        glb = ROOT / "web" / "assets" / "3d_models" / f"vessel_{imo}.glb"
        voxel = ROOT / "web" / "assets" / "3d_models" / f"vessel_{imo}_voxels.json"
        video = ROOT / "assets" / "7000" / "videos" / "vessel_9372743_luma.mp4"
        source = ROOT / "assets" / "7000" / "videos" / "vessel_9372743_luma_source.mp4"
        jpgs = [
            ROOT / "assets" / "7000" / f"{rank}-1.jpg",
            ROOT / "assets" / "7000" / f"{rank}-2.jpg",
            ROOT / "assets" / "7000" / f"{rank}-3.jpg",
        ]
        glb_b = file_bytes(glb)
        voxel_b = file_bytes(voxel)
        video_b = file_bytes(video) if v["has_video"] else 0
        source_b = file_bytes(source) if v["has_video"] else 0
        ortho_b = sum(file_bytes(p) for p in jpgs)
        # Worst-case session: ortho (eager) + each lazy tab visited once (HTTP cache).
        # Source MP4 is archival and is NOT referenced by the video element.
        worst = ortho_b + glb_b + voxel_b + video_b
        rows.append(
            {
                "imo": imo,
                "name": v["name"],
                "ortho_jpg_bytes": ortho_b,
                "glb_bytes": glb_b,
                "voxel_json_bytes": voxel_b,
                "video_bytes": video_b if v["has_video"] else None,
                "video_source_archival_bytes": source_b if v["has_video"] else None,
                "eager_on_open_bytes": ortho_b,
                "worst_case_all_tabs_bytes": worst,
                "worst_case_mib": round(worst / (1024 * 1024), 2),
                "note": "lazy: GLB / voxel / video never prefetch together; source MP4 not loaded",
            }
        )
    return {"vessels": rows}


def badge_ok(tab: str, badge: str | None) -> bool:
    t = badge or ""
    if tab == "glb":
        return "PHOTO-COMPOSITE" in t and "NOT VERIFIED STRUCTURAL" in t
    if tab == "voxel":
        return "VOXEL GRID" in t and "PALETTE" in t and "NOT A CONTINUOUS SURFACE" in t
    if tab == "overhead":
        return "VOXEL GRID" in t and ("PALETTE" in t or "SAT" in t)
    if tab == "video":
        return "LUMA.ai" in t and "NOT OSINT-VERIFIED" in t
    return True


async def main() -> int:
    from services.web_assets_sync import sync_web_assets

    LOGS.mkdir(parents=True, exist_ok=True)
    sync_web_assets(ROOT, force=True, log=False)
    docker_log = docker_push_live_js()

    payload = payload_table()

    kill_port(CDP_PORT)
    profile = LOGS / "edge_cdp_unified_final"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            HUD,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2.8)
    step1: list[dict] = []
    matrix: list[dict] = []
    cells: dict[tuple[str, str], Path] = {}
    badges: list[dict] = []
    try:
        tabs = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=8).read()
        )
        page = next(
            (t for t in tabs if t.get("type") == "page" and "sentinel_dashboard" in (t.get("url") or "")),
            None,
        ) or next((t for t in tabs if t.get("type") == "page"), None)
        if not page:
            REPORT.write_text(json.dumps({"ok": False, "error": "no page"}, indent=2), encoding="utf-8")
            return 1
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 << 20) as ws:
            await cdp(ws, "Page.enable")
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.navigate", {"url": f"{HUD}&nocache={int(time.time())}"})
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
                REPORT.write_text(json.dumps({"ok": False, "error": "no __TOP10__"}, indent=2), encoding="utf-8")
                return 1

            # ── STEP 1: grid isolation ──────────────────────────────────
            await asyncio.sleep(0.4)
            s0 = await snap(ws)
            s0["step"] = "grid_no_modal"
            s0["expect"] = {"webgl": 0, "video": 0}
            s0["status"] = judge_res(s0, 0, 0)
            step1.append(s0)

            # FRAIJAH resource state-machine
            await open_vessel(ws, "9372743", "all")
            s = await wait_tab_ready(ws, "all")
            s["step"] = "fraijah_ortho_open"
            s["expect"] = {"webgl": 0, "video": 0}
            s["status"] = judge_res(s, 0, 0)
            step1.append(s)

            seq = [
                ("glb", 1, 0, "digital_twin"),
                ("video", 0, 1, "real_video"),
                ("voxel", 1, 0, "voxel_grid"),
                ("video", 0, 1, "real_video_again"),
                ("overhead", 1, 0, "overhead"),
                ("video", 0, 1, "real_video_third"),
                ("glb", 1, 0, "twin_after_video"),
                ("all", 0, 0, "back_ortho"),
            ]
            both_live = False
            for tab, wgl, vid, name in seq:
                await click_tab(ws, tab)
                s = await wait_tab_ready(ws, tab, timeout_s=10.0)
                s["step"] = name
                s["expect"] = {"webgl": wgl, "video": vid}
                s["status"] = judge_res(s, wgl, vid)
                if s.get("bothModalLive") or (s.get("glbCanvas") and s.get("videoEl")):
                    both_live = True
                    s["status"] = "FAIL"
                step1.append(s)

            await close_modal(ws)
            s = await snap(ws)
            s["step"] = "after_close"
            s["expect"] = {"webgl": 0, "video": 0}
            s["status"] = judge_res(s, 0, 0)
            step1.append(s)

            # ── STEP 2 + 4: 3 vessels × tabs ────────────────────────────
            for v in VESSELS:
                imo = v["imo"]
                opened = await open_vessel(ws, imo, "all")
                s = await wait_tab_ready(ws, "all")
                default_ok = s.get("activeTab") == "all" and str(s.get("mode") or "").startswith("ortho")
                row = {
                    "imo": imo,
                    "name": v["name"],
                    "tab": "all",
                    "label": "Ortho Triplet",
                    "status": "PASS" if default_ok and expect_res(s, webgl=0, video=0) else "FAIL",
                    "default_ortho": default_ok,
                    "video_tab_hidden": s.get("videoTabHidden"),
                    "expect_video_hidden": not v["has_video"],
                    "resources": {"webgl": s.get("webglLive"), "video": s.get("videoCount")},
                    "mode": s.get("mode"),
                    "badge": s.get("badge"),
                    "open": opened,
                }
                if v["has_video"] and s.get("videoTabHidden"):
                    row["status"] = "FAIL"
                if (not v["has_video"]) and (not s.get("videoTabHidden")):
                    row["status"] = "FAIL"
                shot = LOGS / f"unified_{imo}_all.png"
                meta = await capture_panel(ws, shot)
                row["screenshot"] = str(shot)
                cells[(imo, "all")] = shot
                matrix.append(row)

                for tab, label in TABS[1:]:
                    if tab == "video" and not v["has_video"]:
                        matrix.append(
                            {
                                "imo": imo,
                                "name": v["name"],
                                "tab": "video",
                                "label": label,
                                "status": "N/A",
                                "reason": "LUMA clip isolated to IMO 9372743",
                                "video_tab_hidden": True,
                            }
                        )
                        continue
                    await click_tab(ws, tab)
                    s = await wait_tab_ready(ws, tab, timeout_s=12.0)
                    if tab == "video":
                        await asyncio.sleep(0.7)
                    if tab == "video":
                        ok = expect_res(s, webgl=0, video=1) and badge_ok("video", s.get("badge"))
                    elif tab in ("glb", "voxel", "overhead"):
                        ok = expect_res(s, webgl=1, video=0) and badge_ok(tab, s.get("badge"))
                    else:
                        ok = False
                    badges.append(
                        {
                            "imo": imo,
                            "tab": tab,
                            "badge": s.get("badge"),
                            "badge_ok": badge_ok(tab, s.get("badge")),
                        }
                    )
                    shot = LOGS / f"unified_{imo}_{tab}.png"
                    await capture_panel(ws, shot)
                    cells[(imo, tab)] = shot
                    matrix.append(
                        {
                            "imo": imo,
                            "name": v["name"],
                            "tab": tab,
                            "label": label,
                            "status": "PASS" if ok else "FAIL",
                            "resources": {"webgl": s.get("webglLive"), "video": s.get("videoCount")},
                            "mode": s.get("mode"),
                            "activeTab": s.get("activeTab"),
                            "badge": s.get("badge"),
                            "screenshot": str(shot),
                        }
                    )
                await close_modal(ws)

            # post-grid confirmation
            s = await snap(ws)
            s["step"] = "grid_after_all_vessels"
            s["expect"] = {"webgl": 0, "video": 0}
            s["status"] = judge_res(s, 0, 0)
            step1.append(s)

        contact = build_contact_sheet(cells, matrix)
        step1_fail = any(x.get("status") == "FAIL" for x in step1)
        matrix_fail = any(x.get("status") == "FAIL" for x in matrix)
        badge_fail = any(b.get("badge_ok") is False for b in badges)
        report = {
            "ok": not (step1_fail or matrix_fail or badge_fail or both_live),
            "contact_sheet": str(contact),
            "docker_push": docker_log,
            "step1_webgl_video": {
                "both_active_never": not both_live,
                "steps": step1,
                "pass": not step1_fail and not both_live,
            },
            "matrix": matrix,
            "badges": badges,
            "payload": payload,
            "probe_fix": "isWebGLCapable() caches + loseContext; no leaked probe context per mount",
            "final_product": {
                "closed": [
                    "Ortho Triplet default",
                    "Digital Twin (GLB mesh) lazy single WebGL",
                    "Voxel Grid InstancedMesh lazy single WebGL",
                    "OVERHEAD = voxel ortho camera (SAT deck colors)",
                    "REAL VIDEO = HTML5 <video> for IMO 9372743 only",
                    "mutual exclusion WebGL XOR video",
                    "photo-only cards (0 WebGL on grid)",
                ],
                "open_future": [
                    "LUMA/REAL VIDEO for the other 9 TOP-10 vessels is a separate product decision — not implied by this prompt",
                    "Satellite AIS remains stub-only (G3 terrestrial ceiling)",
                ],
            },
        }
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": report["ok"], "contact": str(contact), "report": str(REPORT)}, indent=2))
        return 0 if report["ok"] else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        kill_port(CDP_PORT)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
