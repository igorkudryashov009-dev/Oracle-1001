"""
TOP-500 Analytics — ORACLE-1001
================================
Generates ``output/top500_analytics.html`` — 15 unique SVG/Canvas dashboards
(D13–D27) for the 200 heaviest vessels by DWT, with CrossFilterManager,
NASA / Wet-Glass UI, and localStorage sync to osint_layers.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
OUT_HTML = OUTPUT / "top500_analytics.html"

_EMPTY = {"", "—", "-", "none", "не извлечено", "nan", "n/a"}


def _f(v: Any) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _s(v: Any) -> str:
    if v is None:
        return ""
    t = str(v).strip()
    return "" if t.lower() in _EMPTY else t


def _risk_bucket(level: Any) -> str:
    u = _s(level).upper()
    if not u:
        return "UNK"
    if "EXTREME" in u or "ЭКСТРЕМАЛЬ" in u or "КРИТИЧ" in u or "GHOST" in u:
        return "EXTREME"
    if "HIGH" in u or "ВЫСОК" in u:
        return "HIGH"
    if "MID" in u or "MEDIUM" in u or "СРЕДН" in u:
        return "MID"
    if "LOW" in u or "НИЗК" in u or "ЧИСТ" in u:
        return "LOW"
    return "UNK"


def _port_region(port: str) -> str:
    p = port.lower()
    if any(k in p for k in (
        "china", "китай", "ningbo", "shanghai", "tianjin", "zap", "korea", "коре",
        "japan", "япон", "incheon", "yeosu", "singapore", "сингапур",
    )):
        return "Азиатско-Тихоокеанский"
    if any(k in p for k in (
        "rotterdam", "zeebrugge", "barcelona", "grain", "fos", "piraeus",
        "uk", "nether", "spain", "belgium", "france", "greece",
    )):
        return "Европа"
    if any(k in p for k in ("houston", "sabine", "corpus", "freeport", "marcus", "usa", "сша", "cove")):
        return "Америка"
    if any(k in p for k in (
        "ras laffan", "qatar", "катар", "jubail", "yanbu", "saudi", "uae",
        "fujairah", "suez", "egypt", "ain sukhna",
    )):
        return "Ближний Восток"
    if any(k in p for k in ("sabetta", "yamal", "russia", "росси", "novorossiysk", "primorsk")):
        return "Арктика / Россия"
    return "Прочее"


def _vtype_group(vt: str) -> str:
    u = vt.upper()
    if "LNG" in u or "FLNG" in u:
        return "LNG"
    if "LPG" in u or "VLGC" in u or "ГАЗО" in u:
        return "LPG"
    if "VLCC" in u or "ULCC" in u or "CRUDE" in u or "НЕФТ" in u:
        return "CRUDE"
    if "PRODUCT" in u or "PROD" in u or "ХИМ" in u:
        return "PRODUCT"
    if "TANKER" in u or "ТАНКЕР" in u:
        return "TANKER"
    return "OTHER"


def _flag_short(flag: str) -> str:
    f = flag.strip()
    if not f:
        return "—"
    low = f.lower()
    aliases = [
        (("panama", "панам"), "Панама"),
        (("liberia", "либер"), "Либерия"),
        (("marshall", "маршал"), "Маршалловы О-ва"),
        (("bahamas", "багам"), "Багамы"),
        (("bermuda", "бермуд"), "Бермуды"),
        (("china", "китай", "hong kong", "гонконг"), "Китай"),
        (("singapore", "сингапур"), "Сингапур"),
        (("malta", "мальт"), "Мальта"),
        (("cyprus", "кипр"), "Кипр"),
        (("greece", "грец"), "Греция"),
        (("norway", "норвег"), "Норвегия"),
        (("russia", "росси"), "Россия"),
        (("japan", "япон"), "Япония"),
        (("korea", "коре"), "Корея"),
        (("uk", "britain", "великобрит"), "Великобритания"),
    ]
    for keys, name in aliases:
        if any(k in low for k in keys):
            return name
    return f[:22]


# D13 operational zones (stable codes → RU labels)
OPS_ZONE_RU = {
    "ME_QATAR": "Ближний Восток / Катар",
    "ATLANTIC_US": "Атлантика и Мексиканский залив",
    "ARCTIC": "Арктика / СМП",
    "APAC": "Азиатско-Тихоокеанский регион",
    "LATAM_AFRICA": "Латам / Африка",
    "OIL_OTHER": "Прочие",
}

# D13 tech types (stable codes → RU labels)
TECH_TYPE_RU = {
    "QFLEX_QMAX": "Q-Flex / Q-Max (Мега-СПГ)",
    "MEMBRANE": "Membrane Conventional (160-174k м³)",
    "MOSS": "Moss Rosenberg (Сферические танкеры)",
    "FLNG_FSRU": "FLNG / FSRU (Плавучие заводы/терминалы)",
    "VLCC_SUEZ": "VLCC / Suezmax / Aframax (Сверхтяжелая нефть)",
}


def _tech_type(vessel_type: str, dwt: float, raw: str = "") -> str:
    """Classify LNG / tanker technology for D13 middle ring."""
    blob = f"{vessel_type} {raw}".upper()
    if any(k in blob for k in ("FLNG", "FSRU", "FLOATING LIQUEFIED", "ПЛАВУЧ", "РЕГАЗИФ")):
        return "FLNG_FSRU"
    if any(k in blob for k in ("Q-MAX", "QMAX", "Q MAX")):
        return "QFLEX_QMAX"
    if any(k in blob for k in ("Q-FLEX", "QFLEX", "Q FLEX", "КУ-ФЛЕКС", "Q-ФЛЕКС")):
        return "QFLEX_QMAX"
    if any(k in blob for k in ("MOSS", "SPHERICAL", "СФЕР")):
        return "MOSS"
    if any(k in blob for k in ("MEMBRANE", "МЕМБРАН", "GTT", "MARK III", "NO96")):
        return "MEMBRANE"
    if any(k in blob for k in ("VLCC", "ULCC", "SUEZMAX", "CRUDE", "НЕФТ", "ТАНКЕР НЕФТ")):
        return "VLCC_SUEZ"
    # LNG without explicit tech → Membrane conventional by tonnage band
    if "LNG" in blob or "ГАЗОВ" in blob or "СПГ" in blob:
        if dwt >= 100000:
            return "QFLEX_QMAX"
        if dwt >= 55000:
            return "MEMBRANE"
        return "MOSS"
    return "VLCC_SUEZ"


def _ops_zone(
    destination: str,
    departure: str,
    context: str,
    vessel_type: str,
    tech: str,
) -> str:
    """Macro operational zone for D13 inner ring."""
    blob = f"{destination} {departure} {context}".lower()
    vt = vessel_type.upper()

    if any(k in blob for k in (
        "sabetta", "yamal", "arctic", "арктик", "смп", "nsr", "murmansk", "мурманск",
        "primorsk", "приморск", "novorossiysk", "новоросс",
    )):
        return "ARCTIC"
    if any(k in blob for k in (
        "ras laffan", "qatar", "катар", "doha", "mesaieed", "jubail", "yanbu",
        "fujairah", "ruwais", "das island", "ain sukhna", "suez", "egypt", "уаэ", "uae", "kuwait",
    )):
        return "ME_QATAR"
    if any(k in blob for k in (
        "houston", "sabine", "corpus", "freeport", "cameron", "cove point", "calcasieu",
        "louisiana", "texas", "mexico", "мексикан", "atlantic", "атлантик", "usa", "сша",
        "bahamas", "trinidad",
    )):
        return "ATLANTIC_US"
    if any(k in blob for k in (
        "china", "китай", "shanghai", "ningbo", "tianjin", "zhoushan", "korea", "коре",
        "japan", "япон", "incheon", "yeosu", "ulsan", "singapore", "сингапур",
        "taiwan", "тайван", "india", "инди", "pakistan", "australia", "browse",
    )):
        return "APAC"
    if any(k in blob for k in (
        "brazil", "бразил", "santos", "argentina", "buenos", "chile", "peru",
        "nigeria", "лагос", "lagos", "angola", "luanda", "south africa", "cape town",
        "ghana", "tema", "latam", "africa", "африка", "west africa",
    )):
        return "LATAM_AFRICA"
    # Crude / oil routes without clear LNG hub
    if tech == "VLCC_SUEZ" or any(k in vt for k in ("CRUDE", "VLCC", "НЕФТ", "TANKER", "AFRAMAX")):
        return "OIL_OTHER"
    return "OIL_OTHER"


def _est_capacity_m3(tech: str, dwt: float, gt: float) -> int:
    """Heuristic cargo capacity (м³) for tooltip OSINT attributes."""
    defaults = {
        "QFLEX_QMAX": 217000 if dwt < 120000 else 266000,
        "MEMBRANE": 170000,
        "MOSS": 135000,
        "FLNG_FSRU": 180000,
        "VLCC_SUEZ": 0,
    }
    if tech == "VLCC_SUEZ":
        return 0
    base = defaults.get(tech, 160000)
    if gt > 50000 and tech.startswith("Q"):
        return int(base)
    if dwt > 0 and tech in ("MEMBRANE", "MOSS"):
        # soft scale around class defaults
        return int(max(90000, min(180000, dwt * 2.1)))
    return int(base)


def _nav_bucket(status: str, raw: str = "") -> str:
    u = (status + " " + raw).upper()
    if any(k in u for k in ("DRYDOCK", "DRY DOCK", "РЕМОНТ", "SHIPYARD", "ДОК")):
        return "DRYDOCK"
    if any(k in u for k in ("ANCHOR", "MOOR", "ЯКОР", "ШВАРТ", "BERTH")):
        return "ANCHOR"
    if any(k in u for k in ("UNDER WAY", "SAILING", "EN ROUTE", "В ХОДУ", "НА ХОДУ", "PASSAGE")):
        return "SEA"
    if any(k in u for k in ("ETA", "WAITING", "ОЖИДАН")):
        return "ETA"
    if status:
        return "SEA"
    return "ETA"


def _age_bucket(age: float) -> str:
    if age <= 5:
        return "0-5"
    if age <= 10:
        return "6-10"
    if age <= 15:
        return "11-15"
    if age <= 20:
        return "16-20"
    return "20+"


def _fuel_tpd(dwt: float, speed: float, age: float) -> float:
    """Heuristic fuel burn (т/сут) — OSINT model, not sensor telemetry."""
    sp = speed if speed > 0.5 else 12.0
    return round(0.000018 * max(dwt, 0) + 0.55 * (sp ** 1.35) + 0.12 * max(age, 0), 2)


def _tz_fill_pct(r: dict) -> float:
    cols = [
        "vessel_name", "imo", "mmsi", "call_sign", "vessel_type", "built_year",
        "age_years", "flag", "dwt_tons", "gt", "loa_m", "beam_m", "draft_m",
        "nav_status", "speed_knots", "destination_port", "destination_context",
        "departure_port", "arrival_datetime", "compliance_risk_level",
    ]
    n = sum(1 for c in cols if _s(r.get(c)) or _f(r.get(c)) > 0)
    return round(100.0 * n / len(cols), 1)


def build_top500_payload(df: pd.DataFrame) -> dict:
    """Select TOP-500 by DWT and build lean records + fleet / provenance totals."""
    records = df.to_dict(orient="records")
    try:
        from pipeline.top500_provenance_fix import (
            fix_top500_provenance,
            summarize_top500_provenance,
        )
        records = fix_top500_provenance(records, top_n=500)
        prov = summarize_top500_provenance(records, top_n=500)
        osint_pct = float(prov.get("pct", {}).get("OSINT", 0.0) or 0.0)
        prov["pct"]["SYNTH"] = round(max(0.0, 100.0 - osint_pct), 2)
    except Exception:  # noqa: BLE001
        prov = {
            "cells": {"OSINT": 0, "SYNTH": 0, "KNN": 0, "AIS": 0},
            "pct": {"OSINT": 0, "SYNTH": 0, "KNN": 0, "AIS": 0},
            "total_cells": 0,
            "top_n": 500,
        }

    fleet_dwt = sum(_f(r.get("dwt_tons")) for r in records)
    fleet_n = len(records)
    ranked = sorted(records, key=lambda r: _f(r.get("dwt_tons")), reverse=True)
    top = ranked[:500]
    rest = ranked[500:]

    vessels: list[dict] = []
    for i, r in enumerate(top, 1):
        dep = _s(r.get("departure_port"))
        dst = _s(r.get("destination_port"))
        ctx = _s(r.get("destination_context"))
        vtype_raw = _s(r.get("vessel_type"))
        dwt = round(_f(r.get("dwt_tons")), 1)
        speed = round(_f(r.get("speed_knots")), 2)
        age = round(_f(r.get("age_years")), 1)
        gt = round(_f(r.get("gt")), 1)
        tech = _tech_type(vtype_raw, dwt, _s(r.get("raw_text")))
        zone = _ops_zone(dst, dep, ctx, vtype_raw, tech)
        vessels.append({
            "rank": i,
            "imo": _s(r.get("imo")),
            "mmsi": _s(r.get("mmsi")),
            "vessel_name": _s(r.get("vessel_name")) or f"IMO {_s(r.get('imo'))}",
            "dwt_tons": dwt,
            "gt": gt,
            "loa_m": round(_f(r.get("loa_m")), 2),
            "beam_m": round(_f(r.get("beam_m")), 2),
            "draft_m": round(_f(r.get("draft_m")), 2),
            "speed_knots": speed,
            "age_years": age,
            "age_bucket": _age_bucket(age),
            "built_year": int(_f(r.get("built_year"))) if _f(r.get("built_year")) else None,
            "flag": _s(r.get("flag")),
            "flag_short": _flag_short(_s(r.get("flag"))),
            "vessel_type": vtype_raw,
            "vtype": _vtype_group(vtype_raw),
            "tech_type": tech,
            "tech_type_ru": TECH_TYPE_RU.get(tech, tech),
            "ops_zone": zone,
            "ops_zone_ru": OPS_ZONE_RU.get(zone, zone),
            "capacity_m3": _est_capacity_m3(tech, dwt, gt),
            "nav_status": _s(r.get("nav_status")),
            "nav_bucket": _nav_bucket(_s(r.get("nav_status")), _s(r.get("raw_text"))),
            "risk": _risk_bucket(r.get("compliance_risk_level")),
            "departure_port": dep,
            "destination_port": dst,
            "destination_context": ctx[:120],
            "arrival_datetime": _s(r.get("arrival_datetime")),
            "region": _port_region(dst or dep),
            "fuel_tpd": _fuel_tpd(dwt, speed, age),
            "fill_pct": _tz_fill_pct(r),
            "in_top500": True,
            "sanctions_tags": [
                t.strip() for t in _s(r.get("sanctions_tags")).split(";") if t.strip()
            ],
            "synthetic_fields": _s(r.get("synthetic_fields")),
            "imputed_fields": _s(r.get("imputed_fields")),
            "registry_mock_fields": _s(r.get("registry_mock_fields")),
        })

    rest_sample = []
    for r in rest[:300]:
        rest_sample.append({
            "imo": _s(r.get("imo")),
            "dwt_tons": round(_f(r.get("dwt_tons")), 1),
            "gt": round(_f(r.get("gt")), 1),
            "vtype": _vtype_group(_s(r.get("vessel_type"))),
            "in_top500": False,
        })

    top_dwt = sum(v["dwt_tons"] for v in vessels)
    avg_fill = round(sum(v["fill_pct"] for v in vessels) / max(len(vessels), 1), 1)
    return {
        "fleet_total_dwt": round(fleet_dwt, 0),
        "fleet_count": fleet_n,
        "top500_dwt": round(top_dwt, 0),
        "top500_share_pct": round(100.0 * top_dwt / fleet_dwt, 2) if fleet_dwt else 0,
        "avg_fill_pct": avg_fill,
        "provenance": prov,
        "vessels": vessels,
        "rest_sample": rest_sample,
        "copyright": "© ORACLE-1001 · ТОП-500 Analytics · NASA / Wet-Glass",
    }


_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ORACLE-1001 · ТОП-500 Analytics · Premium</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=JetBrains+Mono:wght@400;600&family=Manrope:wght@400;600;700&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="js/top500_premium.css"/>
</head>
<body>
<nav class="nav">
  <a class="brand" href="osint_layers.html">ORACLE-1001 · ТОП-500</a>
  <div class="nav-links">
    <a class="pill" href="mission_control.html">Центр управления</a>
    <a class="pill" href="dashboard.html">Реестр флота</a>
    <a class="pill" href="osint_layers.html">9 аналитических слоёв</a>
    <a class="pill" href="top100_analytics.html">Анализ ТОП-100</a>
    <a class="pill" href="top200_analytics.html">Анализ ТОП-200</a>
    <a class="pill active" href="top500_analytics.html">Анализ ТОП-500</a>
  </div>
</nav>

<div class="hero">
  <h1>ТОП-500 ПО ДЕДВЕЙТУ · 15 ИНФОГРАФИКОВ</h1>
  <p>Все оси — глобальный % DWT от флота 81.3 млн т · Flame Glow · NASA / Wet-Glass Premium</p>
</div>

<div class="tabs" id="tabs">
  <button class="tab on" data-tab="t1">① Тоннаж и классы · Д13–Д17</button>
  <button class="tab" data-tab="t2">② Логистика и комплаенс · Д18–Д22</button>
  <button class="tab" data-tab="t3">③ Идентичность и горизонты · Д23–Д27</button>
</div>

<div class="presets" id="presets">
  <button type="button" class="preset" data-preset="shadow">Теневой флот / серые флаги</button>
  <button type="button" class="preset" data-preset="age20">Возраст &gt; 20 лет</button>
  <button type="button" class="preset" data-preset="lng">СПГ-газовозы</button>
  <button type="button" class="preset" data-preset="extreme">Риск Extreme / High</button>
  <button type="button" class="preset" data-preset="vlcc">VLCC / Crude</button>
</div>

<div class="kpi-row" id="kpiRow"></div>
<div class="filter-bar" id="filterBar"></div>

<section class="panel on" id="t1">
  <div class="grid">
    <div class="card wide d13-block" id="c13">
      <div class="d13-head">
        <div>
          <h3>Д13 · Флагманский блок · Нона-Donut / Sunburst</h3>
          <div class="sub">Д13.1–Д13.9 Комплексный корреляционный анализ (19 колонок) · ГЛОБАЛЬНЫЙ % DWT (от 81.3 М т) · сквозной кросс-ховер</div>
        </div>
        <div class="d13-modes" id="d13Modes">
          <button type="button" class="d13-mode on" data-mode="trio">Триада 1: Базовая (Д13.1–13.3)</button>
          <button type="button" class="d13-mode" data-mode="trio2">Триада 2: Корреляционная (Д13.4–13.6)</button>
          <button type="button" class="d13-mode" data-mode="trio3">Триада 3: Гидродинамика (Д13.7–13.9)</button>
          <button type="button" class="d13-mode" data-mode="all">Все 9 Donut (Д13.1–13.9)</button>
          <button type="button" class="d13-mode" data-mode="sunburst">Иерархический Sunburst</button>
        </div>
      </div>
      <div class="d13-trio" id="d13Trio">
        <div class="d13-pane">
          <h4>Д13.1 · Операционные Зоны и Хабы</h4>
          <div class="sub">Ближний Восток · Атлантика/США · Арктика · АТР · Латам/Африка · Прочие</div>
          <div class="chart" id="c13a"></div>
        </div>
        <div class="d13-pane">
          <h4>Д13.2 · Технологические Типы</h4>
          <div class="sub">Q-Flex/Q-Max · Membrane · Moss · FLNG/FSRU · VLCC/Suezmax/Aframax</div>
          <div class="chart" id="c13b"></div>
        </div>
        <div class="d13-pane">
          <h4>Д13.3 · Флаги Юрисдикций</h4>
          <div class="sub">Маршалловы О-ва · Панама · Либерия · Багамы · Бермуды · Китай/Прочие</div>
          <div class="chart" id="c13c"></div>
        </div>
      </div>
      <div class="d13-trio d13-trio2" id="d13Trio2" hidden>
        <div class="d13-pane">
          <h4>Д13.4 · Распределение по Профилю Риска и Санкционному Статусу</h4>
          <div class="sub">Extreme / Blacklist · High Risk / Grey · Medium / Shadow · Clean Compliance</div>
          <div class="chart" id="c13d"></div>
        </div>
        <div class="d13-pane">
          <h4>Д13.5 · Возрастной Профиль и Эко-Класс (EEDI/CII)</h4>
          <div class="sub">&lt; 5 лет (Newbuild) · 5–10 · 10–15 · 15–20 · &gt; 20 лет (Dark Fleet Target)</div>
          <div class="chart" id="c13e"></div>
        </div>
        <div class="d13-pane">
          <h4>Д13.6 · Операционный Статус, Осадка и Топливная Система</h4>
          <div class="sub">Laden (В грузу / Max Draft) · Ballast (В балласте) · Moored / STS · Repair / Верфь</div>
          <div class="chart" id="c13f"></div>
        </div>
      </div>
      <div class="d13-trio d13-trio3" id="d13Trio3" hidden>
        <div class="d13-pane">
          <h4>Д13.7 · Коэффициент Соотношения DWT / GT (Эффективность Тоннажа)</h4>
          <div class="sub">Ultra High (&gt; 1.8) · High (1.4–1.8) · Standard LNG/Gas (0.9–1.4) · Volume-Driven (&lt; 0.9)</div>
          <div class="chart" id="c13g"></div>
        </div>
        <div class="d13-pane">
          <h4>Д13.8 · Скоростные Профили и Деградация (Speed Knots)</h4>
          <div class="sub">Eco (&lt; 11 уз) · Standard Transit (11–14 уз) · High Speed (14–16 уз) · Express (&gt; 16 уз)</div>
          <div class="chart" id="c13h"></div>
        </div>
        <div class="d13-pane">
          <h4>Д13.9 · Уровень Загрузки и Гидродинамическая Осадка (Draft % vs Max)</h4>
          <div class="sub">Full Laden (&gt; 90% Max) · Partial Load (60–90%) · Light Ballast (30–60%) · Min Draft (&lt; 30%)</div>
          <div class="chart" id="c13i"></div>
        </div>
      </div>
      <div class="d13-sunburst chart" id="d13Sun" hidden></div>
    </div>
    <div class="card"><h3>Д14 · Scatter GT / Global % DWT</h3><div class="sub">ТОП-500 vs остальной флот · ось X = глобальный %</div><div class="chart" id="c14"></div></div>
    <div class="card"><h3>Д15 · Возрастной горизонт</h3><div class="sub">Высота столбцов = ГЛОБАЛЬНЫЙ % DWT (от 81.3 М т)</div><div class="chart" id="c15"></div></div>
    <div class="card"><h3>Д16 · ТОП-20 портов</h3><div class="sub">Длина бара = вклад порта в глобальный DWT · 12px JetBrains Mono</div><div class="chart" id="c16"></div></div>
    <div class="card"><h3>Д17 · Радар комплаенс-риска</h3><div class="sub">Радиус = ГЛОБАЛЬНЫЙ % DWT по уровню риска</div><div class="chart" id="c17"></div></div>
  </div>
</section>

<section class="panel" id="t2">
  <div class="grid">
    <div class="card"><h3>Д18 · Treemap маршрутов</h3><div class="sub">Подпись ячейки = глобальный % DWT</div><div class="chart" id="c18"></div></div>
    <div class="card"><h3>Д19 · Навигация и простой</h3><div class="sub">Ширина = ГЛОБАЛЬНЫЙ % DWT (от 81.3 М т)</div><div class="chart" id="c19"></div></div>
    <div class="card"><h3>Д20 · Аудит провенанса</h3><div class="sub">OSINT / AIS / Synth / KNN · контекст глобального среза</div><div class="chart" id="c20"></div></div>
    <div class="card"><h3>Д21 · KPI корпуса</h3><div class="sub">Глобальный вклад + возраст / GT/DWT / LOA</div><div class="chart" id="c21"></div></div>
    <div class="card"><h3>Д22 · Скорость и эффективность</h3><div class="sub">Секторы взвешены по DWT → глобальный %</div><div class="chart" id="c22"></div></div>
  </div>
</section>

<section class="panel" id="t3">
  <div class="grid">
    <div class="card"><h3>Д23 · Heatmap IMO / MMSI</h3><div class="sub">Интенсивность ≈ глобальный вклад судна</div><div class="chart" id="c23"></div></div>
    <div class="card"><h3>Д24 · Gauge комплаенс-риска</h3><div class="sub">DWT-взвешенный индекс · вклад среза в флот</div><div class="chart" id="c24"></div></div>
    <div class="card"><h3>Д25 · Матрица флагов × типов</h3><div class="sub">Ячейка = ГЛОБАЛЬНЫЙ % DWT (от 81.3 М т)</div><div class="chart" id="c25"></div></div>
    <div class="card"><h3>Д26 · DWT по году постройки</h3><div class="sub">Ось Y = ГЛОБАЛЬНЫЙ % DWT (от 81.3 М т)</div><div class="chart" id="c26"></div></div>
    <div class="card wide"><h3>Д27 · Sunburst контекста маршрута</h3><div class="sub">Тип → Регион → Порт · подписи = глобальный %</div><div class="chart" id="c27"></div></div>
  </div>
</section>

<button type="button" class="fab-reset" id="fabReset">Сбросить все фильтры</button>
<div class="tip" id="tip"></div>
<footer class="foot" id="footCopy"></footer>

<script>window.__TOP500_PAYLOAD__ = __PAYLOAD__;</script>
<script src="js/top500_engine.js"></script>
<script src="js/top_validation_suite.js"></script>
</body>
</html>
"""


def write_top500_analytics(df: pd.DataFrame, path: Path | None = None) -> dict:
    """Build TOP-500 payload, copy premium JS/CSS, write analytics HTML."""
    import shutil

    path = path or OUT_HTML
    path.parent.mkdir(parents=True, exist_ok=True)
    js_dir = path.parent / "js"
    js_dir.mkdir(parents=True, exist_ok=True)
    web = ROOT / "web"
    for name in ("top500_engine.js", "top500_premium.css", "top_validation_suite.js"):
        src = web / name
        if src.exists():
            shutil.copy2(src, js_dir / name)

    payload = build_top500_payload(df)
    html = _HTML.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    path.write_text(html, encoding="utf-8")
    stamp = path.with_suffix(path.suffix + ".meta.json")
    stamp.write_text(
        json.dumps(
            {
                "module": "top500_analytics",
                "copyright": payload.get("copyright"),
                "top500_dwt": payload.get("top500_dwt"),
                "top500_share_pct": payload.get("top500_share_pct"),
                "provenance_pct": payload.get("provenance", {}).get("pct"),
                "vessels": len(payload.get("vessels", [])),
                "assets": ["js/top500_engine.js", "js/top500_premium.css"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return payload


def main() -> int:
    fleet = OUTPUT / "fleet_database.csv"
    if not fleet.exists():
        print(f"ERROR: {fleet} missing — run run_all.py first")
        return 1
    df = pd.read_csv(fleet, low_memory=False)
    payload = write_top500_analytics(df, OUT_HTML)
    print(
        f"Wrote {OUT_HTML} · vessels={len(payload['vessels'])} · "
        f"DWT={payload['top500_dwt']:,.0f} · share={payload['top500_share_pct']:.2f}% · "
        f"OSINT={payload['provenance']['pct'].get('OSINT', 0):.2f}% · "
        f"SYNTH={payload['provenance']['pct'].get('SYNTH', 0):.2f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
