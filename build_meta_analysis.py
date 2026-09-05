"""
META-ANALYSIS Generator — ORACLE-1001
======================================
Produces ``output/fleet_meta_analysis.html`` — a self-contained NASA / Wet-Glass
analytics page with absolute macro metrics across all 1 253 vessels × 20 TZ columns.

Sections
--------
1. Macro banner   – total DWT / GT, vessel count, provenance summary
2. Numeric cards  – sum / avg / median / min / max per numeric column
3. Category cards – unique count + Top-5 per categorical column
4. Identity cards – uniqueness checks for ID / text columns
5. Provenance matrix – per-column OSINT / ⚡Synth / 🔬KNN / 🌐AIS bar breakdown
"""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

# ── Column classification ─────────────────────────────────────────────────────
TZ_COLUMNS = [
    "vessel_name", "imo", "mmsi", "call_sign", "vessel_type",
    "built_year", "age_years", "flag", "dwt_tons", "gt",
    "loa_m", "beam_m", "draft_m", "nav_status", "speed_knots",
    "destination_port", "destination_context", "departure_port",
    "arrival_datetime", "compliance_risk_level",
]

_NUMERIC = {
    "dwt_tons":    ("ДЕДВЕЙТ (DWT)",        "т",     True),
    "gt":          ("ВАЛОВАЯ ВМЕСТИМОСТЬ",  "GT",    True),
    "loa_m":       ("ДЛИНА (LOA)",          "м",     False),
    "beam_m":      ("ШИРИНА (BEAM)",        "м",     False),
    "draft_m":     ("ОСАДКА (DRAFT)",       "м",     False),
    "speed_knots": ("СКОРОСТЬ",             "узлов", False),
    "age_years":   ("ВОЗРАСТ",              "лет",   False),
    "built_year":  ("ГОД ПОСТРОЙКИ",        "год",   False),
}

_CATEGORICAL = {
    "flag":                "ФЛАГ ГОСУДАРСТВА",
    "vessel_type":         "ТИП СУДНА",
    "nav_status":          "НАВИГАЦИОННЫЙ СТАТУС",
    "compliance_risk_level": "УРОВЕНЬ РИСКА",
    "departure_port":      "ПОРТ ОТБЫТИЯ",
    "destination_port":    "ПОРТ НАЗНАЧЕНИЯ",
}

_IDENTITY = {
    "vessel_name":          "НАИМЕНОВАНИЕ СУДНА",
    "imo":                  "НОМЕР IMO",
    "mmsi":                 "КОД MMSI",
    "call_sign":            "ПОЗЫВНОЙ",
    "arrival_datetime":     "ВРЕМЯ ПРИБЫТИЯ (ETA)",
    "destination_context":  "КОНТЕКСТ МАРШРУТА",
}

_PROVENANCE_COLOR = {
    "osint":  "#10b981",   # green
    "synth":  "#f59e0b",   # amber
    "knn":    "#00f2fe",   # cyan
    "reg":    "#63caff",   # blue
}

_EMPTY_VALS = {"", "—", "-", "none", "не извлечено", "nan", "n/a"}


# ── Helper functions ──────────────────────────────────────────────────────────
def _is_present(v: Any) -> bool:
    if v is None: return False
    return str(v).strip().lower() not in _EMPTY_VALS


def _safe_float(v: Any) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _fmt_big(n: float) -> str:
    """1 234 567.89"""
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f} млн"
    if n >= 1_000:
        return f"{n:,.0f}".replace(",", " ")
    return f"{n:.1f}"


def _provenance_for(col: str, records: list[dict]) -> dict[str, int]:
    """Count OSINT / synth / knn / reg for a single column across the fleet."""
    counts = {"osint": 0, "synth": 0, "knn": 0, "reg": 0}
    for rec in records:
        sf  = {s.strip().lower() for s in str(rec.get("synthetic_fields")    or "").split(";") if s.strip()}
        kf  = {s.strip().lower() for s in str(rec.get("imputed_fields")      or "").split(";") if s.strip()}
        rf  = {s.strip().lower() for s in str(rec.get("registry_mock_fields") or "").split(";") if s.strip()}
        c   = col.lower()
        if c in rf:
            counts["reg"]   += 1
        elif c in kf:
            counts["knn"]   += 1
        elif c in sf:
            counts["synth"] += 1
        else:
            counts["osint"] += 1
    return counts


# ── Compute all aggregates ────────────────────────────────────────────────────
def compute_meta(df: pd.DataFrame) -> dict:
    """Return a JSON-serialisable dict with all macro-analytics."""
    total  = len(df)
    records = df.to_dict(orient="records")

    # ── 1. Numeric stats ──────────────────────────────────────────────────────
    numeric_stats: dict[str, dict] = {}
    for col, (label, unit, has_sum) in _NUMERIC.items():
        vals = [f for v in df[col].tolist() if (f := _safe_float(v)) is not None]
        if not vals:
            numeric_stats[col] = {"label": label, "unit": unit, "has_sum": has_sum,
                                   "count": 0, "sum": None, "avg": None, "median": None,
                                   "min": None, "max": None, "provenance": {}}
            continue
        numeric_stats[col] = {
            "label":   label,
            "unit":    unit,
            "has_sum": has_sum,
            "count":   len(vals),
            "sum":     round(sum(vals), 1) if has_sum else None,
            "avg":     round(statistics.mean(vals), 1),
            "median":  round(statistics.median(vals), 1),
            "min":     round(min(vals), 1),
            "max":     round(max(vals), 1),
            "provenance": _provenance_for(col, records),
        }

    # ── 2. Categorical stats ──────────────────────────────────────────────────
    cat_stats: dict[str, dict] = {}
    for col, label in _CATEGORICAL.items():
        vals = [str(v).strip() for v in df[col].tolist() if _is_present(v)]
        counter = Counter(vals)
        top5 = [{"value": v, "count": c, "pct": round(c / total * 100, 1)}
                for v, c in counter.most_common(5)]
        cat_stats[col] = {
            "label":       label,
            "total_filled": len(vals),
            "unique_count": len(counter),
            "top5":        top5,
            "provenance":  _provenance_for(col, records),
        }

    # ── 3. Identity / uniqueness stats ───────────────────────────────────────
    id_stats: dict[str, dict] = {}
    for col, label in _IDENTITY.items():
        vals = [str(v).strip() for v in df[col].tolist() if _is_present(v)]
        unique = len(set(vals))
        dups   = len(vals) - unique
        id_stats[col] = {
            "label":        label,
            "total_filled": len(vals),
            "unique":       unique,
            "duplicates":   dups,
            "uniqueness_pct": round(unique / max(len(vals), 1) * 100, 1),
            "provenance":   _provenance_for(col, records),
        }

    # ── 4. Global provenance summary ─────────────────────────────────────────
    global_prov = {"osint": 0, "synth": 0, "knn": 0, "reg": 0}
    for col in TZ_COLUMNS:
        prov = _provenance_for(col, records)
        for k in global_prov:
            global_prov[k] += prov.get(k, 0)

    total_cells = total * len(TZ_COLUMNS)

    # ── 5. Macro banner ───────────────────────────────────────────────────────
    dwt_vals = [f for v in df["dwt_tons"].tolist() if (f := _safe_float(v)) is not None]
    gt_vals  = [f for v in df["gt"].tolist()       if (f := _safe_float(v)) is not None]
    macro = {
        "total_vessels":   total,
        "total_columns":   len(TZ_COLUMNS),
        "total_cells":     total_cells,
        "total_dwt":       round(sum(dwt_vals), 0) if dwt_vals else 0,
        "total_gt":        round(sum(gt_vals),  0) if gt_vals  else 0,
        "avg_dwt":         round(statistics.mean(dwt_vals), 0) if dwt_vals else 0,
        "avg_gt":          round(statistics.mean(gt_vals),  0) if gt_vals  else 0,
        "global_prov":     global_prov,
    }

    return {
        "macro":    macro,
        "numeric":  numeric_stats,
        "category": cat_stats,
        "identity": id_stats,
        "tz_columns": TZ_COLUMNS,
        "all_prov": {col: _provenance_for(col, records) for col in TZ_COLUMNS},
    }


# ── HTML template ─────────────────────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ORACLE-1001 · Мета-аналитика</title>
<style>
:root{
  --bg:#04070f;--bg2:rgba(10,16,30,.85);--border:rgba(0,242,254,.18);
  --text:#e4eaf5;--muted:#6b7fa0;--accent:#00f2fe;--green:#10b981;
  --amber:#f59e0b;--blue:#3b82f6;--cyan:#63caff;--bad:#ef4444;
  --font-mono:'JetBrains Mono','Fira Code','Courier New',monospace;
  --font:'Inter','Segoe UI',system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:var(--font);
  min-height:100vh;padding:0 0 80px;}
/* ── nav ── */
.nav{display:flex;align-items:center;gap:16px;padding:14px 28px;
  background:rgba(4,7,15,.92);border-bottom:1px solid var(--border);
  backdrop-filter:blur(14px);position:sticky;top:0;z-index:40;}
.nav-logo{font-family:var(--font-mono);font-size:11px;letter-spacing:.18em;
  color:var(--accent);text-shadow:0 0 14px var(--accent);}
.nav-links{display:flex;gap:8px;margin-left:auto;}
.nav-links a{font-size:11px;font-family:var(--font-mono);letter-spacing:.08em;
  color:var(--muted);text-decoration:none;padding:5px 11px;border:1px solid transparent;
  border-radius:4px;transition:all .2s;}
.nav-links a:hover,.nav-links a.active{color:var(--accent);
  border-color:rgba(0,242,254,.3);background:rgba(0,242,254,.06);}
/* ── hero ── */
.hero{padding:32px 28px 16px;border-bottom:1px solid var(--border);}
.hero-title{font-family:var(--font-mono);font-size:22px;letter-spacing:.12em;
  color:var(--accent);text-shadow:0 0 24px rgba(0,242,254,.5);margin-bottom:4px;}
.hero-sub{font-size:12px;color:var(--muted);letter-spacing:.06em;}
/* ── macro banner ── */
.macro-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
  gap:12px;padding:22px 28px;}
.macro-card,.metric-card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;
  padding:16px 18px;backdrop-filter:blur(12px);
  transition:transform .3s cubic-bezier(.4,0,.2,1),border-color .3s,box-shadow .3s;position:relative;}
.macro-card .mc-label{font-size:10px;font-family:var(--font-mono);
  letter-spacing:.12em;color:var(--muted);text-transform:uppercase;margin-bottom:6px;}
.macro-card .mc-val{font-size:20px;font-weight:700;letter-spacing:.04em;}
.macro-card .mc-sub{font-size:10px;color:var(--muted);margin-top:3px;}
/* ── section headers ── */
.section{padding:8px 28px 4px;}
.section-title{font-family:var(--font-mono);font-size:11px;letter-spacing:.18em;
  color:var(--accent);text-transform:uppercase;border-bottom:1px solid rgba(0,242,254,.12);
  padding-bottom:8px;margin-bottom:14px;}
/* ── cards grid ── */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));
  gap:12px;padding:0 28px 20px;}
.card,.chart-card,.dashboard-panel{background:var(--bg2);border:1px solid var(--border);border-radius:10px;
  padding:16px;backdrop-filter:blur(12px);transition:transform .3s cubic-bezier(.4,0,.2,1),border-color .3s,box-shadow .3s;
  position:relative;}
@keyframes flameGlowShift{
  0%{box-shadow:0 0 15px rgba(0,242,254,.6),0 0 30px rgba(0,242,254,.4),inset 0 0 15px rgba(0,242,254,.3);border-color:#00f2fe}
  33%{box-shadow:0 0 20px rgba(255,0,128,.8),0 0 40px rgba(255,0,128,.5),inset 0 0 20px rgba(255,0,128,.3);border-color:#ff0080}
  66%{box-shadow:0 0 25px rgba(255,102,0,.9),0 0 50px rgba(255,102,0,.6),inset 0 0 25px rgba(255,102,0,.4);border-color:#ff6600}
  100%{box-shadow:0 0 15px rgba(0,242,254,.6),0 0 30px rgba(0,242,254,.4),inset 0 0 15px rgba(0,242,254,.3);border-color:#00f2fe}
}
.card:hover,.macro-card:hover,.chart-card:hover,.metric-card:hover,.dashboard-panel:hover{
  transform:translateY(-4px) scale(1.01);
  animation:flameGlowShift 2.5s infinite linear;
  z-index:10;
}
.card-head{display:flex;justify-content:space-between;align-items:flex-start;
  margin-bottom:12px;}
.card-name{font-family:var(--font-mono);font-size:11px;letter-spacing:.12em;
  color:var(--accent);}
.card-badge{font-size:9px;font-family:var(--font-mono);padding:2px 7px;
  border-radius:3px;border:1px solid;letter-spacing:.06em;}
.card-badge.num{color:var(--green);border-color:rgba(16,185,129,.35);}
.card-badge.cat{color:var(--amber);border-color:rgba(245,158,11,.35);}
.card-badge.id {color:var(--cyan); border-color:rgba(99,202,255,.35);}
/* ── stat rows ── */
.stat-row{display:flex;justify-content:space-between;align-items:center;
  padding:5px 0;border-bottom:1px solid rgba(255,255,255,.04);}
.stat-row:last-of-type{border-bottom:none;}
.stat-key{font-size:10px;color:var(--muted);font-family:var(--font-mono);}
.stat-val{font-size:12px;font-weight:600;font-family:var(--font-mono);}
.stat-val.big{color:var(--green);}
/* ── top-5 list ── */
.top5{margin-top:10px;}
.t5-row{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:11px;}
.t5-rank{font-family:var(--font-mono);font-size:9px;color:var(--muted);
  width:16px;text-align:right;}
.t5-bar-wrap{flex:1;height:4px;background:rgba(255,255,255,.06);border-radius:2px;}
.t5-bar{height:4px;border-radius:2px;background:var(--accent);
  transition:width .6s cubic-bezier(.4,0,.2,1);}
.t5-label{flex:2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  font-size:10px;color:var(--text);}
.t5-cnt{font-family:var(--font-mono);font-size:10px;color:var(--muted);white-space:nowrap;}
/* ── uniqueness bar ── */
.uniq-bar{margin-top:8px;}
.uniq-track{width:100%;height:6px;background:rgba(255,255,255,.06);
  border-radius:3px;overflow:hidden;margin-top:4px;}
.uniq-fill{height:6px;border-radius:3px;transition:width .6s ease;background:var(--green);}
.uniq-fill.warn{background:var(--amber);}
.uniq-fill.bad {background:var(--bad);}
/* ── provenance bar ── */
.prov-wrap{margin-top:10px;}
.prov-label{font-size:9px;font-family:var(--font-mono);color:var(--muted);
  letter-spacing:.08em;margin-bottom:5px;}
.prov-track{width:100%;height:8px;border-radius:4px;overflow:hidden;
  display:flex;background:rgba(255,255,255,.04);}
.prov-seg{height:100%;transition:width .6s ease;}
.prov-legend{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;}
.prov-leg-item{display:flex;align-items:center;gap:4px;font-size:9px;
  font-family:var(--font-mono);color:var(--muted);}
.prov-dot{width:8px;height:8px;border-radius:50%;}
/* ── global provenance matrix ── */
.prov-matrix{padding:0 28px 28px;}
.prov-col-row{display:grid;grid-template-columns:140px 1fr 80px;
  align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.03);}
.pcr-name{font-size:10px;font-family:var(--font-mono);color:var(--muted);
  letter-spacing:.06em;}
.pcr-bar{height:10px;border-radius:5px;overflow:hidden;display:flex;
  background:rgba(255,255,255,.04);}
.pcr-pct{font-size:9px;font-family:var(--font-mono);color:var(--muted);text-align:right;}
/* ── scrollbar ── */
::-webkit-scrollbar{width:6px;height:6px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:rgba(0,242,254,.2);border-radius:3px;}
@media(max-width:640px){.cards{grid-template-columns:1fr;}
  .prov-col-row{grid-template-columns:100px 1fr 60px;}}
</style>
</head>
<body>
<!-- nav -->
<nav class="nav">
  <span class="nav-logo">ORACLE-1001 · META-ANALYSIS</span>
  <div class="nav-links">
    <a href="osint_layers.html">9 аналитических слоёв</a>
    <a href="dashboard.html">Реестр флота</a>
    <a href="fleet_meta_analysis.html" class="active">Мета-аналитика</a>
    <a href="top100_analytics.html">Анализ ТОП-100</a>
    <a href="top200_analytics.html">Анализ ТОП-200</a>
    <a href="top500_analytics.html">Анализ ТОП-500</a>
  </div>
</nav>

<!-- hero -->
<div class="hero">
  <div class="hero-title">МЕТА-АНАЛИТИКА: 20 КОЛОНОК ТЗ · АБСОЛЮТНЫЕ ВЕЛИЧИНЫ</div>
  <div class="hero-sub">Агрегированные макро-метрики флота &nbsp;·&nbsp; Все 1 253 судна &nbsp;·&nbsp; ORACLE-1001</div>
</div>

<!-- ── injected by Python ── -->
<div id="app"></div>

<script>
const META = __META_JSON__;

const FMT = {
  big:  n => n == null ? '—' : (n >= 1e6 ? (n/1e6).toFixed(2)+' млн' : n.toLocaleString('ru-RU')),
  f1:   n => n == null ? '—' : n.toLocaleString('ru-RU',{maximumFractionDigits:1}),
  pct:  n => n == null ? '—' : n.toFixed(1)+'%',
};

const PROV_COLORS  = {osint:'#10b981',synth:'#f59e0b',knn:'#00f2fe',reg:'#63caff'};
const PROV_LABELS  = {osint:'Прямой OSINT',synth:'⚡ Синтетика',knn:'🔬 Профиль (KNN)',reg:'🌐 Реестр AIS'};

function provBar(prov, small=false) {
  const total = Object.values(prov).reduce((a,b)=>a+b,0)||1;
  const segs   = Object.entries(prov).map(([k,v])=>{
    const w = (v/total*100).toFixed(2);
    return `<div class="prov-seg" style="width:${w}%;background:${PROV_COLORS[k]}" title="${PROV_LABELS[k]}: ${v}"></div>`;
  }).join('');
  const legend = Object.entries(prov).filter(([,v])=>v>0).map(([k,v])=>
    `<div class="prov-leg-item"><div class="prov-dot" style="background:${PROV_COLORS[k]}"></div>${PROV_LABELS[k]} ${v}</div>`
  ).join('');
  return `<div class="prov-wrap">
    <div class="prov-label">ИСТОЧНИК ДАННЫХ</div>
    <div class="prov-track">${segs}</div>
    <div class="prov-legend">${legend}</div>
  </div>`;
}

// ── Macro banner ────────────────────────────────────────────────────────────
function renderMacro() {
  const m = META.macro;
  const gp = m.global_prov;
  const gpTotal = Object.values(gp).reduce((a,b)=>a+b,0)||1;
  return `
  <div class="macro-grid">
    <div class="macro-card">
      <div class="mc-label">Всего судов</div>
      <div class="mc-val" style="color:var(--accent)">${m.total_vessels.toLocaleString('ru-RU')}</div>
      <div class="mc-sub">${m.total_columns} колонок × ${m.total_vessels} судов = ${m.total_cells.toLocaleString('ru-RU')} ячеек</div>
    </div>
    <div class="macro-card">
      <div class="mc-label">Суммарный DWT флота</div>
      <div class="mc-val" style="color:var(--green)">${FMT.big(m.total_dwt)}</div>
      <div class="mc-sub">тонн &nbsp;·&nbsp; avg ${FMT.big(m.avg_dwt)} т/судно</div>
    </div>
    <div class="macro-card">
      <div class="mc-label">Суммарный GT флота</div>
      <div class="mc-val" style="color:var(--blue)">${FMT.big(m.total_gt)}</div>
      <div class="mc-sub">GT &nbsp;·&nbsp; avg ${FMT.big(m.avg_gt)} GT/судно</div>
    </div>
    ${Object.entries(gp).map(([k,v])=>`
    <div class="macro-card">
      <div class="mc-label">${PROV_LABELS[k]}</div>
      <div class="mc-val" style="color:${PROV_COLORS[k]}">${v.toLocaleString('ru-RU')}</div>
      <div class="mc-sub">${(v/gpTotal*100).toFixed(1)}% всех ячеек (из ${m.total_cells.toLocaleString('ru-RU')})</div>
    </div>`).join('')}
  </div>`;
}

// ── Numeric cards ────────────────────────────────────────────────────────────
function renderNumeric() {
  const items = Object.entries(META.numeric).map(([col, d]) => `
  <div class="card">
    <div class="card-head">
      <div class="card-name">${d.label}</div>
      <div class="card-badge num">ЧИСЛОВАЯ</div>
    </div>
    ${d.has_sum ? `<div class="stat-row">
      <span class="stat-key">СУММА</span>
      <span class="stat-val big">${FMT.big(d.sum)} ${d.unit}</span>
    </div>` : ''}
    <div class="stat-row"><span class="stat-key">КОЛ-ВО</span><span class="stat-val">${d.count?.toLocaleString('ru-RU')} судов</span></div>
    <div class="stat-row"><span class="stat-key">СРЕДНЕЕ</span><span class="stat-val">${FMT.f1(d.avg)} ${d.unit}</span></div>
    <div class="stat-row"><span class="stat-key">МЕДИАНА</span><span class="stat-val">${FMT.f1(d.median)} ${d.unit}</span></div>
    <div class="stat-row"><span class="stat-key">МИН</span><span class="stat-val">${FMT.f1(d.min)}</span></div>
    <div class="stat-row"><span class="stat-key">МАКС</span><span class="stat-val">${FMT.f1(d.max)}</span></div>
    ${provBar(d.provenance)}
  </div>`).join('');
  return `<div class="section"><div class="section-title">§1 — Числовые колонки (сумма / среднее / медиана / мин / макс)</div></div>
  <div class="cards">${items}</div>`;
}

// ── Category cards ───────────────────────────────────────────────────────────
function renderCategory() {
  const items = Object.entries(META.category).map(([col, d]) => {
    const top5 = d.top5.map((t, i) => {
      const w = d.top5[0]?.count > 0 ? (t.count / d.top5[0].count * 100).toFixed(1) : 0;
      return `<div class="t5-row">
        <div class="t5-rank">#${i+1}</div>
        <div class="t5-label" title="${t.value}">${t.value}</div>
        <div class="t5-bar-wrap"><div class="t5-bar" style="width:${w}%"></div></div>
        <div class="t5-cnt">${t.count} &nbsp;(${t.pct}%)</div>
      </div>`;
    }).join('');
    return `<div class="card">
      <div class="card-head">
        <div class="card-name">${d.label}</div>
        <div class="card-badge cat">КАТЕГОРИЯ</div>
      </div>
      <div class="stat-row"><span class="stat-key">УНИКАЛЬНЫХ ЗНАЧЕНИЙ</span><span class="stat-val">${d.unique_count}</span></div>
      <div class="stat-row"><span class="stat-key">ЗАПОЛНЕНО</span><span class="stat-val">${d.total_filled?.toLocaleString('ru-RU')}</span></div>
      <div class="top5">${top5}</div>
      ${provBar(d.provenance)}
    </div>`;
  }).join('');
  return `<div class="section"><div class="section-title">§2 — Категориальные колонки (уникальные + топ-5)</div></div>
  <div class="cards">${items}</div>`;
}

// ── Identity / uniqueness cards ──────────────────────────────────────────────
function renderIdentity() {
  const items = Object.entries(META.identity).map(([col, d]) => {
    const pct = d.uniqueness_pct;
    const fillCls = pct === 100 ? '' : pct >= 90 ? 'warn' : 'bad';
    return `<div class="card">
      <div class="card-head">
        <div class="card-name">${d.label}</div>
        <div class="card-badge id">ИДЕНТИФИКАТОР</div>
      </div>
      <div class="stat-row"><span class="stat-key">ЗАПОЛНЕНО</span><span class="stat-val">${d.total_filled?.toLocaleString('ru-RU')}</span></div>
      <div class="stat-row"><span class="stat-key">УНИКАЛЬНЫХ</span><span class="stat-val big">${d.unique?.toLocaleString('ru-RU')}</span></div>
      <div class="stat-row"><span class="stat-key">ДУБЛИКАТЫ</span>
        <span class="stat-val" style="color:${d.duplicates>0?'var(--amber)':'var(--green)'}">${d.duplicates}</span>
      </div>
      <div class="uniq-bar">
        <div class="stat-key">УНИКАЛЬНОСТЬ ${FMT.pct(d.uniqueness_pct)}</div>
        <div class="uniq-track"><div class="uniq-fill ${fillCls}" style="width:${d.uniqueness_pct}%"></div></div>
      </div>
      ${provBar(d.provenance)}
    </div>`;
  }).join('');
  return `<div class="section"><div class="section-title">§3 — Идентификационные колонки (аудит уникальности)</div></div>
  <div class="cards">${items}</div>`;
}

// ── Global provenance matrix ─────────────────────────────────────────────────
function renderProvMatrix() {
  const rows = META.tz_columns.map(col => {
    const prov = META.all_prov[col] || {osint:0,synth:0,knn:0,reg:0};
    const total = Object.values(prov).reduce((a,b)=>a+b,0)||1;
    const segs = Object.entries(prov).map(([k,v])=>`
      <div class="prov-seg" style="width:${(v/total*100).toFixed(2)}%;background:${PROV_COLORS[k]}"
           title="${PROV_LABELS[k]}: ${v} (${(v/total*100).toFixed(1)}%)"></div>`).join('');
    const osintPct = (prov.osint/total*100).toFixed(0);
    return `<div class="prov-col-row">
      <div class="pcr-name">${col.toUpperCase()}</div>
      <div class="pcr-bar">${segs}</div>
      <div class="pcr-pct">OSINT ${osintPct}%</div>
    </div>`;
  }).join('');
  return `<div class="section"><div class="section-title">§4 — Провенанс по каждой колонке (OSINT / ⚡Синтетика / 🔬Профиль / 🌐Реестр AIS)</div></div>
  <div class="prov-matrix">
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;">
      ${Object.entries(PROV_LABELS).map(([k,l])=>
        `<div style="display:flex;align-items:center;gap:5px;font-size:10px;font-family:var(--font-mono);color:var(--muted)">
          <div style="width:10px;height:10px;border-radius:50%;background:${PROV_COLORS[k]}"></div>${l}</div>`
      ).join('')}
    </div>
    ${rows}
  </div>`;
}

// ── Mount ────────────────────────────────────────────────────────────────────
document.getElementById('app').innerHTML =
  renderMacro() +
  renderNumeric() +
  renderCategory() +
  renderIdentity() +
  renderProvMatrix();
</script>
</body>
</html>"""


# ── Public entry point ────────────────────────────────────────────────────────
def write_meta_analysis(df: pd.DataFrame, path: Path) -> dict:
    """Compute meta-analytics and write self-contained HTML.

    Returns the computed meta dict (for console reporting).
    """
    meta = compute_meta(df)
    html = _HTML.replace("__META_JSON__", json.dumps(meta, ensure_ascii=False))
    path.write_text(html, encoding="utf-8")
    return meta
