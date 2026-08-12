"""Build Apple-styled history_dashboard.html (loads archive via fetch from local server)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "история1"
DAILY = HISTORY / "daily"
OUT = ROOT / "output" / "history_dashboard.html"
TARGETS = ROOT / "targets.json"
ARCHIVE_START = "2026-07-28"


def archive_days() -> list[str]:
    if not DAILY.exists():
        return []
    return sorted(p.stem for p in DAILY.glob("*.csv"))


def main() -> int:
    days = archive_days()
    n_days = len(days)
    meta = {
        "archive_start": ARCHIVE_START,
        "days_accumulated": n_days,
        "daily_files": days,
        "last_updated": days[-1] if days else None,
        "targets_path": "targets.json",
        "daily_glob": "история1/daily/*.csv",
        "by_vessel_dir": "история1/by_vessel/",
        "forecast_dir": "история1/forecast/",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "honest_note": (
            "Data collection started: 28.07.2026. There is no retroactive history before this date. "
            "Archive depth only grows forward while the collector runs."
        ),
    }
    if TARGETS.exists():
        t = json.loads(TARGETS.read_text(encoding="utf-8"))
        meta["n_targets"] = len(t.get("targets", []))
        meta["stats"] = t.get("stats")

    html = _TEMPLATE.replace("__META_JSON__", json.dumps(meta, ensure_ascii=False))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT} (days_accumulated={n_days})")
    return 0


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Fleet Intelligence · AIS Archive</title>
<link rel="stylesheet" href="design_system.css" />
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css" />
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  .hist-main { display:grid; grid-template-columns: 280px 1fr 300px; gap:16px; min-height: 62vh; }
  #map { height: 100%; min-height: 420px; border-radius: 16px; }
  .list { max-height: 52vh; overflow:auto; }
  .item { padding:10px 12px; border-radius:12px; cursor:pointer; display:flex; justify-content:space-between; gap:8px; }
  .item:hover, .item.active { background: color-mix(in srgb, var(--accent) 10%, transparent); }
  .item .km { font-variant-numeric: tabular-nums; font-weight:600; color: var(--accent); }
  @media (max-width: 1100px) { .hist-main { grid-template-columns: 1fr; } #map { min-height: 320px; } }
</style>
</head>
<body>
<a class="fi-skip" href="#main">Skip to content</a>
<header class="fi-nav no-print">
  <a class="fi-brand" href="mission_control.html"><span class="fi-brand-mark">FI</span> Fleet Intelligence</a>
  <div class="fi-nav-links">
    <a class="fi-pill" href="mission_control.html">Mission Control</a>
    <a class="fi-pill" href="dashboard.html">Fleet</a>
    <a class="fi-pill" href="forecast_dashboard.html">Forecast</a>
    <button type="button" class="fi-theme-toggle" id="theme-toggle" aria-label="Toggle color theme">◐</button>
  </div>
</header>

<div class="fi-page" id="main">
  <div class="fi-skeleton-wrap">
    <div class="fi-skeleton" style="height:72px;margin-bottom:16px"></div>
    <div class="fi-grid"><div class="fi-skeleton"></div><div class="fi-skeleton"></div><div class="fi-skeleton"></div><div class="fi-skeleton"></div></div>
  </div>
  <div class="fi-ready-wrap">
    <h1 class="fi-title">AIS Tracking Archive</h1>
    <p class="fi-muted" style="margin-top:8px">Fact tracks in Apple Blue · forecast estimates in orange dashed</p>
    <div class="fi-banner wait" id="banner" style="margin-top:16px"></div>
    <div class="fi-grid" id="metrics" style="margin:16px 0"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
      <span class="fi-muted" style="font-size:13px;font-weight:600">Period</span>
      <button type="button" class="fi-pill active" data-p="30">1 month</button>
      <button type="button" class="fi-pill" data-p="90">3 months</button>
      <button type="button" class="fi-pill" data-p="365">1 year</button>
      <button type="button" class="fi-pill" data-p="all">All</button>
    </div>
    <div class="fi-banner wait" id="period-warn" style="display:none"></div>
    <div class="hist-main">
      <aside class="fi-card">
        <div class="fi-card-title">Vessel index</div>
        <input class="fi-pill" id="search" placeholder="IMO / name…" style="width:100%;margin:8px 0 12px;border-radius:12px;border:1px solid var(--separator);background:var(--bg)" />
        <div class="list" id="list"></div>
      </aside>
      <section class="fi-card" style="padding:12px">
        <div class="fi-card-title" style="padding:8px 8px 0">Map</div>
        <div id="map"></div>
      </section>
      <aside class="fi-card">
        <div class="fi-card-title">Dossier</div>
        <div id="dossier" class="fi-muted">Select a vessel</div>
        <div class="fi-card-title" style="margin-top:16px">Top-10 km</div>
        <div class="fi-chart" style="height:220px"><canvas id="chart"></canvas></div>
      </aside>
    </div>
  </div>
</div>
<script>
const META = __META_JSON__;
const PERIOD_DAYS = { "30":30, "90":90, "365":365, "all":null };
let periodKey = "30";
let selected = null;
let map, chart;
let vesselIndex = {};
const polylines = {}, forecastLines = {}, markers = {};

(function themeInit(){
  const root = document.documentElement;
  const saved = localStorage.getItem("fi-theme");
  if(saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  document.getElementById("theme-toggle").onclick = () => {
    const cur = root.getAttribute("data-theme");
    const preferDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const nowDark = cur === "dark" || (!cur && preferDark);
    const next = nowDark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem("fi-theme", next);
  };
})();

function chartColors(){
  const cs = getComputedStyle(document.documentElement);
  return {
    accent: cs.getPropertyValue("--accent").trim() || "#0071E3",
    tick: cs.getPropertyValue("--chart-tick").trim() || "#6E6E73",
    grid: cs.getPropertyValue("--chart-grid").trim() || "rgba(0,0,0,.06)",
    forecast: cs.getPropertyValue("--map-forecast").trim() || "#FF9F0A",
    track: cs.getPropertyValue("--map-track").trim() || "#0071E3",
  };
}

function renderBanner(){
  const n = META.days_accumulated;
  const el = document.getElementById("banner");
  const tone = n === 0 ? "wait" : (n < 30 ? "wait" : "live");
  el.className = "fi-banner " + tone;
  const status = n === 0 ? "STANDBY — waiting for collector" : (n < 30 ? `ACCUMULATING — ${n} / 30 days` : `LIVE — ${n} days`);
  el.innerHTML = `<span class="fi-status ${tone}"><span class="fi-dot ${tone}"></span>${status}</span>
    <div style="margin-top:10px"><strong>Collection started: ${META.archive_start}</strong> · Days: <strong>${n}</strong> · Updated: ${META.last_updated || "—"}<br/>${META.honest_note}</div>`;
}

function checkPeriodDepth(){
  const need = PERIOD_DAYS[periodKey];
  const warn = document.getElementById("period-warn");
  const have = META.days_accumulated;
  if(need != null && have < need){
    warn.style.display = "block";
    warn.textContent = `Requested period (${need}d) exceeds archive depth (${have}d). Showing accumulated data only — not fabricated history.`;
  } else {
    warn.style.display = "none";
  }
}

async function loadDailyIndex(){
  const files = META.daily_files || [];
  for(const day of files){
    try{
      const text = await fetch(`../история1/daily/${day}.csv`).then(r=>r.text());
      const lines = text.trim().split(/\r?\n/);
      if(lines.length < 2) continue;
      const headers = lines[0].split(",");
      const idx = Object.fromEntries(headers.map((h,i)=>[h.trim(), i]));
      for(let i=1;i<lines.length;i++){
        const cols = parseCsvLine(lines[i]);
        const imo = cols[idx.imo];
        if(!imo) continue;
        if(!vesselIndex[imo]) vesselIndex[imo] = {imo, name: cols[idx.vessel_name]||"", rows:[]};
        vesselIndex[imo].rows.push({
          date: day, lat: num(cols[idx.lat]), lon: num(cols[idx.lon]),
          km: num(cols[idx.km_last_24h])||0, speed: num(cols[idx.speed]),
          heading: num(cols[idx.heading]), source: cols[idx.data_source], ts: cols[idx.timestamp],
        });
      }
    }catch(e){ console.warn("daily load fail", day, e); }
  }
}
function parseCsvLine(line){
  const out=[]; let cur=""; let q=false;
  for(let i=0;i<line.length;i++){
    const c=line[i];
    if(c==='"'){ q=!q; continue; }
    if(c===',' && !q){ out.push(cur); cur=""; continue; }
    cur+=c;
  }
  out.push(cur); return out;
}
function num(v){ const n=parseFloat(v); return Number.isFinite(n)?n:null; }
function filteredRows(v){
  const rows = v.rows || [];
  if(periodKey==="all" || !rows.length) return rows;
  const need = PERIOD_DAYS[periodKey];
  const last = rows[rows.length-1].date;
  const cutoff = new Date(last); cutoff.setDate(cutoff.getDate()-need);
  const cut = cutoff.toISOString().slice(0,10);
  return rows.filter(r => r.date >= cut);
}
function vesselKm(v){ return filteredRows(v).reduce((s,r)=>s+(r.km||0),0); }

function renderMetrics(){
  const vs = Object.values(vesselIndex);
  const active = vs.filter(v => filteredRows(v).some(r=>r.source==="aisstream_live"));
  const totalKm = vs.reduce((s,v)=>s+vesselKm(v),0);
  const items = [
    {l:"Archive days", v: META.days_accumulated},
    {l:"Targets", v: META.n_targets||vs.length},
    {l:"Live in period", v: active.length},
    {l:"Km in period", v: totalKm.toFixed(0)},
  ];
  document.getElementById("metrics").innerHTML = items.map(i=>`
    <div class="fi-card"><div class="fi-card-title">${i.l}</div><div class="fi-metric">${i.v}</div></div>`).join("");
}

function renderList(){
  const q=(document.getElementById("search").value||"").toLowerCase();
  const rows = Object.values(vesselIndex).map(v=>({v, km:vesselKm(v)}))
    .filter(x=>!q || x.v.imo.includes(q) || (x.v.name||"").toLowerCase().includes(q))
    .sort((a,b)=>b.km-a.km);
  document.getElementById("list").innerHTML = rows.slice(0,500).map(x=>`
    <div class="item ${x.v.imo===selected?"active":""}" data-imo="${x.v.imo}" tabindex="0">
      <div><div style="font-weight:600">${x.v.name||"(no name)"}</div><div class="fi-muted" style="font-size:13px">IMO ${x.v.imo}</div></div>
      <div class="km">${x.km.toFixed(0)}</div>
    </div>`).join("") || `<div class="fi-muted">Archive empty (days: ${META.days_accumulated})</div>`;
  document.querySelectorAll(".item[data-imo]").forEach(el=>{
    el.onclick=()=>selectVessel(el.dataset.imo);
    el.onkeydown=(e)=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); selectVessel(el.dataset.imo);} };
  });
}

async function loadForecast(imo){
  try{
    const fc = await fetch(`../история1/forecast/${imo}.json`).then(r=>{ if(!r.ok) throw new Error("no fc"); return r.json(); });
    vesselIndex[imo].forecast = fc;
  }catch(_){ vesselIndex[imo].forecast = null; }
}
function clearMap(){
  Object.values(polylines).forEach(l=>map.removeLayer(l));
  Object.values(forecastLines).forEach(l=>map.removeLayer(l));
  Object.values(markers).forEach(m=>map.removeLayer(m));
  for(const k of Object.keys(polylines)) delete polylines[k];
  for(const k of Object.keys(forecastLines)) delete forecastLines[k];
  for(const k of Object.keys(markers)) delete markers[k];
}
function drawSelected(){
  clearMap();
  const c = chartColors();
  if(!selected || !vesselIndex[selected]) return;
  const v = vesselIndex[selected];
  const rows = filteredRows(v).filter(r=>r.lat!=null && r.lon!=null);
  if(rows.length){
    const latlngs = rows.map(r=>[r.lat,r.lon]);
    polylines[selected] = L.polyline(latlngs,{color:c.track,weight:2.5,opacity:0.9}).addTo(map);
    markers[selected] = L.circleMarker(latlngs[latlngs.length-1],{
      radius:6,color:c.track,fillColor:c.track,fillOpacity:0.95,weight:1
    }).addTo(map);
    map.fitBounds(polylines[selected].getBounds(),{padding:[40,40]});
  }
  const fc = v.forecast;
  if(fc && rows.length){
    const last = rows[rows.length-1];
    const pts = (fc.course_speed_projection||[]);
    if(pts.length){
      const ll = [[last.lat,last.lon], ...pts.map(p=>[p.lat,p.lon])];
      forecastLines[selected] = L.polyline(ll,{ color:c.forecast, weight:2, dashArray:"8 8", opacity:0.85 })
        .addTo(map).bindTooltip("ESTIMATE (dead-reckoning)");
    }
  }
}
function renderDossier(){
  const el = document.getElementById("dossier");
  if(!selected || !vesselIndex[selected]){ el.textContent="Select a vessel"; return; }
  const v = vesselIndex[selected];
  const rows = filteredRows(v);
  const km = vesselKm(v);
  const last = rows[rows.length-1];
  const fc = v.forecast;
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--separator)"><span class="fi-muted">Name</span><strong>${v.name||"—"}</strong></div>
    <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--separator)"><span class="fi-muted">IMO</span><strong>${v.imo}</strong></div>
    <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--separator)"><span class="fi-muted">Km period</span><strong>${km.toFixed(1)}</strong></div>
    <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--separator)"><span class="fi-muted">Days w/ data</span><strong>${rows.length}</strong></div>
    <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--separator)"><span class="fi-muted">Last src</span><strong>${last?last.source:"—"}</strong></div>
    <p class="fi-muted" style="margin-top:12px;font-size:13px">${fc?fc.disclaimer:"Forecast appears after live points exist in raw_positions.db"}</p>`;
}
function renderChart(){
  const c = chartColors();
  const top = Object.values(vesselIndex).map(v=>({name:v.name||v.imo, km:vesselKm(v)}))
    .sort((a,b)=>b.km-a.km).slice(0,10);
  if(chart) chart.destroy();
  chart = new Chart(document.getElementById("chart"),{
    type:"bar",
    data:{labels:top.map(t=>t.name.slice(0,14)), datasets:[{data:top.map(t=>t.km),
      backgroundColor: c.accent + "33", borderColor:c.accent, borderWidth:1.5, borderRadius:6}]},
    options:{indexAxis:"y", responsive:true, maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{x:{ticks:{color:c.tick},grid:{color:c.grid}},
              y:{ticks:{color:c.tick,font:{size:11}},grid:{display:false}}}}
  });
}
async function selectVessel(imo){ selected = imo; await loadForecast(imo); renderList(); renderDossier(); drawSelected(); }
function setPeriod(k){
  periodKey = k;
  document.querySelectorAll("[data-p]").forEach(b=>b.classList.toggle("active", b.dataset.p===k));
  checkPeriodDepth(); renderMetrics(); renderList(); renderChart();
  if(selected){ renderDossier(); drawSelected(); }
}
async function init(){
  renderBanner();
  map = L.map("map").setView([20,40],2);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",{
    attribution:"&copy; OSM &copy; CARTO", maxZoom:18
  }).addTo(map);
  await loadDailyIndex();
  document.querySelectorAll("[data-p]").forEach(b=>b.onclick=()=>setPeriod(b.dataset.p));
  document.getElementById("search").oninput=renderList;
  setPeriod("30");
  document.body.classList.add("is-ready");
  setTimeout(()=>map.invalidateSize(),200);
}
init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
