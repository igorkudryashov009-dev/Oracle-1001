"""Acceptance test: G. PARAGON → 20/20 via pipeline.robust_parser."""

from __future__ import annotations

from pipeline.robust_parser import TZ_KEYS_UPPER, fill_rate, parse_osint_narrative

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


def test_paragon_100_percent_uppercase():
    rec = parse_osint_narrative(G_PARAGON_RAW, declared_imo="9656888", as_snake=False)
    assert list(rec.keys()) == TZ_KEYS_UPPER
    assert fill_rate(rec, uppercase=True) == 100.0
    assert rec["VESSEL_NAME"] == "G. PARAGON"
    assert rec["MMSI"] == "356117000"
    assert rec["CALL_SIGN"] == "3FTI4"
    assert rec["DWT_TONS"] == 54776.0
    assert rec["GT"] == 46786.0
    assert rec["DRAFT_M"] == 11.2
    assert rec["SPEED_KNOTS"] == 13.3
    assert "Пенья-Бланка" in rec["DEPARTURE_PORT"]
    assert "Йосу" in rec["DESTINATION_PORT"]
    assert rec["COMPLIANCE_RISK_LEVEL"] == "LOW"
    assert rec["AGE_YEARS"] == 13
