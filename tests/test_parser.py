"""Unit tests for parser_v2 — G. PARAGON эталон, 20/20 ТЗ columns."""

from __future__ import annotations

from parser_v2 import TZ_COLUMNS, parse_vessel_narrative, pre_clean_markdown

# Exact narrative from IMO_Filtered_Clean.xlsx (IMO 9656888)
G_PARAGON_RAW = """Наименование: **G. PARAGON**

IMO номер: **9656888**

MMSI: **356117000**

Позывной: **3FTI4**

Тип судна: **Газовоз / Танкер для сжиженного газа (LPG Tanker / VLGC)**

Год постройки: **2013**

Флаг: **Панама**

Порт назначения: **Йосу, Южная Корея (Yeosu, Korea)**, расчетное время прибытия (ETA): **13 августа 2026 г., 01:00 UTC**.

---

### Технические характеристики

* **Дедвейт (DWT):** 54 776 тонн
* **Валовая вместимость (GT):** 46 786
* **Размеры:** Длина (LOA) — 225.00 м, ширина (Beam) — 36.60 м
* **Текущая осадка:** **11.2 м**
* **Навигационные параметры:** Курс — 232.1°, скорость — **13.3 узла** (финальный этап транстихоокеанского перехода из региона Панамского канала / Америки в Восточную Азию).
* **Последний порт / Локация:** Рейд Пенья-Бланка, Панама (*Pena Blanca Anch., Panama* — отход 17 июля 2026 г.).

---

### Операционный профиль и комплаенс

* **Классификация / Управление:** Судно имеет класс ведущего международного общества (ABS — American Bureau of Shipping) и задействовано в трансконтинентальных поставках энергоносителей (LPG) по маршрутам между американскими терминалами и рынками Южной Кореи / Восточной Азии.
* **Санкционный статус:** **НИЗКИЙ РИСК / ЧИСТЫЙ ПРОФИЛЬ (LOW / CLEARED)**.
* **Факторы риска:** Стандартный крупнотоннажный газовоз под панамским флагом с чистой историей портового контроля (PSC) и отсутствием санкционных ограничений.
"""


def test_pre_clean_removes_markdown_keeps_structure():
    cleaned = pre_clean_markdown(G_PARAGON_RAW)
    assert "**" not in cleaned
    assert "*" not in cleaned
    assert "###" not in cleaned
    assert "G. PARAGON" in cleaned
    assert "356117000" in cleaned
    assert "3FTI4" in cleaned
    assert "54 776" in cleaned or "54776" in cleaned.replace(" ", "")


def test_g_paragon_all_20_tz_columns_filled():
    rec = parse_vessel_narrative(G_PARAGON_RAW, declared_imo="9656888", reference_year=2026)
    assert list(rec.keys()) == TZ_COLUMNS
    missing = [k for k in TZ_COLUMNS if rec[k] is None or rec[k] == ""]
    assert missing == [], f"empty columns: {missing}"


def test_g_paragon_exact_mapping():
    rec = parse_vessel_narrative(G_PARAGON_RAW, declared_imo="9656888", reference_year=2026)

    assert rec["vessel_name"] == "G. PARAGON"
    assert rec["imo"] == "9656888"
    assert rec["mmsi"] == "356117000"
    assert rec["call_sign"] == "3FTI4"
    assert rec["vessel_type"] == "Газовоз / Танкер для сжиженного газа (LPG Tanker / VLGC)"
    assert rec["built_year"] == 2013
    assert rec["age_years"] == 13
    assert rec["flag"] == "Панама"
    assert rec["dwt_tons"] == 54776
    assert rec["gt"] == 46786
    assert rec["loa_m"] == 225.0
    assert rec["beam_m"] == 36.6
    assert rec["draft_m"] == 11.2
    assert "транстихоокеанского перехода" in rec["nav_status"]
    assert rec["speed_knots"] == 13.3
    assert rec["destination_port"] == "Йосу, Южная Корея (Yeosu, Korea)"
    assert "энергоносителей" in rec["destination_context"]
    assert "американскими терминалами" in rec["destination_context"]
    assert "Пенья-Бланка" in rec["departure_port"]
    assert "Pena Blanca" in rec["departure_port"]
    assert "Йосу" not in rec["departure_port"]  # not swapped with destination
    assert "13 августа 2026" in rec["arrival_datetime"]
    assert "01:00" in rec["arrival_datetime"]
    assert "UTC" in rec["arrival_datetime"]
    assert rec["compliance_risk_level"] == "LOW"


def test_no_markdown_residue_in_values():
    rec = parse_vessel_narrative(G_PARAGON_RAW, declared_imo="9656888", reference_year=2026)
    for k, v in rec.items():
        s = str(v)
        assert "**" not in s, k
        assert s.strip("*") == s or k == "vessel_type", k
