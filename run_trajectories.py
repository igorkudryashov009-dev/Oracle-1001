"""
Generate trajectories.html from AIS position history.

When you receive a real AIS position-history file, replace AIS_SOURCE_PATH below
and re-run this script — ais_loader.py will auto-detect columns by alias.
If the real file's columns differ substantially from the expected aliases
(see ais_loader.py), do NOT force a mapping: the loader raises with the found
columns — confirm the mapping before proceeding.
"""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ais_loader import load_ais_history
from distance_calc import PERIOD_DAYS, compute_all_periods, fleet_summary

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"

# ---------------------------------------------------------------------------
# INTEGRATION POINT — replace with the real AIS history path when available
# ---------------------------------------------------------------------------
AIS_SOURCE_PATH = str(OUTPUT / "sample_ais_history_MOCK.csv")
# <- When a real file arrives, set e.g.:
# AIS_SOURCE_PATH = r"C:\path\to\real_ais_history.csv"
# ---------------------------------------------------------------------------

FLEET_JSON = OUTPUT / "fleet_database.json"
MOCK_CSV = OUTPUT / "sample_ais_history_MOCK.csv"
MOCK_README = OUTPUT / "sample_ais_history_MOCK.README.txt"
HTML_OUT = OUTPUT / "trajectories.html"

# Plausible great-circle port pairs (lat, lon)
ROUTE_PAIRS = [
    ((1.264, 103.820), (51.950, 4.140)),      # Singapore → Rotterdam
    ((22.290, 114.170), (33.720, -118.270)),  # Hong Kong → Los Angeles
    ((25.270, 55.300), (51.510, -0.080)),     # Dubai → London
    ((35.450, 139.680), (47.600, -122.340)),  # Tokyo → Seattle
    ((1.264, 103.820), (-33.920, 18.430)),    # Singapore → Cape Town
    ((40.680, -74.040), (51.950, 4.140)),     # New York → Rotterdam
    ((29.370, 47.990), (1.264, 103.820)),     # Kuwait → Singapore
    ((31.230, 121.470), (51.950, 4.140)),     # Shanghai → Rotterdam
    ((-6.110, 106.880), (22.290, 114.170)),   # Jakarta → Hong Kong
    ((19.080, 72.880), (1.264, 103.820)),     # Mumbai → Singapore
]


def _to_cart(lat: float, lon: float) -> tuple[float, float, float]:
    lat_r, lon_r = math.radians(lat), math.radians(lon)
    return (
        math.cos(lat_r) * math.cos(lon_r),
        math.cos(lat_r) * math.sin(lon_r),
        math.sin(lat_r),
    )


def _from_cart(x: float, y: float, z: float) -> tuple[float, float]:
    hyp = math.sqrt(x * x + y * y)
    lat = math.degrees(math.atan2(z, hyp))
    lon = math.degrees(math.atan2(y, x))
    return lat, lon


def interpolate_gc(
    lat1: float, lon1: float, lat2: float, lon2: float, n: int
) -> list[tuple[float, float]]:
    """Spherical linear interpolation along great-circle arc."""
    if n < 2:
        return [(lat1, lon1)]
    a = _to_cart(lat1, lon1)
    b = _to_cart(lat2, lon2)
    dot = max(-1.0, min(1.0, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]))
    omega = math.acos(dot)
    pts = []
    for i in range(n):
        t = i / (n - 1)
        if omega < 1e-9:
            x, y, z = a
        else:
            s1 = math.sin((1 - t) * omega) / math.sin(omega)
            s2 = math.sin(t * omega) / math.sin(omega)
            x = s1 * a[0] + s2 * b[0]
            y = s1 * a[1] + s2 * b[1]
            z = s1 * a[2] + s2 * b[2]
        lat, lon = _from_cart(x, y, z)
        # small jitter (~2–8 km) for realism
        lat += random.uniform(-0.04, 0.04)
        lon += random.uniform(-0.04, 0.04)
        pts.append((lat, lon))
    return pts


def pick_fleet_vessels(n: int = 18) -> pd.DataFrame:
    fleet = pd.read_json(FLEET_JSON)
    fleet = fleet[
        (fleet["source_confidence"] == "parsed")
        & fleet["vessel_name"].notna()
        & fleet["imo"].notna()
    ].drop_duplicates("imo")
    # Prefer named tankers with clean short names
    fleet["name_len"] = fleet["vessel_name"].astype(str).str.len()
    pool = fleet[fleet["name_len"] <= 40].copy()
    if len(pool) < n:
        pool = fleet
    return pool.sample(n=min(n, len(pool)), random_state=20260728).reset_index(drop=True)


def generate_mock_ais(vessels: pd.DataFrame, out_csv: Path) -> pd.DataFrame:
    """Synthesize 40–120 AIS pings per vessel over ~14 months along GC routes."""
    rng = random.Random(20260728)
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    rows: list[dict] = []

    for i, v in vessels.iterrows():
        imo = str(v["imo"]).replace(".0", "")
        route = ROUTE_PAIRS[i % len(ROUTE_PAIRS)]
        # round-trip style: outbound + inbound segments
        n_points = rng.randint(45, 110)
        half = n_points // 2
        outbound = interpolate_gc(*route[0], *route[1], half)
        inbound = interpolate_gc(*route[1], *route[0], n_points - half)
        coords = outbound + inbound

        # timestamps: uneven intervals over ~14 months ending near `now`
        span_days = 420 + rng.randint(-20, 20)
        t = now - timedelta(days=span_days)
        for lat, lon in coords:
            gap_h = rng.choice([3, 4, 6, 8, 12, 18, 24, 36, 48, 60])
            t = t + timedelta(hours=gap_h)
            if t > now:
                t = now - timedelta(hours=rng.randint(1, 12))
            sog = round(rng.uniform(8.5, 15.5), 1)
            cog = round(rng.uniform(0, 359), 0)
            rows.append(
                {
                    "imo": imo,
                    "timestamp": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "lat": round(lat, 5),
                    "lon": round(lon, 5),
                    "speed_knots": sog,
                    "heading": cog,
                }
            )

    df = pd.DataFrame(rows).sort_values(["imo", "timestamp"])
    header = (
        "# MOCK SYNTHETIC AIS HISTORY — NOT REAL POSITIONS\n"
        "# For UI development only. See sample_ais_history_MOCK.README.txt\n"
    )
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        f.write(header)
        df.to_csv(f, index=False)
    return df


def write_mock_readme(path: Path, vessels: pd.DataFrame) -> None:
    lines = [
        "sample_ais_history_MOCK.csv — SYNTHETIC DATA",
        "=" * 50,
        "",
        "This file contains DEMO / MOCK AIS positions generated for UI",
        "development of trajectories.html. Coordinates are interpolated",
        "along plausible great-circle routes with jitter — NOT real AIS.",
        "",
        "DO NOT use for operational, compliance, or sanctions analysis.",
        "",
        f"Vessels included ({len(vessels)}):",
    ]
    for _, v in vessels.iterrows():
        lines.append(f"  - IMO {v['imo']}: {v['vessel_name']}")
    lines += [
        "",
        "To switch to a real AIS history file:",
        "  1. Set AIS_SOURCE_PATH in run_trajectories.py",
        "  2. Re-run: python run_trajectories.py",
        "  3. ais_loader.py will auto-detect columns by alias",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def build_payload(ais: pd.DataFrame, fleet_meta: dict[str, dict]) -> dict:
    by_period = compute_all_periods(ais)
    summaries = {p: fleet_summary(by_period[p]) for p in PERIOD_DAYS}

    # Enrich each vessel with fleet metadata + per-period stats (tracks included)
    vessels_out = {}
    for imo in sorted(ais["imo"].unique()):
        meta = fleet_meta.get(str(imo), {})
        vessels_out[str(imo)] = {
            "imo": str(imo),
            "vessel_name": meta.get("vessel_name"),
            "flag": meta.get("flag"),
            "vessel_type": meta.get("vessel_type"),
            "periods": {p: by_period[p][str(imo)] for p in PERIOD_DAYS},
        }

    return {
        "is_mock": "MOCK" in Path(AIS_SOURCE_PATH).name.upper(),
        "source_path": str(AIS_SOURCE_PATH),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summaries": summaries,
        "vessels": vessels_out,
    }


def render_html(payload: dict) -> str:
    data_json = json.dumps(payload, ensure_ascii=False)
    return _HTML_TEMPLATE.replace("__TRAJECTORY_DATA__", data_json)


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ORACLE-1001 · TRAJECTORIES</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=JetBrains+Mono:wght@400;600&family=Rajdhani:wght@500;600;700&display=swap" rel="stylesheet" />
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css" />
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root {
  --bg0: #05080d;
  --bg1: #0a0e17;
  --panel: rgba(8, 16, 28, 0.92);
  --neon: #00fff0;
  --neon2: #2dfdd0;
  --neon-dim: rgba(0, 255, 240, 0.18);
  --neon-glow: 0 0 12px rgba(0, 255, 240, 0.45), 0 0 28px rgba(0, 255, 240, 0.15);
  --text: #d7fff9;
  --muted: #6a8f8a;
  --danger: #ff4d6d;
  --border: rgba(0, 255, 240, 0.35);
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  background: radial-gradient(ellipse at 20% 0%, #0c1524 0%, var(--bg0) 55%);
  color: var(--text);
  font-family: "Rajdhani", sans-serif;
  overflow: hidden;
}
body::before {
  content: "";
  position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background-image:
    linear-gradient(rgba(0,255,240,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,255,240,0.03) 1px, transparent 1px);
  background-size: 48px 48px;
  mask-image: radial-gradient(ellipse at center, black 30%, transparent 80%);
}
.scanline {
  position: fixed; left: 0; right: 0; height: 2px; z-index: 50; pointer-events: none;
  background: linear-gradient(90deg, transparent, var(--neon), transparent);
  opacity: 0.15; animation: scan 7s linear infinite;
}
@keyframes scan { from { top: 0; } to { top: 100%; } }

.app { position: relative; z-index: 1; display: grid;
  grid-template-rows: auto auto 1fr; height: 100vh; gap: 0; }

/* HEADER */
.header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 20px; border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, #0c1624, transparent);
}
.brand { font-family: "Orbitron", sans-serif; letter-spacing: 0.12em; font-size: 15px; color: var(--neon);
  text-shadow: var(--neon-glow); }
.brand span { color: var(--muted); font-weight: 500; }
.nav a {
  color: var(--neon); text-decoration: none; font-family: "Orbitron", sans-serif;
  font-size: 11px; letter-spacing: 0.1em; border: 1px solid var(--border);
  padding: 6px 12px; margin-left: 8px; transition: 0.2s;
}
.nav a:hover { box-shadow: var(--neon-glow); background: var(--neon-dim); }

.mock-banner {
  background: linear-gradient(90deg, rgba(255,77,109,0.25), rgba(255,77,109,0.08));
  border-bottom: 1px solid var(--danger); color: #ffb3c1;
  font-family: "JetBrains Mono", monospace; font-size: 12px;
  padding: 8px 20px; letter-spacing: 0.04em; text-align: center;
}
.mock-banner strong { color: var(--danger); }

/* METRICS */
.metrics {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
  padding: 12px 20px;
}
.metric {
  background: var(--panel); border: 1px solid var(--border);
  padding: 12px 14px; position: relative;
  box-shadow: inset 0 0 20px rgba(0,255,240,0.04);
  clip-path: polygon(0 0, calc(100% - 12px) 0, 100% 12px, 100% 100%, 12px 100%, 0 calc(100% - 12px));
}
.metric::before {
  content: ""; position: absolute; top: 0; left: 0; right: 12px; height: 1px;
  background: var(--neon); box-shadow: var(--neon-glow); opacity: 0.7;
}
.metric .label {
  font-family: "Orbitron", sans-serif; font-size: 9px; color: var(--muted);
  letter-spacing: 0.14em; text-transform: uppercase;
}
.metric .value {
  font-family: "JetBrains Mono", monospace; font-size: 26px; color: var(--neon);
  text-shadow: var(--neon-glow); margin-top: 4px; font-weight: 600;
}
.metric .unit { font-size: 12px; color: var(--muted); margin-left: 4px; }

/* PERIOD TOGGLE */
.period-bar {
  display: flex; gap: 8px; padding: 0 20px 10px; align-items: center;
}
.period-bar .tag {
  font-family: "Orbitron", sans-serif; font-size: 9px; color: var(--muted);
  letter-spacing: 0.12em; margin-right: 8px;
}
.period-btn {
  font-family: "Orbitron", sans-serif; font-size: 11px; letter-spacing: 0.08em;
  background: transparent; color: var(--muted); border: 1px solid rgba(0,255,240,0.2);
  padding: 8px 14px; cursor: pointer; transition: 0.2s;
}
.period-btn:hover { color: var(--neon); border-color: var(--border); }
.period-btn.active {
  color: var(--bg0); background: var(--neon); border-color: var(--neon);
  box-shadow: var(--neon-glow); font-weight: 700;
}

/* MAIN LAYOUT */
.main {
  display: grid; grid-template-columns: 280px 1fr 320px;
  gap: 12px; padding: 0 20px 16px; min-height: 0;
}
.panel {
  background: var(--panel); border: 1px solid var(--border);
  display: flex; flex-direction: column; min-height: 0;
  box-shadow: inset 0 0 30px rgba(0,255,240,0.03);
}
.panel-h {
  font-family: "Orbitron", sans-serif; font-size: 11px; letter-spacing: 0.14em;
  color: var(--neon); padding: 10px 12px; border-bottom: 1px solid var(--border);
  text-shadow: 0 0 8px rgba(0,255,240,0.4);
}
.search {
  margin: 10px 12px; background: #05080d; border: 1px solid var(--border);
  color: var(--text); font-family: "JetBrains Mono", monospace; font-size: 12px;
  padding: 8px 10px; outline: none;
}
.search:focus { box-shadow: var(--neon-glow); }
.vessel-list { overflow: auto; flex: 1; padding: 0 6px 10px; }
.vessel-item {
  display: grid; grid-template-columns: 1fr auto; gap: 4px;
  padding: 8px 10px; cursor: pointer; border: 1px solid transparent;
  font-family: "JetBrains Mono", monospace; font-size: 11px;
  transition: 0.15s;
}
.vessel-item:hover, .vessel-item.active {
  border-color: var(--border); background: var(--neon-dim);
  box-shadow: inset 0 0 12px rgba(0,255,240,0.08);
}
.vessel-item .name { color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.vessel-item .imo { color: var(--muted); font-size: 10px; }
.vessel-item .km { color: var(--neon); align-self: center; }

#map {
  flex: 1; min-height: 280px; background: #05080d;
  border-top: 1px solid var(--border);
}
.map-wrap { display: flex; flex-direction: column; min-height: 0; }
.leaflet-container { background: #05080d; font-family: "JetBrains Mono", monospace; }

.side-body { padding: 12px; overflow: auto; flex: 1; font-family: "JetBrains Mono", monospace; font-size: 12px; }
.side-body .row { display: flex; justify-content: space-between; padding: 6px 0;
  border-bottom: 1px solid rgba(0,255,240,0.08); }
.side-body .k { color: var(--muted); }
.side-body .v { color: var(--neon); text-align: right; max-width: 60%; }
.side-empty { color: var(--muted); padding: 20px 12px; font-family: "JetBrains Mono", monospace; font-size: 12px; }

.chart-box { height: 220px; padding: 8px 12px 12px; border-top: 1px solid var(--border); }

/* Pulsing marker */
.pulse-marker {
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--neon); box-shadow: 0 0 0 0 rgba(0,255,240,0.7);
  animation: pulse 1.6s infinite;
}
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 rgba(0,255,240,0.7); }
  70% { box-shadow: 0 0 0 14px rgba(0,255,240,0); }
  100% { box-shadow: 0 0 0 0 rgba(0,255,240,0); }
}

@media (max-width: 1100px) {
  .main { grid-template-columns: 1fr; grid-template-rows: 200px 1fr 280px; }
  .metrics { grid-template-columns: 1fr 1fr; }
}
</style>
</head>
<body>
<div class="scanline"></div>
<div class="app">
  <header class="header">
    <div class="brand">ORACLE-1001 <span>// TRAJECTORY TELEMETRY</span></div>
    <nav class="nav">
      <a href="dashboard.html">← FLEET DB</a>
    </nav>
  </header>

  <div id="mock-banner" class="mock-banner" style="display:none">
    <strong>⚠ ДЕМО-ДАННЫЕ</strong> — синтетические AIS-позиции для разработки UI.
    Ожидается интеграция реального источника AIS. Не использовать для оперативных решений.
  </div>

  <div class="metrics" id="metrics"></div>

  <div class="period-bar">
    <span class="tag">PERIOD_FILTER</span>
    <button class="period-btn active" data-period="1m">1 МЕСЯЦ</button>
    <button class="period-btn" data-period="3m">3 МЕСЯЦА</button>
    <button class="period-btn" data-period="1y">1 ГОД</button>
    <button class="period-btn" data-period="all">ВСЁ ВРЕМЯ</button>
  </div>

  <div class="main">
    <aside class="panel">
      <div class="panel-h">VESSEL INDEX</div>
      <input class="search" id="search" placeholder="поиск IMO / имя…" />
      <div class="vessel-list" id="vessel-list"></div>
    </aside>

    <section class="panel map-wrap">
      <div class="panel-h">TRACK MAP · GREAT-CIRCLE OVERLAY</div>
      <div id="map"></div>
    </section>

    <aside class="panel">
      <div class="panel-h">TARGET DOSSIER</div>
      <div class="side-body" id="dossier">
        <div class="side-empty">Выберите судно из списка →</div>
      </div>
      <div class="panel-h">TOP-10 DISTANCE</div>
      <div class="chart-box"><canvas id="top-chart"></canvas></div>
    </aside>
  </div>
</div>

<script>
const DATA = __TRAJECTORY_DATA__;

let period = "1m";
let selectedImo = null;
let map, chart;
const polylines = {};
const markers = {};

function fmtKm(v) {
  if (v == null) return "—";
  return v >= 1000 ? (v/1000).toFixed(1) + "k" : v.toFixed(0);
}
function fmtNum(v) {
  return v == null ? "—" : Number(v).toLocaleString("ru-RU");
}

function renderMetrics() {
  const s = DATA.summaries[period];
  const items = [
    { label: "FLEET DISTANCE", value: fmtKm(s.total_km), unit: "km" },
    { label: "ACTIVE TRACKS", value: s.active_vessels, unit: "vsl" },
    { label: "AVG / VESSEL", value: fmtKm(s.avg_km_per_vessel), unit: "km" },
    { label: "AVG SOG", value: s.avg_speed_knots != null ? s.avg_speed_knots : "—", unit: "kn" },
  ];
  document.getElementById("metrics").innerHTML = items.map(it => `
    <div class="metric">
      <div class="label">${it.label}</div>
      <div class="value">${it.value}<span class="unit">${it.unit}</span></div>
    </div>`).join("");
}

function vesselRows() {
  return Object.values(DATA.vessels).map(v => {
    const st = v.periods[period];
    return { ...v, stats: st, km: st ? st.total_km : 0 };
  }).sort((a,b) => b.km - a.km);
}

function renderList() {
  const q = (document.getElementById("search").value || "").toLowerCase().trim();
  const root = document.getElementById("vessel-list");
  const rows = vesselRows().filter(v => {
    if (!q) return true;
    return String(v.imo).includes(q) || String(v.vessel_name||"").toLowerCase().includes(q);
  });
  root.innerHTML = rows.map(v => `
    <div class="vessel-item ${v.imo===selectedImo?"active":""}" data-imo="${v.imo}">
      <div>
        <div class="name">${v.vessel_name || "(unnamed)"}</div>
        <div class="imo">IMO ${v.imo}</div>
      </div>
      <div class="km">${fmtKm(v.km)}</div>
    </div>`).join("");
  root.querySelectorAll(".vessel-item").forEach(el => {
    el.addEventListener("click", () => selectVessel(el.dataset.imo));
  });
}

function renderDossier(imo) {
  const v = DATA.vessels[imo];
  const st = v.periods[period];
  const el = document.getElementById("dossier");
  if (!st || st.num_pings === 0) {
    el.innerHTML = `<div class="side-empty">Нет точек за выбранный период</div>`;
    return;
  }
  el.innerHTML = `
    <div class="row"><span class="k">NAME</span><span class="v">${v.vessel_name||"—"}</span></div>
    <div class="row"><span class="k">IMO</span><span class="v">${v.imo}</span></div>
    <div class="row"><span class="k">FLAG</span><span class="v">${v.flag||"—"}</span></div>
    <div class="row"><span class="k">TYPE</span><span class="v">${v.vessel_type||"—"}</span></div>
    <div class="row"><span class="k">DISTANCE</span><span class="v">${fmtNum(st.total_km)} km</span></div>
    <div class="row"><span class="k">PINGS</span><span class="v">${st.num_pings}</span></div>
    <div class="row"><span class="k">AVG SOG</span><span class="v">${st.avg_speed_knots ?? "—"} kn</span></div>
    <div class="row"><span class="k">FIRST</span><span class="v">${(st.first_seen||"").slice(0,16)}</span></div>
    <div class="row"><span class="k">LAST</span><span class="v">${(st.last_seen||"").slice(0,16)}</span></div>
  `;
}

function clearLayers() {
  Object.values(polylines).forEach(l => map.removeLayer(l));
  Object.values(markers).forEach(m => map.removeLayer(m));
  Object.keys(polylines).forEach(k => delete polylines[k]);
  Object.keys(markers).forEach(k => delete markers[k]);
}

function drawTracks() {
  clearLayers();
  const bounds = [];
  Object.values(DATA.vessels).forEach(v => {
    const st = v.periods[period];
    if (!st || !st.track || st.track.length < 2) return;
    const latlngs = st.track.map(p => [p.lat, p.lon]);
    latlngs.forEach(ll => bounds.push(ll));
    const isSel = v.imo === selectedImo;
    const line = L.polyline(latlngs, {
      color: isSel ? "#00fff0" : "rgba(0,255,240,0.35)",
      weight: isSel ? 3.5 : 1.5,
      opacity: 1,
      className: "neon-track",
      dashArray: "10 8",
    }).addTo(map);
    // animated dash via CSS on path
    const path = line.getElement();
    if (path) {
      path.style.strokeDasharray = "12 10";
      path.style.animation = "dashmove 1.2s linear infinite";
    }
    polylines[v.imo] = line;

    const last = latlngs[latlngs.length - 1];
    const icon = L.divIcon({
      className: "",
      html: `<div class="pulse-marker" style="opacity:${isSel?1:0.45}"></div>`,
      iconSize: [14, 14],
      iconAnchor: [7, 7],
    });
    markers[v.imo] = L.marker(last, { icon }).addTo(map)
      .bindTooltip(`${v.vessel_name||v.imo}<br>${fmtKm(st.total_km)} km`, { direction: "top" });
  });
  if (bounds.length && !selectedImo) {
    map.fitBounds(bounds, { padding: [30, 30] });
  } else if (selectedImo && polylines[selectedImo]) {
    map.fitBounds(polylines[selectedImo].getBounds(), { padding: [40, 40] });
  }
}

function renderChart() {
  const top = vesselRows().slice(0, 10);
  const labels = top.map(v => (v.vessel_name || v.imo).slice(0, 14));
  const values = top.map(v => v.km);
  if (chart) chart.destroy();
  chart = new Chart(document.getElementById("top-chart"), {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "km",
        data: values,
        backgroundColor: "rgba(0,255,240,0.35)",
        borderColor: "#00fff0",
        borderWidth: 1,
      }]
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#0a0e17",
          titleColor: "#00fff0",
          bodyColor: "#d7fff9",
          borderColor: "#00fff0",
          borderWidth: 1,
        }
      },
      scales: {
        x: {
          ticks: { color: "#6a8f8a", font: { family: "JetBrains Mono", size: 10 } },
          grid: { color: "rgba(0,255,240,0.08)" }
        },
        y: {
          ticks: { color: "#d7fff9", font: { family: "JetBrains Mono", size: 10 } },
          grid: { display: false }
        }
      }
    }
  });
}

function selectVessel(imo) {
  selectedImo = imo;
  renderList();
  renderDossier(imo);
  drawTracks();
}

function setPeriod(p) {
  period = p;
  document.querySelectorAll(".period-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.period === p);
  });
  renderMetrics();
  renderList();
  renderChart();
  if (selectedImo) renderDossier(selectedImo);
  drawTracks();
}

function initMap() {
  map = L.map("map", { zoomControl: true, attributionControl: true }).setView([20, 40], 2);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_matter/{z}/{x}/{y}{r}.png", {
    attribution: "&copy; OpenStreetMap &copy; CARTO",
    subdomains: "abcd",
    maxZoom: 18,
  }).addTo(map);
}

/* inject dash animation style for SVG paths */
const style = document.createElement("style");
style.textContent = `@keyframes dashmove { to { stroke-dashoffset: -44; } }`;
document.head.appendChild(style);

if (DATA.is_mock) {
  document.getElementById("mock-banner").style.display = "block";
}

initMap();
document.querySelectorAll(".period-btn").forEach(b => {
  b.addEventListener("click", () => setPeriod(b.dataset.period));
});
document.getElementById("search").addEventListener("input", renderList);

setPeriod("1m");
setTimeout(() => map.invalidateSize(), 200);
</script>
</body>
</html>
"""


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)

    # Build / refresh MOCK if the configured source is the mock file
    using_mock = "MOCK" in Path(AIS_SOURCE_PATH).name.upper()
    if using_mock:
        print("Generating MOCK AIS history…")
        vessels = pick_fleet_vessels(18)
        generate_mock_ais(vessels, MOCK_CSV)
        write_mock_readme(MOCK_README, vessels)
        print(f"  wrote {MOCK_CSV} ({len(vessels)} vessels)")
        print(f"  wrote {MOCK_README}")

    print(f"Loading AIS: {AIS_SOURCE_PATH}")
    ais = load_ais_history(AIS_SOURCE_PATH)
    print(f"  rows={len(ais)} vessels={ais['imo'].nunique()}")

    # Fleet metadata for dossier
    fleet_meta: dict[str, dict] = {}
    if FLEET_JSON.exists():
        fleet = pd.read_json(FLEET_JSON)
        for _, r in fleet.drop_duplicates("imo").iterrows():
            fleet_meta[str(r["imo"]).replace(".0", "")] = {
                "vessel_name": r.get("vessel_name"),
                "flag": r.get("flag"),
                "vessel_type": r.get("vessel_type"),
            }

    payload = build_payload(ais, fleet_meta)

    # Sanity-check: all-time km per vessel
    print("\n=== SANITY CHECK: total_km (all time) ===")
    all_stats = payload["vessels"]
    rows = []
    for imo, v in all_stats.items():
        km = v["periods"]["all"]["total_km"]
        name = v.get("vessel_name") or ""
        rows.append((km, imo, name))
    rows.sort(reverse=True)
    for km, imo, name in rows:
        flag = "⚠" if km < 500 or km > 120_000 else " "
        print(f"  {flag} IMO {imo}: {km:>10,.1f} km  {name[:40]}")

    fleet_all = payload["summaries"]["all"]
    print(
        f"\nFleet ALL: {fleet_all['total_km']:,.1f} km | "
        f"active={fleet_all['active_vessels']} | "
        f"avg/vsl={fleet_all['avg_km_per_vessel']:,.1f} km"
    )

    html = render_html(payload)
    HTML_OUT.write_text(html, encoding="utf-8")
    print(f"\nWrote {HTML_OUT} ({HTML_OUT.stat().st_size:,} bytes)")
    print(f"Mock banner active: {payload['is_mock']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
