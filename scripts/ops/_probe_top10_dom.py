"""Focused top10 DOM probe after module load."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import websockets

EDGE = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / (
    "Microsoft/Edge/Application/msedge.exe"
)
PORT = 9243
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"


async def main() -> None:
    out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    for line in out.splitlines():
        if f":{PORT}" in line and "LISTENING" in line.upper():
            pid = line.split()[-1]
            if pid.isdigit():
                subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
    prof = Path("logs") / f"edge_top10_{PORT}"
    prof.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={prof}",
            "--disable-gpu",
            "--no-first-run",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(5)
    import urllib.request

    pages = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
    page = next(p for p in pages if p.get("type") == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=8_000_000) as ws:
        n = [0]

        async def call(method, params=None):
            n[0] += 1
            msg = {"id": n[0], "method": method, "params": params or {}}
            await ws.send(json.dumps(msg))
            while True:
                raw = json.loads(await ws.recv())
                if raw.get("id") == msg["id"]:
                    return raw.get("result") or {}

        await call("Runtime.enable")
        await call("Page.enable")
        await asyncio.sleep(4)
        # wait for cards
        for _ in range(10):
            r = await call(
                "Runtime.evaluate",
                {
                    "expression": """(() => {
  const cards = document.querySelectorAll('.t10-card, [data-vessel-id]');
  const g = document.querySelector('#top10-grid-container, #t10Grid, .t10-grid');
  const cs = g ? getComputedStyle(g) : null;
  const tops = [...cards].map(c => c.getBoundingClientRect().top);
  const row1 = tops.length ? tops.filter(t => Math.abs(t - tops[0]) < 8).length : 0;
  const btn = [...document.querySelectorAll('button,a,[data-refs]')].find(el => /ORTHO|REFS|3 VIEW|INSPECT/i.test(el.textContent||'') || el.hasAttribute('data-refs'));
  return {
    cards: cards.length,
    row1,
    cols: cs ? cs.gridTemplateColumns : null,
    hasBtn: !!btn,
    sample: (document.body.innerText||'').replace(/\\s+/g,' ').slice(0,300),
    top10: !!window.__TOP10__,
    errs: (window.__SENTINEL_BOOT_ERRORS__||null)
  };
})()""",
                    "returnByValue": True,
                },
            )
            val = (r.get("result") or {}).get("value") or {}
            if val.get("cards", 0) > 0:
                print(json.dumps(val, indent=2, ensure_ascii=False))
                break
            await asyncio.sleep(1)
        else:
            print(json.dumps(val, indent=2, ensure_ascii=False))
            print("FAIL: no cards after wait")
    proc.terminate()


if __name__ == "__main__":
    asyncio.run(main())
