"""Build output/archive_dashboard.html — daily vessel archive explorer."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output" / "archive_dashboard.html"
MANIFEST = ROOT / "output" / "archive" / "manifest.json"


def main() -> int:
    # Ensure today's snapshot exists when builder runs alone
    try:
        from services.archive_snapshot_worker import take_daily_snapshot

        take_daily_snapshot()
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: archive snapshot: {exc}", file=sys.stderr)

    manifest = {}
    if MANIFEST.is_file():
        try:
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}

    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest_url": "/output/archive/manifest.json",
        "latest_url": "/output/archive/latest.json",
        "latest": manifest.get("latest"),
        "dates": manifest.get("dates") or [],
        "vessel_count_latest": manifest.get("vessel_count_latest"),
    }
    html = _TEMPLATE.replace("__META__", json.dumps(meta, ensure_ascii=False))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT} vessels={meta.get('vessel_count_latest')} latest={meta.get('latest')}")
    return 0


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Sentinel · Vessel Daily Archive</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-mono@5.0.8/400.css"/>
<style>
:root{
  --bg:#030712;--panel:#0f172a;--cyan:#e0f7fc;--accent:#00f0ff;--muted:#94a3b8;
  --line:rgba(0,240,255,.18);--ok:#34d399;--hi:#f87171;
}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 "IBM Plex Mono",ui-monospace,monospace;background:linear-gradient(180deg,#030712,#0f172a);color:var(--cyan);min-height:100vh}
a{color:var(--accent);text-decoration:none}
.wrap{max-width:1400px;margin:0 auto;padding:20px}
.nav{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:18px}
.nav a,.btn{border:1px solid var(--line);background:rgba(3,7,18,.7);color:var(--cyan);padding:8px 12px;border-radius:8px;cursor:pointer;font:inherit}
.btn:hover,.nav a:hover{border-color:var(--accent)}
h1{font-size:22px;letter-spacing:.08em;margin:0 0 6px}
.sub{color:var(--muted);margin:0 0 18px;font-size:12px}
.toolbar{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:14px}
label{display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
input,select{background:#030712;border:1px solid var(--line);color:var(--cyan);padding:8px 10px;border-radius:8px;font:inherit}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.kpi{border:1px solid var(--line);border-radius:12px;padding:12px;background:rgba(15,23,42,.65)}
.kpi b{display:block;font-size:20px;color:#fff;margin-top:4px}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px;max-height:68vh;background:rgba(3,7,18,.55)}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.06);white-space:nowrap;text-align:left}
th{position:sticky;top:0;background:#0b1220;color:var(--accent);z-index:2}
tr:hover td{background:rgba(0,240,255,.05)}
.badge{display:inline-block;padding:2px 6px;border-radius:6px;border:1px solid var(--line)}
.badge.hi{color:var(--hi);border-color:rgba(248,113,113,.4)}
.badge.ok{color:var(--ok);border-color:rgba(52,211,153,.4)}
.scrub{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.scrub input[type=range]{flex:1;min-width:160px}
#status{color:var(--muted);font-size:12px;margin-top:10px}
@media(max-width:800px){.kpis{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="wrap">
  <div class="nav">
    <a href="/output/sentinel_dashboard.html?sheet=archive">← Sentinel HUD · Archive sheet</a>
    <a href="/output/sentinel_dashboard.html?sheet=top10">TOP-10</a>
    <a href="/output/api/v1/health.json">health.json</a>
  </div>
  <h1>VESSEL DAILY ARCHIVE</h1>
  <p class="sub">Immutable UTC snapshots · 20-parameter fleet freeze · source: sentinel_ais.db + fleet_database.csv</p>

  <div class="scrub">
    <label style="min-width:160px">Snapshot date
      <input type="date" id="datePick"/>
    </label>
    <input type="range" id="dateScrub" min="0" max="0" value="0"/>
    <span id="dateLabel">—</span>
    <button type="button" class="btn" id="btnReload">Load</button>
    <button type="button" class="btn" id="btnCsv">Export CSV</button>
    <button type="button" class="btn" id="btnJson">Export JSON</button>
  </div>

  <div class="toolbar">
    <label>Search IMO / name / MMSI<input id="q" placeholder="filter…"/></label>
    <label>Flag<select id="flag"><option value="">ALL</option></select></label>
    <label>Risk<select id="risk"><option value="">ALL</option></select></label>
    <label>Min AIS integrity<input id="ainMin" type="number" min="0" max="1" step="0.05" value="0"/></label>
  </div>

  <div class="kpis">
    <div class="kpi">Vessels<b id="kN">—</b></div>
    <div class="kpi">Live AIS ≥0.5<b id="kLive">—</b></div>
    <div class="kpi">Avg DWT<b id="kDwt">—</b></div>
    <div class="kpi">Snapshot<b id="kDate">—</b></div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>IMO</th><th>Name</th><th>MMSI</th><th>Flag</th><th>Type</th>
          <th>DWT</th><th>LOA</th><th>Beam</th><th>Draft</th><th>Speed</th>
          <th>Risk</th><th>Origin</th><th>Dest</th><th>AIS∫</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <div id="status">Initializing…</div>
</div>
<script>
const META = __META__;
let DATES = (META.dates || []).slice().sort();
let ROWS = [];
let CURRENT = META.latest || (DATES[DATES.length-1] || "");

function $(id){ return document.getElementById(id); }
function fmt(n, d=0){ const x=Number(n); return Number.isFinite(x)?x.toFixed(d):"—"; }

async function loadManifest(){
  try{
    const m = await fetch(META.manifest_url,{cache:"no-store"}).then(r=>r.json());
    DATES = (m.dates||[]).slice().sort();
    CURRENT = m.latest || CURRENT;
  }catch(e){ console.warn(e); }
  const scrub = $("dateScrub");
  scrub.max = Math.max(0, DATES.length-1);
  const idx = Math.max(0, DATES.indexOf(CURRENT));
  scrub.value = String(idx);
  $("datePick").value = CURRENT || "";
  $("dateLabel").textContent = CURRENT || "no snapshots";
}

async function loadDate(day){
  if(!day){ $("status").textContent="No snapshot date"; return; }
  $("status").textContent = `Loading ${day}…`;
  const url = `/output/archive/snapshots/${day}.json`;
  const data = await fetch(url,{cache:"no-store"}).then(r=>{
    if(!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  });
  ROWS = data.vessels || [];
  CURRENT = day;
  $("datePick").value = day;
  $("dateLabel").textContent = day;
  fillFilters();
  render();
  $("status").textContent = `Loaded ${ROWS.length} vessels · ${day}`;
}

function fillFilters(){
  const flags = [...new Set(ROWS.map(r=>r.flag).filter(Boolean))].sort();
  const risks = [...new Set(ROWS.map(r=>r.risk_level).filter(Boolean))].sort();
  const f=$("flag"), rk=$("risk");
  const keepF=f.value, keepR=rk.value;
  f.innerHTML = `<option value="">ALL</option>` + flags.map(x=>`<option>${x}</option>`).join("");
  rk.innerHTML = `<option value="">ALL</option>` + risks.map(x=>`<option>${x}</option>`).join("");
  f.value = keepF; rk.value = keepR;
}

function filtered(){
  const q=($("q").value||"").trim().toLowerCase();
  const flag=$("flag").value;
  const risk=$("risk").value;
  const amin=Number($("ainMin").value||0);
  return ROWS.filter(r=>{
    if(flag && r.flag!==flag) return false;
    if(risk && r.risk_level!==risk) return false;
    if(Number(r.ais_integrity||0) < amin) return false;
    if(!q) return true;
    const hay = `${r.imo} ${r.vessel_name||""} ${r.mmsi||""}`.toLowerCase();
    return hay.includes(q);
  });
}

function render(){
  const rows = filtered();
  const live = rows.filter(r=>Number(r.ais_integrity||0) >= 0.5).length;
  const dwt = rows.map(r=>Number(r.dwt)||0).filter(x=>x>0);
  const avg = dwt.length ? dwt.reduce((a,b)=>a+b,0)/dwt.length : 0;
  $("kN").textContent = String(rows.length);
  $("kLive").textContent = String(live);
  $("kDwt").textContent = avg ? Math.round(avg).toLocaleString() : "—";
  $("kDate").textContent = CURRENT || "—";
  const tb = $("tbody");
  tb.innerHTML = rows.slice(0, 2000).map(r=>{
    const ain = Number(r.ais_integrity||0);
    const risk = String(r.risk_level||"");
    const riskCls = /HIGH|EXTREME/i.test(risk) ? "hi" : "ok";
    return `<tr>
      <td>${r.imo??"—"}</td>
      <td>${(r.vessel_name||"—").toString().slice(0,28)}</td>
      <td>${r.mmsi??"—"}</td>
      <td>${r.flag||"—"}</td>
      <td>${(r.vessel_type||"—").toString().slice(0,18)}</td>
      <td>${fmt(r.dwt,0)}</td>
      <td>${fmt(r.loa,1)}</td>
      <td>${fmt(r.beam,1)}</td>
      <td>${fmt(r.draft,1)}</td>
      <td>${fmt(r.speed,1)}</td>
      <td><span class="badge ${riskCls}">${risk||"—"}</span></td>
      <td>${(r.origin_port||"—").toString().slice(0,16)}</td>
      <td>${(r.destination_port||"—").toString().slice(0,16)}</td>
      <td>${fmt(ain,2)}</td>
    </tr>`;
  }).join("");
}

function exportBlob(kind){
  if(kind==="csv"){
    window.open(`/output/archive/snapshots/${CURRENT}.csv`, "_blank");
  } else {
    window.open(`/output/archive/snapshots/${CURRENT}.json`, "_blank");
  }
}

$("btnReload").onclick = ()=>loadDate($("datePick").value || CURRENT);
$("btnCsv").onclick = ()=>exportBlob("csv");
$("btnJson").onclick = ()=>exportBlob("json");
$("q").oninput = render;
$("flag").onchange = render;
$("risk").onchange = render;
$("ainMin").oninput = render;
$("datePick").onchange = ()=>loadDate($("datePick").value);
$("dateScrub").oninput = ()=>{
  const i = Number($("dateScrub").value||0);
  if(DATES[i]) loadDate(DATES[i]);
};

(async function init(){
  await loadManifest();
  if(CURRENT) await loadDate(CURRENT);
  else $("status").textContent = "No archive snapshots yet — run archive_snapshot_worker";
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
