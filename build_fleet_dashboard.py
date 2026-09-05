"""Rebuild output/dashboard.html in Apple HIG style from existing fleet artifacts."""

from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
FLEET = OUTPUT / "fleet_database.csv"
IDENTITY = OUTPUT / "identity_conflicts.csv"
OUT = OUTPUT / "dashboard.html"
DWT_JS_SRC = ROOT / "dwt_filter_sort.js"
DWT_JS_OUT = OUTPUT / "dwt_filter_sort.js"

# Schema field for deadweight (see schema.py / fleet_database.csv)
DWT_FIELD = "dwt_tons"


def data_dwt_attr(dwt_tons: Any) -> str:
    """HTML data-dwt value: real DWT, or \"0\" when absent/invalid (no invented tonnage)."""
    if dwt_tons is None:
        return "0"
    if isinstance(dwt_tons, float) and (math.isnan(dwt_tons) or math.isinf(dwt_tons)):
        return "0"
    if isinstance(dwt_tons, str) and not dwt_tons.strip():
        return "0"
    try:
        n = float(dwt_tons)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(n):
        return "0"
    if n == int(n):
        return str(int(n))
    return str(n)


def dwt_is_missing(dwt_tons: Any) -> bool:
    """True when source has no usable DWT (do not invent a tonnage)."""
    if dwt_tons is None:
        return True
    if isinstance(dwt_tons, float) and math.isnan(dwt_tons):
        return True
    if isinstance(dwt_tons, str) and not str(dwt_tons).strip():
        return True
    try:
        n = float(dwt_tons)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(n)


def render_fleet_tr(record: dict, cols: list[str]) -> str:
    """Server-side <tr> with data-dwt for tests / static inspection."""
    dwt = record.get(DWT_FIELD)
    attr = data_dwt_attr(dwt)
    missing = dwt_is_missing(dwt)
    miss_attr = ' data-dwt-missing="1"' if missing else ""
    cells = []
    for c in cols:
        v = record.get(c)
        if v is None:
            text = ""
        else:
            text = str(v)
        text = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        cells.append(f"<td>{text}</td>")
    return f'<tr data-dwt="{attr}"{miss_attr}>{"".join(cells)}</tr>'


def _df_records(df: pd.DataFrame) -> list[dict]:
    records = []
    for row in df.to_dict(orient="records"):
        clean = {}
        for k, v in row.items():
            if pd.isna(v) if not isinstance(v, (list, dict)) else False:
                clean[k] = None
            elif hasattr(v, "item"):
                clean[k] = v.item()
            else:
                clean[k] = v
        records.append(clean)
    return records


def ensure_dwt_js() -> None:
    """Copy client module next to dashboard.html (relative script src)."""
    if not DWT_JS_SRC.exists():
        raise FileNotFoundError(f"Missing {DWT_JS_SRC}")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DWT_JS_SRC, DWT_JS_OUT)


def compute_metrics(fleet: pd.DataFrame) -> dict:
    from pathlib import Path as P

    def n(path: P) -> int:
        return len(pd.read_csv(path)) if path.exists() else 0

    vessels = fleet[fleet.get("vessel_category", "vessel") == "vessel"] if "vessel_category" in fleet.columns else fleet
    valid_imo = int(vessels["imo_valid"].fillna(False).astype(bool).sum()) if "imo_valid" in vessels.columns else int(vessels["imo"].nunique())
    return {
        "source_rows": 6036,
        "vessel_count": int(len(vessels)),
        "non_vessel": n(OUTPUT / "non_vessel_entities.csv"),
        "valid_imo": valid_imo,
        "needs_review": n(OUTPUT / "needs_review.csv"),
        "imo_mismatch": n(OUTPUT / "imo_mismatch.csv"),
        "identity_conflicts": n(OUTPUT / "identity_conflicts.csv"),
        "invalid_imo": n(OUTPUT / "invalid_imo_checksum.csv"),
        "avg_fill": 59.96,
    }


def write_dashboard(
    fleet_df: pd.DataFrame,
    identity_df: pd.DataFrame,
    metrics: dict,
    path: Path = OUT,
) -> None:
    ensure_dwt_js()
    # Keep JS next to the HTML even when writing to a temp path (tests).
    if path.parent.resolve() != OUTPUT.resolve():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DWT_JS_SRC, path.parent / "dwt_filter_sort.js")

    fleet_json = json.dumps(_df_records(fleet_df), ensure_ascii=False)
    identity_json = json.dumps(_df_records(identity_df), ensure_ascii=False)
    metrics_json = json.dumps(metrics, ensure_ascii=False)
    html = _TEMPLATE.replace("__FLEET_JSON__", fleet_json).replace(
        "__IDENTITY_JSON__", identity_json
    ).replace("__METRICS_JSON__", metrics_json)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    print(f"Wrote {path} (vessels={metrics.get('vessel_count')})")


def main() -> int:
    if not FLEET.exists():
        print(f"ERROR: {FLEET} missing", file=sys.stderr)
        return 1
    fleet = pd.read_csv(FLEET, low_memory=False)
    identity = pd.read_csv(IDENTITY) if IDENTITY.exists() else fleet.iloc[0:0].copy()
    metrics = compute_metrics(fleet)
    # Prefer embedded METRICS from previous dashboard if present
    if OUT.exists():
        import re

        m = re.search(r"const METRICS = (\{.*?\});", OUT.read_text(encoding="utf-8"), re.S)
        if m:
            try:
                metrics = json.loads(m.group(1))
            except Exception:
                pass
    write_dashboard(fleet, identity, metrics, OUT)
    return 0


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Fleet Intelligence · Registry</title>
<link rel="stylesheet" href="design_system.css" />
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.8/css/jquery.dataTables.min.css" />
<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
<script src="https://cdn.datatables.net/1.13.8/js/jquery.dataTables.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  .filters { display:grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap:10px; margin-bottom:14px; }
  .filters label { display:block; font-size:12px; color:var(--text-secondary); margin-bottom:4px; font-weight:600; }
  .filters select, .filters input[type="number"] { width:100%; padding:8px 10px; border-radius:12px; border:1px solid var(--separator); background:var(--bg); color:var(--text); font:inherit; box-sizing:border-box; }
  .filters .dwt-sort-wrap { display:flex; align-items:flex-end; }
  .filters #dwt-sort-toggle { width:100%; padding:8px 10px; border-radius:12px; border:1px solid var(--separator); background:var(--fill); color:var(--text); font:inherit; font-weight:600; cursor:pointer; }
  .filters #dwt-sort-toggle:hover { border-color:var(--accent); color:var(--accent); }
  .raw-expand { display:none; margin-top:12px; padding:14px; border-radius:12px; background:var(--bg); border:1px solid var(--separator); white-space:pre-wrap; max-height:320px; overflow:auto; font-size:13px; }
  .raw-expand.open { display:block; }
  .charts { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:900px){ .charts { grid-template-columns:1fr; } }
</style>
</head>
<body>
<a class="fi-skip" href="#main">Skip to content</a>
<header class="fi-nav no-print">
  <a class="fi-brand" href="mission_control.html"><span class="fi-brand-mark">FI</span> Fleet Intelligence</a>
  <div class="fi-nav-links">
    <a class="fi-pill" href="mission_control.html">Mission Control</a>
    <a class="fi-pill active" href="dashboard.html">Fleet</a>
    <a class="fi-pill" href="osint_layers.html">9 Layers</a>
    <a class="fi-pill" href="history_dashboard.html">AIS Archive</a>
    <a class="fi-pill" href="forecast_dashboard.html">Forecast</a>
    <button type="button" class="fi-theme-toggle" id="theme-toggle" aria-label="Toggle color theme">◐</button>
  </div>
</header>
<div class="fi-page" id="main">
  <div class="fi-skeleton-wrap"><div class="fi-skeleton" style="height:80px;margin-bottom:16px"></div><div class="fi-grid"><div class="fi-skeleton"></div><div class="fi-skeleton"></div><div class="fi-skeleton"></div><div class="fi-skeleton"></div></div></div>
  <div class="fi-ready-wrap">
    <h1 class="fi-title">Fleet Registry</h1>
    <p class="fi-muted" style="margin-top:8px">OSINT-normalized vessel database · local embedded data · iteration 5</p>
    <div class="fi-banner live" style="margin-top:16px">
      <span class="fi-status live"><span class="fi-dot live"></span>LIVE</span>
      <div style="margin-top:8px">Static since last <code>run_all.py</code> — not a streaming AIS feed.</div>
    </div>
    <div class="fi-grid" id="metrics" style="margin:16px 0"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
      <button type="button" class="fi-pill active" data-tab="fleet">Fleet database</button>
      <button type="button" class="fi-pill" data-tab="identity">Identity conflicts</button>
      <button type="button" class="fi-pill" data-tab="charts">Charts</button>
    </div>
    <section id="tab-fleet" class="fi-card">
      <div class="filters" id="dwt-filter-sort">
        <div><label for="f-confidence">source_confidence</label><select id="f-confidence"><option value="">All</option></select></div>
        <div><label for="f-category">vessel_category</label><select id="f-category"><option value="">All</option></select></div>
        <div><label for="f-flag">flag</label><select id="f-flag"><option value="">All</option></select></div>
        <div><label for="f-risk">compliance_risk_level</label><select id="f-risk"><option value="">All</option></select></div>
        <div><label for="f-type">vessel_type</label><select id="f-type"><option value="">All</option></select></div>
        <div><label for="dwt-from">DWT от</label><input type="number" id="dwt-from" min="0" step="1" placeholder="мин" inputmode="numeric" /></div>
        <div><label for="dwt-to">DWT до</label><input type="number" id="dwt-to" min="0" step="1" placeholder="макс" inputmode="numeric" /></div>
        <div class="dwt-sort-wrap"><button type="button" id="dwt-sort-toggle" data-dir="desc">DWT ↓ убыв.</button></div>
      </div>
      <div class="fi-table-wrap"><table id="fleet-table" class="display" style="width:100%"></table></div>
      <div id="raw-panel" class="raw-expand"></div>
    </section>
    <section id="tab-identity" class="fi-card hidden">
      <h2 style="color:var(--danger);margin-bottom:8px">Identity spoofing suspects — priority #1</h2>
      <p class="fi-muted">Rows where text mentions a different “true/real” IMO than the primary identifier.</p>
      <div class="fi-table-wrap"><table id="identity-table" class="display" style="width:100%"></table></div>
    </section>
    <section id="tab-charts" class="hidden">
      <div class="charts">
        <div class="fi-card"><div class="fi-card-title">Compliance risk</div><div class="fi-chart"><canvas id="chart-risk"></canvas></div></div>
        <div class="fi-card"><div class="fi-card-title">Top-10 flags</div><div class="fi-chart"><canvas id="chart-flags"></canvas></div></div>
      </div>
    </section>
  </div>
</div>
<script src="dwt_filter_sort.js"></script>
<script>
const FLEET_DATA = __FLEET_JSON__;
const IDENTITY_DATA = __IDENTITY_JSON__;
const METRICS = __METRICS_JSON__;
const COLS = ["imo","vessel_name","mmsi","call_sign","vessel_type","built_year","age_years","flag","dwt_tons","gt","loa_m","beam_m","draft_m","nav_status","speed_knots","compliance_risk_level","sanctions_tags","destination_port","destination_context","departure_port","arrival_datetime","source_confidence"];

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

function uniq(arr){ return [...new Set(arr.filter(v => v !== null && v !== undefined && String(v).trim() !== ""))].sort(); }
function fillSelect(id, values){
  const el = document.getElementById(id);
  values.forEach(v => { const o=document.createElement("option"); o.value=String(v); o.textContent=String(v); el.appendChild(o); });
}
function chartColors(){
  const cs = getComputedStyle(document.documentElement);
  return { accent: cs.getPropertyValue("--accent").trim()||"#0071E3", tick: cs.getPropertyValue("--chart-tick").trim()||"#6E6E73", grid: cs.getPropertyValue("--chart-grid").trim()||"rgba(0,0,0,.06)", danger: cs.getPropertyValue("--danger").trim()||"#FF3B30" };
}

function renderMetrics(){
  const items = [
    { label: "Vessels", value: METRICS.vessel_count },
    { label: "Valid IMO", value: METRICS.valid_imo },
    { label: "Needs review", value: METRICS.needs_review },
    { label: "IMO mismatch", value: METRICS.imo_mismatch },
    { label: "Identity conflicts", value: METRICS.identity_conflicts, danger: true },
    { label: "Non-vessel", value: METRICS.non_vessel },
  ];
  document.getElementById("metrics").innerHTML = items.map(it => `
    <div class="fi-card"><div class="fi-card-title">${it.danger?"⚠ ":""}${it.label}</div>
    <div class="fi-metric ${it.danger?"danger":""}">${it.value}</div></div>`).join("");
}

let fleetTable, identityTable;
function initFleetTable(){
  fillSelect("f-confidence", uniq(FLEET_DATA.map(r => r.source_confidence)));
  fillSelect("f-category", uniq(FLEET_DATA.map(r => r.vessel_category)));
  fillSelect("f-flag", uniq(FLEET_DATA.map(r => r.flag)));
  fillSelect("f-risk", uniq(FLEET_DATA.map(r => r.compliance_risk_level)));
  fillSelect("f-type", uniq(FLEET_DATA.map(r => r.vessel_type)).slice(0, 200));
  fleetTable = new DataTable("#fleet-table", {
    data: FLEET_DATA,
    columns: COLS.map(c => ({ title: c, data: c, render: (d) => d == null ? "" : String(d) })),
    pageLength: 25,
    scrollX: true,
    order: [],
    deferRender: true,
    createdRow: function(row, data){
      if (window.DwtFilterSort && typeof DwtFilterSort.stampRow === "function") {
        DwtFilterSort.stampRow(row, data ? data.dwt_tons : null);
      } else {
        const raw = data && data.dwt_tons;
        const missing = raw == null || raw === "" || !Number.isFinite(Number(raw));
        row.setAttribute("data-dwt", missing ? "0" : String(Number(raw)));
        if (missing) row.setAttribute("data-dwt-missing", "1");
      }
    },
  });
  $("#fleet-table tbody").on("click", "tr", function(){
    const data = fleetTable.row(this).data();
    if(!data) return;
    const panel = document.getElementById("raw-panel");
    panel.textContent = data.raw_text || "(empty)";
    panel.classList.add("open");
  });
  function applyFilters(){
    const conf = document.getElementById("f-confidence").value;
    const cat = document.getElementById("f-category").value;
    const flag = document.getElementById("f-flag").value;
    const risk = document.getElementById("f-risk").value;
    const typ = document.getElementById("f-type").value;
    $.fn.dataTable.ext.search = $.fn.dataTable.ext.search.filter(fn => !fn._fleetFilter);
    const filterFn = function(settings, data, dataIndex){
      if(settings.nTable.id !== "fleet-table") return true;
      const row = fleetTable.row(dataIndex).data();
      if(!row) return true;
      if(conf && String(row.source_confidence||"") !== conf) return false;
      if(cat && String(row.vessel_category||"") !== cat) return false;
      if(flag && String(row.flag||"") !== flag) return false;
      if(risk && String(row.compliance_risk_level||"") !== risk) return false;
      if(typ && String(row.vessel_type||"") !== typ) return false;
      return true;
    };
    filterFn._fleetFilter = true;
    $.fn.dataTable.ext.search.push(filterFn);
    fleetTable.draw();
  }
  ["f-confidence","f-category","f-flag","f-risk","f-type"].forEach(id => document.getElementById(id).onchange = applyFilters);
  if (window.DwtFilterSort) {
    DwtFilterSort.init({ dataTable: fleetTable, tableSelector: "#fleet-table" });
  }
}

function initIdentityTable(){
  identityTable = new DataTable("#identity-table", {
    data: IDENTITY_DATA,
    columns: [
      {title:"imo", data:"imo"},
      {title:"vessel_name", data:"vessel_name"},
      {title:"suspected_imo", data:"identity_spoofing_suspected_imo"},
      {title:"note", data:"identity_spoofing_note"},
      {title:"source_confidence", data:"source_confidence"},
    ],
    pageLength: 25,
    order: [],
  });
}

function initCharts(){
  const c = chartColors();
  const riskCounts = {};
  FLEET_DATA.forEach(r => { const k = r.compliance_risk_level || "(null)"; riskCounts[k]=(riskCounts[k]||0)+1; });
  const flagCounts = {};
  FLEET_DATA.forEach(r => { const k = r.flag || "(null)"; flagCounts[k]=(flagCounts[k]||0)+1; });
  const topFlags = Object.entries(flagCounts).sort((a,b)=>b[1]-a[1]).slice(0,10);
  new Chart(document.getElementById("chart-risk"), {
    type:"doughnut",
    data:{ labels:Object.keys(riskCounts), datasets:[{ data:Object.values(riskCounts), backgroundColor:[c.accent,c.danger,"#FF9F0A","#30D158","#98989D","#64D2FF"] }] },
    options:{ plugins:{ legend:{ labels:{ color:c.tick } } } }
  });
  new Chart(document.getElementById("chart-flags"), {
    type:"bar",
    data:{ labels:topFlags.map(x=>String(x[0]).slice(0,16)), datasets:[{ data:topFlags.map(x=>x[1]), backgroundColor:c.accent+"33", borderColor:c.accent, borderWidth:1.5, borderRadius:6 }] },
    options:{ plugins:{ legend:{ display:false } }, scales:{ x:{ ticks:{ color:c.tick }, grid:{ color:c.grid } }, y:{ ticks:{ color:c.tick }, grid:{ color:c.grid } } } }
  });
}

document.querySelectorAll("[data-tab]").forEach(btn => btn.onclick = () => {
  document.querySelectorAll("[data-tab]").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  const tab = btn.getAttribute("data-tab");
  document.getElementById("tab-fleet").classList.toggle("hidden", tab !== "fleet");
  document.getElementById("tab-identity").classList.toggle("hidden", tab !== "identity");
  document.getElementById("tab-charts").classList.toggle("hidden", tab !== "charts");
});

renderMetrics();
initFleetTable();
initIdentityTable();
initCharts();
document.body.classList.add("is-ready");
</script>
<style>.hidden{display:none!important}</style>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
