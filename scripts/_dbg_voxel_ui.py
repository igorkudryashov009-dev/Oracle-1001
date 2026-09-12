#!/usr/bin/env python3
import asyncio, json, os, subprocess, time, urllib.request
from pathlib import Path
import websockets

ROOT = Path(__file__).resolve().parents[1]
PORT = 9270
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


async def cdp(ws, method, params=None):
    global _n
    msg = {"id": _n, "method": method, "params": params or {}}
    _n += 1
    await ws.send(json.dumps(msg))
    while True:
        raw = json.loads(await ws.recv())
        if raw.get("id") == msg["id"]:
            return raw.get("result") or {}


async def main():
    # health
    try:
        h = urllib.request.urlopen("http://127.0.0.1:8765/output/api/v1/health", timeout=3).read()[:200]
        print("health", h[:120])
    except Exception as e:
        print("SERVER DOWN", e)
        return 2

    profile = ROOT / "logs" / "edge_cdp_voxel_dbg"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "--no-first-run", URL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1).read()
            break
        except Exception:
            await asyncio.sleep(0.4)
    else:
        print("edge debug port failed")
        proc.kill()
        return 3

    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next(t for t in tabs if t.get("type") == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 << 20) as ws:
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Page.enable")
            await cdp(ws, "Page.navigate", {"url": f"{URL}&t={int(time.time())}"})
            await asyncio.sleep(6)
            r = await cdp(
                ws,
                "Runtime.evaluate",
                {
                    "expression": """(() => ({
                      top10: !!window.__TOP10__,
                      vessels: window.__TOP10__?.vessels?.length,
                      sheetBtn: !!document.querySelector('[data-sheet=\"top10\"], .sheet-top10, #sheet-top10'),
                      t10cards: document.querySelectorAll('.t10-card, [data-vessel-id]').length,
                      href: location.href
                    }))()""",
                    "returnByValue": True,
                },
            )
            print("STATE", json.dumps((r.get("result") or {}).get("value"), indent=2))
            r2 = await cdp(
                ws,
                "Runtime.evaluate",
                {
                    "expression": """
                    (async () => {
                      try {
                        const m = await import('/output/js/top10_3d_viewer.js?v='+Date.now());
                        return {ok:true, keys:Object.keys(m)};
                      } catch(e) { return {ok:false, err:String(e), stack:e.stack}; }
                    })()
                    """,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            print("VIEWER", json.dumps((r2.get("result") or {}).get("value"), indent=2)[:2500])
            r3 = await cdp(
                ws,
                "Runtime.evaluate",
                {
                    "expression": """
                    (async () => {
                      try {
                        const m = await import('/output/js/top10_sheet.js?v='+Date.now());
                        return {ok:true, keys:Object.keys(m), top10:!!window.__TOP10__};
                      } catch(e) { return {ok:false, err:String(e), stack:e.stack}; }
                    })()
                    """,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            print("SHEET", json.dumps((r3.get("result") or {}).get("value"), indent=2)[:2500])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
