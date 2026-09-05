"""Build Oracle-1001 / Sentinel 18-chart analytics dashboard + TTF Forecast sheet."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.sentinel_analytics import build_sentinel_payload  # noqa: E402
from services.ttf_forecast.dashboard_payload import build_ttf_forecast_payload  # noqa: E402

OUT_HTML = ROOT / "output" / "sentinel_dashboard.html"
WEB_DIR = ROOT / "web"
JS_OUT = ROOT / "output" / "js"

DB_CANDIDATES = [
    ROOT / "история1" / "sentinel_ais.db",
    ROOT / "sentinel_ais.db",
    Path("/opt/oracle1001/analytical_engine/история1/sentinel_ais.db"),
    Path("/opt/oracle1001/analytical_engine/sentinel_ais.db"),
]
STALE_AFTER_SEC = 600


def inspect_replica_freshness(stale_after_sec: int = STALE_AFTER_SEC) -> dict:
    db = next((p for p in DB_CANDIDATES if p.exists()), None)
    now = time.time()
    if db is None:
        return {
            "stale": True,
            "db_path": None,
            "age_sec": None,
            "lag_minutes": None,
            "status": "MISSING_REPLICA",
            "banner": "[STALE AIS REPLICA DETECTED - DATABASE FILE MISSING]",
        }
    mtime = db.stat().st_mtime
    age = max(0.0, now - mtime)
    lag_min = round(age / 60.0, 1)
    stale = age > float(stale_after_sec)
    return {
        "stale": stale,
        "db_path": str(db),
        "mtime_epoch": mtime,
        "age_sec": round(age, 1),
        "lag_minutes": lag_min,
        "status": "STALE" if stale else "FRESH",
        "banner": (
            f"[STALE AIS REPLICA DETECTED - DATA LAG > {lag_min} MINS]"
            if stale
            else None
        ),
    }


_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ORACLE-1001 · Sentinel · TTF Forecast</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Manrope:wght@400;600;700&family=Orbitron:wght@500;700&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="js/sentinel_premium.css"/>
<script>
/* Early sheet sync — before Chart.js boot (avoids FOUC + blank TTF canvases) */
(function () {
  try {
    var q = new URLSearchParams(location.search || "");
    var h = String(location.hash || "").replace(/^#/, "").toLowerCase();
    var sheet = (q.get("sheet") || "").toLowerCase();
    if (sheet === "ttf" || h === "ttf" || h === "tab-ttf-forecast") {
      document.documentElement.setAttribute("data-sheet", "ttf");
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
html[data-sheet="ttf"] #kpiRow{display:none !important}
html[data-sheet="ais"] #tab-ttf-forecast{display:none !important}
html[data-sheet="ais"] #sheet-ais{display:block !important}
.sheet-tab[aria-selected="true"]{color:var(--cyan);border-color:var(--cyan);box-shadow:0 0 18px rgba(0,229,255,.18);
  background:rgba(0,229,255,.1)}
.ttf-glass{background:linear-gradient(145deg,rgba(8,18,32,.92),rgba(12,28,48,.78));
  border:1px solid rgba(0,229,255,.28);box-shadow:inset 0 0 40px rgba(0,229,255,.04),0 10px 40px rgba(0,0,0,.35);
  backdrop-filter:blur(16px);border-radius:16px}
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
.ttf-skel{min-height:220px;display:flex;align-items:center;justify-content:center;
  border:1px dashed rgba(248,113,113,.45);border-radius:12px;color:#fca5a5;
  font-family:var(--font-hud);font-size:11px;letter-spacing:.05em;text-align:center;padding:16px;
  background:rgba(127,29,29,.15)}
.ttf-badge.err{border-color:rgba(248,113,113,.55);color:#fca5a5;background:rgba(127,29,29,.2)}
</style>
</head>
<body>
<div id="staleBanner" class="stale-banner"></div>
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
</nav>
<script>
(function () {
  var sheet = document.documentElement.getAttribute("data-sheet") || "ais";
  document.querySelectorAll(".sheet-tab").forEach(function (btn) {
    var on = btn.getAttribute("data-sheet") === sheet;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  var ais = document.getElementById("sheet-ais");
  var ttf = document.getElementById("tab-ttf-forecast");
  var kpi = document.getElementById("kpiRow");
  if (ais) { ais.classList.toggle("active", sheet === "ais"); ais.style.display = sheet === "ais" ? "block" : "none"; }
  if (ttf) { ttf.classList.toggle("active", sheet === "ttf"); ttf.style.display = sheet === "ttf" ? "block" : "none"; }
  if (kpi && sheet === "ttf") kpi.style.display = "none";
  if (sheet === "ttf") {
    var ht = document.getElementById("heroTitle");
    if (ht) ht.textContent = "ПРОГНОЗ TTF · MARKET FORECAST ENSEMBLE";
  }
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

<footer class="foot">
  <span>ORACLE-1001 / Sentinel · Dual-Plane Edge→Core · TTF Meta-Ensemble</span>
  <span id="footMeta"></span>
</footer>

<script>
window.__SENTINEL_PAYLOAD__ = __PAYLOAD__;
window.SENTINEL_TTF_DATA = (window.__SENTINEL_PAYLOAD__ && window.__SENTINEL_PAYLOAD__.ttf_forecast) || null;
</script>
<script src="js/sentinel_engine.js"></script>
</body>
</html>
"""


def write_sentinel_dashboard(path: Path | None = None) -> dict:
    from services.ttf_forecast.integrity import SREBuildError, validate_html_artifact

    out = path or OUT_HTML
    freshness = inspect_replica_freshness()
    payload = build_sentinel_payload()
    payload["replica_freshness"] = freshness
    if freshness.get("stale"):
        payload["operational_status"] = "DEGRADED_STALE_REPLICA"
        payload["source_mode"] = f"{payload.get('source_mode', 'unknown')}+STALE"
    else:
        payload["operational_status"] = "NOMINAL"

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

    JS_OUT.mkdir(parents=True, exist_ok=True)
    for name in ("sentinel_engine.js", "sentinel_premium.css"):
        src = WEB_DIR / name
        if src.exists():
            shutil.copy2(src, JS_OUT / name)

    html = _HTML.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
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
    health = {
        "service": "sentinel_dashboard",
        "status": "unhealthy" if freshness.get("stale") else "healthy",
        "operational_status": payload.get("operational_status"),
        "replica": freshness,
        "live_vessel_count": payload.get("live_vessel_count"),
        "source_mode": payload.get("source_mode"),
        "ttf_spot": (payload.get("ttf_forecast") or {}).get("spot_eur_mwh"),
        "ttf_integrity": (payload.get("ttf_forecast") or {}).get("integrity_status"),
    }
    (health_dir / "health").write_text(json.dumps(health, indent=2), encoding="utf-8")
    (health_dir / "health.json").write_text(json.dumps(health, indent=2), encoding="utf-8")

    meta = {
        "path": str(out),
        "bytes": out.stat().st_size,
        "live_vessel_count": payload.get("live_vessel_count"),
        "source_mode": payload.get("source_mode"),
        "operational_status": payload.get("operational_status"),
        "replica_freshness": freshness,
        "ttf_kpi": (payload.get("ttf_forecast") or {}).get("kpi"),
        "ttf_integrity": (payload.get("ttf_forecast") or {}).get("integrity_status"),
    }
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
