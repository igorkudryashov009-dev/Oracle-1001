"""E2E UI/grid/quant/sheets verification via Edge CDP (prod gate)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    raise SystemExit("need websockets")

EDGE = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / (
    "Microsoft/Edge/Application/msedge.exe"
)
if not EDGE.is_file():
    EDGE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / (
        "Microsoft/Edge/Application/msedge.exe"
    )
PORT = 9240
BASE = "http://127.0.0.1:8765/output/sentinel_dashboard.html"
URL = BASE + "?sheet=top10"
ROOT = Path(__file__).resolve().parents[1]


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


async def call(ws, method, params=None, n=[0]):
    n[0] += 1
    msg = {"id": n[0], "method": method, "params": params or {}}
    await ws.send(json.dumps(msg))
    while True:
        raw = json.loads(await ws.recv())
        if raw.get("id") == msg["id"]:
            if "error" in raw:
                raise RuntimeError(f"{method}: {raw['error']}")
            return raw.get("result") or {}


async def eval_js(ws, expression: str):
    r = await call(
        ws,
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    return (r.get("result") or {}).get("value")


async def drain(ws, report, timeout=0.4):
    end = time.time() + timeout
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.1)
            msg = json.loads(raw)
            m = msg.get("method")
            if m == "Runtime.exceptionThrown":
                det = ((msg.get("params") or {}).get("exceptionDetails") or {})
                report["exceptions"].append(det.get("text") or str(det)[:200])
            if m == "Console.messageAdded":
                p = (msg.get("params") or {}).get("message") or {}
                lvl = p.get("level")
                txt = p.get("text")
                if lvl == "error":
                    report["exceptions"].append(txt)
                elif lvl == "warning":
                    report["warnings"].append(txt)
        except Exception:
            break


async def main() -> int:
    kill_port(PORT)
    profile = ROOT / "logs" / f"edge_verify_{PORT}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(EDGE),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    report: dict = {"exceptions": [], "warnings": [], "checks": {}}
    try:
        pages = json.loads(
            subprocess.check_output(
                ["curl", "-s", f"http://127.0.0.1:{PORT}/json/list"], text=True
            )
        )
        page = next((p for p in pages if p.get("type") == "page"), None)
        if not page:
            print("FAIL: no page")
            return 2
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=8_000_000) as ws:
            await call(ws, "Runtime.enable")
            await call(ws, "Console.enable")
            await call(ws, "Page.enable")
            await asyncio.sleep(2)
            await drain(ws, report, 1)

            await call(
                ws,
                "Emulation.setDeviceMetricsOverride",
                {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
            )
            await call(ws, "Page.reload", {"ignoreCache": True})
            await asyncio.sleep(3)
            await drain(ws, report, 1)

            report["checks"]["desktop_2col"] = await eval_js(
                ws,
                """(() => {
  const g = document.querySelector('#t10Grid, #top10-grid-container, .t10-grid, .top10-grid');
  if (!g) return {ok:false, err:'grid missing'};
  const cs = getComputedStyle(g);
  const cards = g.querySelectorAll('.t10-card, [data-vessel-id], .top10-card');
  const cols = cs.gridTemplateColumns.split(' ').filter(Boolean);
  const tops = [...cards].map(c => c.getBoundingClientRect().top);
  const first = tops[0];
  const row1 = tops.filter(t => Math.abs(t-first) < 8).length;
  return {ok:true, colsCount: cols.length, gridTemplateColumns: cs.gridTemplateColumns,
          cardCount: cards.length, cardsInFirstRow: row1};
})()""",
            )

            report["checks"]["metrics"] = await eval_js(
                ws,
                """(() => {
  const nodes = [...document.querySelectorAll('.t10-card, [data-vessel-id]')].slice(0,2);
  return nodes.map(card => {
    const txt = card.innerText || '';
    const overflow = [...card.querySelectorAll('*')].filter(el => {
      const s = getComputedStyle(el);
      return (el.scrollWidth > el.clientWidth + 2) && (s.overflowX === 'hidden' || s.textOverflow === 'ellipsis');
    }).length;
    return {
      hasLOA: /LOA/i.test(txt), hasBeam: /Beam/i.test(txt), hasDraft: /Draft/i.test(txt),
      hasIntegrity: /Integrity|AIS/i.test(txt), overflowish: overflow,
      sample: txt.slice(0,200).replace(/\\s+/g,' ')
    };
  });
})()""",
            )

            report["checks"]["modal"] = await eval_js(
                ws,
                """(() => {
  const clickTarget =
    document.querySelector('[data-refs], [data-action="open-refs"], .t10-btn-refs, button.t10-refs')
    || [...document.querySelectorAll('button, a')].find(el => /ORTHO|3 VIEW|REFS|INSPECT/i.test(el.textContent||''))
    || document.querySelector('.t10-card, [data-vessel-id]');
  if (!clickTarget) return {ok:false, err:'no click target'};
  clickTarget.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
  const modal = document.querySelector('#t10Inspector, .t10-modal, .t10-insp, [data-inspector]');
  if (!modal) return {ok:false, err:'modal missing', clicked: clickTarget.tagName};
  const cs = getComputedStyle(modal);
  const visible = cs.display !== 'none' && cs.visibility !== 'hidden'
    && (modal.classList.contains('open') || modal.getAttribute('aria-hidden') !== 'true' || cs.opacity !== '0');
  const figs = modal.querySelectorAll('img, figure, .t10-ortho-fig, .ortho, canvas');
  const telem = modal.innerText || '';
  return {
    ok: !!visible, display: cs.display, className: modal.className,
    figCount: figs.length, hasTelemetry: /SOG|COG|Draft|MMSI|IMO|lat|lon/i.test(telem),
    telemSample: telem.slice(0,240).replace(/\\s+/g,' ')
  };
})()""",
            )
            await drain(ws, report, 0.5)

            await call(
                ws,
                "Emulation.setDeviceMetricsOverride",
                {"width": 900, "height": 1200, "deviceScaleFactor": 1, "mobile": True},
            )
            await asyncio.sleep(1)
            report["checks"]["mobile_1col"] = await eval_js(
                ws,
                """(() => {
  const g = document.querySelector('#t10Grid, #top10-grid-container, .t10-grid, .top10-grid');
  if (!g) return {ok:false};
  const cs = getComputedStyle(g);
  const cols = cs.gridTemplateColumns.split(' ').filter(Boolean);
  const cards = g.querySelectorAll('.t10-card, [data-vessel-id], .top10-card');
  const tops = [...cards].map(c => c.getBoundingClientRect().top);
  const first = tops[0];
  const row1 = tops.filter(t => Math.abs(t-first) < 8).length;
  return {colsCount: cols.length, gridTemplateColumns: cs.gridTemplateColumns,
          cardsInFirstRow: row1, width: window.innerWidth};
})()""",
            )

            report["checks"]["quant_payload"] = await eval_js(
                ws,
                """(() => {
  const p = window.__SENTINEL_PAYLOAD__ || {};
  const q = p.quant_pipeline || {};
  return {
    live_vessel_count: p.live_vessel_count,
    top500_universe: (p.fleet_summary||{}).top500_universe,
    top500_live: (p.fleet_summary||{}).top500_live,
    source_mode: p.source_mode,
    ensemble_accuracy_pct: q.ensemble_accuracy_pct,
    catboost_accuracy_pct: q.catboost_accuracy_pct,
    generated_at_utc: p.generated_at_utc
  };
})()""",
            )

            await call(
                ws,
                "Emulation.setDeviceMetricsOverride",
                {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
            )
            sheet_results = {}
            for sh in ("ttf", "top10", "route", "balance", "osint", "alerts"):
                await call(ws, "Page.navigate", {"url": f"{BASE}?sheet={sh}"})
                await asyncio.sleep(2)
                await drain(ws, report, 0.5)
                sheet_results[sh] = await eval_js(
                    ws,
                    f"""(() => {{
  const active = document.documentElement.getAttribute('data-sheet');
  const html = document.body ? document.body.innerHTML : '';
  const text = document.body ? document.body.innerText.slice(0,400) : '';
  return {{
    dataSheet: active,
    markers: {{
      speed21: /21\\s*kn|V\\s*>\\s*21|>\\s*21/i.test(html),
      hmm: /HMM|Markov|alert/i.test(html),
      whatIf: /What-If|bal-slider|elasticity|Blockage/i.test(html)
    }},
    sample: text.replace(/\\s+/g,' ').slice(0,160)
  }};
}})()""",
                )
            report["checks"]["sheets"] = sheet_results
    finally:
        proc.terminate()

    out = ROOT / "logs" / "e2e_verify_report.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
