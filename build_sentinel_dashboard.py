"""Build Oracle-1001 / Sentinel dashboard: AIS · TTF · TOP10 · ROUTE Analytics."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.balance_analytics import build_balance_payload  # noqa: E402
from services.alerts_engine import build_alerts_payload  # noqa: E402
from services.ttf_forecast.quant_pipeline import build_quant_pipeline_payload  # noqa: E402
from services.route_analytics import assert_route_payload, build_route_analytics_payload  # noqa: E402
from services.sentinel_analytics import build_sentinel_payload  # noqa: E402
from services.top10_vessels import (  # noqa: E402
    assert_reference_images_exist,
    sync_reference_assets,
    write_js_manifest,
)
from services.ttf_forecast.dashboard_payload import build_ttf_forecast_payload  # noqa: E402
from services.web_assets_sync import format_sync_line, sync_web_assets  # noqa: E402
from services.ais_health import (  # noqa: E402
    LIVE_OK_LAG_SEC,
    compute_ais_freshness,
    write_health_files,
)
from services.utils.path_sanitizer import sanitize_structure  # noqa: E402

OUT_HTML = ROOT / "output" / "sentinel_dashboard.html"
WEB_DIR = ROOT / "web"
JS_OUT = ROOT / "output" / "js"
CSS_OUT = ROOT / "output" / "css"
WEB_JS = WEB_DIR / "js"
WEB_CSS = WEB_DIR / "css"

# Cache-bust for HUD JS (route/map modules). Bump when basemap / route logic changes.
ASSET_V = os.environ.get("SENTINEL_ASSET_V", "basemap-proxy-v1")
# Default: same-origin tile proxy (server holds MAPTILES_PROVIDER_KEY; Esri fallback inside proxy).
DEFAULT_TILE_URL = "/api/tiles/mapbox/{z}/{x}/{y}.png"
DEFAULT_TILE_SUBDOMAINS = ""
DEFAULT_TILE_ATTR = "Tiles via Sentinel proxy · Esri fallback"

DB_CANDIDATES = [
    ROOT / "история1" / "sentinel_ais.db",
    ROOT / "sentinel_ais.db",
    Path("/opt/oracle1001/analytical_engine/история1/sentinel_ais.db"),
    Path("/opt/oracle1001/analytical_engine/sentinel_ais.db"),
]
# Truth Contract: live_ok when lag < 5 minutes; STALE banner after 10 minutes
STALE_AFTER_SEC = 600
LIVE_OK_AFTER_SEC = LIVE_OK_LAG_SEC


def _copy_web_assets(*, force: bool = False) -> list[dict]:
    """Atomic MD5-gated pipeline: web/js|css (+ web root) → output/js|css.

    - Overwrites only when checksum differs (or force=True).
    - Writes via temp+os.replace to avoid torn reads during HTTP serve.
    - CSS is dual-deployed to output/css/ and output/js/ (HTML href=js/*.css).
    - Logs exact byte size + MD5 for every file.
    """
    JS_OUT.mkdir(parents=True, exist_ok=True)
    CSS_OUT.mkdir(parents=True, exist_ok=True)
    results = sync_web_assets(ROOT, force=force, log=False)
    for result in results:
        # Explicit structural log required by SRE sync contract
        print(format_sync_line(result, ROOT), flush=True)
    copied = sum(1 for r in results if r["status"] == "OK")
    skipped = sum(1 for r in results if r["status"] == "SKIP")
    print(
        f"[SYNC] _copy_web_assets done: written={copied} unchanged={skipped} total={len(results)}",
        flush=True,
    )
    return results


def inspect_replica_freshness(stale_after_sec: int = STALE_AFTER_SEC) -> dict:
    """WAL-safe freshness from SQL timestamps (not file mtime)."""
    return compute_ais_freshness(
        live_ok_lag_sec=LIVE_OK_AFTER_SEC,
        stale_after_sec=float(stale_after_sec),
    )


_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ORACLE-1001 · Sentinel · TTF Forecast</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;650;700&family=JetBrains+Mono:wght@400;600&family=Manrope:wght@400;600;700&family=Orbitron:wght@500;700&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="js/sentinel_premium.css"/>
<link rel="stylesheet" href="js/sentinel_hud.css"/>
<script>
/* Early sheet sync — before Chart.js boot (avoids FOUC + blank TTF canvases) */
(function () {
  try {
    var q = new URLSearchParams(location.search || "");
    var h = String(location.hash || "").replace(/^#/, "").toLowerCase();
    var sheet = (q.get("sheet") || "").toLowerCase();
    if (sheet === "ttf" || h === "ttf" || h === "tab-ttf-forecast") {
      document.documentElement.setAttribute("data-sheet", "ttf");
    } else if (sheet === "top10" || sheet === "qflex" || sheet === "q-flex" || h === "top10" || h === "qflex" || h === "q-flex" || h === "sheet-top10") {
      document.documentElement.setAttribute("data-sheet", "top10");
    } else if (sheet === "route" || h === "route" || h === "sheet-route") {
      document.documentElement.setAttribute("data-sheet", "route");
    } else if (sheet === "balance" || h === "balance" || h === "sheet-balance") {
      document.documentElement.setAttribute("data-sheet", "balance");
    } else if (sheet === "archive" || h === "archive" || h === "sheet-archive") {
      document.documentElement.setAttribute("data-sheet", "archive");
    } else if (sheet === "arctic" || h === "arctic" || h === "sheet-arctic") {
      document.documentElement.setAttribute("data-sheet", "arctic");
    } else if (sheet === "oracle" || h === "oracle" || h === "sheet-oracle") {
      document.documentElement.setAttribute("data-sheet", "oracle");
    } else {
      document.documentElement.setAttribute("data-sheet", "ais");
    }
  } catch (e) {
    document.documentElement.setAttribute("data-sheet", "ais");
  }
})();
</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
.stale-banner{display:none;background:linear-gradient(90deg,#7f1d1d,#991b1b);color:#fecaca;
  padding:10px 18px;font-family:var(--font-mono,monospace);font-size:12px;letter-spacing:.04em;
  border-bottom:1px solid #f87171;text-align:center}
.stale-banner.on{display:block}
.ops-pill{display:inline-flex;align-items:center;gap:6px;margin-left:10px;padding:3px 10px;
  border-radius:999px;font-size:10px;border:1px solid rgba(160,220,255,.3)}
.ops-pill.fresh{color:#10b981;border-color:#10b981}
.ops-pill.stale{color:#f87171;border-color:#f87171;animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.sheet-tabs{display:flex;gap:8px;padding:0 20px 10px;flex-wrap:wrap}
.sheet-tab{cursor:pointer;border:1px solid var(--stroke);background:rgba(0,229,255,.04);
  color:var(--muted);padding:8px 14px;border-radius:10px;font-family:var(--font-hud);
  font-size:10px;letter-spacing:.08em}
.sheet-tab.active{color:var(--cyan);border-color:var(--cyan);box-shadow:0 0 18px rgba(0,229,255,.18);
  background:rgba(0,229,255,.1)}
.sheet{display:none}
.sheet.active{display:block}
html[data-sheet="ttf"] #sheet-ais{display:none !important}
html[data-sheet="ttf"] #tab-ttf-forecast{display:block !important}
html[data-sheet="ttf"] #sheet-top10{display:none !important}
html[data-sheet="ttf"] #sheet-arctic{display:none !important}
html[data-sheet="ttf"] #sheet-route{display:none !important}
html[data-sheet="ttf"] #sheet-balance{display:none !important}
html[data-sheet="ttf"] #sheet-archive{display:none !important}
html[data-sheet="ttf"] #sheet-oracle{display:none !important}
html[data-sheet="ttf"] #kpiRow{display:none !important}
html[data-sheet="ais"] #tab-ttf-forecast{display:none !important}
html[data-sheet="ais"] #sheet-top10{display:none !important}
html[data-sheet="ais"] #sheet-arctic{display:none !important}
html[data-sheet="ais"] #sheet-route{display:none !important}
html[data-sheet="ais"] #sheet-balance{display:none !important}
html[data-sheet="ais"] #sheet-archive{display:none !important}
html[data-sheet="ais"] #sheet-oracle{display:none !important}
html[data-sheet="ais"] #sheet-ais{display:block !important}
html[data-sheet="top10"] #sheet-ais{display:none !important}
html[data-sheet="top10"] #tab-ttf-forecast{display:none !important}
html[data-sheet="top10"] #sheet-top10{display:block !important}
html[data-sheet="top10"] #sheet-arctic{display:none !important}
html[data-sheet="top10"] #sheet-route{display:none !important}
html[data-sheet="top10"] #sheet-balance{display:none !important}
html[data-sheet="top10"] #sheet-archive{display:none !important}
html[data-sheet="top10"] #sheet-oracle{display:none !important}
html[data-sheet="top10"] #kpiRow{display:none !important}
html[data-sheet="arctic"] #sheet-ais{display:none !important}
html[data-sheet="arctic"] #tab-ttf-forecast{display:none !important}
html[data-sheet="arctic"] #sheet-top10{display:none !important}
html[data-sheet="arctic"] #sheet-arctic{display:block !important}
html[data-sheet="arctic"] #sheet-route{display:none !important}
html[data-sheet="arctic"] #sheet-balance{display:none !important}
html[data-sheet="arctic"] #sheet-archive{display:none !important}
html[data-sheet="arctic"] #sheet-oracle{display:none !important}
html[data-sheet="arctic"] #kpiRow{display:none !important}
html[data-sheet="arctic"] #sheet-arctic #arctic-grid-container,
html[data-sheet="arctic"] #sheet-arctic .t10-grid{display:grid !important;grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:24px;width:100%}
html[data-sheet="arctic"] #sheet-arctic,.ark-wrap{max-width:1600px;margin-left:auto;margin-right:auto}
@media(max-width:1100px){html[data-sheet="arctic"] #sheet-arctic #arctic-grid-container{grid-template-columns:1fr !important}}
.ark-intro{margin:0 20px 16px;padding:12px 14px;border-radius:12px;border:1px solid rgba(245,158,11,.4);background:rgba(245,158,11,.08);color:#fde68a;font-size:12px;line-height:1.45}
.ark-unconfirmed{color:#fbbf24;font-size:0.85em;letter-spacing:.02em}
.ark-derived-banner{display:flex;flex-direction:column;gap:8px;margin-bottom:14px}
.ark-badge{padding:10px 12px;border-radius:10px;border:1px solid rgba(248,113,113,.45);background:rgba(127,29,29,.35);color:#fecaca;font-family:JetBrains Mono,ui-monospace,monospace;font-size:11px;letter-spacing:.03em}
.ark-badge--warn{border-color:rgba(251,191,36,.5);background:rgba(120,53,15,.35);color:#fde68a}
.ark-derived-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
@media(max-width:1100px){.ark-derived-grid{grid-template-columns:1fr}}
.ark-derived-cell{margin:0;border:1px solid rgba(255,255,255,.08);border-radius:12px;overflow:hidden;background:rgba(15,23,42,.7)}
.ark-derived-cell img{display:block;width:100%;height:auto;min-height:180px;object-fit:cover;background:#0b1220}
.ark-derived-cell figcaption{padding:8px 10px;font-size:11px;font-family:JetBrains Mono,monospace;color:#93c5fd}
.ark-note{padding:0 10px 10px;font-size:11px;color:#94a3b8}
.ark-derived-cell--missing{display:flex;flex-direction:column;min-height:220px}
.ark-missing{flex:1;display:flex;align-items:center;justify-content:center;padding:18px;text-align:center;color:#fca5a5;font-family:JetBrains Mono,monospace;font-size:12px;line-height:1.4;background:rgba(127,29,29,.25)}
.ark-video-stage{display:flex;flex-direction:column;gap:10px}
.ark-video-stage[hidden],.ark-derived-stage[hidden]{display:none !important}
html[data-sheet="route"] #sheet-ais{display:none !important}
html[data-sheet="route"] #tab-ttf-forecast{display:none !important}
html[data-sheet="route"] #sheet-top10{display:none !important}
html[data-sheet="route"] #sheet-arctic{display:none !important}
html[data-sheet="route"] #sheet-route{display:block !important}
html[data-sheet="route"] #sheet-balance{display:none !important}
html[data-sheet="route"] #sheet-archive{display:none !important}
html[data-sheet="route"] #sheet-oracle{display:none !important}
html[data-sheet="route"] #kpiRow{display:none !important}
html[data-sheet="balance"] #sheet-ais{display:none !important}
html[data-sheet="balance"] #tab-ttf-forecast{display:none !important}
html[data-sheet="balance"] #sheet-top10{display:none !important}
html[data-sheet="balance"] #sheet-arctic{display:none !important}
html[data-sheet="balance"] #sheet-route{display:none !important}
html[data-sheet="balance"] #sheet-archive{display:none !important}
html[data-sheet="balance"] #sheet-oracle{display:none !important}
html[data-sheet="balance"] #sheet-balance{display:block !important}
html[data-sheet="balance"] #kpiRow{display:none !important}
html[data-sheet="archive"] #sheet-ais{display:none !important}
html[data-sheet="archive"] #tab-ttf-forecast{display:none !important}
html[data-sheet="archive"] #sheet-top10{display:none !important}
html[data-sheet="archive"] #sheet-arctic{display:none !important}
html[data-sheet="archive"] #sheet-route{display:none !important}
html[data-sheet="archive"] #sheet-balance{display:none !important}
html[data-sheet="archive"] #sheet-oracle{display:none !important}
html[data-sheet="archive"] #sheet-archive{display:block !important}
html[data-sheet="archive"] #kpiRow{display:none !important}
html[data-sheet="oracle"] #sheet-ais{display:none !important}
html[data-sheet="oracle"] #tab-ttf-forecast{display:none !important}
html[data-sheet="oracle"] #sheet-top10{display:none !important}
html[data-sheet="oracle"] #sheet-arctic{display:none !important}
html[data-sheet="oracle"] #sheet-route{display:none !important}
html[data-sheet="oracle"] #sheet-balance{display:none !important}
html[data-sheet="oracle"] #sheet-archive{display:none !important}
html[data-sheet="oracle"] #sheet-oracle{display:block !important}
html[data-sheet="oracle"] #kpiRow{display:none !important}
/* ══ ARCHIVE SHEET — Apple Data Grid × NASA Control ══ */
#sheet-archive{display:none}
.arch-wrap{padding:0 12px 32px;width:100% !important;max-width:100% !important;margin:0 auto;box-sizing:border-box}
.arch-demo-banner{margin:0 0 14px;padding:12px 16px;border-radius:12px;border:1px solid rgba(245,158,11,.45);background:rgba(245,158,11,.08);backdrop-filter:blur(8px)}
.arch-demo-banner-title{font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:11px;font-weight:700;letter-spacing:.04em;color:#fcd34d;display:flex;align-items:center;gap:8px}
.arch-demo-banner-body{margin-top:6px;font-family:Inter,system-ui,sans-serif;font-size:12px;color:#94a3b8;line-height:1.4}
.arch-demo-banner-body b{color:#f8fafc;font-family:JetBrains Mono,monospace}
.arch-api-status{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:0 0 14px;padding:12px 14px;border-radius:14px;border:1px solid rgba(255,255,255,.08);background:rgba(15,23,42,.75);backdrop-filter:blur(12px)}
@media(max-width:1100px){.arch-api-status{grid-template-columns:repeat(2,minmax(0,1fr))}}
.arch-api-cell .lbl{font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:rgba(148,163,184,.95);margin-bottom:6px}
.arch-api-cell .val{font-family:Inter,SF Pro Display,system-ui,sans-serif;font-size:14px;font-weight:650;letter-spacing:-.02em;color:#f5f5f7;display:inline-flex;align-items:center;gap:8px}
.arch-api-cell .val .dot{width:8px;height:8px;border-radius:50%;background:#38bdf8;box-shadow:0 0 8px rgba(56,189,248,.55);flex-shrink:0}
.arch-balance-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:0 0 14px}
@media(max-width:1100px){.arch-balance-strip{grid-template-columns:repeat(2,minmax(0,1fr))}}
.arch-bal-card{padding:12px 14px;border-radius:14px;border:1px solid rgba(255,255,255,.08);background:linear-gradient(165deg,rgba(15,23,42,.88),rgba(2,6,23,.72));backdrop-filter:blur(12px)}
.arch-bal-card .lbl{font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:rgba(148,163,184,.95);margin-bottom:6px}
.arch-bal-card .val{font-family:Inter,SF Pro Display,system-ui,sans-serif;font-size:22px;font-weight:700;letter-spacing:-.03em;color:#f5f5f7;line-height:1.1}
.arch-bal-card .val .unit{font-size:12px;font-weight:500;color:rgba(148,163,184,.95);margin-left:4px}
.arch-bal-card .sub{margin-top:6px;font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:10px;letter-spacing:.04em;color:rgba(148,163,184,.85)}
.arch-bal-oil{border-color:rgba(251,146,60,.28)}
.arch-bal-oil .val{color:#fdba74}
.arch-bal-gas{border-color:rgba(56,189,248,.28)}
.arch-bal-gas .val{color:#7dd3fc}
.arch-bal-rot .val{font-size:15px;font-weight:650}
.arch-api-status.is-ok .arch-api-cell .val .dot,.arch-dot-ok{background:#32d74b !important;box-shadow:0 0 10px rgba(50,215,75,.55) !important}
.arch-api-status.is-hybrid .arch-api-cell .val .dot,.arch-dot-hybrid{background:#38bdf8 !important;box-shadow:0 0 10px rgba(56,189,248,.55) !important}
.arch-api-status.is-degraded .arch-api-cell .val .dot{background:#f87171 !important;box-shadow:0 0 10px rgba(248,113,113,.45) !important}
.arch-key-actions{display:flex;align-items:center;gap:10px;margin:0 0 12px;flex-wrap:wrap}
.arch-key-actions button{appearance:none;border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.05);color:#f5f5f7;border-radius:10px;padding:9px 14px;font:650 12px/1.2 Inter,system-ui,sans-serif;cursor:pointer}
.arch-key-actions button:hover{background:rgba(255,255,255,.1)}
.arch-key-actions .hint{font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:11px;color:rgba(148,163,184,.9)}
.arch-key-modal[hidden]{display:none !important}
.arch-key-modal{position:fixed;inset:0;z-index:10050;display:flex;align-items:center;justify-content:center;padding:20px}
.arch-key-backdrop{position:absolute;inset:0;background:rgba(5,8,15,.72);backdrop-filter:blur(10px)}
.arch-key-panel{position:relative;z-index:1;width:min(480px,94vw);border-radius:16px;border:1px solid rgba(255,255,255,.1);background:rgba(15,23,42,.92);backdrop-filter:blur(16px);padding:20px 22px 18px;box-shadow:0 24px 64px rgba(0,0,0,.5)}
.arch-key-panel h3{margin:0 0 8px;font:700 18px/1.2 Inter,SF Pro Display,system-ui,sans-serif;color:#f5f5f7;letter-spacing:-.02em}
.arch-key-panel p{margin:0 0 14px;font-size:13px;color:rgba(148,163,184,.95);line-height:1.45}
.arch-key-panel label{display:block;font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:rgba(148,163,184,.95);margin-bottom:6px}
.arch-key-panel input[type=password],.arch-key-panel input[type=text]{width:100%;box-sizing:border-box;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(8,12,20,.9);color:#e2e8f0;padding:12px 14px;font:600 14px/1.2 JetBrains Mono,SF Mono,ui-monospace,monospace;margin-bottom:12px}
.arch-key-panel .row{display:flex;gap:10px;align-items:center;justify-content:flex-end;margin-top:8px}
.arch-key-panel .row button{appearance:none;border-radius:10px;padding:10px 14px;font:650 12px/1.2 Inter,system-ui,sans-serif;cursor:pointer;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.05);color:#e2e8f0}
.arch-key-panel .row button.primary{background:#f5f5f7;color:#081018;border-color:#f5f5f7}
.arch-key-panel .row button:disabled{opacity:.4;cursor:not-allowed}
#arch-key-msg{min-height:18px;font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:11px;color:rgba(148,163,184,.95);margin-top:4px}
#arch-key-msg.is-ok{color:#86efac}
#arch-key-msg.is-err{color:#fca5a5}
.arch-toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-bottom:12px;padding:12px 14px;border-radius:14px;border:1px solid rgba(255,255,255,.08);background:rgba(15,23,42,.55)}
.arch-toolbar label{display:flex;flex-direction:column;gap:5px;font-size:10px;color:rgba(148,163,184,.95);letter-spacing:.06em;text-transform:uppercase;font-family:JetBrains Mono,SF Mono,ui-monospace,monospace}
.arch-toolbar input,.arch-toolbar select,.arch-toolbar button{
  background:rgba(8,12,20,.85);color:#e2e8f0;border:1px solid rgba(255,255,255,.1);border-radius:10px;padding:8px 10px;font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:12px;min-height:38px;min-width:100px}
.arch-toolbar button{cursor:pointer;font-weight:650}
.arch-toolbar button:hover{border-color:rgba(255,255,255,.22);background:rgba(255,255,255,.06)}
.arch-kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:12px}
.arch-kpi{border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:12px 14px;background:rgba(15,23,42,.72);backdrop-filter:blur(10px)}
.arch-kpi span{font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:rgba(148,163,184,.95)}
.arch-kpi b{display:block;font-family:Inter,SF Pro Display,system-ui,sans-serif;font-size:24px;font-weight:700;letter-spacing:-.03em;color:#f5f5f7;margin-top:4px}
/* No-scroll archive data grid */
.arch-table-wrap,.archive-table-container{
  width:100% !important;max-width:100% !important;overflow-x:hidden !important;overflow-y:hidden !important;
  border:1px solid rgba(255,255,255,.08);border-radius:14px;background:rgba(15,23,42,.55);backdrop-filter:blur(10px);padding:0}
.arch-table-wrap table,.archive-table{
  width:100% !important;table-layout:fixed !important;border-collapse:collapse;font-size:12px}
.arch-table-wrap th,.arch-table-wrap td,.archive-table th,.archive-table td{
  padding:8px 10px;height:40px;max-height:40px;border-bottom:1px solid rgba(255,255,255,.06);text-align:left;vertical-align:middle;
  font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.arch-table-wrap th,.archive-table th{position:static;background:rgba(8,12,20,.96);color:rgba(148,163,184,.98);font-size:10px;letter-spacing:.06em;text-transform:uppercase;font-weight:650}
.arch-table-wrap td,.archive-table td{color:rgba(226,232,240,.94);font-size:12px}
.arch-table-wrap tbody tr:nth-child(even),.archive-table tbody tr:nth-child(even){background:rgba(255,255,255,.025)}
.arch-table-wrap tbody tr,.archive-table tbody tr{transition:background .2s ease}
.arch-table-wrap tbody tr:hover,.archive-table tbody tr:hover{background:rgba(255,255,255,.06)}
.arch-col-imo{width:8%}.arch-col-name{width:18%}.arch-col-mmsi{width:10%}.arch-col-flag{width:8%}
.arch-col-dwt{width:9%}.arch-col-spd{width:7%}.arch-col-risk{width:8%}.arch-col-dest{width:22%}.arch-col-ais{width:10%}
.arch-pager{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:10px;flex-wrap:wrap}
.arch-pager .arch-pager-btns{display:inline-flex;gap:8px}
.arch-pager button{appearance:none;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.04);color:#e2e8f0;border-radius:8px;padding:7px 12px;font:650 12px/1.2 Inter,system-ui,sans-serif;cursor:pointer}
.arch-pager button:disabled{opacity:.35;cursor:not-allowed}
.arch-pager #arch-page-label{font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:12px;color:rgba(148,163,184,.95)}
#arch-status{color:rgba(148,163,184,.9);font-size:12px;margin-top:8px;font-family:JetBrains Mono,SF Mono,ui-monospace,monospace}
/* ══ BALANCE SHEET — Apple Refine × NASA Mission Control ══ */
#sheet-balance{display:none}
.bal-wrap{padding:0 22px 40px;max-width:1520px;margin:0 auto}
.bal-kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;
  margin-bottom:22px}
.bal-kpi-card{background:rgba(18,24,38,.65);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:14px 16px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 8px 32px rgba(0,0,0,.32)}
.bkc-label{font-family:"JetBrains Mono",monospace;font-size:9px;letter-spacing:.1em;text-transform:uppercase;
  color:rgba(245,245,247,.42);margin-bottom:6px}
.bkc-value{font-family:"Orbitron","Inter",sans-serif;font-size:22px;font-weight:700;
  color:#f5f5f7;line-height:1.15}
.bkc-sub{font-family:"JetBrains Mono",monospace;font-size:9px;color:rgba(245,245,247,.38);
  letter-spacing:.04em;margin-top:4px}
#balance-sheet-container,.bal-panels-grid{display:grid !important;grid-template-columns:repeat(auto-fit,minmax(360px,1fr)) !important;gap:20px !important;visibility:visible !important;opacity:1 !important;margin-bottom:18px;min-height:400px}
@media(max-width:420px){#balance-sheet-container,.bal-panels-grid{grid-template-columns:1fr !important}}
.bal-panel{background:rgba(18,24,38,.65);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:18px 20px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 8px 32px rgba(0,0,0,.4)}
.bal-panel:hover{border-color:rgba(0,240,255,.22)}
.bal-panel-title{font-family:"Orbitron","Inter",sans-serif;font-size:11px;font-weight:700;
  letter-spacing:.1em;text-transform:uppercase;color:#f5f5f7;margin:0 0 4px}
.bal-panel-sub{font-family:"JetBrains Mono",monospace;font-size:10px;color:rgba(245,245,247,.42);
  letter-spacing:.04em;margin-bottom:14px}
.bal-panel-inner{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}
.bal-chart-box{position:relative;height:220px}
.bal-chart-box.sm{height:160px}
.bal-chart-box.tall{height:280px}
.bal-donut-wrap{position:relative}
.bal-donut-center{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;pointer-events:none;text-align:center;
  font-family:"Orbitron","Inter",sans-serif}
.bal-gauge-bar-wrap{height:10px;border-radius:999px;background:rgba(255,255,255,.1);
  overflow:hidden;margin:8px 0 4px}
.bal-gauge-bar{height:100%;border-radius:999px;transition:width .8s cubic-bezier(.22,1,.36,1)}
.bal-gauge-label{font-family:"JetBrains Mono",monospace;font-size:9px;letter-spacing:.1em;
  text-transform:uppercase;color:rgba(245,245,247,.42);margin-bottom:4px}
.bal-gauge-value{font-family:"Orbitron","Inter",sans-serif;font-size:20px;font-weight:700;
  color:#00f0ff;margin-bottom:2px}
.bal-gauge-meta{font-family:"JetBrains Mono",monospace;font-size:9px;color:rgba(245,245,247,.38);
  letter-spacing:.04em}
.bal-sre-tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.bal-sre-tile{border:1px solid rgba(255,255,255,.07);border-radius:10px;padding:8px 10px;
  background:rgba(255,255,255,.03)}
.bst-label{font-family:"JetBrains Mono",monospace;font-size:8px;letter-spacing:.1em;
  text-transform:uppercase;color:rgba(245,245,247,.38);margin-bottom:3px}
.bst-value{font-family:"Orbitron","Inter",sans-serif;font-size:12px;font-weight:600}
/* Panel C — Table */
.bal-table-wrap{overflow-x:auto;margin-top:12px}
.bal-anomaly-table{width:100%;border-collapse:collapse;font-family:"JetBrains Mono",monospace;font-size:10px}
.bal-th{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.1);color:rgba(245,245,247,.55);
  font-size:9px;letter-spacing:.08em;text-transform:uppercase;text-align:left;white-space:nowrap}
.bal-td-rank{padding:7px 10px;color:rgba(245,245,247,.45);font-size:9px;width:36px}
.bal-td-name{padding:7px 10px;color:#f5f5f7;font-size:10px;max-width:180px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.bal-td-num{padding:7px 10px;text-align:right;font-size:10px}
.bal-td-flag{padding:7px 10px}
.bal-td-score{padding:7px 10px;text-align:right;font-weight:700;font-size:11px}
.bal-table-row{border-bottom:1px solid rgba(255,255,255,.05);transition:background .2s}
.bal-table-row:hover{background:rgba(0,240,255,.04)}
.bal-risk-badge{font-size:9px;letter-spacing:.06em;padding:2px 7px;border-radius:5px;border:1px solid transparent}
.bal-tier-badge{font-size:9px;letter-spacing:.06em;font-weight:700}
.bal-flag-tag{font-size:9px;letter-spacing:.06em;padding:2px 6px;border-radius:4px;margin-right:3px;
  background:rgba(255,255,255,.07)}
/* Panel D */
.bal-lssi-row{display:flex;justify-content:space-between;align-items:center;
  padding:6px 0;border-bottom:1px solid rgba(255,255,255,.06)}
.bal-lssi-label{font-family:"JetBrains Mono",monospace;font-size:9px;letter-spacing:.08em;
  text-transform:uppercase;color:rgba(245,245,247,.42)}
.bal-lssi-value{font-family:"Orbitron","Inter",sans-serif;font-size:13px;font-weight:600}
/* Panel E — Quant Models */
.bal-qp-card{border:1px solid rgba(255,255,255,.09);border-radius:14px;padding:14px 16px;
  background:rgba(255,255,255,.03);transition:border-color .2s}
.bal-qp-card:hover{border-color:rgba(0,240,255,.25)}
.bal-qp-card-title{font-family:"JetBrains Mono",monospace;font-size:9px;letter-spacing:.12em;
  text-transform:uppercase;color:rgba(245,245,247,.45);margin-bottom:8px;
  display:flex;align-items:center;gap:6px}
.bal-qp-icon{font-size:13px;opacity:.8}
.bal-qp-value{font-family:"Orbitron","Inter",sans-serif;font-size:20px;font-weight:700;
  color:#f5f5f7;margin-bottom:4px}
.bal-qp-rows{margin-top:8px}
.bal-qp-row{display:flex;justify-content:space-between;padding:4px 0;
  border-bottom:1px solid rgba(255,255,255,.05);font-family:"JetBrains Mono",monospace;font-size:10px}
.bal-qp-key{color:rgba(245,245,247,.42);letter-spacing:.04em}
.bal-qp-val{font-weight:600}
.sheet-tab[aria-selected="true"]{color:var(--cyan);border-color:var(--cyan);box-shadow:0 0 18px rgba(0,229,255,.18);
  background:rgba(0,229,255,.1)}
.ttf-glass{background:linear-gradient(145deg,rgba(8,18,32,.92),rgba(12,28,48,.78));
  border:1px solid rgba(0,229,255,.28);box-shadow:inset 0 0 40px rgba(0,229,255,.04),0 10px 40px rgba(0,0,0,.35);
  backdrop-filter:blur(16px);border-radius:16px}
/* ── Q-Flex Digital Twin Fleet (Apple × NASA · 2-col) ── */
.t10-wrap{padding:0 24px 48px;width:100%;max-width:1600px;margin:0 auto;box-sizing:border-box}
.t10-intro{margin:0 0 16px;color:rgba(245,245,247,.55);font-size:14px;max-width:720px;line-height:1.5}
#t10Grid,#top10-grid-container,.t10-grid,.top10-grid,.qflex-grid{
  display:grid !important;grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:24px;width:100%;
  visibility:visible;opacity:1;position:relative;z-index:2;isolation:isolate;min-height:320px;align-items:stretch;box-sizing:border-box;margin-top:8px}
html[data-sheet="top10"] #sheet-top10 #t10Grid,
html[data-sheet="top10"] #sheet-top10 #top10-grid-container,
html[data-sheet="top10"] #sheet-top10 .t10-grid{display:grid !important;grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:24px;width:100%}
html[data-sheet="top10"] #sheet-top10,html[data-sheet="top10"] #sheet-top10 .t10-wrap{max-width:1600px;margin-left:auto;margin-right:auto}
@media(max-width:1024px){
  #t10Grid,#top10-grid-container,.t10-grid,.top10-grid,.qflex-grid,
  html[data-sheet="top10"] #sheet-top10 #t10Grid,
  html[data-sheet="top10"] #sheet-top10 #top10-grid-container{grid-template-columns:1fr !important}
}
body.t10-modal-open #t10Grid,body.t10-modal-open #top10-grid-container,body.t10-modal-open .t10-card{
  visibility:visible !important;opacity:1 !important}
body.t10-modal-open #t10Grid,body.t10-modal-open #top10-grid-container{display:grid !important}
body.t10-modal-open #t10-shared-webgl{visibility:hidden !important;opacity:0 !important;pointer-events:none !important}
.t10-card{background:rgba(15,23,42,.75);border:1px solid rgba(255,255,255,.08);border-radius:16px;overflow:hidden;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 18px 40px rgba(0,0,0,.35);
  backdrop-filter:blur(12px) saturate(1.15);-webkit-backdrop-filter:blur(12px) saturate(1.15);display:flex;flex-direction:column}
.t10-card:hover{border-color:rgba(255,255,255,.14);box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 22px 48px rgba(0,0,0,.42);transform:translateY(-3px)}
.t10-media,.t10-viewport{position:relative;width:100%;aspect-ratio:16/9;min-height:320px;background:#0b1220;overflow:hidden;border-radius:12px 12px 0 0;cursor:pointer}
@supports not (aspect-ratio:16/9){.t10-media,.t10-viewport{height:320px;min-height:320px}}
.t10-media img,.t10-media .t10-photo-fallback,.t10-viewport .t10-photo-fallback{position:absolute !important;inset:0 !important;width:100% !important;height:100% !important;object-fit:cover !important;z-index:1 !important;display:block !important;opacity:1 !important;background:#07090e !important}
.t10-canvas{display:none}
.t10-vp-hint{display:none}
.t10-media-fade{position:absolute;inset:auto 0 0 0;height:46%;z-index:2;pointer-events:none;background:linear-gradient(180deg,transparent,rgba(8,12,20,.88))}
.t10-seg{position:absolute;left:14px;bottom:14px;z-index:3;display:inline-flex;padding:3px;border-radius:10px;background:rgba(8,12,20,.72);border:1px solid rgba(255,255,255,.1);backdrop-filter:blur(10px);gap:2px}
.t10-seg-btn{appearance:none;border:0;cursor:pointer;border-radius:8px;padding:8px 14px;font:600 12px/1.2 Inter,SF Pro Display,system-ui,sans-serif;letter-spacing:-.01em;color:rgba(245,245,247,.72);background:transparent}
.t10-seg-btn.is-primary{color:#081018;background:#f5f5f7}
.t10-seg-btn:disabled{opacity:.35;cursor:not-allowed}
.t10-card-head{display:flex;flex-direction:column;gap:10px;padding:18px 18px 8px}
.t10-title-row{display:flex;align-items:baseline;gap:12px;min-width:0}
.t10-rank{font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:13px;font-weight:600;color:rgba(148,163,184,.95);letter-spacing:.04em;flex-shrink:0}
.t10-name{margin:0;font-family:Inter,SF Pro Display,system-ui,sans-serif;font-size:28px;font-weight:700;letter-spacing:-.03em;color:#f5f5f7;line-height:1.1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t10-meta-row{display:flex;flex-wrap:wrap;gap:8px}
.t10-pill{display:inline-flex;align-items:center;padding:5px 10px;border-radius:980px;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04);font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:10px;font-weight:600;letter-spacing:.04em;color:rgba(226,232,240,.9)}
.t10-pill--ok{border-color:rgba(50,215,75,.35);color:#86efac;background:rgba(50,215,75,.1)}
.t10-pill--signal{border-color:rgba(0,240,255,.28);color:#67e8f9;background:rgba(0,240,255,.08)}
.t10-pill--risk-mid{border-color:rgba(251,191,36,.35);color:#fde68a;background:rgba(251,191,36,.1)}
.t10-pill--risk-hi{border-color:rgba(255,59,48,.4);color:#fca5a5;background:rgba(255,59,48,.1)}
.t10-telem{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;padding:8px 18px 14px}
@media(max-width:720px){.t10-telem{grid-template-columns:repeat(2,minmax(0,1fr))}.t10-name{font-size:22px}}
.t10-telem-cell{padding:12px 12px 10px;border-radius:12px;border:1px solid rgba(255,255,255,.06);background:rgba(8,12,20,.45)}
.t10-telem-cell .lbl{font-family:JetBrains Mono,SF Mono,ui-monospace,monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:rgba(148,163,184,.9);margin-bottom:6px}
.t10-telem-cell .val{font-family:Inter,SF Pro Display,system-ui,sans-serif;font-size:26px;font-weight:700;letter-spacing:-.03em;color:#f5f5f7;line-height:1}
.t10-telem-cell .val span{font-size:13px;font-weight:600;color:rgba(148,163,184,.95);margin-left:4px}
.t10-card-foot{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:4px 18px 18px;margin-top:auto}
.t10-foot-btn,.t10-ref-btn{appearance:none;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04);color:#e2e8f0;border-radius:10px;padding:9px 14px;font:600 12px/1.2 Inter,system-ui,sans-serif;cursor:pointer}
.t10-foot-btn:hover,.t10-ref-btn:hover{background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.16)}
.t10-actions,.t10-metrics,.t10-badge,.t10-flag,.t10-id,.t10-imo{display:none !important}
.t10-risk{font-family:JetBrains Mono,monospace;font-size:10px;letter-spacing:.05em;padding:2px 7px;border-radius:6px}
.t10-risk.risk-lo{color:#34d399;background:rgba(16,185,129,.12)}
.t10-risk.risk-mid{color:#fbbf24;background:rgba(245,158,11,.12)}
.t10-risk.risk-hi{color:#f87171;background:rgba(239,68,68,.12)}
#t10-shared-webgl{pointer-events:none;background:transparent !important;z-index:5}
.sentinel-modal-overlay,.t10-modal{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center}
.sentinel-modal-overlay[hidden],.t10-modal[hidden]{display:none !important}
.sentinel-modal-overlay .t10-modal-backdrop,.t10-modal-backdrop{
  position:absolute;inset:0;background:rgba(5,8,15,.75);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
.t10-modal-panel{position:relative;z-index:1;width:min(1180px,96vw);max-height:94vh;overflow:auto;
  background:linear-gradient(160deg,rgba(10,18,32,.96),rgba(8,14,24,.94));
  border:1px solid rgba(0,229,255,.35);border-radius:16px;padding:16px 18px 20px;
  box-shadow:0 24px 80px rgba(0,0,0,.55)}
.t10-modal-panel header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.t10-modal-panel h3{margin:0;font-family:var(--font-hud);font-size:13px;letter-spacing:.06em;color:#e2e8f0}
.t10-modal-x{cursor:pointer;border:0;background:transparent;color:#94a3b8;font-size:22px;line-height:1}
.t10-modal-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
@media(max-width:720px){.t10-modal-grid{grid-template-columns:1fr}}
.t10-ref-fig{margin:0;border:1px solid rgba(0,229,255,.2);border-radius:12px;overflow:hidden;position:relative;min-height:0;background:radial-gradient(ellipse at 50% 20%,rgba(0,240,255,.08),transparent 55%),linear-gradient(160deg,#0c121c,#07090e 55%,#0a1520)}.t10-ref-unavailable{position:absolute;inset:0 0 36px 0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;padding:12px;text-align:center;background:repeating-linear-gradient(-45deg,rgba(0,240,255,.03) 0 8px,transparent 8px 16px),rgba(10,16,24,.96);z-index:2}.t10-ref-unavailable[hidden]{display:none!important}.t10-ref-unavailable .ua-title{font-family:var(--font-mono);font-size:10px;letter-spacing:.08em;color:#00f0ff;text-transform:uppercase}.t10-ref-unavailable .ua-sub{font-family:var(--font-mono);font-size:9px;color:#94a3b8}.t10-ref-fig img.is-broken{display:none!important}
.t10-ref-fig img{display:block;width:100%;height:auto;object-fit:contain;object-position:center}
.t10-ref-fig figcaption{padding:8px 10px;font-family:var(--font-mono);font-size:10px;color:#94a3b8}
body.t10-modal-open{overflow:hidden}
/* ── ROUTE Analytics ── */
.route-wrap{padding:0 16px 24px}
.route-sre{display:none;margin:0 0 12px;padding:10px 14px;border-radius:10px;border:1px solid #f87171;
  background:rgba(127,29,29,.9);color:#fecaca;font-family:var(--font-hud);font-size:11px}
.route-sre.on{display:block}
.route-toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px;
  padding:10px 12px;border-radius:12px;border:1px solid rgba(212,175,55,.28);
  background:linear-gradient(145deg,rgba(11,14,20,.95),rgba(18,22,32,.88))}
.route-toolbar select{background:#0b0e14;color:#e2e8f0;border:1px solid rgba(0,229,255,.3);
  border-radius:8px;padding:8px 10px;font-family:var(--font-mono);font-size:11px;min-width:220px}
.route-hz{display:inline-flex;gap:4px}
.route-hz button{cursor:pointer;border:1px solid rgba(212,175,55,.35);background:rgba(212,175,55,.06);
  color:#c4b59a;font-family:var(--font-hud);font-size:10px;letter-spacing:.08em;padding:8px 12px;border-radius:8px}
.route-hz button.active{color:#0b0e14;background:linear-gradient(90deg,#d4af37,#f0d78c);border-color:#d4af37}
.route-toggles{display:inline-flex;gap:12px;font-family:var(--font-mono);font-size:10px;color:#94a3b8}
.route-toggles label{display:inline-flex;align-items:center;gap:5px;cursor:pointer}
.route-export{margin-left:auto;display:inline-flex;gap:6px}
.route-export button{cursor:pointer;border:1px solid rgba(0,229,255,.35);background:rgba(0,229,255,.08);
  color:var(--cyan);font-family:var(--font-hud);font-size:10px;letter-spacing:.06em;padding:8px 12px;border-radius:8px}
.route-kpi{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:12px}
@media(max-width:900px){.route-kpi{grid-template-columns:repeat(2,minmax(0,1fr))}}
.route-kpi .rk{padding:12px 14px;border-radius:12px;border:1px solid rgba(212,175,55,.25);
  background:linear-gradient(160deg,rgba(11,14,20,.92),rgba(20,26,36,.8))}
.route-kpi .rk .l{font-family:var(--font-hud);font-size:9px;letter-spacing:.08em;color:#94a3b8}
.route-kpi .rk .v{font-family:var(--font-hud);font-size:20px;color:#d4af37;margin-top:4px}
.route-layout{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(0,1fr);gap:12px}
@media(max-width:1200px){.route-layout{grid-template-columns:1fr}}
.route-map-panel{border:1px solid rgba(212,175,55,.28);border-radius:14px;overflow:hidden;
  background:#0b0e14;min-height:520px;display:flex;flex-direction:column}
.route-map{flex:1;min-height:460px;background:#0b0e14}
.route-map-foot{display:flex;align-items:center;gap:12px;padding:8px 12px;
  border-top:1px solid rgba(212,175,55,.2);font-family:var(--font-mono);font-size:10px;color:#94a3b8}
.route-map-foot input[type=range]{flex:1;accent-color:#d4af37}
.route-panels{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
@media(max-width:900px){.route-panels{grid-template-columns:1fr 1fr}}
.route-panel{border:1px solid rgba(0,229,255,.2);border-radius:12px;padding:8px 10px 6px;
  background:linear-gradient(155deg,rgba(11,14,20,.94),rgba(16,22,34,.85));min-height:168px}
.route-panel h4{margin:0 0 4px;font-family:var(--font-hud);font-size:9px;letter-spacing:.06em;color:#e2e8f0}
.route-panel .sub{font-size:9px;color:#64748b;margin-bottom:4px}
.route-panel .chart-box{position:relative;height:120px}
.route-panel .chart-box.donut{height:120px}
.route-draft-alert span{display:inline-block;background:rgba(245,158,11,.9);color:#0b0e14;
  font-family:Orbitron,sans-serif;font-size:9px;padding:2px 6px;border-radius:4px;white-space:nowrap}
.route-eta-wrap{position:relative}
.route-eta-center{position:absolute;inset:38% 0 auto;text-align:center;font-family:var(--font-hud);
  font-size:16px;color:#d4af37;pointer-events:none}
.ttf-kpi .v{font-size:20px}
.ttf-badge{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:10px;
  border:1px solid rgba(245,158,11,.45);color:#fbbf24;font-family:var(--font-hud);font-size:11px;
  letter-spacing:.06em;background:rgba(245,158,11,.08)}
.ttf-badge.ok{border-color:rgba(16,185,129,.5);color:#34d399;background:rgba(16,185,129,.08)}
.chart-box{position:relative;height:280px}
.chart-box.tall{height:320px}
.markov-heat{width:100%;border-collapse:collapse;font-family:var(--font-mono);font-size:11px;margin-top:8px}
.markov-heat th,.markov-heat td{border:1px solid rgba(120,180,220,.2);padding:8px;text-align:center}
.markov-heat th{color:var(--muted);font-weight:500}
.donut-wrap{position:relative}
.donut-center{position:absolute;inset:42% 18% auto;text-align:center;pointer-events:none;
  font-family:var(--font-hud);font-size:11px;letter-spacing:.04em;color:var(--cyan);
  line-height:1.35;text-shadow:0 0 18px rgba(0,229,255,.35)}
.hedge-matrix{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:6px}
@media(max-width:1100px){.hedge-matrix{grid-template-columns:1fr}}
.hedge-card{border:1px solid rgba(0,229,255,.22);border-radius:12px;padding:10px 12px;
  background:linear-gradient(160deg,rgba(0,229,255,.06),rgba(8,16,28,.4))}
.hedge-card h4{font-family:var(--font-hud);font-size:11px;letter-spacing:.06em;color:#e2e8f0;margin:0 0 6px}
.hedge-card .tier{font-size:10px;color:#fbbf24;margin-bottom:8px}
.hedge-card .metrics{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-family:var(--font-mono);font-size:10px;margin-bottom:8px}
.hedge-card .metrics span{color:var(--muted)}
.hedge-card .metrics b{color:#e2e8f0;font-weight:600}
.hedge-card ul{margin:0;padding-left:14px;font-size:10px;color:var(--muted);line-height:1.45}
.hedge-card.rec{border-color:rgba(0,229,255,.55);box-shadow:0 0 24px rgba(0,229,255,.12)}
.ttf-sre-banner{display:none;margin:0 0 14px;padding:12px 16px;border-radius:12px;
  border:1px solid #f87171;background:linear-gradient(90deg,rgba(127,29,29,.95),rgba(69,10,10,.9));
  color:#fecaca;font-family:var(--font-hud);font-size:11px;letter-spacing:.06em;line-height:1.4}
.ttf-sre-banner.on{display:block}
.fleet-sample-banner{display:none;margin:0 0 12px;padding:10px 16px;border-radius:10px;
  border:1px solid rgba(245,158,11,.55);background:linear-gradient(90deg,rgba(120,53,15,.92),rgba(69,26,3,.88));
  color:#fde68a;font-family:var(--font-hud);font-size:11px;letter-spacing:.05em;line-height:1.45;
  position:relative;z-index:40}
.fleet-sample-banner.on{display:block}
.fleet-sample-banner strong{color:#fbbf24;font-weight:700}
.ttf-skel{min-height:220px;display:flex;align-items:center;justify-content:center;
  border:1px dashed rgba(248,113,113,.45);border-radius:12px;color:#fca5a5;
  font-family:var(--font-hud);font-size:11px;letter-spacing:.05em;text-align:center;padding:16px;
  background:rgba(127,29,29,.15)}
.ttf-badge.err{border-color:rgba(248,113,113,.55);color:#fca5a5;background:rgba(127,29,29,.2)}
</style>
</head>
<body>
<div id="staleBanner" class="stale-banner"></div>
<div id="fleetSampleBanner" class="fleet-sample-banner" role="status" aria-live="polite"></div>
<header class="nav">
  <a class="brand" href="mission_control.html">ORACLE-1001 · SENTINEL</a>
  <span id="opsStatus" class="ops-pill">OPS —</span>
  <div class="nav-links">
    <a class="pill" href="dashboard.html">Fleet DB</a>
    <a class="pill" href="top500_analytics.html">ТОП-500</a>
    <a class="pill active" href="sentinel_dashboard.html">Sentinel 18</a>
    <a class="pill" href="mission_control.html">Mission Control</a>
  </div>
</header>

<section class="hero">
  <h1 id="heroTitle">SENTINEL LIVE AIS · 18 INFOGRAPHICS</h1>
  <p id="heroSub">Strategic fleet Alpha–Delta · AISStream ingestion · Real-time kinetics & pipeline health</p>
</section>

<nav class="sheet-tabs" id="sheetTabs" role="tablist">
  <button type="button" class="sheet-tab active" data-sheet="ais" role="tab" aria-selected="true">AIS · 18 CHARTS</button>
  <button type="button" class="sheet-tab" data-sheet="ttf" role="tab" aria-selected="false">ПРОГНОЗ TTF / MARKET FORECAST</button>
  <button type="button" class="sheet-tab" data-sheet="qflex" role="tab" aria-selected="false">Q-Flex</button>
  <button type="button" class="sheet-tab" data-sheet="arctic" role="tab" aria-selected="false">ARCTIC</button>
  <button type="button" class="sheet-tab" data-sheet="route" role="tab" aria-selected="false">МАРШРУТ</button>
  <button type="button" class="sheet-tab" data-sheet="balance" role="tab" aria-selected="false">БАЛАНС / TOP-500 BALANCE</button>
  <button type="button" class="sheet-tab" data-sheet="archive" role="tab" aria-selected="false">ARCHIVE · DAILY SNAPSHOTS</button>
  <button type="button" class="sheet-tab" data-sheet="oracle" role="tab" aria-selected="false">Oracle Engine</button>
</nav>
<script>
(function () {
  var sheet = document.documentElement.getAttribute("data-sheet") || "ais";
  document.querySelectorAll(".sheet-tab").forEach(function (btn) {
    var ds = btn.getAttribute("data-sheet");
    var on = ds === sheet || (sheet === "top10" && (ds === "qflex" || ds === "q-flex" || ds === "top10"));
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  var ais     = document.getElementById("sheet-ais");
  var ttf     = document.getElementById("tab-ttf-forecast");
  var top10   = document.getElementById("sheet-top10");
  var arctic  = document.getElementById("sheet-arctic");
  var route   = document.getElementById("sheet-route");
  var balance = document.getElementById("sheet-balance");
  var archive = document.getElementById("sheet-archive");
  var oracle  = document.getElementById("sheet-oracle");
  var kpi     = document.getElementById("kpiRow");
  if (ais)     { ais.classList.toggle("active", sheet === "ais");         ais.style.display     = sheet === "ais"     ? "block" : "none"; }
  if (ttf)     { ttf.classList.toggle("active", sheet === "ttf");         ttf.style.display     = sheet === "ttf"     ? "block" : "none"; }
  if (top10)   { top10.classList.toggle("active", sheet === "top10");     top10.style.display   = sheet === "top10"   ? "block" : "none"; }
  if (arctic)  { arctic.classList.toggle("active", sheet === "arctic");   arctic.style.display  = sheet === "arctic"  ? "block" : "none"; }
  if (route)   { route.classList.toggle("active", sheet === "route");     route.style.display   = sheet === "route"   ? "block" : "none"; }
  if (balance) { balance.classList.toggle("active", sheet === "balance"); balance.style.display = sheet === "balance" ? "block" : "none"; }
  if (archive) { archive.classList.toggle("active", sheet === "archive"); archive.style.display = sheet === "archive" ? "block" : "none"; }
  if (oracle)  { oracle.classList.toggle("active", sheet === "oracle");   oracle.style.display  = sheet === "oracle"  ? "block" : "none"; }
  if (kpi && (sheet === "ttf" || sheet === "top10" || sheet === "arctic" || sheet === "route" || sheet === "balance" || sheet === "archive" || sheet === "oracle")) kpi.style.display = "none";
  var ht = document.getElementById("heroTitle");
  if (sheet === "ttf"     && ht) ht.textContent = "ПРОГНОЗ TTF · MARKET FORECAST ENSEMBLE";
  if (sheet === "top10"   && ht) ht.textContent = "Q-FLEX DIGITAL TWIN & VIDEO FLEET · REAL VIDEO";
  if (sheet === "arctic"  && ht) ht.textContent = "ARCTIC · Arc7 YAMALMAX · AI-GENERATED FLIGHT VIDEO";
  if (sheet === "route"   && ht) ht.textContent = "МАРШРУТ · ROUTE ANALYTICS · SPATIOTEMPORAL";
  if (sheet === "balance" && ht) ht.textContent = "БАЛАНС · TOP-500 FLEET BALANCE · 6 QUANT METRICS";
  if (sheet === "archive" && ht) ht.textContent = "ARCHIVE · VESSEL DAILY SNAPSHOTS · 1253 FLEET";
  if (sheet === "oracle"  && ht) ht.textContent = "ORACLE ENGINE · CONTROL & FORECAST";
})();
</script>

<section class="kpi-row" id="kpiRow"></section>

<div id="sheet-ais" class="sheet active">
<main class="grid">
  <section class="panel span2" id="p01"><h3>1 · Interactive Global Fleet Heatmap</h3><div class="sub">Tier-coded live positions (Alpha / Bravo / Charlie / Delta)</div><div id="map01" class="map"></div></section>
  <section class="panel"><h3>2 · Chokepoint Traffic Density</h3><div class="sub">Malacca · Suez · Bab-el-Mandeb · Bosphorus · Danish · Hormuz</div><canvas id="c02"></canvas></section>
  <section class="panel"><h3>3 · Shadow Fleet STS Clusters</h3><div class="sub">&lt; 0.5 nm · SOG &lt; 1.0 kn · duration &gt; 30 min · TOP-500</div><div id="stsList" class="list"></div></section>
  <section class="panel"><h3>4 · Alpha Route Corridor</h3><div class="sub">Tier-Alpha tankers · track heads + COG</div><div id="map04" class="map sm"></div></section>
  <section class="panel"><h3>5 · Port Arrival / Berth Velocity</h3><div class="sub">Anchor vs berth time proxy</div><canvas id="c05"></canvas></section>
  <section class="panel"><h3>6 · AIS Dark Activity Timeline</h3><div class="sub">Gaps &gt; 4h near Malacca · Bab-el-Mandeb · Danish · Suez</div><div id="darkList" class="list"></div></section>
  <section class="panel"><h3>7 · Draft vs Cargo Load Delta</h3><div class="sub">Load % estimator · draft × design draft</div><canvas id="c07"></canvas></section>
  <section class="panel"><h3>8 · Speed Profile (SOG Spectrum)</h3><div class="sub">0-2 · 2-8 · 8-12 · 12-16 · 16-20 · 20+</div><canvas id="c08"></canvas></section>
  <section class="panel"><h3>9 · Course Deviation Gauge</h3><div class="sub">Δθ &gt; 35° vs heading · underway</div><canvas id="c09"></canvas></section>
  <section class="panel"><h3>10 · Spoofing / GNSS Radar</h3><div class="sub">Jump · velocity spike · heading incoherence</div><canvas id="c10"></canvas></section>
  <section class="panel span2"><h3>11 · Tanker Tonnage in Transit</h3><div class="sub">Floating storage vs steaming DWT</div><canvas id="c11"></canvas></section>
  <section class="panel"><h3>12 · Flag State Dispersion</h3><div class="sub">FOC / ownership DWT treemap bars</div><canvas id="c12"></canvas></section>
  <section class="panel"><h3>13 · Risk & Tier Compliance Radar</h3><div class="sub">Multi-variable spider</div><canvas id="c13"></canvas></section>
  <section class="panel span2"><h3>14 · Geopolitical Exposure Matrix</h3><div class="sub">High-risk zones by tier</div><canvas id="c14"></canvas></section>
  <section class="panel"><h3>15 · Ingestion Throughput (MPS)</h3><div class="sub">AIS messages / second</div><canvas id="c15"></canvas></section>
  <section class="panel"><h3>16 · DB Write Latency</h3><div class="sub">Async insert ms</div><canvas id="c16"></canvas></section>
  <section class="panel"><h3>17 · WebSocket Health</h3><div class="sub">Uptime · reconnects · heartbeat</div><div id="wsHealth" class="health"></div></section>
  <section class="panel"><h3>18 · Matching Efficiency</h3><div class="sub">Registry hits vs background</div><canvas id="c18"></canvas></section>
</main>
</div>

<div id="tab-ttf-forecast" class="sheet">
  <div id="ttfSreBanner" class="ttf-sre-banner" role="alert"></div>
  <section class="kpi-row ttf-kpi" id="ttfKpiRow"></section>
  <main class="grid">
    <section class="panel span2 ttf-glass">
      <h3>B · TTF Price · Ensemble Forecast Cone</h3>
      <div class="sub">History + 30d P10–P50–P90 fan · CatBoost × Markov × Spectral × Elliott</div>
      <div class="chart-box tall"><canvas id="ttfCone"></canvas></div>
    </section>
    <section class="panel ttf-glass">
      <h3>C · Probabilistic KDE</h3>
      <div class="sub">Gaussian KDE · horizons 7 / 14 / 30d</div>
      <div id="ttfOptBadge" class="ttf-badge" style="margin-bottom:8px">OPT RANGE —</div>
      <div class="chart-box"><canvas id="ttfKde"></canvas></div>
    </section>
    <section class="panel ttf-glass">
      <h3>D · Granger Causal Lags</h3>
      <div class="sub">AIS Shadow Density → TTF · −log₁₀(p)</div>
      <div class="chart-box"><canvas id="ttfGranger"></canvas></div>
    </section>
    <section class="panel ttf-glass">
      <h3>D · Elliott Wave Structure</h3>
      <div class="sub">Impulse 1–5 / Corrective A–C · Fibonacci 0.618 / 1.618</div>
      <div id="ttfElliottBadge" class="ttf-badge ok" style="margin:12px 0">WAVE —</div>
      <div id="ttfElliottMeta" class="list"></div>
    </section>
    <section class="panel ttf-glass">
      <h3>E · CatBoost Feature Importance</h3>
      <div class="sub">Top-10 · Quantile P50 @ 7d</div>
      <div class="chart-box"><canvas id="ttfImportance"></canvas></div>
    </section>
    <section class="panel span2 ttf-glass">
      <h3>E · Markov State Transition Heatmap</h3>
      <div class="sub">P<sub>ij</sub> · LowVol / Transit / HighVol Supply Shock</div>
      <div id="ttfMarkovState" class="ttf-badge" style="margin-bottom:8px">STATE —</div>
      <table class="markov-heat" id="ttfMarkovHeat"><thead></thead><tbody></tbody></table>
    </section>
    <section class="panel ttf-glass">
      <h3>G · Allocation &amp; Yield Profile ($1,000)</h3>
      <div class="sub">Donut book · Cash / Futures / Options Collar / AIS Spread · 30d ROI</div>
      <div class="donut-wrap">
        <div class="chart-box"><canvas id="ttfAllocDonut"></canvas></div>
        <div id="ttfAllocCenter" class="donut-center">$1,000 → —</div>
      </div>
      <div id="ttfAllocMeta" class="list" style="margin-top:8px"></div>
    </section>
    <section class="panel span2 ttf-glass">
      <h3>H · 3 Optimal Energy Desk Hedging Strategies</h3>
      <div class="sub">A Collar · B AIS-Gated Futures · C Convexity Vol · VaR 95% / Sharpe / Rules</div>
      <div id="ttfLivePaperBadge" class="ttf-badge ok" style="margin-bottom:10px">LIVE PAPER P&amp;L —</div>
      <div id="ttfHedgeMatrix" class="hedge-matrix"></div>
    </section>
    <section class="panel span2 ttf-glass">
      <h3>I · 30-Day Strategy Equity Curves</h3>
      <div class="sub">Cumulative PnL ($) · Strategies A/B/C vs $1,000 benchmark</div>
      <div class="chart-box tall"><canvas id="ttfEquityCurves"></canvas></div>
    </section>
  </main>
</div>

<div id="sheet-top10" class="sheet">
  <div class="t10-wrap">
    <p class="t10-intro">
      Q-Flex Digital Twin & Video Fleet — REAL VIDEO flight loops (default inspector tab) · DIGITAL TWIN GLB · orthographic OSINT.
    </p>
    <div class="t10-grid" id="top10-grid-container" data-legacy-id="t10Grid" aria-live="polite"></div>
  </div>
</div>

<div id="sheet-arctic" class="sheet">
  <div class="ark-wrap t10-wrap">
    <p class="ark-intro t10-intro"></p>
    <div class="t10-grid" id="arctic-grid-container" aria-live="polite"></div>
  </div>
</div>

<div id="sheet-oracle" class="sheet">
  <div id="oracle-sheet-mount" aria-live="polite"></div>
</div>

<div id="sheet-route" class="sheet">
  <div class="route-wrap">
    <div id="routeSreBanner" class="route-sre" role="alert"></div>
    <div class="route-toolbar">
      <select id="routeVesselSelect" aria-label="Vessel or group"></select>
      <div class="route-hz" role="group" aria-label="Time horizon">
        <button type="button" data-route-hz="1d">1D</button>
        <button type="button" data-route-hz="7d" class="active">7D</button>
        <button type="button" data-route-hz="30d">30D</button>
      </div>
      <div class="route-toggles">
        <label><input type="checkbox" id="routeToggleHeat" checked/> Heatmap</label>
        <label><input type="checkbox" id="routeToggleSts" checked/> STS zones</label>
      </div>
      <div class="route-export">
        <button type="button" id="routeExportBtn">EXPORT CSV</button>
        <button type="button" id="routeExportJsonBtn">EXPORT JSON</button>
      </div>
    </div>
    <div class="route-kpi">
      <div class="rk"><div class="l">CURRENT SPEED</div><div class="v" id="routeKpiSog">—</div></div>
      <div class="rk"><div class="l">TOTAL PAYLOAD CAPACITY (DWT)</div><div class="v" id="routeKpiDwt">—</div></div>
      <div class="rk"><div class="l">VOYAGE EFFICIENCY SCORE</div><div class="v" id="routeKpiEff">—</div></div>
      <div class="rk"><div class="l">ANOMALY FLAG COUNT</div><div class="v" id="routeKpiAnom">—</div></div>
    </div>
    <div class="route-layout">
      <section class="route-map-panel">
        <div id="routeMap" class="route-map" role="img" aria-label="Route map"></div>
        <div class="route-map-foot">
          <span>TIME SCRUB</span>
          <input type="range" id="routeTimeScrub" min="0" max="100" value="50"/>
          <span id="routeMeta">—</span>
        </div>
      </section>
      <section class="route-panels" id="routePanels">
        <article class="route-panel"><h4>1 · SPEED DYNAMICS</h4><div class="sub">SOG vs design service speed</div><div class="chart-box"><canvas id="routeP1"></canvas></div></article>
        <article class="route-panel"><h4>2 · DRAUGHT &amp; PAYLOAD Δ</h4><div class="sub">Loading / unloading events</div><div class="chart-box"><canvas id="routeP2"></canvas></div></article>
        <article class="route-panel"><h4>3 · TONNAGE FLOW / DWT</h4><div class="sub">Capacity vs utilization</div><div class="chart-box"><canvas id="routeP3"></canvas></div></article>
        <article class="route-panel"><h4>4 · ENGINE LOAD vs FUEL</h4><div class="sub">Kinematic power curve</div><div class="chart-box"><canvas id="routeP4"></canvas></div></article>
        <article class="route-panel"><h4>5 · AIS GAP TIMELINE</h4><div class="sub">Dark activity histogram</div><div class="chart-box"><canvas id="routeP5"></canvas></div></article>
        <article class="route-panel"><h4>6 · ROUTE ANOMALY RADAR</h4><div class="sub">Sanction · variance · STS</div><div class="chart-box"><canvas id="routeP6"></canvas></div></article>
        <article class="route-panel"><h4>7 · ETA vs PROGRESS</h4><div class="sub" id="routeEtaBadge">—</div><div class="chart-box donut route-eta-wrap"><canvas id="routeP7"></canvas><div id="routeEtaCenter" class="route-eta-center">—</div></div></article>
        <article class="route-panel"><h4>8 · WEATHER &amp; HYDRO</h4><div class="sub">Wave · wind · current</div><div class="chart-box"><canvas id="routeP8"></canvas></div></article>
        <article class="route-panel"><h4>9 · FLEET DENSITY</h4><div class="sub">Chokepoints · STS · active</div><div class="chart-box"><canvas id="routeP9"></canvas></div></article>
      </section>
    </div>
  </div>
</div>

<!-- ═══════════════════ BALANCE SHEET ═══════════════════ -->
<div id="sheet-balance" class="sheet">
  <div class="bal-wrap">

    <!-- KPI Banner -->
    <div class="bal-kpi-row" id="bal-kpi-row"></div>

    <!-- ── What-If Scenario Simulator ── -->
    <section class="bal-panel bal-whatif" id="bal-whatif-panel" data-panel="WHATIF">
      <div class="bal-panel-hdr" style="border:none;padding-bottom:0">
        <div>
          <div class="bal-panel-label">SCENARIO ENGINE</div>
          <h3 class="bal-panel-title">WHAT-IF SIMULATOR · TTF / SUPPLY</h3>
        </div>
        <span class="bal-panel-badge" id="bal-whatif-badge">BASELINE</span>
      </div>
      <div class="bal-panel-body">
        <div class="bal-whatif-grid">
          <label class="bal-slider-block">
            <span class="bal-slider-label">Strait Blockage Delay (Days)</span>
            <input type="range" id="bal-slider-blockage" min="0" max="30" step="1" value="0"/>
            <span class="bal-slider-value"><strong id="bal-val-blockage">0</strong> d</span>
          </label>
          <label class="bal-slider-block">
            <span class="bal-slider-label">European Temperature Anomaly (°C)</span>
            <input type="range" id="bal-slider-temp" min="-10" max="10" step="0.5" value="0"/>
            <span class="bal-slider-value"><strong id="bal-val-temp">0.0</strong> °C</span>
          </label>
        </div>
        <div class="bal-whatif-impact" id="bal-whatif-impact">
          Adjust sliders to stress-test LSSI elasticity &amp; volumetric capacity.
        </div>
      </div>
    </section>

    <!-- ── Panels A–D Mission Control Grid ── -->
    <div id="balance-sheet-container" class="bal-panels-grid">
      <section class="bal-panel" data-panel="A">
        <h3 class="bal-panel-title">A · TOP-500 VOLUMETRIC CAPACITY MATRIX</h3>
        <div class="bal-panel-sub">Cargo M³ in transit · Laden/Ballast distribution · Fleet utilization by tier</div>
        <div class="bal-panel-inner">
          <div>
            <div class="bal-donut-wrap">
              <div class="bal-chart-box"><canvas id="balA1"></canvas></div>
              <div class="bal-donut-center" id="balA1-center"></div>
            </div>
          </div>
          <div>
            <div class="bal-chart-box sm"><canvas id="balA2"></canvas></div>
            <div id="balA3-gauge" style="margin-top:14px"></div>
          </div>
        </div>
      </section>

      <!-- ── Panel B: Data Fidelity & SRE Trust ── -->
      <section class="bal-panel" data-panel="B">
        <h3 class="bal-panel-title">B · DATA FIDELITY &amp; SRE TRUST HUD</h3>
        <div class="bal-panel-sub">DFS circular HUD · PIL latency monitor · MPS throughput · Source mode tags</div>
        <div class="bal-panel-inner">
          <div>
            <div class="bal-donut-wrap">
              <div class="bal-chart-box" style="height:180px"><canvas id="balB1"></canvas></div>
              <div class="bal-donut-center" id="balB1-center" style="padding-top:80px"></div>
            </div>
            <div id="balB4-status" class="bal-sre-tiles" style="margin-top:10px"></div>
          </div>
          <div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:.08em;
              text-transform:uppercase;color:rgba(245,245,247,.42);margin-bottom:4px">INSERT LATENCY (ms)</div>
            <div class="bal-chart-box sm"><canvas id="balB2"></canvas></div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:.08em;
              text-transform:uppercase;color:rgba(245,245,247,.42);margin:10px 0 4px">INGESTION MPS</div>
            <div class="bal-chart-box sm"><canvas id="balB3"></canvas></div>
          </div>
        </div>
      </section>

      <!-- ── Panel C: OSINT Anomaly & STS Risk Ranking ── -->
      <section class="bal-panel" data-panel="C">
        <h3 class="bal-panel-title">C · OSINT ANOMALY &amp; STS RISK RANKING</h3>
        <div class="bal-panel-sub">Composite DAR × ΔV risk table · Top flagged vessels · STS / Anomalous halt detection</div>
        <div class="bal-panel-inner" style="margin-bottom:14px">
          <div class="bal-chart-box sm"><canvas id="balC1"></canvas></div>
          <div class="bal-chart-box sm"><canvas id="balC2"></canvas></div>
        </div>
        <div class="bal-table-wrap">
          <table class="bal-anomaly-table">
            <thead>
              <tr>
                <th class="bal-th">#</th>
                <th class="bal-th">Vessel</th>
                <th class="bal-th">Tier</th>
                <th class="bal-th">Risk</th>
                <th class="bal-th" style="text-align:right">Dark Hrs</th>
                <th class="bal-th">Flags</th>
                <th class="bal-th" style="text-align:right">Score</th>
              </tr>
            </thead>
            <tbody id="balC-table"></tbody>
          </table>
        </div>
      </section>

      <!-- ── Panel D: TTF Elasticity & Supply Curve ── -->
      <section class="bal-panel" data-panel="D">
        <h3 class="bal-panel-title">D · TTF ELASTICITY &amp; SUPPLY CURVE</h3>
        <div class="bal-panel-sub">LSSI · LNG supply volume → TTF price impact · ε=0.25 empirical · P10/P50/P90 forecast bands</div>
        <div class="bal-panel-inner">
          <div>
            <div class="bal-chart-box tall"><canvas id="balD1"></canvas></div>
          </div>
          <div>
            <div class="bal-chart-box" style="height:180px"><canvas id="balD2"></canvas></div>
            <div id="balD3-meta" style="margin-top:14px"></div>
          </div>
        </div>
      </section>

      <!-- ── Panel E: Quant Pipeline Dashboard ── -->
      <section class="bal-panel" data-panel="E">
        <h3 class="bal-panel-title">E · ADVANCED QUANT MATHEMATICAL PIPELINE</h3>
        <div class="bal-panel-sub">BOG Decay · HMM Routing · NOAA ETA · Terminal Queue · ICE Microstructure Ensemble · >80% Directional Target</div>
        <div id="balE-quant" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-top:4px"></div>
      </section>
    </div>

  </div>
</div>
<!-- ═══════════════════ /BALANCE SHEET ══════════════════ -->

<!-- ═══════════════════ ARCHIVE SHEET ═══════════════════ -->
<div id="sheet-archive" class="sheet">
  <div class="arch-wrap">
    <div class="arch-demo-banner" id="arch-demo-banner">
      <div class="arch-demo-banner-title">
        <span class="icon">ℹ</span>
        <span>ARCHIVE REGISTRY: SNAPSHOT / DEMO MODE · NOT LIVE VESSELFINDER REST · NOT SATELLITE</span>
      </div>
      <div class="arch-demo-banner-body">
        <span class="lbl">Known fleet registry:</span> <b id="arch-known-fleet-desc">1,253 known vessels (OSINT / VesselFinder hybrid snapshot)</b> &nbsp;|&nbsp;
        <span class="lbl">Live AIS tracked:</span> <b id="arch-live-g3-desc">N=— live AIS-tracked (G3 Terrestrial Ceiling)</b>.
      </div>
    </div>
    <p class="t10-intro">Immutable UTC daily freeze of the full known OSINT fleet registry (~1,253 vessels). Live tracking is provided exclusively by terrestrial G3 AISstream feed (Dual Deploy Gate). Commercial VesselFinder REST is inactive (hybrid local fallback).</p>
    <div class="arch-api-status is-hybrid" id="arch-api-status" aria-label="Vessel Tracking API status">
      <div class="arch-api-cell"><div class="lbl">API</div><div class="val" id="arch-api-plan">HYBRID LOCAL FALLBACK</div></div>
      <div class="arch-api-cell"><div class="lbl">KNOWN REGISTRY</div><div class="val" id="arch-known-fleet-kpi">1,253 vessels</div></div>
      <div class="arch-api-cell"><div class="lbl">LIVE G3 AIS</div><div class="val" id="arch-live-g3-kpi">N=— live</div></div>
      <div class="arch-api-cell"><div class="lbl">SLOT ROTATION</div><div class="val" id="arch-api-slots">500/500 (ROTATING)</div></div>
    </div>
    <div class="arch-balance-strip" id="arch-balance-strip" aria-label="Daily oil and gas transit balance">
      <div class="arch-bal-card arch-bal-oil">
        <div class="lbl">Oil in transit</div>
        <div class="val"><span id="arch-bal-oil">—</span><span class="unit"> ktons</span></div>
        <div class="sub" id="arch-bal-oil-sub">Tier 2 · tankers</div>
      </div>
      <div class="arch-bal-card arch-bal-gas">
        <div class="lbl">Gas in transit</div>
        <div class="val"><span id="arch-bal-gas">—</span><span class="unit"> ktons</span></div>
        <div class="sub" id="arch-bal-gas-sub">Tier 1 · LNG/LPG</div>
      </div>
      <div class="arch-bal-card arch-bal-rot">
        <div class="lbl">Active rotation</div>
        <div class="val" id="arch-bal-tier">—</div>
        <div class="sub" id="arch-bal-rot-sub">slot phase</div>
      </div>
      <div class="arch-bal-card arch-bal-date">
        <div class="lbl">Balance date</div>
        <div class="val" id="arch-bal-date">—</div>
        <div class="sub" id="arch-bal-sample">sample n</div>
      </div>
    </div>
    <div class="arch-key-actions">
      <button type="button" id="arch-open-key-modal">Inject Commercial Userkey</button>
      <span class="hint" id="arch-key-hint">POST /api/config/update-key · localhost</span>
    </div>
    <div class="arch-key-modal" id="arch-key-modal" hidden>
      <div class="arch-key-backdrop" id="arch-key-backdrop" aria-hidden="true"></div>
      <div class="arch-key-panel" role="dialog" aria-modal="true" aria-labelledby="arch-key-title">
        <h3 id="arch-key-title">Commercial Userkey</h3>
        <p>Paste VesselFinder commercial REST <code>userkey</code>. Valid key switches ingest to LIVE; invalid key keeps hybrid local fallback.</p>
        <label for="arch-key-input">Userkey</label>
        <input id="arch-key-input" type="password" autocomplete="off" spellcheck="false" placeholder="•••• commercial userkey"/>
        <label style="display:flex;align-items:center;gap:8px;text-transform:none;letter-spacing:0;font-size:12px;margin-bottom:10px">
          <input type="checkbox" id="arch-key-sync" checked/> Run archive force-sync after accept
        </label>
        <div id="arch-key-msg" aria-live="polite"></div>
        <div class="row">
          <button type="button" id="arch-key-cancel">Cancel</button>
          <button type="button" class="primary" id="arch-key-submit">Validate &amp; Apply</button>
        </div>
      </div>
    </div>
    <div class="arch-toolbar">
      <label>Snapshot date<select id="arch-date"></select></label>
      <label style="flex:1;min-width:180px">Timeline<input type="range" id="arch-scrub" min="0" max="0" value="0"/></label>
      <label>Search<input id="arch-q" placeholder="IMO / name / MMSI"/></label>
      <label>Flag<select id="arch-flag"><option value="">ALL FLAGS</option></select></label>
      <label>Risk<select id="arch-risk"><option value="">ALL RISK</option></select></label>
      <label>Min AIS∫<input id="arch-ain" type="number" min="0" max="1" step="0.05" value="0"/></label>
      <button type="button" id="arch-csv">Export CSV</button>
      <button type="button" id="arch-json">Export JSON</button>
      <a class="sheet-tab" id="arch-open-full" href="/output/archive_dashboard.html" target="_blank" rel="noopener">Full archive UI</a>
    </div>
    <div class="arch-kpis">
      <div class="arch-kpi"><span>Vessels</span><b id="arch-k-n">—</b></div>
      <div class="arch-kpi"><span>AIS integrity ≥0.5</span><b id="arch-k-live">—</b></div>
      <div class="arch-kpi"><span>Snapshot date</span><b id="arch-k-date">—</b></div>
    </div>
    <div class="arch-table-wrap archive-table-container">
      <table class="archive-table">
        <colgroup>
          <col class="arch-col-imo"/><col class="arch-col-name"/><col class="arch-col-mmsi"/>
          <col class="arch-col-flag"/><col class="arch-col-dwt"/><col class="arch-col-spd"/>
          <col class="arch-col-risk"/><col class="arch-col-dest"/><col class="arch-col-ais"/>
        </colgroup>
        <thead>
          <tr>
            <th>IMO</th><th>Name</th><th>MMSI</th><th>Flag</th><th>DWT</th>
            <th>Spd</th><th>Risk</th><th>Dest</th><th>AIS∫</th>
          </tr>
        </thead>
        <tbody id="arch-tbody"></tbody>
      </table>
    </div>
    <div class="arch-pager">
      <span id="arch-page-label">Page 1 / 1</span>
      <div class="arch-pager-btns">
        <button type="button" id="arch-prev" disabled>← Prev</button>
        <button type="button" id="arch-next" disabled>Next →</button>
      </div>
    </div>
    <div id="arch-status">Select ARCHIVE tab to hydrate…</div>
  </div>
</div>
<!-- ═══════════════════ /ARCHIVE SHEET ══════════════════ -->

<footer class="foot">
  <span>ORACLE-1001 / Sentinel · Dual-Plane · TTF · TOP10 · ROUTE · BALANCE · ARCHIVE</span>
  <span id="footMeta"></span>
</footer>

<script>
window.__SENTINEL_PAYLOAD__ = __PAYLOAD__;
window.SENTINEL_TTF_DATA   = (window.__SENTINEL_PAYLOAD__ && window.__SENTINEL_PAYLOAD__.ttf_forecast)    || null;
window.__ROUTE_PAYLOAD__   = (window.__SENTINEL_PAYLOAD__ && window.__SENTINEL_PAYLOAD__.route_analytics) || null;
window.__BALANCE_PAYLOAD__ = (window.__SENTINEL_PAYLOAD__ && window.__SENTINEL_PAYLOAD__.balance)         || null;
window.__QUANT_PIPELINE__  = (window.__SENTINEL_PAYLOAD__ && window.__SENTINEL_PAYLOAD__.quant_pipeline)  || null;
window.__ALERTS_PAYLOAD__  = (window.__SENTINEL_PAYLOAD__ && window.__SENTINEL_PAYLOAD__.alerts)          || null;
/* Basemap: CARTO Dark by default (no Mapbox key / no "API KEY REQUIRED").
   Override at rebuild: TILE_SERVER / TILE_SUBDOMAINS / TILE_ATTR. */
window.__SENTINEL_MAP__ = window.__SENTINEL_MAP__ || {
  tileUrl: "__TILE_URL__",
  maxZoom: 12,
  subdomains: "__TILE_SUBDOMAINS__",
  attribution: "__TILE_ATTR__"
};

/* P0 DOM Truth Contract: vessel cards MUST be constructed before any async hydration.
   TOP10 card DOM is built synchronously from TOP10_VESSELS manifest (imported as ES module).
   AIS sheet KPI skeletons render on DOMContentLoaded — no DB latency blocking. */
</script>
<script src="js/map_tiles.js?v=__ASSET_V__"></script>
<script src="js/sentinel_engine.js?v=__ASSET_V__"></script>
<script src="js/hud_state.js?v=__ASSET_V__"></script>
<script src="js/balance_engine.js?v=__ASSET_V__"></script>
<script type="importmap">
{
  "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"
  }
}
</script>
<script type="module" src="js/top10_sheet.js?v=glb-twin-v6"></script>
<script type="module" src="js/arctic_sheet.js?v=arctic-arc7-v1"></script>
<script type="module" src="js/oracle_event_bus.js?v=oracle-bus-v1"></script>
<script type="module" src="js/oracle_sheet.js?v=oracle-sheet-v1"></script>
<script type="module" src="js/route_sheet.js?v=__ASSET_V__"></script>
<script type="module" src="js/archive_sheet.js?v=archive-balance-168h-v1"></script>
</body>
</html>
"""


def write_sentinel_dashboard(path: Path | None = None) -> dict:
    from services.ttf_forecast.integrity import SREBuildError, validate_html_artifact

    out = path or OUT_HTML
    freshness = inspect_replica_freshness()
    payload = build_sentinel_payload()
    payload["replica_freshness"] = freshness
    from services.ais_health import resolve_source_mode, strip_stale_tag

    base_mode = strip_stale_tag(payload.get("source_mode") or "live_ais")
    payload["source_mode"] = resolve_source_mode(freshness, base_mode=base_mode)
    if freshness.get("live_ok"):
        payload["operational_status"] = "NOMINAL"
    elif freshness.get("stale"):
        payload["operational_status"] = "DEGRADED_STALE_REPLICA"
    else:
        payload["operational_status"] = "WARMING"

    # HARD FAIL — never emit silent empty TTF HTML
    try:
        payload["ttf_forecast"] = build_ttf_forecast_payload(refresh_ensemble=False)
    except SREBuildError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SREBuildError("TTF_PAYLOAD_BUILD", str(exc)) from exc

    if freshness.get("stale"):
        ttf = payload["ttf_forecast"]
        ttf["replica_stale"] = True
        ttf["integrity_status"] = "PASS_STALE_REPLICA"

    # ROUTE Analytics payload (aggregates + tracks)
    try:
        payload["route_analytics"] = build_route_analytics_payload()
        assert_route_payload(payload["route_analytics"])
    except SREBuildError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SREBuildError("ROUTE_PAYLOAD_BUILD", str(exc)) from exc

    # QUANT PIPELINE — 5 mathematical models (BOG, HMM, NOAA, TBI, ICE)
    try:
        payload["quant_pipeline"] = build_quant_pipeline_payload(payload)
        qp = payload["quant_pipeline"]
        print(
            f"[QUANT] direction={qp.get('direction')} "
            f"confidence={qp.get('confidence'):.2f} "
            f"acc={qp.get('ensemble_accuracy_pct')}% "
            f"elapsed={qp.get('elapsed_ms')}ms"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: quant_pipeline build failed (non-blocking): {exc}")
        payload["quant_pipeline"] = {"status": "BUILD_FAILED", "error": str(exc)}

    # BALANCE payload — TOP-500 6-metric aggregation (non-blocking, soft-fail)
    try:
        payload["balance"] = build_balance_payload(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: balance_analytics build failed (non-blocking): {exc}")
        payload["balance"] = {"status": "BUILD_FAILED", "error": str(exc)}

    # REACTIVE ALERTS — HMM destination ΔP monitor (non-blocking)
    try:
        payload["alerts"] = build_alerts_payload(payload)
        ac = int((payload["alerts"] or {}).get("alert_count") or 0)
        print(f"[ALERTS] hmm_dest monitored={payload['alerts'].get('monitored_vessels')} fired={ac}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: alerts_engine failed (non-blocking): {exc}")
        payload["alerts"] = {"status": "BUILD_FAILED", "error": str(exc), "alerts": []}

    # TOP10: orthographic assets + generated manifest (writes web/js + output/js)
    assert_reference_images_exist()
    sync_meta = sync_reference_assets(force=True)
    write_js_manifest()

    # P0: full web→output JS/CSS sync (MD5 + atomic) — kills drift / stale top10_sheet.js
    _copy_web_assets(force=False)

    route_meta = payload.get("route_analytics") or {}
    ttf_meta = payload.get("ttf_forecast") or {}
    ais_mode = str(payload.get("source_mode") or "unknown")
    if freshness.get("live_ok"):
        ais_truth = "live_ok"
    elif freshness.get("ais_truth"):
        ais_truth = str(freshness.get("ais_truth"))
    elif "synthetic" in ais_mode:
        ais_truth = "degraded"
    elif "live" in ais_mode:
        ais_truth = "live_ais"
    else:
        ais_truth = ais_mode
    route_mode = str(route_meta.get("source_mode") or "unknown")
    if "synthetic" in route_mode:
        route_truth = "synthetic"
    elif "live" in route_mode or route_mode == "live_telemetry":
        route_truth = "live_ok" if route_mode == "live_telemetry" else "live"
    else:
        route_truth = route_mode
    ttf_integrity = str(ttf_meta.get("integrity_status") or "PASS")
    ttf_truth = "ensemble" if "PASS" in ttf_integrity.upper() else "fallback"
    top10_truth = "http_assets_ok" if sync_meta.get("ok") else "assets_degraded"

    truth_contract = {
        "top10": top10_truth,
        "ais": ais_truth,
        "route": route_truth,
        "ttf": ttf_truth,
    }
    payload["truth_contract"] = truth_contract

    # Coverage + dual-gate MUST stamp payload BEFORE HTML embed (UI banner / caveats).
    try:
        from services.ais_health import compute_top500_live_coverage

        cov = compute_top500_live_coverage()
    except Exception as exc:  # noqa: BLE001
        cov = {"top500_live_coverage": 0, "ok": False, "error": str(exc)}

    from services.dual_gate import (
        compute_fleet_sample_status,
        compute_pipeline_health_status,
        live_inference_confidence,
    )

    cov_n = int(cov.get("top500_live_coverage") or 0)
    payload["top500_live_coverage"] = cov_n
    payload["top500_coverage"] = cov
    sample = compute_fleet_sample_status(
        cov_n, universe=int(cov.get("top500_universe") or 500)
    )
    payload["fleet_sample_status"] = sample["fleet_sample_status"]
    payload["sample_size_caveat"] = sample.get("sample_size_caveat")
    if isinstance(payload.get("balance"), dict):
        payload["balance"]["fleet_sample_status"] = sample["fleet_sample_status"]
        payload["balance"]["sample_size_caveat"] = sample.get("sample_size_caveat")
        payload["balance"]["top500_live_coverage"] = cov_n
        pil_b = dict(payload["balance"].get("pil") or {})
        if sample["fleet_sample_status"] != "FULL":
            pil_b["sample_size_caveat"] = sample.get("sample_size_caveat")
            pil_b["fleet_sample_status"] = sample["fleet_sample_status"]
            pil_b["status_display"] = (
                f"{pil_b.get('status', 'NOMINAL')} · SAMPLE {sample['fleet_sample_status']} "
                f"(N={cov_n})"
            )
        payload["balance"]["pil"] = pil_b
        for mk in ("dar", "dfs", "lssi"):
            node = payload["balance"].get(mk)
            if isinstance(node, dict):
                from services.dual_gate import apply_fleet_sample_to_metric

                payload["balance"][mk] = apply_fleet_sample_to_metric(
                    node, sample=sample, metric_name=mk.upper()
                )
    if isinstance(payload.get("quant_pipeline"), dict):
        qp = payload["quant_pipeline"]
        qp["fleet_sample_status"] = sample["fleet_sample_status"]
        qp["sample_size_caveat"] = sample.get("sample_size_caveat")
        qp["top500_live_coverage"] = cov_n
        live_conf = live_inference_confidence(
            model_cv_accuracy_pct=qp.get("ensemble_accuracy_pct")
            if qp.get("accuracy_basis") == "purged_cv_directional"
            else qp.get("model_cv_accuracy_pct"),
            fleet_sample_status=sample["fleet_sample_status"],
            coverage=cov_n,
        )
        qp.update({
            "model_cv_accuracy_pct": live_conf.get("model_cv_accuracy_pct"),
            "live_inference_confidence": live_conf.get("live_inference_confidence"),
            "live_inference_confidence_pct": live_conf.get("live_inference_confidence_pct"),
            "live_confidence_factor": live_conf.get("live_confidence_factor"),
            "live_confidence_note": live_conf.get("note"),
        })
    # Re-stamp alerts after authoritative coverage (so production_actionable matches fleet gate)
    if isinstance(payload.get("alerts"), dict) or payload.get("alerts") is None:
        try:
            payload["alerts"] = build_alerts_payload(payload)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: alerts re-stamp failed: {exc}")

    pipe = compute_pipeline_health_status(
        freshness=freshness,
        connector=(payload.get("connector") or {}),
        port_ok=True,
        port_drift_8478=False,
    )
    payload["pipeline_health_status"] = pipe["pipeline_health_status"]
    payload["pipeline_health"] = pipe

    # Strip host-absolute paths from embedded analytics before HTML/JSON dumps
    payload = sanitize_structure(payload)

    html = _HTML.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    tile_url = os.environ.get("TILE_SERVER") or DEFAULT_TILE_URL
    tile_sub = os.environ.get("TILE_SUBDOMAINS") or DEFAULT_TILE_SUBDOMAINS
    tile_attr = os.environ.get("TILE_ATTR") or DEFAULT_TILE_ATTR
    html = (
        html.replace("__TILE_URL__", tile_url)
        .replace("__TILE_SUBDOMAINS__", tile_sub)
        .replace("__TILE_ATTR__", tile_attr)
        .replace("__ASSET_V__", ASSET_V)
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    try:
        validate_html_artifact(out)
    except SREBuildError:
        if out.exists():
            out.unlink()
        raise

    health_dir = out.parent / "api" / "v1"
    health_dir.mkdir(parents=True, exist_ok=True)
    bal_meta  = payload.get("balance") or {}
    qp_meta   = payload.get("quant_pipeline") or {}
    alerts_meta = payload.get("alerts") or {}
    spoof_meta = payload.get("ais_spoofing") or {}

    published_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    build_mode = os.environ.get("SENTINEL_BUILD_MODE", "rebuild")

    health = {
        "service": "sentinel_dashboard",
        "status": "ok" if freshness.get("live_ok") else (
            "unhealthy" if freshness.get("stale") else "degraded"
        ),
        "http": "200",
        "port": int(os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or 8765),
        "published_at": published_at,
        "build_mode": build_mode,
        "operational_status": payload.get("operational_status"),
        "pipeline_health_status": pipe["pipeline_health_status"],
        "pipeline_health": pipe,
        "fleet_sample_status": sample["fleet_sample_status"],
        "fleet_sample": sample,
        "sample_size_caveat": sample.get("sample_size_caveat"),
        "coverage_window_seconds": cov.get("coverage_window_seconds"),
        "replica": freshness,
        "live_vessel_count": payload.get("live_vessel_count"),
        "top500_live_coverage": cov_n,
        "top500_coverage": cov,
        "source_mode": payload.get("source_mode"),
        "truth_contract": truth_contract,
        "ttf_spot": ttf_meta.get("spot_eur_mwh"),
        "ttf_integrity": ttf_meta.get("integrity_status"),
        "top10": {**sync_meta, "truth": top10_truth},
        "ais_spoofing": {
            "spoofed_count": spoof_meta.get("spoofed_count", 0),
            "bound_kn": spoof_meta.get("bound_kn", 21.0),
            "engine": spoof_meta.get("engine"),
        },
        "reactive_alerts": {
            "alert_count": alerts_meta.get("alert_count", 0),
            "monitored_vessels": alerts_meta.get("monitored_vessels", 0),
            "delta_threshold": alerts_meta.get("delta_threshold"),
            "window_hours": alerts_meta.get("window_hours"),
            "production_actionable": alerts_meta.get("production_actionable"),
            "signal_status": alerts_meta.get("signal_status"),
            "fleet_sample_status": alerts_meta.get("fleet_sample_status"),
        },
        "top500_balance_status": {
            "status": bal_meta.get("status", "UNKNOWN"),
            "fleet_size": bal_meta.get("fleet_size"),
            "spoofed_excluded": bal_meta.get("spoofed_excluded", 0),
            "laden_pct": (bal_meta.get("cargo_load") or {}).get("laden_pct"),
            "dar_pct": (bal_meta.get("dar") or {}).get("dar_pct"),
            "dar_signal_status": (bal_meta.get("dar") or {}).get("signal_status"),
            "dfs_grade": (bal_meta.get("dfs") or {}).get("grade"),
            "dfs_signal_status": (bal_meta.get("dfs") or {}).get("signal_status"),
            "lssi_signal": (bal_meta.get("lssi") or {}).get("signal"),
            "lssi_signal_status": (bal_meta.get("lssi") or {}).get("signal_status"),
            "pil_status": (bal_meta.get("pil") or {}).get("status"),
            "pil_status_display": (bal_meta.get("pil") or {}).get("status_display"),
            "sample_size_caveat": bal_meta.get("sample_size_caveat") or sample.get("sample_size_caveat"),
            "total_m3_transit": (bal_meta.get("lssi") or {}).get("total_m3_transit"),
        },
        "quant_pipeline": {
            "direction":              qp_meta.get("direction"),
            "confidence":             qp_meta.get("confidence"),
            "ensemble_accuracy_pct":  qp_meta.get("ensemble_accuracy_pct"),
            "model_cv_accuracy_pct":  qp_meta.get("model_cv_accuracy_pct"),
            "accuracy_basis":         qp_meta.get("accuracy_basis"),
            "ensemble_confidence_proxy_pct": qp_meta.get("ensemble_confidence_proxy_pct"),
            "live_inference_confidence": qp_meta.get("live_inference_confidence"),
            "live_inference_confidence_pct": qp_meta.get("live_inference_confidence_pct"),
            "live_confidence_factor": qp_meta.get("live_confidence_factor"),
            "fleet_sample_status":    sample["fleet_sample_status"],
            "sample_size_caveat":     sample.get("sample_size_caveat"),
            "catboost_accuracy_pct":  qp_meta.get("catboost_accuracy_pct"),
            "component_accuracy_pct": qp_meta.get("component_accuracy_pct"),
            "ensemble_weights":       qp_meta.get("ensemble_weights"),
            "weight_governance":      qp_meta.get("weight_governance"),
            "markov_state":           qp_meta.get("markov_state"),
            "bog_loss_pct":           (qp_meta.get("bog") or {}).get("fleet_bog_loss_pct"),
            "eu_bound_count":         (qp_meta.get("routing") or {}).get("eu_bound_count"),
            "tbi_score":              (qp_meta.get("tbi") or {}).get("tbi_score"),
            "ice_p_bull":             (qp_meta.get("ice") or {}).get("p_bull"),
            "errors":                 qp_meta.get("errors", []),
        },
        "route": {
            "source_mode": route_meta.get("source_mode"),
            "truth": route_truth,
            "vessels": len(route_meta.get("vessels") or []),
            "aggregates_days": route_meta.get("aggregates_days"),
        },
        "generated_at": published_at,
    }
    write_health_files(sanitize_structure(health), root=ROOT)

    meta = sanitize_structure({
        "path": str(out),
        "bytes": out.stat().st_size,
        "live_vessel_count": payload.get("live_vessel_count"),
        "source_mode": payload.get("source_mode"),
        "operational_status": payload.get("operational_status"),
        "replica_freshness": freshness,
        "truth_contract": truth_contract,
        "ttf_kpi": ttf_meta.get("kpi"),
        "ttf_integrity": ttf_meta.get("integrity_status"),
        "top10_assets": sync_meta,
        "route_analytics": health["route"],
    })
    out.with_suffix(out.suffix + ".meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return payload


def main() -> int:
    from services.ttf_forecast.integrity import SREBuildError

    try:
        payload = write_sentinel_dashboard()
    except SREBuildError as exc:
        print(f"SRE BUILD HARD-FAIL: {exc}")
        return 2
    fr = payload.get("replica_freshness") or {}
    size = OUT_HTML.stat().st_size
    ttf = payload.get("ttf_forecast") or {}
    print(
        f"Wrote {OUT_HTML} · bytes={size} · vessels={payload.get('live_vessel_count')} · "
        f"mode={payload.get('source_mode')} · ops={payload.get('operational_status')} · "
        f"replica={fr.get('status')} lag={fr.get('lag_minutes')}m · "
        f"ttf_spot={ttf.get('spot_eur_mwh')} h7={(ttf.get('kpi') or {}).get('h7_p50')} · "
        f"integrity={ttf.get('integrity_status')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
