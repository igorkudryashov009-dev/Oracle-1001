"""Tests for VesselProfile20Cols + extract_vessel (G. PARAGON = 100%)."""

from __future__ import annotations

from extract_vessel_llm import extract_vessel, tz_fill_rate
from schemas.vessel_schema import TZ_COLUMNS, VesselProfile20Cols, parse_numeric, strip_markdown

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
"""


def test_strip_markdown_and_numeric():
    assert "**" not in strip_markdown("**G. PARAGON**")
    assert parse_numeric("54 776 тонн") == 54776.0
    assert parse_numeric("11.2 м") == 11.2


def test_g_paragon_pydantic_100_percent():
    rec = extract_vessel(G_PARAGON_RAW, declared_imo="9656888", use_llm=False)
    assert set(rec.keys()) >= set(TZ_COLUMNS)
    assert tz_fill_rate(rec) == 100.0
    profile = VesselProfile20Cols.model_validate(rec)
    assert profile.vessel_name == "G. PARAGON"
    assert profile.mmsi == "356117000"
    assert profile.call_sign == "3FTI4"
    assert profile.dwt_tons == 54776.0
    assert profile.gt == 46786.0
    assert profile.draft_m == 11.2
    assert profile.age_years == 13
    assert "Пенья-Бланка" in profile.departure_port
    assert "Йосу" in profile.destination_port
    assert "Йосу" not in profile.departure_port
    assert profile.compliance_risk_level == "LOW"
    assert "01:00" in profile.arrival_datetime
