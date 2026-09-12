#!/usr/bin/env python3
"""Capture console/exceptions while loading top10 sheet."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
PORT = 9275
URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10"
EDGE = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe"
)
PROFILE = ROOT / "logs" / "edge_cdp_console"
_n = 1


async def cdp(ws, method, params=None):
    global _n
    msg = {"id": _n, "method": method, "params": params or {}}
    _n += 1
    await ws.send(json.dumps(msg))
    while True:
        raw = json.loads(await ws.recv())
        if raw.get("method") and raw.get("id") is None:
            continue
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


async def main() -> int:
    import shutil

    kill_port(PORT)
    shutil.rmtree(PROFILE, ignore_errors=True)
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
    cons: list[str] = []
    try:
        await asyncio.sleep(2.5)
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())
        page = next(t for t in tabs if t.get("type") == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024) as ws:
            # Drain + listen pattern: enable domains then navigate
            await cdp(ws, "Runtime.enable")
            await cdp(ws, "Console.enable")
            await cdp(ws, "Page.enable")
            await cdp(ws, "Network.enable")
            await cdp(ws, "Page.navigate", {"url": URL})

            deadline = asyncio.get_event_loop().time() + 20
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    # poll readiness
                    ev = await cdp(
                        ws,
                        "Runtime.evaluate",
                        {
                            "expression": "({top10:!!window.__TOP10__, sheet:document.documentElement.getAttribute('data-sheet'), err: window.__lastModErr||null})",
                            "returnByValue": True,
                        },
                    )
                    val = (ev.get("result") or {}).get("value") or {}
                    if val.get("top10"):
                        break
                    continue
                msg = json.loads(raw)
                method = msg.get("method")
                if method == "Console.messageAdded":
                    m = msg["params"]["message"]
                    cons.append(f"{m.get('level')}: {m.get('text')}")
                elif method == "Runtime.exceptionThrown":
                    d = msg["params"].get("exceptionDetails") or {}
                    cons.append(
                        "EXCEPTION: "
                        + json.dumps(
                            {
                                "text": d.get("text"),
                                "url": d.get("url"),
                                "line": d.get("lineNumber"),
                                "col": d.get("columnNumber"),
                                "desc": (d.get("exception") or {}).get("description"),
                            },
                            ensure_ascii=False,
                        )
                    )
                elif method == "Network.responseReceived":
                    resp = msg["params"].get("response") or {}
                    url = resp.get("url") or ""
                    if any(x in url for x in ("top10", "three", "vessel_", "manifest")):
                        cons.append(f"NET {resp.get('status')} {url[:140]}")

            state = await cdp(
                ws,
                "Runtime.evaluate",
                {
                    "expression": "({top10:!!window.__TOP10__, vessels:(window.__TOP10__&&window.__TOP10__.vessels||[]).length, sheet:document.documentElement.getAttribute('data-sheet'), href:location.href})",
                    "returnByValue": True,
                },
            )
            report = {
                "state": (state.get("result") or {}).get("value"),
                "console": cons[-80:],
            }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except Exception:
            proc.kill()
        kill_port(PORT)

    out = ROOT / "logs" / "pipeline_console_probe.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"wrote": str(out), "top10": (report.get("state") or {}).get("top10"), "n_cons": len(cons)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
