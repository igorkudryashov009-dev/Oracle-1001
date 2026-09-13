import asyncio
import base64
import json
import subprocess
import urllib.request
from pathlib import Path
import websockets

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PORT = 9352
URL = "http://45.8.230.214:8765/output/sentinel_dashboard.html?sheet=top10"


async def main():
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            "--window-size=1600,1050",
            "--no-first-run",
            "--no-default-browser-check",
            "--user-data-dir=C:/Users/MSI/AppData/Local/Temp/edge_probe_final_view",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(3)
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next(t for t in tabs if t.get("type") == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as ws:
            mid = 1

            async def cdp(method, params=None):
                nonlocal mid
                m = mid
                mid += 1
                await ws.send(json.dumps({"id": m, "method": method, "params": params or {}}))
                while True:
                    msg = await ws.recv()
                    r = json.loads(msg)
                    if r.get("id") == m:
                        return r.get("result", {})

            await cdp("Runtime.enable")
            await cdp("Page.enable")
            await cdp("Emulation.setDeviceMetricsOverride", {
                "width": 1600,
                "height": 1050,
                "deviceScaleFactor": 1,
                "mobile": False
            })
            await cdp("Page.navigate", {"url": URL})
            await asyncio.sleep(6)

            # Test opening IMO 9388833 with DEFAULT tab (should be GLB Digital Twin)
            open_glb = await cdp(
                "Runtime.evaluate",
                {
                    "expression": """(async () => {
                        const T = window.__TOP10__;
                        const v = T.vessels.find(x => String(x.imo) === '9388833');
                        if (!v) return { ok: false, error: 'vessel not found' };
                        
                        // Open inspector with default hierarchy (GLB)
                        T.openRefs(v);
                        
                        // Wait for GLB canvas to render and loading spinner to vanish
                        for (let i = 0; i < 150; i++) {
                            await new Promise(r => setTimeout(r, 100));
                            const modal = document.getElementById('t10-ref-modal');
                            const stage = document.getElementById('t10-glb-stage');
                            const canvas = stage?.querySelector('canvas');
                            const mode = modal?.dataset?.viewerMode;
                            const isLoading = stage?.classList.contains('is-glb-loading');
                            if (mode === 'glb' && canvas && !isLoading) {
                                stage.scrollIntoView({ behavior: 'instant', block: 'center' });
                                return {
                                    ok: true,
                                    imo: '9388833',
                                    name: v.name,
                                    mode: mode,
                                    canvasWidth: canvas.width,
                                    canvasHeight: canvas.height,
                                    activeTab: modal.querySelector('.t10-insp-tab.active')?.getAttribute('data-tab')
                                };
                            }
                        }
                        const modal = document.getElementById('t10-ref-modal');
                        return { ok: false, mode: modal?.dataset?.viewerMode };
                    })()""",
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            print("GLB RENDER STATUS:", json.dumps(open_glb.get("result", {}).get("value"), indent=2))
            await asyncio.sleep(1)

            # Capture screenshot
            shot = await cdp("Page.captureScreenshot", {"format": "png"})
            data = shot.get("data")
            if data:
                img_bytes = base64.b64decode(data)
                out_png = Path("logs/node_a_glb_restored_9388833.png")
                out_png.write_bytes(img_bytes)
                print(f"Saved GLB viewport screenshot: {out_png} ({len(img_bytes)} bytes)")

    finally:
        proc.terminate()


if __name__ == "__main__":
    asyncio.run(main())
