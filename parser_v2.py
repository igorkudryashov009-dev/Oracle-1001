"""
parser_v2 — markdown-aware OSINT narrative → 20 ТЗ columns.

Honest extraction only: never invents MMSI/call_sign/risk when absent from source.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

# Canonical 20 ТЗ columns (snake_case keys)
TZ_COLUMNS: list[str] = [
    "vessel_name",
    "imo",
    "mmsi",
    "call_sign",
    "vessel_type",
    "built_year",
    "age_years",
    "flag",
    "dwt_tons",
    "gt",
    "loa_m",
    "beam_m",
    "draft_m",
    "nav_status",
    "speed_knots",
    "destination_port",
    "destination_context",
    "departure_port",
    "arrival_datetime",
    "compliance_risk_level",
]

REFERENCE_YEAR = datetime.now().year


def pre_clean_markdown(text: str) -> str:
    """Strip Markdown markers while keeping digits, brackets, commas, colons.

    Removes: **, *, #, _  (emphasis / headings / italics / bold)
    Does not remove: () [] {}, commas, periods, colons, digits, letters.
    """
    if text is None:
        return ""
    s = str(text)
    # Bold / italic markers
    s = s.replace("**", "")
    s = s.replace("__", "")
    # Headings
    s = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]*", "", s)
    # Remaining single * used as list bullets or italics — drop the marker only
    s = s.replace("*", "")
    # Underscore italics leftovers (not part of call signs: call signs rarely use _)
    # Only strip standalone underscore emphasis, keep underscores inside tokens
    s = re.sub(r"(?<![A-Za-z0-9])_(?![A-Za-z0-9])", "", s)
    s = re.sub(r"(?<=\s)_([^_\n]+)_(?=\s|$|[.,;:])", r"\1", s)
    # Collapse leftover whitespace but keep newlines as structure
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def _clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip().replace("\xa0", " ").replace("~", "").replace("≈", "")
    if not s:
        return None
    compact = s.replace(" ", "")
    # thousands: 54,776 or 54 776
    if re.fullmatch(r"\d{1,3}([ ,]\d{3})+(?:\.\d+)?", s) or re.fullmatch(
        r"\d{1,3}([ ,]\d{3})+(?:\.\d+)?", compact.replace(",", " ")
    ):
        s = compact.replace(",", "").replace(" ", "")
    elif re.fullmatch(r"\d+,\d{1,2}", compact):
        s = compact.replace(",", ".")
    else:
        s = compact.replace(",", "")
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in (".", "-", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(raw: Optional[str]) -> Optional[int]:
    f = _to_float(raw)
    if f is None:
        return None
    return int(f)


def _search(pattern: str, text: str, flags: int = re.IGNORECASE | re.MULTILINE) -> Optional[str]:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    val = m.group(1).strip()
    return val if val else None


def _normalize_risk(raw: Optional[str]) -> Optional[str]:
    """Map free-form risk text → LOW | MID | HIGH | EXTREME."""
    if not raw:
        return None
    u = raw.upper()
    if any(k in u for k in ("ЭКСТРЕМАЛЬ", "EXTREME", "GHOST", "ПРИЗРАК")):
        return "EXTREME"
    if any(k in u for k in ("КРИТИЧЕСКИ", "CRITICAL")):
        return "EXTREME"
    if any(k in u for k in ("ВЫСОК", "HIGH", "СТРАТЕГ")):
        return "HIGH"
    if any(k in u for k in ("СРЕДН", "MEDIUM", "MID")):
        return "MID"
    if any(k in u for k in ("НИЗК", "LOW", "CLEAR", "ЧИСТ", "LEGIT", "ЛЕГИТИМ")):
        return "LOW"
    return None


def parse_vessel_narrative(
    raw_text: str,
    declared_imo: Optional[str] = None,
    *,
    reference_year: int = REFERENCE_YEAR,
) -> dict[str, Any]:
    """Parse OSINT narrative into exactly 20 ТЗ columns.

    Returns dict with keys in TZ_COLUMNS. Missing values are None (never fabricated).
    """
    cleaned = pre_clean_markdown(raw_text or "")
    text = cleaned
    out: dict[str, Any] = {k: None for k in TZ_COLUMNS}

    # --- Identity ---
    name = _search(
        r"(?:Наименование\s*судна|Наименование|Название\s*судна|Название)\s*[:：]\s*(.+)",
        text,
    )
    if name:
        name = re.split(
            r"\s+(?:IMO|MMSI|Позывной|Тип|Год|Флаг)\b",
            name,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        # Glued NAMEIMO:
        name = re.sub(r"(?i)IMO\s*(?:номер\s*)?[:：]?\s*\d{7}.*$", "", name)
        out["vessel_name"] = _clean_spaces(name).rstrip(".")

    imo = _search(r"IMO\s*(?:номер\s*)?[:：]\s*(\d{7})", text)
    if not imo:
        imo = _search(r"IMO\s*/\s*MMSI\s*[:：]\s*(\d{7})", text)
    if declared_imo is not None:
        digits = re.sub(r"\D", "", str(declared_imo))
        if len(digits) == 7:
            imo = digits
    out["imo"] = imo

    mmsi = _search(r"IMO\s*/\s*MMSI\s*[:：]\s*\d{7}\s*/\s*(\d{7,9})", text)
    if not mmsi:
        mmsi = _search(r"MMSI\s*[:：]\s*(\d{5,12})", text)
    out["mmsi"] = mmsi

    call = _search(
        r"(?:Позывной(?:\s*\([^)]*\))?|Call\s*sign|Callsign)\s*[:：]\s*([A-Za-z0-9/\-]{1,15})",
        text,
    )
    if call:
        out["call_sign"] = call.strip().rstrip(".")

    vtype = _search(
        r"(?:Тип\s*судна|Т\s*ип\s*судна|Тип\s*объекта|Vessel\s*type|Type|Тип)\s*[:：]\s*"
        r"(.+?)(?=\s*Год\s*постройки|\s*Флаг|\s*MMSI|\s*Позывной|\s*Порт|\s*Основные|"
        r"\s*Размер|\s*Габарит|\s*Дедвейт|\n\n|$)",
        text,
    )
    if vtype:
        out["vessel_type"] = _clean_spaces(vtype)

    year = _to_int(
        _search(r"(?:Год\s*постройки|Built|Year\s*built)\s*[:：]\s*[~≈]?\s*(\d{4})", text)
    )
    out["built_year"] = year
    if year is not None and 1900 <= year <= reference_year:
        out["age_years"] = reference_year - year
    else:
        age = _to_int(
            _search(
                r"Год\s*постройки\s*[:：]\s*\d{4}\s*\(\s*(?:возраст\s*[:：]?\s*)?(\d+)\s*"
                r"(?:лет|года|год)",
                text,
            )
        )
        out["age_years"] = age

    flag = _search(
        r"(?:Флаг|Flag)\s*[:：]\s*(.+?)(?=\s*Порт\s*назначения|\s*Основные|\s*Размер|"
        r"\s*Габарит|\s*Дедвейт|\s*Год|\s*Техническ|\s*2\.|\n\n|$)",
        text,
    )
    if flag:
        flag = _clean_spaces(flag)
        # Drop long OSINT lectures in parentheses for landlocked-flag essays
        if len(flag) > 60 and "(" in flag:
            flag = _clean_spaces(flag.split("(", 1)[0])
        out["flag"] = flag.rstrip(".")

    # --- Tonnage / dimensions ---
    dwt = _to_float(
        _search(
            r"(?:Дедвейт|DWT)\s*(?:\([^)]*\))?\s*[:：—\-–]?\s*[~≈]?\s*"
            r"([\d][\d\xa0 ]*(?:[.,]\d+)?)(?![.\d])",
            text,
        )
    )
    out["dwt_tons"] = int(dwt) if dwt is not None and dwt == int(dwt) else dwt

    gt = _to_float(
        _search(
            r"(?:Валовая\s*вместимость|Gross\s*Tonnage|\bGT\b)\s*(?:\([^)]*\))?\s*[:：—\-–]?\s*"
            r"[~≈]?\s*([\d][\d\xa0 ]*(?:[.,]\d+)?)(?![.\d])",
            text,
        )
    )
    out["gt"] = int(gt) if gt is not None and gt == int(gt) else gt

    loa = _to_float(
        _search(
            r"(?:Длина\s*(?:общая\s*)?\(?LOA\)?|LOA|Length\s*(?:overall)?)\s*[:：—\-–]?\s*"
            r"([\d]+(?:[.,]\d+)?)",
            text,
        )
    )
    beam = _to_float(
        _search(
            r"(?:ширина|beam|width)(?:\s*\([^)]*\))?\s*[:：—\-–]?\s*([\d]+(?:[.,]\d+)?)",
            text,
        )
    )
    if loa is None or beam is None:
        dims = re.search(
            r"(?:Размер(?:ения|ы)?|Dimensions?|(?:Основные\s+)?[Гг]абариты)\s*[:：]?\s*"
            r"(?:Длина\s*(?:общая\s*)?(?:\(?LOA\)?)?\s*[—\-–]?\s*)?"
            r"([\d\xa0.,]+)\s*м?(?:\s*\([^)]*\))?\s*"
            r"(?:[×xXх\*]|,?\s*ширина(?:\s*\([^)]*\))?\s*[—\-–]?\s*)\s*([\d\xa0.,]+)",
            text,
            re.IGNORECASE,
        )
        if dims:
            if loa is None:
                loa = _to_float(dims.group(1))
            if beam is None:
                beam = _to_float(dims.group(2))
        if loa is None or beam is None:
            dims2 = re.search(
                r"([\d]+(?:[.,]\d+)?)\s*м\s*\(\s*длина\s*\)\s*[×xXх\*]\s*"
                r"([\d]+(?:[.,]\d+)?)\s*м",
                text,
                re.IGNORECASE,
            )
            if dims2:
                if loa is None:
                    loa = _to_float(dims2.group(1))
                if beam is None:
                    beam = _to_float(dims2.group(2))
    out["loa_m"] = loa
    out["beam_m"] = beam

    draft = _to_float(
        _search(
            r"(?:Текущая\s*осадка|осадка|Draft|Draught)\s*[:：—\-–]?\s*(?:—\s*)?"
            r"([\d]+(?:[.,]\d+)?)",
            text,
        )
    )
    out["draft_m"] = draft

    # --- Nav / speed ---
    # Prefer parenthetical route description after speed (G. PARAGON style)
    nav_paren = _search(
        r"скорость\s*[:：—\-–]?\s*[~≈]?\s*[\d.,]+\s*узл[а-я]*\s*\(([^)]+)\)",
        text,
    )
    nav_labeled = None
    nav_m = re.search(
        r"Навигационный статус\s*[:：]\s*([^\n]+)",
        text,
        re.IGNORECASE,
    )
    if nav_m:
        nav_labeled = _clean_spaces(nav_m.group(1)).rstrip(".")
        if re.search(r"риск|комплаенс", nav_labeled, re.IGNORECASE):
            nav_labeled = None
    out["nav_status"] = _clean_spaces(nav_paren) if nav_paren else nav_labeled

    speed = _to_float(
        _search(
            r"(?:Скорость|Speed)\s*[:：—\-–]?\s*(?:—\s*)?([\d]+(?:[.,]\d+)?)\s*(?:узл|knots?|kn)?",
            text,
        )
    )
    out["speed_knots"] = speed

    # --- Logistics (strict separation destination vs departure) ---
    # Destination port — labeled first
    dest = _search(
        r"(?:Заявленный\s*пункт\s*назначения|Пункт\s*назначения|Порт\s*назначения|"
        r"Reported\s*destination|Destination)\s*[:：]\s*(.+?)(?=,\s*расчетное|"
        r",\s*расчётное|\.\s*расчетное|расчетное\s*время|ETA|\n|$)",
        text,
    )
    if not dest:
        dest = _search(
            r"(?:следует|направляется|идёт|идет)\s+в\s+порт\s+([^.(,\n]{2,80})",
            text,
        )
    if not dest:
        dest_m = re.search(
            r"направля(?:ется|ясь)\s+в\s+([^.(,\n]{2,60})(?:,\s*([^.(,\n]{2,40}))?",
            text,
            re.IGNORECASE,
        )
        if dest_m:
            dest = _clean_spaces(dest_m.group(1))
            if dest_m.group(2):
                dest = f"{dest}, {_clean_spaces(dest_m.group(2))}"
    if dest:
        # Keep parenthetical English alias inside destination_port
        dest = _clean_spaces(dest).rstrip(".,;")
        out["destination_port"] = dest

    # Arrival / ETA — never confuse with departure
    eta = _search(
        r"(?:расч[её]тное\s*время\s*прибытия|Ожидаемое\s*(?:время\s*)?прибытия|"
        r"Reported\s*ETA|\bETA\b)\s*(?:\([^)]*\))?\s*[:：—\-–]?\s*(?:—\s*)?"
        r"(?:Указанный\s+ETA\s*)?\(?"
        r"(\d{1,2}\s+\S+\s+\d{4}\s*г\.?(?:,\s*\d{1,2}:\d{2}(?:\s*UTC)?)?)",
        text,
    )
    if not eta:
        eta = _search(
            r"(?:расч[её]тное\s*время\s*прибытия|Ожидаемое\s*(?:время\s*)?прибытия|"
            r"Reported\s*ETA|\bETA\b)\s*(?:\([^)]*\))?\s*[:：—\-–]?\s*(?:—\s*)?"
            r"(?:Указанный\s+ETA\s*)?\(?"
            r"(\d{1,2}\s+\S+(?:\s+\d{4})?[^.\n)]*?)"
            r"\)?",
            text,
        )
    if eta:
        eta = _clean_spaces(eta).strip(" .;()")
        eta = re.split(r"\s+уже\s+прошел|\s+что\s+может", eta, maxsplit=1, flags=re.I)[0]
        out["arrival_datetime"] = _clean_spaces(eta).rstrip(".,;")

    # Departure — last port / location / пункт отправления (NOT destination)
    dep = _search(
        r"(?:Пункт\s*отправления|Последний\s*порт\s*/\s*Локация|Последний\s*порт|"
        r"Last\s*port)\s*[:：]\s*(.+?)(?=\s*—\s*отход|\s*отход\s+\d|\n\n|$)",
        text,
    )
    if dep:
        dep = _clean_spaces(dep)
        # Normalize italic-stripped English alias already in parens
        dep = re.sub(r"\s*—\s*отход.*$", "", dep, flags=re.IGNORECASE)
        dep = dep.rstrip(".,;")
        # Close truncated parenthesis left after cutting "— отход …"
        if dep.count("(") > dep.count(")"):
            dep += ")"
        out["departure_port"] = dep
    if not out["departure_port"]:
        dep_m = re.search(
            r"(?:вышло|вышел|вышла|вышли)\s+из\s+(?:района|порта|акватории)?\s*"
            r"([^.(,\n]{2,60})(?:\s*\(([^)]+)\))?(?:,\s*([^.(,\n]{2,40}))?",
            text,
            re.IGNORECASE,
        )
        if dep_m:
            port = _clean_spaces(dep_m.group(1))
            bits = [b for b in (dep_m.group(2), dep_m.group(3)) if b]
            if bits:
                port = f"{port} ({', '.join(_clean_spaces(b) for b in bits)})"
            out["departure_port"] = port

    # Destination context — route / mission narrative (NOT the port name)
    ctx = _search(
        r"(?:задействовано\s+в|осуществляет|занят[оа]?\s+в)\s+(.+?)(?=\.\s*\*|.\s*Санкцион|\n\n|\n\s*Санкцион|$)",
        text,
    )
    if not ctx:
        ctx = _search(
            r"(?:по\s+маршрутам\s+между|маршрутах\s+между)\s+(.+?)(?=\.|\n|$)",
            text,
        )
        if ctx:
            ctx = "поставки энергоносителей по маршрутам между " + ctx
    if ctx:
        ctx = _clean_spaces(ctx)
        # Prefer the energy-supply phrasing from G. PARAGON style
        m_supply = re.search(
            r"((?:трансконтинентальных\s+)?поставк[а-я]+\s+энергоносителей"
            r"[^.]{0,160})",
            ctx,
            re.IGNORECASE,
        )
        if m_supply:
            ctx = _clean_spaces(m_supply.group(1))
        # Normalize case/grammar → "поставки энергоносителей …"
        ctx = re.sub(
            r"^трансконтинентальных\s+поставках\s+",
            "поставки ",
            ctx,
            flags=re.IGNORECASE,
        )
        ctx = re.sub(r"^поставках\s+", "поставки ", ctx, flags=re.IGNORECASE)
        ctx = re.sub(r"\s*/\s*Восточной\s+Азии\s*$", "", ctx, flags=re.IGNORECASE)
        ctx = re.sub(r"\s*\(LPG\)\s*", " ", ctx)
        ctx = _clean_spaces(ctx)
        # Trim trailing class society noise
        ctx = re.split(r"\s+Судно\s+имеет\s+класс", ctx, maxsplit=1)[0]
        out["destination_context"] = ctx.rstrip(".,;")

    # If still empty — use geography / operational activity sentence
    if not out["destination_context"]:
        geo = _search(
            r"(?:География|Операционная\s*активность|Операционный\s*профиль)\s*[:：]\s*(.+?)(?=\n\n|\n\s*\d\.|$)",
            text,
        )
        if geo:
            out["destination_context"] = _clean_spaces(geo)[:220].rstrip(".,;")

    # --- Compliance (canonical enum only) ---
    risk_block = _search(
        r"(?:Санкционный\s*статус|Уровень\s*риска|Статус\s*комплаенса|"
        r"Риск(?:-|\s*)профиль|Статус)\s*[:：]\s*(.+?)(?=\n|$)",
        text,
    )
    risk = _normalize_risk(risk_block) or _normalize_risk(text)
    out["compliance_risk_level"] = risk

    return out


def parse_block_v2(raw_text: str, declared_imo: str = "") -> dict[str, Any]:
    """Pipeline adapter: 20 ТЗ columns + raw_text for dossier UI."""
    rec = parse_vessel_narrative(raw_text, declared_imo=declared_imo or None)
    rec["raw_text"] = str(raw_text) if raw_text is not None else ""
    return rec
