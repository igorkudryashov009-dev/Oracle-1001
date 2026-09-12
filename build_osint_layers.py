"""Build output/osint_layers.html — NASA / wet-glass 9-layer OSINT dossier.

20 ТЗ columns (operational schema) × 9 analytical layers per vessel.
Honest degradation: missing fields render as — / empty rings, never invented.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from pipeline.robust_parser import TZ_COLUMNS

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
FLEET = OUTPUT / "fleet_database.csv"
OUT = OUTPUT / "osint_layers.html"

LAYERS = [
    {
        "id": "L1",
        "code": "IDENTITY",
        "title": "Идентификация",
        "hint": "Name · IMO · MMSI · Call Sign",
        "fields": ["vessel_name", "imo", "mmsi", "call_sign"],
    },
    {
        "id": "L2",
        "code": "CLASS",
        "title": "Классификация",
        "hint": "Type · Flag · Built year",
        "fields": ["vessel_type", "flag", "built_year"],
    },
    {
        "id": "L3",
        "code": "HULL",
        "title": "Корпус / тоннаж",
        "hint": "LOA · Beam · DWT · GT",
        "fields": ["loa_m", "beam_m", "dwt_tons", "gt"],
    },
    {
        "id": "L4",
        "code": "NAVOPS",
        "title": "Оперативная обстановка",
        "hint": "Nav status · Speed · Location",
        "fields": ["nav_status", "speed_knots", "destination_context"],
    },
    {
        "id": "L5",
        "code": "DRAUGHT",
        "title": "Осадка / груз",
        "hint": "Draft · load inference",
        "fields": ["draft_m", "draft_note"],
    },
    {
        "id": "L6",
        "code": "LOGISTICS",
        "title": "Логистический трек",
        "hint": "Departure · Destination · ETA",
        "fields": [
            "departure_port",
            "departure_datetime_utc",
            "destination_port",
            "arrival_datetime",
        ],
    },
    {
        "id": "L7",
        "code": "OSINT",
        "title": "Промышленный OSINT",
        "hint": "Narrative density · signal quality",
        "fields": ["raw_text"],
    },
    {
        "id": "L8",
        "code": "COMPLIANCE",
        "title": "Комплаенс",
        "hint": "Risk · sanctions tags (OFAC / STS / Shadow / Dark / Flag)",
        "fields": ["compliance_risk_level", "sanctions_tags"],
    },
    {
        "id": "L9",
        "code": "VERDICT",
        "title": "Вердикт",
        "hint": "Confidence · category · integrity",
        "fields": ["source_confidence", "vessel_category", "imo_valid"],
    },
]


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


_EMPTY_SENTINELS = frozenset({"", "—", "-", "none", "не извлечено", "nan", "n/a", "n\\a"})


def _present(v: Any) -> bool:
    """True when a field contains a meaningful non-missing value."""
    if v is None:
        return False
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return False
    s = str(v).strip().lower()
    if s in _EMPTY_SENTINELS:
        return False
    return True


def _fill_bucket(pct: float) -> str:
    if pct >= 100.0:
        return "perfect"
    if pct >= 80.0:
        return "high"
    if pct >= 50.0:
        return "medium"
    return "low"


def _risk_score(level: Optional[str]) -> float:
    if not level:
        return 0.35
    u = str(level).upper()
    if u in ("EXTREME",) or "ЭКСТРЕМАЛЬ" in u or "КРИТИЧЕСКИ" in u or "GHOST" in u:
        return 1.0
    if u in ("HIGH",) or "ВЫСОК" in u or "STRATEG" in u:
        return 0.82
    if u in ("MID", "MEDIUM") or "СРЕДН" in u:
        return 0.55
    if u in ("LOW",) or "НИЗК" in u or "CLEAR" in u or "ЧИСТ" in u:
        return 0.18
    return 0.4


def _layer_scores(rec: dict) -> list[float]:
    """0..1 fill / signal strength per layer — never invents missing facts."""
    scores: list[float] = []
    for layer in LAYERS:
        fields = layer["fields"]
        if layer["id"] == "L7":
            raw = rec.get("raw_text") or ""
            n = len(str(raw))
            # Density of dossier text as OSINT depth proxy
            scores.append(min(1.0, n / 2200.0) if n else 0.0)
            continue
        if layer["id"] == "L8":
            base = _risk_score(rec.get("compliance_risk_level"))
            tags_raw = rec.get("sanctions_tags") or ""
            n_tags = len([t for t in str(tags_raw).split(";") if t.strip()]) if tags_raw else 0
            # Boost L8 when sanctions tags present (cap at 1.0)
            scores.append(round(min(1.0, base + 0.08 * n_tags), 3))
            continue
        if layer["id"] == "L9":
            conf = str(rec.get("source_confidence") or "")
            base = {
                "parsed": 0.92,
                "needs_review": 0.45,
                "imo_mismatch": 0.28,
                "identity_conflict_flagged": 0.15,
            }.get(conf, 0.4)
            if rec.get("imo_valid") is False:
                base = min(base, 0.25)
            scores.append(base)
            continue
        filled = sum(1 for f in fields if _present(rec.get(f)))
        scores.append(filled / max(len(fields), 1))
    return [round(s, 3) for s in scores]


def _excerpt(raw: Any, limit: int = 420) -> str:
    if not raw:
        return ""
    text = re.sub(r"\s+", " ", str(raw)).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def enrich_records(records: list[dict]) -> list[dict]:
    out = []
    for rec in records:
        item = dict(rec)
        item["layers"] = _layer_scores(rec)
        item["risk_score"] = _risk_score(rec.get("compliance_risk_level"))
        item["excerpt"] = _excerpt(rec.get("raw_text"))
        # Strict fill count: None / "" / "—" / "не извлечено" / "None" / NaN → empty
        filled = sum(1 for c in TZ_COLUMNS if _present(item.get(c)))
        item["tz_fill"] = filled          # 0..20
        item["tz_fill_pct"] = round(100.0 * filled / max(len(TZ_COLUMNS), 1), 1)  # 0.0..100.0
        item["fill_bucket"] = _fill_bucket(item["tz_fill_pct"])
        # Propagate risk_source (explicit / heuristic) for UI audit badge
        item["risk_source"] = rec.get("risk_source") or "explicit"
        # Propagate synthetic_fields for ⚡ synth badge in UI
        item["synthetic_fields"] = rec.get("synthetic_fields") or ""
        # Propagate KNN/profile imputed fields for 🔬 badge
        item["imputed_fields"]   = rec.get("imputed_fields") or ""
        # Propagate registry-mock fields for 🌐 AIS/Equasis badge
        item["registry_mock_fields"] = rec.get("registry_mock_fields") or ""
        raw = str(item.get("raw_text") or "")
        raw_u = raw.upper()
        # Fields absent from source text (honest gaps — never invent)
        gaps = []
        if not _present(item.get("mmsi")) and not re.search(r"\bMMSI\b", raw, re.I):
            gaps.append("mmsi")
        if not _present(item.get("call_sign")) and not re.search(
            r"Позывной|Call\s*sign", raw, re.I
        ):
            gaps.append("call_sign")
        if not _present(item.get("compliance_risk_level")) and not re.search(
            r"РИСК|CLEARED|КОМПЛАЕНС|GHOST|ПРИЗРАК", raw_u
        ):
            gaps.append("compliance_risk_level")
        item["source_gaps"] = gaps
        # Cap raw_text in payload for HTML size
        if isinstance(item.get("raw_text"), str) and len(item["raw_text"]) > 6000:
            item["raw_text"] = item["raw_text"][:6000] + "…"
        out.append(item)
    return out


def write_osint_layers(
    fleet_df: pd.DataFrame,
    metrics: dict,
    path: Path = OUT,
) -> None:
    records = enrich_records(_df_records(fleet_df))
    # ── Fill-rate bucket aggregator ──────────────────────────────────────────
    bucket_counts: dict[str, int] = {"perfect": 0, "high": 0, "medium": 0, "low": 0}
    fill_sum = 0.0
    for r in records:
        fill_sum += r.get("tz_fill_pct", 0.0)
        bucket_counts[r.get("fill_bucket", "low")] = (
            bucket_counts.get(r.get("fill_bucket", "low"), 0) + 1
        )
    avg_fill = round(fill_sum / max(len(records), 1), 1)
    metrics = dict(metrics)
    metrics["avg_fill_pct"] = avg_fill
    metrics["fill_buckets"] = bucket_counts
    # ── Per-column fill stats (20 ТЗ columns across all vessels) ─────────────
    total_v = max(len(records), 1)
    col_stats: dict[str, dict] = {}
    for col in TZ_COLUMNS:
        n_filled = sum(1 for r in records if _present(r.get(col)))
        col_stats[col] = {
            "count": n_filled,
            "total": total_v,
            "pct": round(100.0 * n_filled / total_v, 1),
        }
    metrics["column_fill_stats"] = col_stats
    # ── Risk labeling audit ───────────────────────────────────────────────────
    labeled  = sum(1 for r in records if _present(r.get("compliance_risk_level")))
    heur     = sum(1 for r in records if r.get("risk_source") == "heuristic")
    metrics["risk_labeled_count"]   = labeled
    metrics["risk_heuristic_count"] = heur
    risk_dist: dict[str, int] = {}
    for r in records:
        lvl = str(r.get("compliance_risk_level") or "UNLABELED").upper()
        risk_dist[lvl] = risk_dist.get(lvl, 0) + 1
    metrics["risk_distribution"] = risk_dist
    # ────────────────────────────────────────────────────────────────────────
    payload = {
        "metrics": metrics,
        "tz_columns": TZ_COLUMNS,
        "layers": LAYERS,
        "vessels": records,
    }
    # Flagship for LV 3D exhibit — prefer IMO 9001772 (LARA, 239×40 m LNG)
    def _dwt(r: dict) -> float:
        try:
            return float(r.get("dwt_tons") or 0)
        except (TypeError, ValueError):
            return 0.0

    preferred = next((r for r in records if str(r.get("imo") or "") == "9001772"), None)
    top = preferred or (max(records, key=_dwt) if records else {})
    flagship = {
        "vessel_name": top.get("vessel_name") or "LARA",
        "imo": top.get("imo") or "9001772",
        "dwt_tons": top.get("dwt_tons") or 48817,
        "gt": top.get("gt"),
        "flag": top.get("flag") or "",
        "loa_m": float(top.get("loa_m") or 239.0),
        "beam_m": float(top.get("beam_m") or 40.0),
        "draft_m": top.get("draft_m"),
        "vessel_type": top.get("vessel_type") or "LNG Tanker",
    }
    html = _TEMPLATE.replace(
        "__PAYLOAD__",
        json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
    ).replace(
        "%%LV3D_FLAGSHIP%%",
        json.dumps(flagship, ensure_ascii=False).replace("</", "<\\/"),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Deploy Three.js exhibit modules next to HTML
    js_dir = path.parent / "js"
    js_dir.mkdir(parents=True, exist_ok=True)
    web_root = Path(__file__).resolve().parent / "web"
    for src in (
        web_root / "js" / "vessel_3d_viewer.js",
        web_root / "vessel_lv_3d.js",
    ):
        if src.is_file():
            (js_dir / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(html, encoding="utf-8")
    print(f"Wrote {path} (vessels={len(records)} · 3d={flagship.get('imo')})")


def main() -> int:
    if not FLEET.exists():
        print(f"ERROR: {FLEET} missing — run run_all.py first", file=sys.stderr)
        return 1
    fleet = pd.read_csv(FLEET, low_memory=False)
    metrics = {
        "vessel_count": int(len(fleet)),
        "valid_imo": int(fleet["imo_valid"].fillna(False).astype(bool).sum())
        if "imo_valid" in fleet.columns
        else int(fleet["imo"].nunique()),
        "source": "IMO_Filtered_Clean.xlsx",
    }
    write_osint_layers(fleet, metrics, OUT)
    return 0


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ORACLE-1001 · 9 аналитических слоёв</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600;700&family=Orbitron:wght@500;700&family=JetBrains+Mono:wght@400;600&family=Manrope:wght@400;600;700&display=swap" rel="stylesheet" />
<style>
:root {
  --void: #03070f;
  --deep: #06101f;
  --glass: rgba(140, 190, 230, 0.07);
  --glass-strong: rgba(160, 210, 255, 0.12);
  --stroke: rgba(160, 220, 255, 0.22);
  --stroke-hot: rgba(90, 230, 255, 0.55);
  --cyan: #5ce1ff;
  --mint: #7dffe1;
  --amber: #ffc857;
  --rose: #ff6b8a;
  --text: #e8f4ff;
  --muted: #8aa4bf;
  --ok: #3dff9a;
  --warn: #ffc857;
  --bad: #ff5d7a;
  --fill-high: #3dff9a;
  --fill-med: #ffc857;
  --fill-low: #ff6b5e;
  --lv-gold: #D4AF37;
  --lv-gold-soft: #F3E5AB;
  --lv-obsidian: #0a0c10;
  --lv-titanium: #8a9099;
  --font-ui: "Manrope", "Segoe UI", sans-serif;
  --font-hud: "Orbitron", sans-serif;
  --font-lv: "Cormorant Garamond", Georgia, serif;
  --font-mono: "JetBrains Mono", ui-monospace, monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body {
  font-family: var(--font-ui);
  color: var(--text);
  background:
    radial-gradient(1200px 700px at 12% -10%, rgba(40, 120, 200, 0.28), transparent 55%),
    radial-gradient(900px 600px at 90% 10%, rgba(20, 180, 170, 0.16), transparent 50%),
    radial-gradient(800px 500px at 50% 110%, rgba(80, 40, 140, 0.12), transparent 45%),
    linear-gradient(180deg, #02060d 0%, #071221 45%, #040a14 100%);
  overflow-x: hidden;
}
body::before {
  content: "";
  position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background-image:
    radial-gradient(1px 1px at 20% 30%, rgba(255,255,255,.55), transparent),
    radial-gradient(1px 1px at 70% 18%, rgba(180,230,255,.45), transparent),
    radial-gradient(1px 1px at 40% 70%, rgba(255,255,255,.35), transparent),
    radial-gradient(1.5px 1.5px at 85% 60%, rgba(120,220,255,.4), transparent),
    radial-gradient(1px 1px at 10% 80%, rgba(255,255,255,.3), transparent);
  opacity: .55;
  animation: drift 48s linear infinite;
}
@keyframes drift { to { transform: translateY(-40px); } }
@keyframes shimmer {
  0% { background-position: 0% 50%; }
  100% { background-position: 200% 50%; }
}
@keyframes pulseRing {
  0%, 100% { box-shadow: 0 0 0 0 rgba(92,225,255,.35); }
  50% { box-shadow: 0 0 0 10px rgba(92,225,255,0); }
}
@keyframes scan {
  0% { transform: translateY(-100%); opacity: 0; }
  20% { opacity: .45; }
  100% { transform: translateY(220%); opacity: 0; }
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0); }
}

/* ── Neon Flame Glow · Cyberpunk огненный неон ───────────────────────────── */
@keyframes flameGlowShift {
  0% {
    box-shadow: 0 0 15px rgba(0, 242, 254, 0.6), 0 0 30px rgba(0, 242, 254, 0.4), inset 0 0 15px rgba(0, 242, 254, 0.3);
    border-color: #00f2fe;
  }
  33% {
    box-shadow: 0 0 20px rgba(255, 0, 128, 0.8), 0 0 40px rgba(255, 0, 128, 0.5), inset 0 0 20px rgba(255, 0, 128, 0.3);
    border-color: #ff0080;
  }
  66% {
    box-shadow: 0 0 25px rgba(255, 102, 0, 0.9), 0 0 50px rgba(255, 102, 0, 0.6), inset 0 0 25px rgba(255, 102, 0, 0.4);
    border-color: #ff6600;
  }
  100% {
    box-shadow: 0 0 15px rgba(0, 242, 254, 0.6), 0 0 30px rgba(0, 242, 254, 0.4), inset 0 0 15px rgba(0, 242, 254, 0.3);
    border-color: #00f2fe;
  }
}
@keyframes flameDropShadow {
  0%   { filter: drop-shadow(0 0 6px #00f2fe) drop-shadow(0 0 14px rgba(0,242,254,.55)); }
  33%  { filter: drop-shadow(0 0 8px #ff0080) drop-shadow(0 0 20px rgba(255,0,128,.6)); }
  66%  { filter: drop-shadow(0 0 10px #ff6600) drop-shadow(0 0 26px rgba(255,102,0,.65)); }
  100% { filter: drop-shadow(0 0 6px #00f2fe) drop-shadow(0 0 14px rgba(0,242,254,.55)); }
}
.chart-card, .metric-card, .dashboard-panel, .donut-center-container,
.metric, .prog-card, .dwt-panel, .dwt-info-card, .dwt-donut-box,
.layer, .analytics-wrap, .list button {
  transition: transform .3s cubic-bezier(0.4, 0, 0.2, 1), box-shadow .3s, border-color .3s;
  position: relative;
}
.chart-card:hover, .metric-card:hover, .dashboard-panel:hover,
.metric:hover, .prog-card:hover, .dwt-panel:hover, .dwt-info-card:hover,
.dwt-donut-box:hover, .layer:hover, .analytics-wrap:hover,
.list button:hover {
  transform: translateY(-4px) scale(1.01);
  animation: flameGlowShift 2.5s infinite linear;
  z-index: 10;
}
#donutSvg path:hover,
.dwt-donut-box svg path:hover,
.donut-svg path:hover {
  animation: flameDropShadow 2s infinite linear;
  cursor: pointer;
}

.wrap { position: relative; z-index: 1; max-width: 1280px; margin: 0 auto; padding: 20px 18px 64px; }
.nav {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between;
  margin-bottom: 22px;
}
.brand {
  display: flex; align-items: center; gap: 12px; text-decoration: none; color: var(--text);
}
.brand-mark {
  width: 42px; height: 42px; border-radius: 14px;
  display: grid; place-items: center;
  font-family: var(--font-hud); font-size: 13px; font-weight: 700;
  background: linear-gradient(135deg, rgba(92,225,255,.35), rgba(125,255,225,.12));
  border: 1px solid var(--stroke-hot);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.25), 0 8px 28px rgba(20,80,140,.35);
}
.brand strong { font-family: var(--font-hud); letter-spacing: .08em; font-size: 14px; display: block; }
.brand span { color: var(--muted); font-size: 12px; }
.nav-links { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.pill {
  border: 1px solid var(--stroke);
  background: var(--glass);
  color: var(--text);
  text-decoration: none;
  padding: 8px 12px; border-radius: 999px;
  font-size: 12px; backdrop-filter: blur(14px);
  transition: border-color .2s, background .2s, transform .2s;
  cursor: pointer; font-family: inherit;
}
.pill:hover { border-color: var(--cyan); background: var(--glass-strong); transform: translateY(-1px); }
.pill.active { border-color: var(--cyan); color: var(--cyan); }
.pill.analytics-btn {
  border-color: rgba(0,242,254,.45);
  color: #00f2fe;
  background: linear-gradient(135deg, rgba(0,242,254,.14), rgba(16,185,129,.08));
  box-shadow: 0 0 14px rgba(0,242,254,.25);
  font-weight: 600;
  letter-spacing: .04em;
}
.pill.analytics-btn:hover {
  box-shadow: 0 0 22px rgba(0,242,254,.45);
  border-color: #00f2fe;
}

/* ── DWT Analytics Overlay ───────────────────────────────────────────────── */
.dwt-overlay {
  position: fixed; inset: 0; z-index: 900;
  display: none; align-items: stretch; justify-content: center;
  padding: 24px 16px;
  background: rgba(2,6,13,.72);
  backdrop-filter: blur(16px) saturate(140%);
  -webkit-backdrop-filter: blur(16px) saturate(140%);
  opacity: 0; transition: opacity .28s ease;
}
.dwt-overlay.open { display: flex; opacity: 1; }
.dwt-modal {
  width: min(1100px, 100%);
  max-height: calc(100vh - 48px);
  overflow: auto;
  border-radius: 22px;
  border: 1px solid rgba(0,242,254,.28);
  background:
    linear-gradient(145deg, rgba(8,18,36,.92), rgba(4,10,22,.96)),
    var(--glass-strong);
  box-shadow: 0 0 40px rgba(0,242,254,.12), 0 30px 80px rgba(0,0,0,.55),
              inset 0 1px 0 rgba(255,255,255,.1);
  padding: 22px 24px 28px;
  animation: dwtSlideIn .32s cubic-bezier(.2,.8,.2,1);
}
@keyframes dwtSlideIn {
  from { transform: translateY(18px) scale(.98); opacity: 0; }
  to   { transform: none; opacity: 1; }
}
.dwt-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; margin-bottom: 18px; flex-wrap: wrap;
}
.dwt-head h2 {
  margin: 0; font-family: var(--font-hud); font-size: 16px;
  letter-spacing: .12em; color: #00f2fe;
  text-shadow: 0 0 18px rgba(0,242,254,.45);
}
.dwt-head p { margin: 4px 0 0; font-size: 12px; color: var(--muted); }
.dwt-close {
  border: 1px solid var(--stroke); background: var(--glass);
  color: var(--text); border-radius: 999px; padding: 8px 14px;
  cursor: pointer; font-size: 12px; font-family: inherit;
}
.dwt-close:hover { border-color: var(--rose); color: var(--rose); }
.dwt-grid {
  display: grid; grid-template-columns: minmax(280px, 1fr) minmax(300px, 1.1fr);
  gap: 18px; align-items: start;
}
@media (max-width: 860px) { .dwt-grid { grid-template-columns: 1fr; } }
.dwt-panel {
  border: 1px solid var(--stroke); border-radius: 16px;
  background: rgba(10,20,40,.45); padding: 14px 16px;
}
.dwt-panel label {
  display: block; font-size: 10px; font-family: var(--font-mono);
  letter-spacing: .1em; color: var(--muted); margin-bottom: 6px; text-transform: uppercase;
}
.dwt-presets { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
.dwt-preset {
  border: 1px solid var(--stroke); background: transparent; color: var(--muted);
  border-radius: 999px; padding: 6px 11px; font-size: 11px; cursor: pointer;
  font-family: inherit; transition: all .25s cubic-bezier(.2,.8,.2,1);
  position: relative;
}
.dwt-preset:hover {
  color: #00f2fe; border-color: rgba(0,242,254,.45);
  background: rgba(0,242,254,.1); transform: translateY(-1px);
}
.dwt-preset.on {
  color: #00f2fe;
  background: rgba(0, 242, 254, 0.2);
  border: 1px solid #00f2fe;
  box-shadow: 0 0 12px rgba(0,242,254,0.4);
  font-weight: 600;
}
.dwt-preset.clear-btn:hover {
  color: var(--rose); border-color: rgba(255,93,122,.5);
  background: rgba(255,93,122,.1); box-shadow: 0 0 10px rgba(255,93,122,.25);
}
.dwt-search {
  width: 100%; padding: 10px 12px; border-radius: 10px;
  border: 1px solid var(--stroke); background: rgba(0,0,0,.25);
  color: var(--text); font-family: inherit; font-size: 13px; margin-bottom: 8px;
}
.dwt-search:focus { outline: none; border-color: #00f2fe; box-shadow: 0 0 12px rgba(0,242,254,.2); }
.dwt-vessel-list {
  max-height: 280px; overflow: auto; border: 1px solid rgba(160,220,255,.12);
  border-radius: 10px; background: rgba(0,0,0,.2);
}
.dwt-vrow {
  display: flex; align-items: center; gap: 10px; padding: 8px 10px;
  border-bottom: 1px solid rgba(255,255,255,.04); cursor: pointer;
  font-size: 12px; transition: background .2s, opacity .25s, filter .25s;
}
.dwt-vrow:hover, .dwt-vrow.hl { background: rgba(0,242,254,.1); }
.dwt-vrow.sel { background: rgba(0,242,254,.12); box-shadow: inset 3px 0 0 #00f2fe; }
.dwt-vrow.dim { opacity: .28; filter: grayscale(.4); }
.dwt-vrow input { accent-color: #00f2fe; }
.dwt-vrow .vn { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dwt-vrow .vd { font-family: var(--font-mono); font-size: 10px; color: var(--cyan); white-space: nowrap; }
.dwt-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; min-height: 28px;
  transition: opacity .25s; }
.dwt-chip {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid rgba(0,242,254,.3); background: rgba(0,242,254,.08);
  color: #00f2fe; border-radius: 999px; padding: 3px 8px 3px 10px;
  font-size: 11px; font-family: var(--font-mono);
  animation: chipIn .28s cubic-bezier(.2,.8,.2,1);
}
@keyframes chipIn {
  from { opacity: 0; transform: scale(.85) translateY(4px); }
  to   { opacity: 1; transform: none; }
}
.dwt-chip button {
  border: none; background: transparent; color: var(--muted); cursor: pointer;
  font-size: 14px; line-height: 1; padding: 0 2px;
}
.dwt-chip button:hover { color: var(--rose); }
.dwt-chart-wrap {
  display: flex; flex-direction: column; align-items: center; gap: 14px;
  transition: opacity .3s ease;
}
.dwt-chart-wrap.crossfade { opacity: .35; }
.dwt-donut-box { position: relative; width: min(280px, 100%); aspect-ratio: 1; }
.dwt-donut-box svg { width: 100%; height: 100%; filter: drop-shadow(0 0 12px rgba(0,242,254,.35));
  transition: filter .25s; }
.dwt-donut-box svg path {
  transition: opacity .25s, filter .25s; cursor: pointer;
}
.dwt-donut-box svg path.dim { opacity: .22; filter: none !important; }
.dwt-donut-box svg path.hl {
  opacity: 1;
  filter: drop-shadow(0 0 10px #00f2fe) !important;
}
.dwt-donut-center {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; pointer-events: none; text-align: center;
  padding: 20%;
}
.dwt-donut-center .dc-val {
  font-family: var(--font-hud); font-size: clamp(13px, 3.2vw, 18px);
  color: #00f2fe; letter-spacing: .04em; line-height: 1.2;
  text-shadow: 0 0 14px rgba(0,242,254,.5);
  transition: opacity .2s;
}
.dwt-donut-center .dc-sub { font-size: 10px; color: var(--muted); margin-top: 4px; font-family: var(--font-mono); }
.dwt-legend { width: 100%; display: flex; flex-direction: column; gap: 6px; max-height: 200px; overflow: auto; }
.dwt-leg-row {
  display: grid; grid-template-columns: 12px 1fr auto; gap: 8px; align-items: center;
  font-size: 12px; padding: 4px 6px; border-radius: 6px;
  transition: background .2s, opacity .25s; cursor: default;
}
.dwt-leg-row:hover, .dwt-leg-row.hl { background: rgba(0,242,254,.08); }
.dwt-leg-row.dim { opacity: .3; }
.dwt-leg-dot { width: 10px; height: 10px; border-radius: 50%; box-shadow: 0 0 8px currentColor; }
.dwt-info-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px; margin-top: 16px;
}
.dwt-info-card {
  border: 1px solid var(--stroke); border-radius: 12px; padding: 12px 14px;
  background: rgba(0,0,0,.22);
  transition: border-color .3s, box-shadow .3s, transform .3s;
}
.dwt-info-card.pulse {
  border-color: rgba(0,242,254,.45);
  box-shadow: 0 0 16px rgba(0,242,254,.18);
  transform: translateY(-2px);
}
.dwt-info-card .ik { font-size: 9px; font-family: var(--font-mono); letter-spacing: .1em; color: var(--muted); text-transform: uppercase; }
.dwt-info-card .iv { font-size: 15px; font-weight: 700; margin-top: 4px; color: var(--text);
  font-variant-numeric: tabular-nums; }
.dwt-info-card .is { font-size: 11px; color: var(--muted); margin-top: 2px; }
.dwt-mode {
  display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap;
}
.dwt-mode button {
  flex: 1; min-width: 120px; border: 1px solid var(--stroke); background: transparent;
  color: var(--muted); border-radius: 8px; padding: 8px; font-size: 11px;
  cursor: pointer; font-family: inherit; transition: all .2s;
}
.dwt-mode button.on {
  color: #00f2fe; border-color: rgba(0,242,254,.45); background: rgba(0,242,254,.08);
}

.hero {
  position: relative;
  border-radius: 28px;
  padding: 28px 28px 24px;
  overflow: hidden;
  border: 1px solid var(--stroke);
  background:
    linear-gradient(120deg, rgba(255,255,255,.08), rgba(255,255,255,.02) 35%, rgba(92,225,255,.06)),
    var(--glass);
  backdrop-filter: blur(22px) saturate(140%);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.18), 0 24px 60px rgba(0,0,0,.35);
  margin-bottom: 18px;
}
.hero::after {
  content: "";
  position: absolute; left: 0; right: 0; height: 2px; width: 100%;
  background: linear-gradient(90deg, transparent, var(--cyan), transparent);
  animation: scan 4.8s ease-in-out infinite;
  pointer-events: none;
}
.hero h1 {
  margin: 0;
  font-family: var(--font-hud);
  font-size: clamp(22px, 4vw, 34px);
  letter-spacing: .06em;
  background: linear-gradient(90deg, #fff, var(--cyan), var(--mint), #fff);
  background-size: 200% auto;
  -webkit-background-clip: text; background-clip: text; color: transparent;
  animation: shimmer 8s linear infinite;
}
.hero p { margin: 10px 0 0; color: var(--muted); max-width: 62ch; line-height: 1.55; }
.metrics {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 18px;
}
@media (max-width: 900px) { .metrics { grid-template-columns: 1fr 1fr; } }
.metric {
  padding: 12px 14px; border-radius: 16px;
  border: 1px solid var(--stroke);
  background: rgba(4, 14, 28, .45);
  transition: border-color .25s;
}
.metric:hover { border-color: rgba(92,225,255,.4); }
.metric b { display: block; font-family: var(--font-hud); font-size: 20px; color: var(--cyan); }
.metric span { font-size: 11px; color: var(--muted); letter-spacing: .04em; text-transform: uppercase; }

/* ── Toolbar / filter panel ─────────────────────────────────────────────── */
.panel {
  border-radius: 24px;
  border: 1px solid var(--stroke);
  background: var(--glass);
  backdrop-filter: blur(18px);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.12);
  padding: 16px;
  margin-bottom: 14px;
}
.toolbar {
  display: grid;
  grid-template-columns: 1.4fr .8fr .8fr;
  gap: 10px;
}
@media (max-width: 900px) { .toolbar { grid-template-columns: 1fr 1fr; } }
@media (max-width: 600px) { .toolbar { grid-template-columns: 1fr; } }
.fill-stack {
  grid-column: 1 / -1;
  display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
  padding: 10px 14px;
  border-radius: 16px;
  border: 1px solid rgba(0, 242, 254, 0.28);
  background: rgba(0, 242, 254, 0.04);
  backdrop-filter: blur(12px);
  box-shadow: 0 0 10px rgba(0, 242, 254, 0.08), inset 0 1px 0 rgba(255,255,255,.08);
}
.fill-stack label {
  font-family: var(--font-hud); font-size: 10px; letter-spacing: .1em; color: var(--cyan);
  white-space: nowrap; margin: 0;
}
.fill-badge {
  font-family: var(--font-mono); font-size: 13px; font-weight: 600;
  color: #00f2fe;
  text-shadow: 0 0 10px #00f2fe;
  min-width: 56px; text-align: center;
  white-space: nowrap;
}
.slider-wrap { flex: 1; min-width: 160px; position: relative; }
/* Custom NASA range slider */
input[type=range]#fillRateSlider {
  -webkit-appearance: none; appearance: none;
  width: 100%; height: 6px;
  border-radius: 99px;
  background: linear-gradient(90deg, #00f2fe var(--fill-pct, 0%), rgba(160,220,255,.15) var(--fill-pct, 0%));
  outline: none; cursor: pointer;
  transition: box-shadow .2s;
}
input[type=range]#fillRateSlider:hover,
input[type=range]#fillRateSlider:focus {
  box-shadow: 0 0 10px rgba(0,242,254,.55);
}
input[type=range]#fillRateSlider::-webkit-slider-thumb {
  -webkit-appearance: none; appearance: none;
  width: 18px; height: 18px; border-radius: 50%;
  background: radial-gradient(circle, #00f2fe 40%, rgba(0,242,254,.4));
  border: 2px solid rgba(255,255,255,.6);
  box-shadow: 0 0 8px #00f2fe, 0 0 20px rgba(0,242,254,.4);
  transition: box-shadow .15s, transform .15s;
}
input[type=range]#fillRateSlider::-webkit-slider-thumb:hover {
  transform: scale(1.25);
  box-shadow: 0 0 14px #00f2fe, 0 0 30px rgba(0,242,254,.6);
}
input[type=range]#fillRateSlider::-moz-range-thumb {
  width: 16px; height: 16px; border-radius: 50%;
  background: radial-gradient(circle, #00f2fe 40%, rgba(0,242,254,.4));
  border: 2px solid rgba(255,255,255,.6);
  box-shadow: 0 0 8px #00f2fe;
}
label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 4px; letter-spacing: .06em; text-transform: uppercase; }
input[type=search], select {
  width: 100%;
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid var(--stroke);
  background: rgba(3, 10, 20, .65);
  color: var(--text);
  font: inherit;
  outline: none;
  transition: border-color .2s, box-shadow .2s;
}
input[type=search]:focus, select:focus {
  border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(92,225,255,.18);
}
select#fillPreset {
  border-color: rgba(0, 242, 254, 0.3);
}
select#fillPreset:focus { box-shadow: 0 0 0 3px rgba(0,242,254,.22); }

/* ── Live stats bar ─────────────────────────────────────────────────────── */
.stats-bar {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  padding: 8px 14px; border-radius: 12px;
  border: 1px solid rgba(92,225,255,.15);
  background: rgba(4,14,28,.4);
  font-family: var(--font-mono); font-size: 11px; color: var(--muted);
  margin-top: 10px;
}
.stats-bar strong { color: var(--cyan); }

/* ── Layout ─────────────────────────────────────────────────────────────── */
.layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 14px;
}
@media (max-width: 960px) { .layout { grid-template-columns: 1fr; } }

.list {
  max-height: 72vh; overflow: auto;
  border-radius: 18px; border: 1px solid var(--stroke);
  background: rgba(2, 8, 16, .45);
}
/* ── Column Analytics widget ─────────────────────────────────────────────── */
.analytics-wrap {
  border-radius: 24px; border: 1px solid var(--stroke);
  background: var(--glass); backdrop-filter: blur(18px);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.1);
  padding: 20px; margin-bottom: 14px;
}
.analytics-hud {
  font-family: var(--font-hud); font-size: 11px; letter-spacing: .12em;
  color: var(--cyan); margin-bottom: 14px; display: flex; align-items: center; gap: 10px;
}
.analytics-hud::after {
  content: ""; flex: 1; height: 1px;
  background: linear-gradient(90deg, var(--stroke-hot), transparent);
}
.analytics-grid {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 20px; align-items: start;
}
@media (max-width: 800px) { .analytics-grid { grid-template-columns: 1fr; } }
/* donut */
.donut-wrap {
  display: flex; flex-direction: column; align-items: center; gap: 10px;
}
.donut-svg { filter: drop-shadow(0 0 12px rgba(92,225,255,.18)); cursor: pointer; }
.donut-legend { display: flex; flex-direction: column; gap: 6px; width: 100%; }
.legend-item {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; cursor: pointer;
  padding: 4px 8px; border-radius: 8px;
  border: 1px solid transparent;
  transition: background .15s, border-color .15s;
}
.legend-item:hover, .legend-item.active {
  background: rgba(255,255,255,.06); border-color: var(--stroke);
}
.legend-dot {
  width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
  box-shadow: 0 0 6px currentColor;
}
.legend-label { flex: 1; color: var(--muted); }
.legend-count { font-family: var(--font-mono); font-size: 11px; }
/* progress grid */
.prog-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 7px;
}
.prog-card {
  padding: 8px 10px; border-radius: 12px;
  border: 1px solid rgba(160,220,255,.14);
  background: rgba(4,12,24,.5);
  transition: border-color .2s, background .2s, transform .15s;
  cursor: default;
}
.prog-card:hover { background: rgba(20,50,90,.5); }
.prog-card.highlight-high { border-color: rgba(16,185,129,.55); background: rgba(16,185,129,.06); }
.prog-card.highlight-med  { border-color: rgba(245,158,11,.55); background: rgba(245,158,11,.05); }
.prog-card.highlight-low  { border-color: rgba(239,68,68,.5);   background: rgba(239,68,68,.04);  }
.prog-card.dimmed         { opacity: .35; }
.prog-col-name {
  font-family: var(--font-mono); font-size: 9px; color: var(--muted);
  letter-spacing: .06em; text-transform: uppercase; margin-bottom: 4px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.prog-pct {
  font-family: var(--font-hud); font-size: 14px; font-weight: 700;
  line-height: 1;
}
.prog-pct.h { color: #10b981; text-shadow: 0 0 8px rgba(16,185,129,.5); }
.prog-pct.m { color: #f59e0b; text-shadow: 0 0 8px rgba(245,158,11,.4); }
.prog-pct.l { color: #ef4444; text-shadow: 0 0 8px rgba(239,68,68,.4); }
.prog-bar-bg {
  margin-top: 5px; height: 4px; border-radius: 99px;
  background: rgba(255,255,255,.07); overflow: hidden;
}
.prog-bar-fill {
  height: 100%; border-radius: inherit;
  transition: width .6s cubic-bezier(.2,.8,.2,1);
}
.prog-bar-fill.h { background: linear-gradient(90deg, #10b981, #34d399); box-shadow: 0 0 6px rgba(16,185,129,.5); }
.prog-bar-fill.m { background: linear-gradient(90deg, #f59e0b, #fbbf24); box-shadow: 0 0 6px rgba(245,158,11,.4); }
.prog-bar-fill.l { background: linear-gradient(90deg, #ef4444, #f87171); box-shadow: 0 0 6px rgba(239,68,68,.4); }

.list button {
  display: block; width: 100%; text-align: left;
  border: 0; border-bottom: 1px solid rgba(160,220,255,.08);
  background: transparent; color: var(--text);
  padding: 12px 14px; cursor: pointer; font: inherit;
  transition: background .15s, opacity .2s, transform .2s;
}
.list button:hover, .list button.active { background: rgba(92,225,255,.1); }
.list button.hidden { display: none; }
.list .n { font-family: var(--font-mono); font-size: 11px; color: var(--cyan); }
.list .t { font-size: 13px; font-weight: 600; margin-top: 2px; }
.list .f { font-size: 11px; color: var(--muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* fill-rate pill on list item */
.list .fr-pill {
  display: inline-block; border-radius: 999px;
  font-family: var(--font-mono); font-size: 10px; font-weight: 600;
  padding: 1px 7px; margin-top: 3px;
  border: 1px solid transparent;
}
.fr-high  { color: var(--fill-high); border-color: rgba(61,255,154,.35); }
.fr-med   { color: var(--fill-med);  border-color: rgba(255,200,87,.35); }
.fr-low   { color: var(--fill-low);  border-color: rgba(255,107,94,.35); }

.dossier { position: relative; }
.dossier-head {
  display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; align-items: flex-start;
  margin-bottom: 14px;
}
.dossier-head h2 {
  margin: 0; font-family: var(--font-hud); font-size: 22px; letter-spacing: .04em;
}
.badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 10px; border-radius: 999px; font-size: 11px; font-family: var(--font-mono);
  border: 1px solid var(--stroke); background: rgba(0,0,0,.25);
}
.badge.ok  { color: var(--ok);  border-color: rgba(61,255,154,.35); }
.badge.warn{ color: var(--warn);  border-color: rgba(255,200,87,.35); }
.badge.bad { color: var(--bad);   border-color: rgba(255,93,122,.4); animation: pulseRing 2.4s infinite; }
.badge.info{ color: #00f2fe;     border-color: rgba(0,242,254,.35); }
.badge.reg { color: #63caff;     border-color: rgba(99,202,255,.4); }
/* fill-rate badge variants */
.badge.fill-high { color: var(--fill-high); border-color: rgba(61,255,154,.45); box-shadow: 0 0 8px rgba(61,255,154,.3); }
.badge.fill-med  { color: var(--fill-med);  border-color: rgba(255,200,87,.45); }
.badge.fill-low  { color: var(--fill-low);  border-color: rgba(255,107,94,.45);  box-shadow: 0 0 6px rgba(255,107,94,.25); }

.radar-wrap {
  display: grid; grid-template-columns: 280px 1fr; gap: 14px; align-items: center;
  margin-bottom: 14px;
}
@media (max-width: 800px) { .radar-wrap { grid-template-columns: 1fr; } }
.radar {
  width: 100%; max-width: 280px; aspect-ratio: 1; margin: 0 auto;
  filter: drop-shadow(0 0 18px rgba(92,225,255,.2));
}
.layers { display: grid; gap: 8px; }
.layer {
  position: relative; overflow: hidden;
  border-radius: 16px; border: 1px solid var(--stroke);
  background: linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.02));
  backdrop-filter: blur(16px); padding: 12px 14px;
  transition: border-color .2s, transform .2s, background .2s;
}
.layer:hover {
  border-color: var(--stroke-hot); transform: translateY(-1px);
  background: linear-gradient(180deg, rgba(92,225,255,.12), rgba(255,255,255,.03));
}
.layer::before {
  content: ""; position: absolute; inset: 0 auto 0 0; width: 3px;
  background: linear-gradient(180deg, var(--cyan), var(--mint)); opacity: .75;
}
.layer-top { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.layer-id   { font-family: var(--font-hud); font-size: 11px; color: var(--cyan); letter-spacing: .12em; }
.layer-title{ font-weight: 700; font-size: 14px; }
.layer-hint { color: var(--muted); font-size: 11px; margin-top: 2px; }
.bar {
  margin-top: 8px; height: 6px; border-radius: 99px;
  background: rgba(255,255,255,.08); overflow: hidden;
}
.bar > i {
  display: block; height: 100%; width: 0; border-radius: inherit;
  background: linear-gradient(90deg, var(--cyan), var(--mint));
  transition: width .7s cubic-bezier(.2,.8,.2,1);
}
.fields {
  display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 6px 12px; margin-top: 8px;
}
@media (max-width: 600px) { .fields { grid-template-columns: 1fr; } }
.kv { font-size: 12px; }
.kv em { display: block; color: var(--muted); font-style: normal; font-size: 10px; letter-spacing: .05em; text-transform: uppercase; }
.kv b  { font-weight: 600; font-family: var(--font-mono); font-size: 12px; word-break: break-word; }

.tz {
  display: grid; grid-template-columns: repeat(5, minmax(0,1fr)); gap: 8px; margin-top: 14px;
}
@media (max-width: 1100px) { .tz { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 700px)  { .tz { grid-template-columns: repeat(2, 1fr); } }
.tz .cell {
  padding: 10px; border-radius: 14px;
  border: 1px solid rgba(160,220,255,.14); background: rgba(4,12,24,.5);
}
.tz .cell span { display: block; font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
.tz .cell.empty { opacity: .72; border-style: dashed; }
.tz .cell.synth { border-color: rgba(245,158,11,.3); background: rgba(245,158,11,.04); }
.tz .cell.knn    { border-color: rgba(0,242,254,.25); background: rgba(0,242,254,.03); }
.tz .cell.reg    { border-color: rgba(99,202,255,.3); background: rgba(99,202,255,.04); }
.tz .cell.gap strong { color: var(--muted); font-weight: 500; }
.tz .cell span.gap-tag   { color: var(--amber); margin-top: 4px; text-transform: none; letter-spacing: 0; }
.tz .cell span.synth-tag { color: #f59e0b; font-size: 9px; margin-top: 3px;
  font-family: var(--font-mono); opacity: .8; letter-spacing: .04em; }
.tz .cell span.knn-tag   { color: #00f2fe; font-size: 9px; margin-top: 3px;
  font-family: var(--font-mono); opacity: .8; letter-spacing: .04em; }
.tz .cell span.reg-tag   { color: #63caff; font-size: 9px; margin-top: 3px;
  font-family: var(--font-mono); opacity: .85; letter-spacing: .04em; }

.excerpt {
  margin-top: 12px; padding: 12px 14px; border-radius: 14px;
  border: 1px dashed rgba(160,220,255,.25);
  color: var(--muted); font-size: 12px; line-height: 1.55;
  max-height: 160px; overflow: auto; background: rgba(0,0,0,.2);
}
.empty { color: var(--muted); padding: 24px; text-align: center; }

/* ══ LV Vessel Exhibit · Тяжелый люкс ═══════════════════════════════════════ */
.lv-exhibit {
  margin: 8px 0 28px; padding: 0;
  border: 1px solid rgba(212,175,55,.28);
  border-radius: 2px;
  background:
    linear-gradient(165deg, rgba(18,16,12,.92) 0%, rgba(6,8,12,.96) 45%, rgba(4,5,8,.98) 100%);
  box-shadow:
    0 0 0 1px rgba(243,229,171,.06) inset,
    0 24px 64px rgba(0,0,0,.45),
    0 0 48px rgba(212,175,55,.06);
  position: relative;
  overflow: hidden;
}
.lv-exhibit::before {
  content: ""; position: absolute; inset: 0; pointer-events: none; z-index: 2;
  background:
    linear-gradient(90deg, transparent 0%, rgba(212,175,55,.07) 50%, transparent 100%);
  opacity: 0; transition: opacity .6s ease;
}
.lv-exhibit.lv-pulse::before { opacity: 1; animation: lvFlash .9s ease; }
@keyframes lvFlash {
  0% { opacity: .55; } 100% { opacity: 0; }
}
.lv-exhibit-head {
  display: flex; justify-content: space-between; align-items: flex-end;
  gap: 16px; padding: 22px 24px 14px;
  border-bottom: 1px solid rgba(212,175,55,.18);
  position: relative; z-index: 3;
}
.lv-kicker {
  font-family: var(--font-mono); font-size: 10px; letter-spacing: .22em;
  color: var(--lv-gold); text-transform: uppercase; margin-bottom: 6px;
}
.lv-exhibit-head h2 {
  margin: 0; font-family: var(--font-lv); font-weight: 600;
  font-size: clamp(22px, 3vw, 32px); letter-spacing: .04em;
  color: var(--lv-gold-soft); line-height: 1.15;
}
.lv-exhibit-head p {
  margin: 6px 0 0; max-width: 48ch; color: var(--lv-titanium);
  font-size: 13px; line-height: 1.5;
}
.lv-fps {
  font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
  color: rgba(212,175,55,.65); border: 1px solid rgba(212,175,55,.25);
  padding: 6px 10px; white-space: nowrap;
}
.lv-body {
  display: grid; grid-template-columns: minmax(200px, 260px) 1fr;
  gap: 0; min-height: 460px; position: relative; z-index: 1;
}
@media (max-width: 900px) {
  .lv-body { grid-template-columns: 1fr; min-height: auto; }
}
.lv-rail {
  padding: 16px 14px 20px;
  border-right: 1px solid rgba(212,175,55,.16);
  background: linear-gradient(180deg, rgba(20,18,14,.5), rgba(8,9,12,.35));
  display: flex; flex-direction: column; gap: 8px;
}
@media (max-width: 900px) {
  .lv-rail {
    border-right: none; border-bottom: 1px solid rgba(212,175,55,.16);
    flex-direction: row; flex-wrap: wrap; gap: 6px;
  }
}
.lv-rail-label {
  font-family: var(--font-mono); font-size: 9px; letter-spacing: .18em;
  color: rgba(212,175,55,.7); text-transform: uppercase; margin: 0 0 6px;
  width: 100%;
}
.lv-mode {
  display: flex; align-items: flex-start; gap: 10px; text-align: left;
  width: 100%; padding: 11px 12px;
  border: 1px solid rgba(138,144,153,.35);
  background:
    linear-gradient(135deg, rgba(40,42,48,.85), rgba(16,18,22,.9));
  color: #c8cdd4; cursor: pointer; font-family: var(--font-ui);
  transition: border-color .35s ease, box-shadow .35s ease, transform .35s ease;
  border-radius: 1px;
}
.lv-mode:hover {
  border-color: rgba(212,175,55,.45);
  transform: translateX(2px);
}
.lv-mode.on {
  border-color: var(--lv-gold);
  box-shadow:
    0 0 0 1px rgba(212,175,55,.25) inset,
    0 0 18px rgba(212,175,55,.18);
  color: var(--lv-gold-soft);
}
.lv-mode-idx {
  font-family: var(--font-mono); font-size: 10px; color: var(--lv-gold);
  letter-spacing: .08em; min-width: 1.6em;
}
.lv-mode-label {
  font-size: 12px; line-height: 1.35; font-weight: 600;
}
.lv-stage-wrap {
  position: relative; min-height: 420px;
  background:
    radial-gradient(ellipse at 50% 30%, rgba(212,175,55,.08), transparent 55%),
    radial-gradient(ellipse at 70% 80%, rgba(80,100,130,.12), transparent 50%),
    #05070b;
}
.lv-stage {
  position: absolute; inset: 0;
}
.lv-stage canvas { display: block; width: 100% !important; height: 100% !important; }
.lv-stage-meta {
  position: absolute; left: 16px; bottom: 14px; z-index: 4;
  pointer-events: none;
  border-left: 1px solid var(--lv-gold); padding-left: 12px;
}
.lv-stage-meta strong {
  display: block; font-family: var(--font-lv); font-size: 18px;
  color: var(--lv-gold-soft); font-weight: 600; letter-spacing: .03em;
}
.lv-stage-meta span {
  font-family: var(--font-mono); font-size: 10px; color: var(--lv-titanium);
  letter-spacing: .06em;
}
.lv-hairline {
  height: 1px; background: linear-gradient(90deg, transparent, rgba(212,175,55,.45), transparent);
  margin: 0;
}
.lv-badge3d {
  pointer-events: none;
  padding: 8px 12px;
  background: rgba(8,9,12,.82);
  border: 1px solid rgba(212,175,55,.45);
  box-shadow: 0 8px 28px rgba(0,0,0,.4), 0 0 16px rgba(212,175,55,.12);
  backdrop-filter: blur(8px);
  min-width: 110px;
  transition: opacity .5s ease;
}
.lv-badge3d b {
  display: block; font-family: var(--font-mono); font-size: 9px;
  letter-spacing: .14em; color: var(--lv-gold); text-transform: uppercase;
  font-weight: 500; margin-bottom: 3px;
}
.lv-badge3d span {
  font-family: var(--font-lv); font-size: 15px; color: var(--lv-gold-soft);
  letter-spacing: .02em;
}
.lv-footnote {
  padding: 10px 24px 14px; font-size: 11px; color: rgba(138,144,153,.85);
  font-family: var(--font-mono); letter-spacing: .04em;
  border-top: 1px solid rgba(212,175,55,.12);
}
</style>
</head>
<body>
<div class="wrap">
  <div class="nav">
    <a class="brand" href="mission_control.html">
      <div class="brand-mark">O1</div>
      <div>
        <strong>ORACLE-1001</strong>
        <span>Досье OSINT · 9 слоёв · NASA / Wet Glass</span>
      </div>
    </a>
    <div class="nav-links">
      <a class="pill" href="mission_control.html">Центр управления</a>
      <a class="pill" href="dashboard.html">Реестр флота</a>
      <a class="pill active" href="osint_layers.html">9 аналитических слоёв</a>
      <a class="pill" href="fleet_meta_analysis.html">Мета-аналитика</a>
      <a class="pill" href="top100_analytics.html">Анализ ТОП-100</a>
      <a class="pill" href="top200_analytics.html">Анализ ТОП-200</a>
      <a class="pill" href="top500_analytics.html">Анализ ТОП-500</a>
      <button type="button" class="pill analytics-btn" id="btnAnalytics" title="Аналитика дедвейта">
        ◉ Аналитика
      </button>
    </div>
  </div>

  <section class="hero">
    <h1>ДЕВЯТЬ СЛОЁВ OSINT</h1>
    <p>Очищенная БЗ газовозов → 20 столбцов ТЗ × 9 аналитических слоёв. Пропущенные поля остаются пустыми — без фабрикации.</p>
    <div class="metrics" id="metrics"></div>
  </section>

  <section class="lv-exhibit" id="lvVesselExhibit" aria-label="3D LNG Tanker · IMO 9001772">
    <div class="lv-exhibit-head">
      <div>
        <div class="lv-kicker">ORACLE-1001 · WebGL LNG Exhibit</div>
        <h2>LARA · IMO 9001772</h2>
        <p>Procedural Moss-class LNG tanker — LOA 239 m × Beam 40 m. R1–R5 interactive layers: cinematic PBR, X-ray, hydrodynamics, OSINT risk heatmap, identity plate.</p>
      </div>
      <div class="lv-fps" data-lv-fps>— FPS</div>
    </div>
    <div class="lv-hairline"></div>
    <div class="lv-body">
      <aside class="lv-rail" aria-label="Режимы R1–R5">
        <div class="lv-rail-label">Modes · R1–R5</div>
        <div data-lv-modes>
          <button type="button" class="lv-mode on" id="mode-r1" data-mode="r1" aria-pressed="true">
            <span class="lv-mode-idx">R1</span>
            <span class="lv-mode-label">Cinematic</span>
          </button>
          <button type="button" class="lv-mode" id="mode-r2" data-mode="r2" aria-pressed="false">
            <span class="lv-mode-idx">R2</span>
            <span class="lv-mode-label">X-Ray Profiling</span>
          </button>
          <button type="button" class="lv-mode" id="mode-r3" data-mode="r3" aria-pressed="false">
            <span class="lv-mode-idx">R3</span>
            <span class="lv-mode-label">Hydrodynamics</span>
          </button>
          <button type="button" class="lv-mode" id="mode-r4" data-mode="r4" aria-pressed="false">
            <span class="lv-mode-idx">R4</span>
            <span class="lv-mode-label">Risk Scan</span>
          </button>
          <button type="button" class="lv-mode" id="mode-r5" data-mode="r5" aria-pressed="false">
            <span class="lv-mode-idx">R5</span>
            <span class="lv-mode-label">Spec Plate</span>
          </button>
        </div>
      </aside>
      <div class="lv-stage-wrap">
        <div class="lv-stage" data-lv-stage id="vessel-3d-stage"></div>
        <div class="lv-stage-meta">
          <strong data-lv-mode-title>R1 · Cinematic</strong>
          <span data-lv-mode-hint>PBR Titanium / Dark Chrome · 360° orbit</span>
        </div>
        <div data-lv-badges hidden></div>
      </div>
    </div>
    <div class="lv-footnote">Keys 1–5 switch modes · mouse orbit · adaptive GPU · target ≥60 FPS</div>
  </section>

  <section class="analytics-wrap dashboard-panel" id="colAnalytics">
    <div class="analytics-hud">20 КЛЮЧЕВЫХ КОЛОНОК ТЗ · РАСПРЕДЕЛЕНИЕ ЗАПОЛНЕННОСТИ ФЛОТА</div>
    <div class="analytics-grid">
      <div class="donut-wrap">
        <svg id="donutSvg" class="donut-svg" viewBox="0 0 200 200" width="200" height="200"
             role="img" aria-label="Column fill distribution donut"></svg>
        <div class="donut-legend" id="donutLegend"></div>
      </div>
      <div class="prog-grid" id="progGrid"></div>
    </div>
  </section>

  <section class="panel">
    <div class="toolbar">
      <div>
        <label for="q">Поиск (имя / IMO / флаг)</label>
        <input id="q" type="search" placeholder="MIHZEM · 9986635 · SINGAPORE" autocomplete="off" />
      </div>
      <div>
        <label for="risk">Риск</label>
        <select id="risk">
          <option value="">Все</option>
          <option value="extreme">Экстремальный / критический</option>
          <option value="high">Высокий</option>
          <option value="mid">Средний</option>
          <option value="low">Низкий / чистый</option>
          <option value="unk">Без метки</option>
        </select>
      </div>
      <div>
        <label for="sort">Сортировка</label>
        <select id="sort">
          <option value="risk">Риск ↓</option>
          <option value="fill_desc">Полнота ↓ (сначала заполненные)</option>
          <option value="fill_asc">Полнота ↑ (сначала пропуски)</option>
          <option value="name">Имя A→Z</option>
          <option value="dwt">Дедвейт ↓</option>
          <option value="imo">Номер IMO</option>
        </select>
      </div>

      <!-- Fill Rate filter stack -->
      <div class="fill-stack">
        <label>ПОЛНОТА ≥</label>
        <span class="fill-badge" id="fillRateVal">≥ 0%</span>
        <div class="slider-wrap">
          <input type="range" id="fillRateSlider" min="0" max="100" step="5" value="0" />
        </div>
        <select id="fillPreset" style="width:auto;min-width:180px;padding:8px 10px">
          <option value="0">Все суда</option>
          <option value="100">Только 100% (20/20)</option>
          <option value="80">Высокая полнота (≥80%)</option>
          <option value="50x">&lt;50%: Требуют доработки</option>
        </select>
      </div>
    </div>

    <!-- Live stats bar -->
    <div class="stats-bar" id="statsBar">
      <span>Загрузка…</span>
    </div>
  </section>

  <div class="layout">
    <aside class="list" id="list" aria-label="Vessel list"></aside>
    <main class="panel dossier" id="dossier">
      <div class="empty">Выберите судно слева</div>
    </main>
  </div>
</div>

<!-- DWT Analytics Overlay -->
<div class="dwt-overlay" id="dwtOverlay" role="dialog" aria-modal="true" aria-labelledby="dwtTitle" hidden>
  <div class="dwt-modal">
    <div class="dwt-head">
      <div>
        <h2 id="dwtTitle">◉ АНАЛИТИКА ДЕДВЕЙТА · СРАВНЕНИЕ</h2>
        <p id="dwtHeroSub">Множественный выбор судов → доля дедвейта считается на лету из поля «Дедвейт»</p>
      </div>
      <button type="button" class="dwt-close" id="dwtClose">✕ Закрыть</button>
    </div>
    <div class="dwt-grid">
      <div class="dwt-panel">
        <label>Пресеты</label>
        <div class="dwt-presets" id="dwtPresets">
          <button type="button" class="dwt-preset" data-preset="top5">ТОП-5 по дедвейту</button>
          <button type="button" class="dwt-preset" data-preset="top10lng">ТОП-10 СПГ</button>
          <button type="button" class="dwt-preset" data-preset="comoros">Флаг Комор</button>
          <button type="button" class="dwt-preset" data-preset="shadow">Теневой флот / Серый флаг</button>
          <button type="button" class="dwt-preset" data-preset="extreme">Крайний риск (EXTREME)</button>
          <button type="button" class="dwt-preset" data-preset="age20">Возраст &gt; 20 лет</button>
          <button type="button" class="dwt-preset" data-preset="flng">ФПСУ и мегаструктуры</button>
          <button type="button" class="dwt-preset" data-preset="idle">На якоре / В простое</button>
          <button type="button" class="dwt-preset" data-preset="vsfleet">Выбранное против флота</button>
          <button type="button" class="dwt-preset clear-btn" data-preset="clear">Сбросить выбор</button>
        </div>
        <label>Режим диаграммы</label>
        <div class="dwt-mode" id="dwtMode">
          <button type="button" class="on" data-mode="vsfleet">Выборка против остального флота</button>
          <button type="button" data-mode="ingroup">Доли внутри выборки</button>
        </div>
        <label>Множественный выбор (IMO / Название)</label>
        <input class="dwt-search" id="dwtSearch" type="search" placeholder="WOODSIDE · 9633161 · СПГ…" autocomplete="off" />
        <div class="dwt-vessel-list" id="dwtVesselList"></div>
        <div class="dwt-chips" id="dwtChips"></div>
      </div>
      <div class="dwt-panel dwt-chart-wrap">
        <div class="dwt-donut-box">
          <svg id="dwtDonutSvg" viewBox="0 0 200 200" role="img" aria-label="Круговая диаграмма дедвейта"></svg>
          <div class="dwt-donut-center">
            <div class="dc-val" id="dwtCenterVal">—</div>
            <div class="dc-sub" id="dwtCenterSub">выберите суда</div>
          </div>
        </div>
        <div class="dwt-legend" id="dwtLegend"></div>
      </div>
    </div>
    <div class="dwt-info-grid" id="dwtInfo"></div>
  </div>
</div>

<script>
const DATA = __PAYLOAD__;
const LAYERS = DATA.layers;
const TZ = DATA.tz_columns;

/** Русские подписи 20 колонок ТЗ */
const TZ_RU = {
  vessel_name: "Наименование судна",
  imo: "Номер IMO",
  mmsi: "Код MMSI",
  call_sign: "Позывной",
  vessel_type: "Тип судна",
  built_year: "Год постройки",
  age_years: "Возраст (лет)",
  flag: "Флаг государства",
  dwt_tons: "Дедвейт (DWT, т)",
  gt: "Валовая вместимость (GT)",
  loa_m: "Длина (LOA, м)",
  beam_m: "Ширина (Beam, м)",
  draft_m: "Осадка (Draft, м)",
  nav_status: "Навигационный статус",
  speed_knots: "Скорость (узлы)",
  destination_port: "Порт назначения",
  destination_context: "Контекст маршрута",
  departure_port: "Порт отбытия",
  arrival_datetime: "Расчетное время прибытия (ETA)",
  compliance_risk_level: "Уровень комплаенс-риска",
};
function colRu(c){ return TZ_RU[c] || String(c||"").replace(/_/g," "); }

/** Локализация навигационных статусов для отображения */
function navRu(s){
  const t = String(s||"");
  const map = [
    [/underway using engine/i, "В пути (под двигателем)"],
    [/under\s*way/i, "В пути (под двигателем)"],
    [/at anchor/i, "На якорной стоянке"],
    [/moored/i, "Пришвартовано"],
    [/not under command/i, "Ограничено в маневрировании"],
    [/restricted manoeuvrability/i, "Ограниченная манёвренность"],
    [/not in service|out of service/i, "Выведено из эксплуатации"],
    [/constrained by her draught/i, "Ограничено осадкой"],
    [/engaged in fishing/i, "Занято промыслом"],
  ];
  for (const [re, ru] of map) if (re.test(t)) return t.replace(re, ru);
  return t;
}

function fmt(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number" && Number.isFinite(v)) {
    return Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
  }
  return String(v);
}

function fmtField(col, v) {
  if (col === "nav_status") return navRu(fmt(v));
  return fmt(v);
}

function riskClass(level) {
  const u = String(level || "").toUpperCase();
  if (!u) return { cls: "warn", label: "NO RISK LABEL" };
  if (u === "EXTREME" || u.includes("ЭКСТРЕМАЛЬ") || u.includes("КРИТИЧЕСКИ") || u.includes("GHOST")) return { cls: "bad", label: level };
  if (u === "HIGH"    || u.includes("ВЫСОК") || u.includes("HIGH"))   return { cls: "bad",  label: level };
  if (u === "MID"     || u === "MEDIUM" || u.includes("СРЕДН"))        return { cls: "warn", label: level };
  if (u === "LOW"     || u.includes("НИЗК") || u.includes("LOW") || u.includes("ЧИСТ")) return { cls: "ok", label: level };
  return { cls: "warn", label: level };
}

function riskBucket(level) {
  const u = String(level || "").toUpperCase();
  if (!u) return "unk";
  if (u === "EXTREME" || u.includes("ЭКСТРЕМАЛЬ") || u.includes("КРИТИЧЕСКИ") || u.includes("GHOST")) return "extreme";
  if (u === "HIGH"    || u.includes("ВЫСОК") || u.includes("HIGH"))   return "high";
  if (u === "MID"     || u === "MEDIUM" || u.includes("СРЕДН"))        return "mid";
  if (u === "LOW"     || u.includes("НИЗК") || u.includes("LOW") || u.includes("ЧИСТ")) return "low";
  return "unk";
}

function fillClass(pct) {
  if (pct >= 80) return "fill-high";
  if (pct >= 50) return "fill-med";
  return "fill-low";
}
function fillBadgeClass(pct) {
  if (pct >= 80) return "badge fill-high";
  if (pct >= 50) return "badge fill-med";
  return "badge fill-low";
}

// ── State ────────────────────────────────────────────────────────────────────
let fillThreshold = 0;   // 0..100
let fillPresetMode = "normal"; // "normal" | "under50"

function getSliderValue() { return parseInt(document.getElementById("fillRateSlider").value, 10); }

function applyFilters() {
  const q    = document.getElementById("q").value.trim().toLowerCase();
  const risk = document.getElementById("risk").value;
  const sort = document.getElementById("sort").value;
  const minFill = fillThreshold;
  const under50 = fillPresetMode === "under50";

  let rows = DATA.vessels.slice();

  // ── search ──────────────────────────────────────────────────────────────
  if (q) {
    rows = rows.filter(v => {
      const blob = [v.vessel_name, v.imo, v.flag, v.mmsi, v.vessel_type]
        .map(x => String(x || "").toLowerCase()).join(" ");
      return blob.includes(q);
    });
  }

  // ── risk ─────────────────────────────────────────────────────────────────
  if (risk) rows = rows.filter(v => riskBucket(v.compliance_risk_level) === risk);

  // ── fill rate ─────────────────────────────────────────────────────────────
  if (under50) {
    rows = rows.filter(v => (v.tz_fill_pct || 0) < 50);
  } else if (minFill > 0) {
    rows = rows.filter(v => (v.tz_fill_pct || 0) >= minFill);
  }

  // ── sort ──────────────────────────────────────────────────────────────────
  rows.sort((a, b) => {
    if (sort === "name")      return String(a.vessel_name || "").localeCompare(String(b.vessel_name || ""));
    if (sort === "imo")       return String(a.imo || "").localeCompare(String(b.imo || ""));
    if (sort === "dwt")       return (Number(b.dwt_tons) || 0) - (Number(a.dwt_tons) || 0);
    if (sort === "fill_desc") return (Number(b.tz_fill_pct) || 0) - (Number(a.tz_fill_pct) || 0);
    if (sort === "fill_asc")  return (Number(a.tz_fill_pct) || 0) - (Number(b.tz_fill_pct) || 0);
    return (Number(b.risk_score) || 0) - (Number(a.risk_score) || 0);
  });

  return rows;
}

// ── Stats bar ─────────────────────────────────────────────────────────────────
function updateStats(rows) {
  const count = rows.length;
  const total = DATA.vessels.length;
  const avgFill = count > 0
    ? (rows.reduce((s, v) => s + (v.tz_fill_pct || 0), 0) / count).toFixed(1)
    : "—";
  const m = DATA.metrics || {};
  const buckets = m.fill_buckets || {};
  document.getElementById("statsBar").innerHTML =
    `<strong>${count}</strong> из ${total} судов · Средний уровень заполнения: <strong>${avgFill}%</strong>` +
    ` &nbsp;|&nbsp; ` +
    `<span style="color:var(--fill-high)">100%: ${buckets.perfect||0}</span> · ` +
    `<span style="color:var(--fill-high)">≥80%: ${buckets.high||0}</span> · ` +
    `<span style="color:var(--fill-med)">50-79%: ${buckets.medium||0}</span> · ` +
    `<span style="color:var(--fill-low)">&lt;50%: ${buckets.low||0}</span>`;
}

// ── Render ────────────────────────────────────────────────────────────────────
function renderMetrics() {
  const m = DATA.metrics || {};
  const rd = m.risk_distribution || {};
  const highPlus = (rd["EXTREME"] || 0) + (rd["HIGH"] || 0);
  const avgFill  = m.avg_fill_pct ?? "—";
  const heurPct  = m.vessel_count
    ? Math.round(100 * (m.risk_heuristic_count || 0) / m.vessel_count) : 0;
  document.getElementById("metrics").innerHTML = `
    <div class="metric"><b>${m.vessel_count ?? DATA.vessels.length}</b><span>Суда флота</span></div>
    <div class="metric"><b>20</b><span>Ключевые колонки ТЗ</span></div>
    <div class="metric"><b>${avgFill}%</b><span>Средний уровень заполнения</span></div>
    <div class="metric" title="Крайний: ${rd['EXTREME']||0} · Высокий: ${rd['HIGH']||0}"><b>${highPlus}</b><span>Суда с повышенным риском</span></div>
    <div class="metric" style="grid-column:1/-1;display:flex;gap:10px;flex-wrap:wrap;padding:10px 14px">
      ${Object.entries(rd).sort((a,b)=>b[1]-a[1]).map(([k,v])=>{
        const cls = k==='EXTREME'||k==='HIGH' ? 'bad' : k==='MID' ? 'warn' : 'ok';
        const label = ({EXTREME:'Крайний',HIGH:'Высокий',MID:'Средний',LOW:'Низкий',UNLABELED:'Без метки'})[k] || k;
        return `<span class="badge ${cls}">${label}: ${v}</span>`;
      }).join("")}
      <span class="badge" style="margin-left:auto;opacity:.6">${heurPct}% эвристика</span>
    </div>
  `;
}

let selectedImo = null;

function renderList() {
  const rows = applyFilters();
  updateStats(rows);

  const box = document.getElementById("list");
  if (!rows.length) {
    box.innerHTML = `<div class="empty">Нет совпадений</div>`;
    selectedImo = null;
    return;
  }
  if (!selectedImo || !rows.find(r => String(r.imo) === String(selectedImo))) {
    selectedImo = rows[0].imo;
  }
  box.innerHTML = rows.slice(0, 400).map(v => {
    const pct = v.tz_fill_pct || 0;
    const fc  = fillClass(pct);
    return `
    <button type="button" data-imo="${fmt(v.imo)}" class="${String(v.imo)===String(selectedImo)?'active':''}">
      <div class="n">IMO ${fmt(v.imo)}</div>
      <div class="t">${fmt(v.vessel_name)}</div>
      <div class="f">${fmt(v.flag)} · ${fmt(v.compliance_risk_level)}</div>
      <span class="fr-pill ${fc}">${pct}% · ${v.tz_fill||0}/20</span>
    </button>`;
  }).join("");

  box.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      selectedImo = btn.getAttribute("data-imo");
      renderList();
      renderDossier();
    });
  });
}

function radarSVG(scores) {
  const n = scores.length;
  const cx = 140, cy = 140, R = 108;
  const pts = scores.map((s, i) => {
    const a = -Math.PI/2 + (i * 2 * Math.PI / n);
    const r = R * Math.max(0.08, s);
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  });
  const poly = pts.map(p => p.join(",")).join(" ");
  let rings = "";
  for (let k = 1; k <= 4; k++) {
    const rr = R * k / 4;
    rings += `<circle cx="${cx}" cy="${cy}" r="${rr}" fill="none" stroke="rgba(160,220,255,.18)" stroke-width="1"/>`;
  }
  let spokes = "", labels = "";
  for (let i = 0; i < n; i++) {
    const a = -Math.PI/2 + (i * 2 * Math.PI / n);
    const x2 = cx + R * Math.cos(a), y2 = cy + R * Math.sin(a);
    spokes += `<line x1="${cx}" y1="${cy}" x2="${x2}" y2="${y2}" stroke="rgba(160,220,255,.16)"/>`;
    const lx = cx + (R+18)*Math.cos(a), ly = cy + (R+18)*Math.sin(a);
    labels += `<text x="${lx}" y="${ly}" fill="#8aa4bf" font-size="10" font-family="Orbitron,sans-serif" text-anchor="middle" dominant-baseline="middle">L${i+1}</text>`;
  }
  return `<svg class="radar" viewBox="0 0 280 280" role="img" aria-label="9-layer radar">
    ${rings}${spokes}
    <polygon points="${poly}" fill="rgba(92,225,255,.18)" stroke="#5ce1ff" stroke-width="2"/>
    ${pts.map(p => `<circle cx="${p[0]}" cy="${p[1]}" r="3.5" fill="#7dffe1"/>`).join("")}
    ${labels}
  </svg>`;
}

function renderDossier() {
  const v = DATA.vessels.find(x => String(x.imo) === String(selectedImo));
  const root = document.getElementById("dossier");
  if (!v) {
    root.innerHTML = `<div class="empty">Судно не найдено</div>`;
    return;
  }
  const risk   = riskClass(v.compliance_risk_level);
  const scores = v.layers || LAYERS.map(() => 0);
  const pct    = v.tz_fill_pct || 0;
  const fbc    = fillBadgeClass(pct);

  const layerCards = LAYERS.map((L, i) => {
    const s = scores[i] || 0;
    const fieldsHtml = L.fields.map(f => {
      if (f === "raw_text") {
        return `<div class="kv"><em>signal depth</em><b>${Math.round(s*100)}% · ${fmt((v.raw_text||"").length)} chars</b></div>`;
      }
      return `<div class="kv"><em>${colRu(f)}</em><b>${fmtField(f, v[f])}</b></div>`;
    }).join("");
    return `<article class="layer" data-layer="${L.id}">
      <div class="layer-top">
        <div>
          <div class="layer-id">${L.id} · ${L.code}</div>
          <div class="layer-title">${L.title}</div>
          <div class="layer-hint">${L.hint}</div>
        </div>
        <div class="badge">${Math.round(s*100)}%</div>
      </div>
      <div class="bar"><i style="width:${Math.round(s*100)}%"></i></div>
      <div class="fields">${fieldsHtml}</div>
    </article>`;
  }).join("");

  const gaps    = new Set(v.source_gaps || []);
  const synthF  = new Set(String(v.synthetic_fields || "").split(";").map(s=>s.trim()).filter(Boolean));
  const knnF    = new Set(String(v.imputed_fields   || "").split(";").map(s=>s.trim()).filter(Boolean));
  const regF    = new Set(String(v.registry_mock_fields || "").split(";").map(s=>s.trim()).filter(Boolean));
  const tzHtml  = TZ.map(c => {
    const val      = fmtField(c, v[c]);
    const empty    = val === "—";
    const isReg    = regF.has(c);
    const isKNN    = knnF.has(c);
    const isSynth  = synthF.has(c);
    const cls      = empty    ? "cell empty"
                   : isReg   ? "cell reg"
                   : isKNN   ? "cell knn"
                   : isSynth ? "cell synth"
                   : "cell";
    const tag      = empty   ? `<span class="gap-tag">—</span>`
                   : isReg   ? `<span class="reg-tag">🌐 Реестр AIS</span>`
                   : isKNN   ? `<span class="knn-tag">🔬 Профиль (KNN)</span>`
                   : isSynth ? `<span class="synth-tag">⚡ Синтетика</span>` : "";
    const title    = isReg   ? "🌐 Эмулятор реестра AIS / Equasis / Lloyd's"
                   : isKNN   ? "🔬 Профильная экстраполяция (KNN)"
                   : isSynth ? "⚡ Синтетика / детерминированный хэш"
                   : colRu(c);
    return `<div class="${cls}" title="${title}"><span>${colRu(c)}</span><strong>${val}</strong>${tag}</div>`;
  }).join("");

  root.innerHTML = `
    <div class="dossier-head">
      <div>
        <h2>${fmt(v.vessel_name)}</h2>
        <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
          <span class="badge">IMO ${fmt(v.imo)}</span>
          <span class="badge">MMSI ${fmt(v.mmsi)}</span>
          <span class="badge ${risk.cls}">${risk.label}</span>
          <span class="badge">${fmt(v.source_confidence)}</span>
      <span class="${fbc}">ТЗ ${fmt(v.tz_fill)}/20 · ${pct}%</span>
          ${v.risk_source === 'heuristic' ? '<span class="badge warn" title="Риск назначен эвристическим движком">⚙ Эвристика</span>' : ''}
          ${synthF.size > 0 ? `<span class="badge warn"  title="Синтетика: ${[...synthF].map(colRu).join(', ')}">⚡ Синтетика ${synthF.size}</span>` : ''}
          ${knnF.size   > 0 ? `<span class="badge info"  title="Профиль: ${[...knnF].map(colRu).join(', ')}">🔬 Профиль ${knnF.size}</span>` : ''}
          ${regF.size   > 0 ? `<span class="badge reg"   title="Реестр AIS: ${[...regF].map(colRu).join(', ')}">🌐 Реестр AIS ${regF.size}</span>` : ''}
          ${(String(v.sanctions_tags||"").split(";").map(s=>s.trim()).filter(Boolean).map(t=>`<span class="badge bad">L8 · ${t}</span>`).join(""))}
        </div>
      </div>
    </div>
    <div class="radar-wrap">
      ${radarSVG(scores)}
      <div>
        <div style="font-family:var(--font-hud);font-size:12px;letter-spacing:.12em;color:var(--cyan);margin-bottom:8px">РЕЗОНАНС СЛОЁВ</div>
        <div class="layers">${layerCards}</div>
      </div>
    </div>
    <div style="font-family:var(--font-hud);font-size:12px;letter-spacing:.12em;color:var(--cyan);margin:8px 0">20 КЛЮЧЕВЫХ КОЛОНОК ТЗ · ЗАПОЛНЕНИЕ ${pct}%</div>
    <div class="tz">${tzHtml}</div>
    <div class="excerpt">${fmt(v.excerpt)}</div>
  `;
  requestAnimationFrame(() => {
    root.querySelectorAll(".bar > i").forEach(el => {
      const w = el.style.width; el.style.width = "0";
      requestAnimationFrame(() => { el.style.width = w; });
    });
  });
}

// ── Column Analytics: SVG Donut + Progress Grid ──────────────────────────────
(function renderColAnalytics() {
  const stats   = (DATA.metrics || {}).column_fill_stats || {};
  const cols    = DATA.tz_columns || [];
  const total   = cols.length;

  // Classify each column
  const high = cols.filter(c => (stats[c]?.pct || 0) >= 80);
  const med  = cols.filter(c => { const p = stats[c]?.pct || 0; return p >= 50 && p < 80; });
  const low  = cols.filter(c => (stats[c]?.pct || 0) < 50);

  const groups = [
    { key: "high", label: "Высокая плотность ≥80%",   cols: high, color: "#10b981", glow: "rgba(16,185,129,.55)"  },
    { key: "med",  label: "Средняя 50–79%",            cols: med,  color: "#f59e0b", glow: "rgba(245,158,11,.5)"  },
    { key: "low",  label: "Низкая плотность <50%",     cols: low,  color: "#ef4444", glow: "rgba(239,68,68,.5)"   },
  ];

  let activeGroup = null; // null = all highlighted

  // ── SVG Donut ───────────────────────────────────────────────────────────
  function buildDonut() {
    const svg   = document.getElementById("donutSvg");
    const cx    = 100, cy = 100, R = 78, r = 48;
    const TAU   = 2 * Math.PI;
    let startAngle = -Math.PI / 2;
    let paths = "";

    groups.forEach(g => {
      if (!g.cols.length) return;
      const frac  = g.cols.length / total;
      const sweep = frac * TAU;
      const endA  = startAngle + sweep;
      const large = sweep > Math.PI ? 1 : 0;
      const x1 = cx + R * Math.cos(startAngle), y1 = cy + R * Math.sin(startAngle);
      const x2 = cx + R * Math.cos(endA),       y2 = cy + R * Math.sin(endA);
      const ix1 = cx + r * Math.cos(endA),       iy1 = cy + r * Math.sin(endA);
      const ix2 = cx + r * Math.cos(startAngle), iy2 = cy + r * Math.sin(startAngle);
      const d = `M ${x1} ${y1} A ${R} ${R} 0 ${large} 1 ${x2} ${y2} `
              + `L ${ix1} ${iy1} A ${r} ${r} 0 ${large} 0 ${ix2} ${iy2} Z`;
      paths += `<path d="${d}" fill="${g.color}" fill-opacity="0.85"
                  stroke="rgba(3,7,15,.6)" stroke-width="1.5"
                  data-group="${g.key}" style="cursor:pointer;transition:fill-opacity .2s,filter .2s"
                  filter="url(#glow_${g.key})">
                  <title>${g.label}: ${g.cols.length} columns</title>
                </path>`;
      startAngle = endA;
    });

    // Center label
    const avgPct = (DATA.metrics || {}).avg_fill_pct || 0;
    svg.innerHTML = `
      <defs>
        ${groups.map(g => `
          <filter id="glow_${g.key}" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur in="SourceGraphic" stdDeviation="3" result="blur"/>
            <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
          </filter>`).join("")}
      </defs>
      ${paths}
      <circle cx="${cx}" cy="${cy}" r="${r - 4}" fill="rgba(2,8,16,.82)"/>
      <text x="${cx}" y="${cy - 8}" text-anchor="middle"
            fill="#5ce1ff" font-family="Orbitron,sans-serif" font-size="18" font-weight="700">${avgPct}%</text>
      <text x="${cx}" y="${cy + 9}" text-anchor="middle"
            fill="#8aa4bf" font-family="Orbitron,sans-serif" font-size="8" letter-spacing=".1em">СРЕД. ЗАПОЛН.</text>
      <text x="${cx}" y="${cy + 22}" text-anchor="middle"
            fill="#8aa4bf" font-family="Orbitron,sans-serif" font-size="7" letter-spacing=".06em">20 КОЛОНОК ТЗ</text>
    `;

    // Donut segment click
    svg.querySelectorAll("path[data-group]").forEach(el => {
      el.addEventListener("click", () => {
        const gk = el.getAttribute("data-group");
        setActiveGroup(activeGroup === gk ? null : gk);
      });
    });
  }

  // ── Legend ───────────────────────────────────────────────────────────────
  function buildLegend() {
    const box = document.getElementById("donutLegend");
    box.innerHTML = groups.map(g => `
      <div class="legend-item ${activeGroup === g.key ? 'active' : ''}"
           data-group="${g.key}" style="color:${g.color}">
        <span class="legend-dot" style="background:${g.color};color:${g.color}"></span>
        <span class="legend-label">${g.label}</span>
        <span class="legend-count" style="color:${g.color}">${g.cols.length}</span>
      </div>`).join("");
    box.querySelectorAll(".legend-item").forEach(el => {
      el.addEventListener("click", () => {
        const gk = el.getAttribute("data-group");
        setActiveGroup(activeGroup === gk ? null : gk);
      });
    });
  }

  // ── Progress Grid ─────────────────────────────────────────────────────────
  function buildGrid(highlight) {
    const grid = document.getElementById("progGrid");
    grid.innerHTML = cols.map(c => {
      const s   = stats[c] || { pct: 0, count: 0 };
      const pct = s.pct;
      const tier = pct >= 80 ? "h" : pct >= 50 ? "m" : "l";
      const inGroup = !highlight ||
        (highlight === "high" && pct >= 80) ||
        (highlight === "med"  && pct >= 50 && pct < 80) ||
        (highlight === "low"  && pct < 50);
      const hlClass = !highlight ? "" :
        inGroup ? `highlight-${highlight === "high" ? "high" : highlight === "med" ? "med" : "low"}` : "dimmed";
      return `<div class="prog-card ${hlClass}" title="${colRu(c)}: ${s.count}/${s.total} судов">
        <div class="prog-col-name">${colRu(c)}</div>
        <div class="prog-pct ${tier}">${pct}%</div>
        <div class="prog-bar-bg">
          <div class="prog-bar-fill ${tier}" style="width:0%" data-w="${pct}%"></div>
        </div>
      </div>`;
    }).join("");

    // Animate bars on next frame
    requestAnimationFrame(() => {
      grid.querySelectorAll(".prog-bar-fill[data-w]").forEach(el => {
        el.style.width = el.getAttribute("data-w");
      });
    });
  }

  // ── Interactive: set active group ─────────────────────────────────────────
  function setActiveGroup(gk) {
    activeGroup = gk;
    // Update donut opacity
    document.querySelectorAll("#donutSvg path[data-group]").forEach(el => {
      el.style.fillOpacity = (!gk || el.getAttribute("data-group") === gk) ? "0.95" : "0.25";
      el.style.filter = el.getAttribute("data-group") === gk
        ? `drop-shadow(0 0 6px ${groups.find(g=>g.key===gk)?.color || "#fff"})`
        : "none";
    });
    buildLegend();
    buildGrid(gk);
  }

  // Boot analytics
  buildDonut();
  buildLegend();
  buildGrid(null);
})();

// ── Slider / preset wiring ────────────────────────────────────────────────────
const slider  = document.getElementById("fillRateSlider");
const valBadge = document.getElementById("fillRateVal");
const preset  = document.getElementById("fillPreset");

function syncSliderCSS(val) {
  slider.style.setProperty("--fill-pct", val + "%");
}

function onSliderChange() {
  fillThreshold  = getSliderValue();
  fillPresetMode = "normal";
  // Sync preset dropdown
  if      (fillThreshold === 100) preset.value = "100";
  else if (fillThreshold === 80)  preset.value = "80";
  else if (fillThreshold === 0)   preset.value = "0";
  else                            preset.value = "0"; // no matching preset
  valBadge.textContent = fillThreshold === 0 ? "≥ 0%" : `≥ ${fillThreshold}%`;
  syncSliderCSS(fillThreshold);
  renderList();
  renderDossier();
}

function onPresetChange() {
  const val = preset.value;
  if (val === "50x") {
    fillPresetMode = "under50";
    fillThreshold  = 0;
    slider.value   = "0";
    valBadge.textContent = "< 50%";
    syncSliderCSS(0);
  } else {
    fillPresetMode = "normal";
    fillThreshold  = parseInt(val, 10) || 0;
    slider.value   = String(fillThreshold);
    valBadge.textContent = fillThreshold === 0 ? "≥ 0%" : `≥ ${fillThreshold}%`;
    syncSliderCSS(fillThreshold);
  }
  renderList();
  renderDossier();
}

slider.addEventListener("input",  onSliderChange);
slider.addEventListener("change", onSliderChange);
preset.addEventListener("change", onPresetChange);

["q","risk","sort"].forEach(id => {
  document.getElementById(id).addEventListener("input",  () => { renderList(); renderDossier(); });
  document.getElementById(id).addEventListener("change", () => { renderList(); renderDossier(); });
});

// ── DWT Analytics Overlay — dynamic aggregation from live fleet DB ────────────
(function initDwtAnalytics() {
  // Source of truth: payload vessels from ORACLE-1001 fleet_database
  const vesselsData = DATA.vessels;

  /** Absolute fleet DWT — recomputed from every vessel's dwt_tons (no hardcoded totals). */
  const TOTAL_FLEET_DWT = vesselsData.reduce(
    (sum, v) => sum + (parseFloat(v.dwt_tons) || 0),
    0
  );

  const overlay   = document.getElementById("dwtOverlay");
  const btnOpen   = document.getElementById("btnAnalytics");
  const btnClose  = document.getElementById("dwtClose");
  const searchEl  = document.getElementById("dwtSearch");
  const listEl    = document.getElementById("dwtVesselList");
  const chipsEl   = document.getElementById("dwtChips");
  const svgEl     = document.getElementById("dwtDonutSvg");
  const centerVal = document.getElementById("dwtCenterVal");
  const centerSub = document.getElementById("dwtCenterSub");
  const legendEl  = document.getElementById("dwtLegend");
  const infoEl    = document.getElementById("dwtInfo");
  const presetsEl = document.getElementById("dwtPresets");
  const modeEl    = document.getElementById("dwtMode");
  const heroSub   = document.getElementById("dwtHeroSub");

  const PALETTE = [
    "#00f2fe", "#10b981", "#f59e0b", "#3b82f6", "#a78bfa",
    "#ef4444", "#ec4899", "#14b8a6", "#84cc16", "#f97316",
  ];
  const REST_COLOR = "rgba(106,127,160,.45)";

  let selectedImos = new Set();   // IMO strings currently checked
  let chartMode = "vsfleet";      // "vsfleet" | "ingroup"
  let listFilter = "";

  function dwtOf(v) {
    const n = parseFloat(v && v.dwt_tons);
    return Number.isFinite(n) && n > 0 ? n : 0;
  }

  /** Space-separated absolute tons: 48 817 т · 81 300 198 т */
  function fmtTons(n) {
    const x = Math.round(Number(n) || 0);
    return x.toLocaleString("ru-RU") + " т";
  }

  function fmtPct2(n) {
    if (!Number.isFinite(n)) return "—";
    return n.toFixed(2) + "%";
  }

  function selectedVessels() {
    return vesselsData.filter(v => selectedImos.has(String(v.imo)));
  }

  function selectedDwtSum(vessels) {
    return vessels.reduce((sum, v) => sum + dwtOf(v), 0);
  }

  function syncHeroSub() {
    if (heroSub) {
      heroSub.textContent =
        `Живая база: ${vesselsData.length.toLocaleString("ru-RU")} судов · ` +
        `Общий дедвейт флота = ${fmtTons(TOTAL_FLEET_DWT)}`;
    }
  }

  function openOverlay() {
    syncHeroSub();
    applyTop100CrossFilter();
    overlay.hidden = false;
    overlay.classList.add("open");
    document.body.style.overflow = "hidden";
    if (selectedImos.size === 0 && selectedImo) {
      selectedImos.add(String(selectedImo));
    }
    renderPicker();
    renderChart();
  }

  /** Подхват среза Д10 (ТОП-100) → мульти-селект + фокус досье / LV. */
  function applyTop100CrossFilter() {
    try {
      const raw = localStorage.getItem("oracle1001_cross_filter");
      if (!raw) return;
      const p = JSON.parse(raw);
      if (!p || !p.sector || !Array.isArray(p.imos) || !p.imos.length) return;
      const valid = p.imos.map(String).filter(imo => vesselsData.some(v => String(v.imo) === imo));
      if (!valid.length) return;
      selectedImos = new Set(valid);
      // Гевиест в срезе → фокус списка / LV-контекста
      const ranked = vesselsData
        .filter(v => selectedImos.has(String(v.imo)))
        .sort((a, b) => dwtOf(b) - dwtOf(a));
      if (ranked[0]) {
        selectedImo = String(ranked[0].imo);
        try { renderList(); renderDossier(); } catch (_e) {}
      }
      chartMode = selectedImos.size > 1 ? "ingroup" : "vsfleet";
      syncModeButtons();
    } catch (_e) {}
  }

  function closeOverlay() {
    overlay.classList.remove("open");
    document.body.style.overflow = "";
    setTimeout(() => { overlay.hidden = true; }, 280);
  }

  function toggleImo(imo) {
    const k = String(imo);
    if (selectedImos.has(k)) selectedImos.delete(k);
    else selectedImos.add(k);
    // Manual checkbox change clears preset active state
    presetsEl.querySelectorAll(".dwt-preset").forEach(b => b.classList.remove("on"));
    renderPicker();
    renderChart(true);
  }

  const GREY_FLAGS = [
    "comoros", "комор", "gabon", "габон", "palau", "палау",
    "cameroon", "камерун", "eswatini", "свазиленд", "cook", "кука",
    "belize", "белиз", "mali", "мали",
  ];

  function isGreyFlag(flag) {
    const f = String(flag || "").toLowerCase();
    return GREY_FLAGS.some(k => f.includes(k));
  }

  function isHighRisk(v) {
    const r = String(v.compliance_risk_level || "").toUpperCase();
    return r === "EXTREME" || r === "HIGH"
      || r.includes("ЭКСТРЕМАЛЬ") || r.includes("ВЫСОК");
  }

  function isLngType(v) {
    const t = String(v.vessel_type || "").toLowerCase();
    return t.includes("lng") || t.includes("газовоз") || t.includes("flng");
  }

  function isOldVessel(v) {
    const age = parseFloat(v.age_years);
    if (Number.isFinite(age) && age > 20) return true;
    const by = parseFloat(v.built_year);
    if (Number.isFinite(by) && by < 2006) return true;
    return false;
  }

  function isIdle(v) {
    const n = String(v.nav_status || "").toLowerCase();
    return n.includes("at anchor") || n.includes("на якоре")
      || n.includes("moored") || n.includes("ошвартов")
      || n.includes("not in service") || n.includes("out of service")
      || n.includes("выведен") || n.includes("простой");
  }

  /** Power BI-style animated number counter (300ms). */
  function animateTons(el, toValue, duration) {
    if (!el) return;
    const dur = duration || 300;
    const from = parseFloat(el.dataset.raw || "0") || 0;
    const to = Number(toValue) || 0;
    el.dataset.raw = String(to);
    if (from === to) {
      el.textContent = fmtTons(to);
      return;
    }
    const t0 = performance.now();
    function frame(now) {
      const p = Math.min(1, (now - t0) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      const cur = from + (to - from) * eased;
      el.textContent = fmtTons(cur);
      if (p < 1) requestAnimationFrame(frame);
      else el.textContent = fmtTons(to);
    }
    requestAnimationFrame(frame);
  }

  function animateCount(el, toValue, duration) {
    if (!el) return;
    const dur = duration || 300;
    const from = parseFloat(el.dataset.raw || "0") || 0;
    const to = Number(toValue) || 0;
    el.dataset.raw = String(to);
    const t0 = performance.now();
    function frame(now) {
      const p = Math.min(1, (now - t0) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = String(Math.round(from + (to - from) * eased) || "—");
      if (p < 1) requestAnimationFrame(frame);
      else el.textContent = to ? String(to) : "—";
    }
    requestAnimationFrame(frame);
  }

  function pulseCards() {
    infoEl.querySelectorAll(".dwt-info-card").forEach(c => {
      c.classList.remove("pulse");
      void c.offsetWidth;
      c.classList.add("pulse");
      setTimeout(() => c.classList.remove("pulse"), 320);
    });
  }

  function setCrossHighlight(imo) {
    const key = imo ? String(imo) : null;
    listEl.querySelectorAll(".dwt-vrow").forEach(row => {
      const rim = row.getAttribute("data-imo");
      row.classList.toggle("hl",  !!key && rim === key);
      row.classList.toggle("dim", !!key && rim !== key);
    });
    svgEl.querySelectorAll("path[data-imo]").forEach(p => {
      const pim = p.getAttribute("data-imo");
      if (!key) {
        p.classList.remove("hl", "dim");
      } else if (pim === key) {
        p.classList.add("hl"); p.classList.remove("dim");
      } else {
        p.classList.add("dim"); p.classList.remove("hl");
      }
    });
    legendEl.querySelectorAll(".dwt-leg-row").forEach(row => {
      const rim = row.getAttribute("data-imo");
      row.classList.toggle("hl",  !!key && rim === key);
      row.classList.toggle("dim", !!key && rim !== key && rim !== "");
    });
  }

  function clearCrossHighlight() { setCrossHighlight(null); }

  function applyPreset(name) {
    selectedImos.clear();
    const chartWrap = overlay.querySelector(".dwt-chart-wrap");
    if (chartWrap) chartWrap.classList.add("crossfade");

    if (name === "top5") {
      vesselsData.slice().sort((a, b) => dwtOf(b) - dwtOf(a)).slice(0, 5)
        .forEach(v => selectedImos.add(String(v.imo)));
      chartMode = "ingroup";
    } else if (name === "top10lng") {
      vesselsData.filter(isLngType)
        .sort((a, b) => dwtOf(b) - dwtOf(a)).slice(0, 10)
        .forEach(v => selectedImos.add(String(v.imo)));
      chartMode = "ingroup";
    } else if (name === "comoros") {
      vesselsData.filter(v => {
        const f = String(v.flag || "").toLowerCase();
        return f.includes("комор") || f.includes("comor");
      }).forEach(v => selectedImos.add(String(v.imo)));
      chartMode = "vsfleet";
    } else if (name === "shadow") {
      vesselsData.filter(v => isGreyFlag(v.flag) || isHighRisk(v))
        .forEach(v => selectedImos.add(String(v.imo)));
      chartMode = "vsfleet";
    } else if (name === "extreme") {
      vesselsData.filter(isHighRisk)
        .forEach(v => selectedImos.add(String(v.imo)));
      chartMode = "vsfleet";
    } else if (name === "age20") {
      vesselsData.filter(isOldVessel)
        .forEach(v => selectedImos.add(String(v.imo)));
      chartMode = "vsfleet";
    } else if (name === "flng") {
      vesselsData.filter(v => dwtOf(v) > 250000)
        .forEach(v => selectedImos.add(String(v.imo)));
      chartMode = selectedImos.size > 1 ? "ingroup" : "vsfleet";
    } else if (name === "idle") {
      vesselsData.filter(isIdle)
        .forEach(v => selectedImos.add(String(v.imo)));
      chartMode = "vsfleet";
    } else if (name === "vsfleet") {
      if (selectedImo) selectedImos.add(String(selectedImo));
      chartMode = "vsfleet";
    }
    // "clear" → empty selection, default state

    syncModeButtons();
    presetsEl.querySelectorAll(".dwt-preset").forEach(b => {
      b.classList.toggle("on", b.dataset.preset === name && name !== "clear");
    });
    renderPicker();
    setTimeout(() => {
      renderChart(true);
      if (chartWrap) chartWrap.classList.remove("crossfade");
      pulseCards();
    }, 160);
  }

  function syncModeButtons() {
    modeEl.querySelectorAll("button").forEach(b => {
      b.classList.toggle("on", b.dataset.mode === chartMode);
    });
  }

  function renderPicker() {
    const q = listFilter.trim().toLowerCase();
    let rows = vesselsData.slice();
    if (q) {
      rows = rows.filter(v => {
        const blob = [v.vessel_name, v.imo, v.flag, v.vessel_type]
          .map(x => String(x || "").toLowerCase()).join(" ");
        return blob.includes(q);
      });
    }
    rows.sort((a, b) => {
      const as = selectedImos.has(String(a.imo)) ? 0 : 1;
      const bs = selectedImos.has(String(b.imo)) ? 0 : 1;
      if (as !== bs) return as - bs;
      return dwtOf(b) - dwtOf(a);
    });
    const show = rows.slice(0, 150);
    listEl.innerHTML = show.map(v => {
      const imo = String(v.imo);
      const on  = selectedImos.has(imo);
      return `<label class="dwt-vrow ${on ? "sel" : ""}" data-imo="${imo}">
        <input type="checkbox" data-imo="${imo}" ${on ? "checked" : ""} />
        <span class="vn">${fmt(v.vessel_name)} · <span style="color:var(--muted)">${imo}</span></span>
        <span class="vd">${fmtTons(dwtOf(v))}</span>
      </label>`;
    }).join("") || `<div class="empty" style="padding:16px">Нет совпадений</div>`;

    listEl.querySelectorAll("input[type=checkbox]").forEach(inp => {
      inp.addEventListener("change", () => toggleImo(inp.dataset.imo));
    });
    listEl.querySelectorAll(".dwt-vrow").forEach(row => {
      row.addEventListener("mouseenter", () => setCrossHighlight(row.getAttribute("data-imo")));
      row.addEventListener("mouseleave", clearCrossHighlight);
    });

    const picked = selectedVessels();
    const maxChips = 24;
    const shown = picked.slice(0, maxChips);
    chipsEl.innerHTML = shown.map(v => `
      <span class="dwt-chip" data-imo="${v.imo}">[${fmt(v.vessel_name)}
        <button type="button" data-rm="${v.imo}" title="убрать">×</button>]
      </span>`).join("")
      + (picked.length > maxChips
        ? `<span class="dwt-chip" style="opacity:.7">+${picked.length - maxChips} ещё</span>`
        : "");
    chipsEl.querySelectorAll("button[data-rm]").forEach(b => {
      b.addEventListener("click", () => toggleImo(b.dataset.rm));
    });
  }

  function polar(cx, cy, r, angleDeg) {
    const a = (angleDeg - 90) * Math.PI / 180;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  }

  function arcPath(cx, cy, rOuter, rInner, start, end) {
    if (end - start >= 359.99) {
      return [
        `M ${cx} ${cy - rOuter}`,
        `A ${rOuter} ${rOuter} 0 1 1 ${cx - 0.01} ${cy - rOuter}`,
        `L ${cx - 0.01} ${cy - rInner}`,
        `A ${rInner} ${rInner} 0 1 0 ${cx} ${cy - rInner}`,
        "Z",
      ].join(" ");
    }
    const large = end - start > 180 ? 1 : 0;
    const [x1, y1] = polar(cx, cy, rOuter, start);
    const [x2, y2] = polar(cx, cy, rOuter, end);
    const [x3, y3] = polar(cx, cy, rInner, end);
    const [x4, y4] = polar(cx, cy, rInner, start);
    return [
      `M ${x1} ${y1}`,
      `A ${rOuter} ${rOuter} 0 ${large} 1 ${x2} ${y2}`,
      `L ${x3} ${y3}`,
      `A ${rInner} ${rInner} 0 ${large} 0 ${x4} ${y4}`,
      "Z",
    ].join(" ");
  }

  function renderChart(animate) {
    const doAnim = animate !== false;
    const picked = selectedVessels();
    const selectedDwt = selectedDwtSum(picked);
    const sharePercent = TOTAL_FLEET_DWT > 0
      ? ((selectedDwt / TOTAL_FLEET_DWT) * 100).toFixed(2)
      : "0.00";
    const restDwt = Math.max(0, TOTAL_FLEET_DWT - selectedDwt);

    let segments = [];
    let centerTarget = selectedDwt;

    if (picked.length === 0) {
      segments = [{
        label: "Весь флот", dwt: TOTAL_FLEET_DWT, color: REST_COLOR, imo: "",
      }];
      centerTarget = TOTAL_FLEET_DWT;
      centerSub.textContent = `${vesselsData.length} судов · 100.00%`;
    } else if (chartMode === "ingroup") {
      const sorted = picked.slice().sort((a, b) => dwtOf(b) - dwtOf(a));
      // Cap visual segments at 12 + "other" for readability
      const top = sorted.slice(0, 12);
      const other = sorted.slice(12);
      top.forEach((v, i) => {
        segments.push({
          label: fmt(v.vessel_name), dwt: dwtOf(v),
          color: PALETTE[i % PALETTE.length], imo: String(v.imo),
        });
      });
      if (other.length) {
        segments.push({
          label: `+${other.length} судов`, dwt: other.reduce((s, v) => s + dwtOf(v), 0),
          color: REST_COLOR, imo: "",
        });
      }
      centerSub.textContent = `${picked.length} судов · ${sharePercent}% флота`;
    } else {
      const selLabel = picked.length === 1
        ? fmt(picked[0].vessel_name)
        : `Выборка (${picked.length})`;
      segments = [
        {
          label: selLabel, dwt: selectedDwt, color: PALETTE[0],
          imo: picked.length === 1 ? String(picked[0].imo) : "",
        },
        { label: "Остальной флот", dwt: restDwt, color: REST_COLOR, imo: "" },
      ];
      centerSub.textContent = `${sharePercent}% от ${fmtTons(TOTAL_FLEET_DWT)}`;
    }

    if (doAnim) animateTons(centerVal, centerTarget, 300);
    else {
      centerVal.dataset.raw = String(centerTarget);
      centerVal.textContent = fmtTons(centerTarget);
    }

    const totalSeg = segments.reduce((s, x) => s + x.dwt, 0) || 1;
    let angle = 0;
    const paths = [];
    segments.forEach(seg => {
      const sweep = (seg.dwt / totalSeg) * 360;
      const end = angle + Math.max(sweep, seg.dwt > 0 ? 0.01 : 0);
      if (seg.dwt <= 0 && segments.length > 1) { angle = end; return; }
      const pctOfSeg = ((seg.dwt / totalSeg) * 100).toFixed(2);
      const imoAttr = seg.imo ? ` data-imo="${seg.imo}"` : ` data-imo=""`;
      paths.push(`<path d="${arcPath(100, 100, 88, 58, angle, end)}"${imoAttr}
        fill="${seg.color}" stroke="rgba(3,7,15,.6)" stroke-width="1"
        style="filter:drop-shadow(0 0 6px ${seg.color})">
        <title>${seg.label}: ${fmtTons(seg.dwt)} (${pctOfSeg}%)</title>
      </path>`);
      angle = end;
    });
    svgEl.innerHTML = paths.join("");

    svgEl.querySelectorAll("path[data-imo]").forEach(p => {
      const im = p.getAttribute("data-imo");
      if (!im) return;
      p.addEventListener("mouseenter", () => setCrossHighlight(im));
      p.addEventListener("mouseleave", clearCrossHighlight);
    });

    legendEl.innerHTML = segments.map(seg => {
      const pctOfSeg = ((seg.dwt / totalSeg) * 100).toFixed(2);
      const pctFleet = TOTAL_FLEET_DWT > 0
        ? ((seg.dwt / TOTAL_FLEET_DWT) * 100).toFixed(2) : "0.00";
      return `<div class="dwt-leg-row" data-imo="${seg.imo || ""}">
        <span class="dwt-leg-dot" style="background:${seg.color};color:${seg.color}"></span>
        <span>${seg.label}${seg.imo ? ` · ${seg.imo}` : ""}</span>
        <span style="font-family:var(--font-mono);font-size:10px;color:var(--muted)">
          ${fmtTons(seg.dwt)} · ${pctOfSeg}%${chartMode === "vsfleet" ? ` · ${pctFleet}% флота` : ""}
        </span>
      </div>`;
    }).join("");

    legendEl.querySelectorAll(".dwt-leg-row[data-imo]").forEach(row => {
      const im = row.getAttribute("data-imo");
      if (!im) return;
      row.addEventListener("mouseenter", () => setCrossHighlight(im));
      row.addEventListener("mouseleave", clearCrossHighlight);
    });

    const avgFleet = TOTAL_FLEET_DWT / Math.max(vesselsData.length, 1);
    let avgClass = avgFleet;
    let classPeers = vesselsData.length;
    if (picked.length === 1) {
      const vt = String(picked[0].vessel_type || "").toUpperCase();
      const peers = vesselsData.filter(v => {
        const t = String(v.vessel_type || "").toUpperCase();
        if (vt.includes("LNG") && t.includes("LNG")) return true;
        if ((vt.includes("LPG") || vt.includes("ГАЗО")) && (t.includes("LPG") || t.includes("ГАЗО"))) return true;
        return t === vt && vt.length > 2;
      });
      if (peers.length) {
        avgClass = peers.reduce((s, v) => s + dwtOf(v), 0) / peers.length;
        classPeers = peers.length;
      }
    }

    const primary = picked[0];
    const primaryDwt = primary ? dwtOf(primary) : 0;
    const delta = primary ? primaryDwt - avgClass : 0;
    const deltaStr = primary
      ? (delta >= 0 ? `+${fmtTons(delta)} выше ср.` : `${fmtTons(Math.abs(delta))} ниже ср.`)
      : "—";

    infoEl.innerHTML = `
      <div class="dwt-info-card">
        <div class="ik">Выбрано судов</div>
        <div class="iv" id="kpiCount" data-raw="0">0</div>
        <div class="is" id="kpiCountSub">${primary ? `${fmt(primary.vessel_name)} · IMO ${primary.imo}` : "нет выбора"}</div>
      </div>
      <div class="dwt-info-card">
        <div class="ik">Дедвейт выборки</div>
        <div class="iv" id="kpiSelDwt" style="color:#00f2fe" data-raw="0">0 т</div>
        <div class="is">${sharePercent}% от общего дедвейта флота</div>
      </div>
      <div class="dwt-info-card">
        <div class="ik">Средний дедвейт флота</div>
        <div class="iv" id="kpiAvgFleet" data-raw="0">0 т</div>
        <div class="is">${vesselsData.length.toLocaleString("ru-RU")} судов · всего ${fmtTons(TOTAL_FLEET_DWT)}</div>
      </div>
      <div class="dwt-info-card">
        <div class="ik">Ср. дедвейт класса vs судно</div>
        <div class="iv" id="kpiClass" data-raw="0">0 т</div>
        <div class="is">среднее по классу ${fmtTons(avgClass)} (n=${classPeers}) · ${deltaStr}</div>
      </div>`;

    const kpiCount = document.getElementById("kpiCount");
    const kpiSel   = document.getElementById("kpiSelDwt");
    const kpiAvg   = document.getElementById("kpiAvgFleet");
    const kpiCls   = document.getElementById("kpiClass");
    if (doAnim) {
      animateCount(kpiCount, picked.length, 300);
      animateTons(kpiSel, selectedDwt, 300);
      animateTons(kpiAvg, avgFleet, 300);
      animateTons(kpiCls, primary ? primaryDwt : avgClass, 300);
    } else {
      if (kpiCount) { kpiCount.dataset.raw = String(picked.length); kpiCount.textContent = picked.length || "—"; }
      if (kpiSel)   { kpiSel.dataset.raw = String(selectedDwt); kpiSel.textContent = fmtTons(selectedDwt); }
      if (kpiAvg)   { kpiAvg.dataset.raw = String(avgFleet); kpiAvg.textContent = fmtTons(avgFleet); }
      if (kpiCls)   {
        const cv = primary ? primaryDwt : avgClass;
        kpiCls.dataset.raw = String(cv); kpiCls.textContent = fmtTons(cv);
      }
    }
  }

  btnOpen.addEventListener("click", openOverlay);
  btnClose.addEventListener("click", closeOverlay);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) closeOverlay(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay.classList.contains("open")) closeOverlay();
  });
  searchEl.addEventListener("input", () => {
    listFilter = searchEl.value;
    renderPicker();
  });
  presetsEl.querySelectorAll(".dwt-preset").forEach(b => {
    b.addEventListener("click", () => applyPreset(b.dataset.preset));
  });
  modeEl.querySelectorAll("button").forEach(b => {
    b.addEventListener("click", () => {
      chartMode = b.dataset.mode;
      syncModeButtons();
      renderChart(true);
      pulseCards();
    });
  });

  window.__DWT_ANALYTICS__ = {
    get TOTAL_FLEET_DWT() { return TOTAL_FLEET_DWT; },
    vesselsCount: vesselsData.length,
    presetCount: presetsEl.querySelectorAll(".dwt-preset").length,
  };
})();

// ── Boot ──────────────────────────────────────────────────────────────────────
syncSliderCSS(0);
(function bootCrossFilterFromTop100(){
  try {
    const raw = localStorage.getItem("oracle1001_cross_filter");
    if (!raw) return;
    const p = JSON.parse(raw);
    if (!p || !p.sector || !Array.isArray(p.imos) || !p.imos.length) return;
    const hit = DATA.vessels.find(v => p.imos.map(String).includes(String(v.imo)));
    if (hit) selectedImo = String(hit.imo);
  } catch (_e) {}
})();
renderMetrics();
renderList();
renderDossier();
</script>
<script type="importmap">
{
  "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"
  }
}
</script>
<script>window.__LV3D_FLAGSHIP__ = %%LV3D_FLAGSHIP%%;</script>
<script type="module" src="js/vessel_3d_viewer.js"></script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
