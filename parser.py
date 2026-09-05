"""Parse unstructured OSINT vessel text blocks into VesselRecord."""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple

from schema import SIGNIFICANT_FIELDS, VesselRecord, imo_checksum_valid


def _clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _is_empty_text(raw_text: Any) -> bool:
    if raw_text is None:
        return True
    if isinstance(raw_text, float) and raw_text != raw_text:
        return True
    text = str(raw_text).strip()
    return (not text) or text.lower() in ("nan", "none")


def _normalize_column_a(value: Any) -> Tuple[Optional[str], bool]:
    """Return (normalized A without spaces, imo_format_error).

    imo_format_error=True when A has digits but is not exactly 7 digits.
    """
    if value is None:
        return None, False
    if isinstance(value, float):
        if value != value:  # NaN
            return None, False
        value = str(int(value)) if value == int(value) else str(value)
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", ""):
        return None, False
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None, False
    if len(digits) == 7:
        return digits, False
    # Keep the digit string as-is (e.g. "112000") — format error
    return digits, True


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip().replace("\xa0", " ").replace("~", "").replace("≈", "")
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s.replace(" ", "")):
        s = s.replace(" ", "").replace(",", "")
    elif re.fullmatch(r"\d{1,3}(\s\d{3})+([.,]\d+)?", s):
        s = s.replace(" ", "").replace(",", ".")
    else:
        if re.fullmatch(r"\d+,\d{1,3}", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(" ", "")
            if s.count(",") == 1 and s.count(".") == 0:
                left, right = s.split(",")
                if len(right) <= 3 and len(left) <= 4:
                    s = f"{left}.{right}"
                else:
                    s = s.replace(",", "")
            else:
                s = s.replace(",", "")
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


def _trim_name(name: str) -> str:
    """Stop name at common next-field markers on the same line."""
    # Glued forms: "GUADALUPE EXPLORERIMO: 9926934" / "NAMEIMO номер:"
    name = re.sub(r"(?i)IMO\s*(?:номер\s*)?[:：]?\s*\d{7}.*$", "", name)
    name = re.sub(r"(?i)IMO\s*$", "", name)
    cut = re.split(
        r"\s+(?:IMO\s*(?:номер|number|/)|MMSI|Позывной|Тип\s*(?:судна|объекта)|"
        r"Т\s*ип\s*судна|Год\s*постройки|Флаг|Call\s*sign|Идентификация)\b",
        name,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return _clean_spaces(cut).rstrip(".")


def _extract_imo_from_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (imo_from_text, mmsi_from_combined_label).

    Only the FIRST explicit label among:
      - IMO / MMSI: X / Y
      - IMO номер:
      - IMO:
    Secondary mentions (Истинный IMO etc.) are excluded.
    """
    # Candidates as (start_pos, imo, mmsi_or_None)
    candidates: list[Tuple[int, str, Optional[str]]] = []

    for m in re.finditer(
        r"IMO\s*/\s*MMSI:\s*(\d{7})\s*/\s*(\d{7,9})",
        text,
        re.IGNORECASE,
    ):
        candidates.append((m.start(), m.group(1), m.group(2)))

    for m in re.finditer(r"IMO\s*номер\s*:\s*(\d{7})", text, re.IGNORECASE):
        candidates.append((m.start(), m.group(1), None))

    # Plain "IMO:" — but NOT "Истинный/Реальный/Подлинный IMO" and NOT "IMO номер" / "IMO / MMSI"
    for m in re.finditer(r"(?<![A-Za-zА-Яа-я/])IMO\s*:\s*(\d{7})", text, re.IGNORECASE):
        start = m.start()
        prefix = text[max(0, start - 20) : start].lower()
        if any(w in prefix for w in ("истинный", "реальный", "подлинный")):
            continue
        # Skip if this is the start of "IMO номер" or "IMO / MMSI" (already handled)
        after = text[start : start + 20].lower()
        if after.startswith("imo номер") or re.match(r"imo\s*/\s*mmsi", after):
            continue
        candidates.append((start, m.group(1), None))

    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    _, imo, mmsi = candidates[0]
    return imo, mmsi


def _extract_identity_spoofing(text: str) -> Tuple[Optional[str], Optional[str]]:
    m = re.search(
        r"(?:Истинный IMO|Реальный IMO|Подлинный IMO)[:\s]*(\d{7})",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None, None
    suspected = m.group(1)
    # ±100 chars context around the match
    lo = max(0, m.start() - 100)
    hi = min(len(text), m.end() + 100)
    note = _clean_spaces(text[lo:hi])
    return suspected, note


# Non-vessel markers — checked ONLY against extracted vessel_type (iter 5).
# "AtoN" is case-SENSITIVE; all others are case-insensitive.
_NON_VESSEL_PATTERNS = (
    re.compile(r"\bAid to Navigation\b", re.IGNORECASE),
    re.compile(r"(?<![A-Za-zА-Яа-я])AtoN(?![A-Za-zА-Яа-я])"),  # exact case
    # Explicit buoy forms only — NOT буя/буем (SPM mooring context)
    re.compile(r"(?<![А-Яа-яA-Za-z])бу[йи](?:ки)?(?![А-Яа-яA-Za-z])", re.IGNORECASE),
    re.compile(r"(?<![А-Яа-яA-Za-z])навигационный знак(?![А-Яа-яA-Za-z])", re.IGNORECASE),
    re.compile(r"(?<![А-Яа-яA-Za-z])маяк(?![А-Яа-яA-Za-z])", re.IGNORECASE),
)


def _vessel_type_is_non_vessel(vessel_type: Optional[str]) -> bool:
    """True if vessel_type label content itself is a non-vessel entity."""
    if not vessel_type:
        return False
    return any(p.search(vessel_type) for p in _NON_VESSEL_PATTERNS)


def _classify_vessel_category(
    *,
    vessel_type: Optional[str],
    vessel_name: Optional[str],
    mmsi: Optional[str],
    imo_from_text: Optional[str],
    loa_m: Optional[float],
    nav_status: Optional[str],
) -> str:
    """Rule 6 (iter 5): non-vessel markers only in vessel_type; else presume vessel."""
    # Step 2 — marker in extracted type field only
    if _vessel_type_is_non_vessel(vessel_type):
        return "non_vessel"
    # Step 3 — presumption of vessel (iter 3)
    if any(
        [
            bool(vessel_name),
            bool(mmsi),
            bool(imo_from_text),
            loa_m is not None,
            bool(nav_status),
        ]
    ):
        return "vessel"
    return "unknown"


def _extract_compliance_risk(text: str) -> Optional[str]:
    """Return compact risk label only — never a narrative paragraph."""

    def _normalize_hit(hit: str) -> str:
        u = hit.upper()
        if "GHOST" in u or "ПРИЗРАК" in u:
            return "ЭКСТРЕМАЛЬНЫЙ РИСК"
        if "СТРАТЕГИЧЕСКИЙ" in u or "STRATEGIC" in u or "HIGH RISK" in u:
            return "ВЫСОКИЙ РИСК"
        if "CLEARED" in u or ("ЧИСТЫЙ" in u and "РИСК" not in u[:6]):
            return "НИЗКИЙ РИСК"
        if u.startswith("НИЗКИЙ"):
            return "НИЗКИЙ РИСК"
        if u.startswith("ВЫСОКИЙ"):
            return "ВЫСОКИЙ РИСК"
        if u.startswith("СРЕДНИЙ"):
            return "СРЕДНИЙ РИСК"
        if u.startswith("ЭКСТРЕМАЛЬНЫЙ"):
            return "ЭКСТРЕМАЛЬНЫЙ РИСК"
        if u.startswith("КРИТИЧЕСКИ"):
            return "КРИТИЧЕСКИ ВЫСОКИЙ РИСК"
        return hit

    phrases = [
        "ЭКСТРЕМАЛЬНЫЙ РИСК",
        "КРИТИЧЕСКИ ВЫСОКИЙ РИСК",
        "ВЫСОКИЙ РИСК / СТРАТЕГИЧЕСКИЙ",
        "ВЫСОКИЙ РИСК",
        "HIGH RISK / STRATEGIC",
        "СРЕДНИЙ РИСК",
        "НИЗКИЙ РИСК / ЧИСТЫЙ",
        "НИЗКИЙ РИСК",
        "LOW / CLEARED",
        "ЧИСТЫЙ ПРОФИЛЬ",
        "GHOST VESSEL",
        "СУДНО-ПРИЗРАК",
    ]
    upper = text.upper()
    matches = []
    for phrase in phrases:
        pos = upper.find(phrase.upper())
        if pos >= 0:
            matches.append((pos, -len(phrase), phrase))
    if matches:
        matches.sort()
        return _normalize_hit(matches[0][2])

    labeled = _search(
        r"(?:Уровень риска|Статус комплаенса|Санкционный статус|Риск(?:-|\s*)профиль)"
        r"\s*[:：]\s*([^\n]+)",
        text,
    )
    if labeled:
        cleaned = _clean_spaces(labeled)
        for phrase in phrases:
            if phrase.upper() in cleaned.upper():
                return _normalize_hit(phrase)
        # Accept only compact known-style labels (no markdown / essays)
        if (
            len(cleaned) <= 40
            and "**" not in cleaned
            and not cleaned.upper().startswith("СУДНО ")
        ):
            u = cleaned.upper()
            if any(k in u for k in ("РИСК", "CLEAR", "LOW", "HIGH", "MEDIUM", "ЧИСТ")):
                return _normalize_hit(cleaned)
    return None


def parse_block(raw_text: str, declared_imo: str) -> VesselRecord:
    """Parse one OSINT text block into a VesselRecord."""
    imo_a, imo_format_error = _normalize_column_a(declared_imo)
    empty = _is_empty_text(raw_text)

    # --- Rule 1: empty text ---
    if empty:
        if imo_a and not imo_format_error and len(imo_a) == 7:
            return VesselRecord(
                imo=imo_a,
                imo_valid=imo_checksum_valid(imo_a),
                imo_format_error=False,
                imo_from_text=None,
                imo_source_match=None,
                vessel_category="unknown",
                source_confidence="needs_review",
                raw_text="",
            )
        return VesselRecord(
            imo="UNKNOWN",
            imo_valid=False,
            imo_format_error=bool(imo_format_error) if imo_a else False,
            imo_from_text=None,
            imo_source_match=None,
            vessel_category="unknown",
            source_confidence="needs_review",
            raw_text="",
        )

    original_raw = raw_text
    text = str(raw_text)

    # --- Rule 2: vessel name ---
    vessel_name = _search(
        r"^(?:Наименование судна|Наименование|Название судна|Название|Реальное имя судна):\s*(.+)",
        text,
    )
    if vessel_name:
        vessel_name = _trim_name(vessel_name)

    # --- Rules 3–4: IMO / MMSI from text (first explicit label only) ---
    imo_from_text, mmsi_from_combo = _extract_imo_from_text(text)

    mmsi = mmsi_from_combo or _search(r"MMSI\s*[:：]?\s*(\d{5,12})", text)

    call_sign = _search(
        r"(?:Позывной(?:\s*\([^)]*\))?|Call\s*sign|Callsign)\s*[:：]\s*([A-Za-z0-9/\-]{1,15})",
        text,
    )
    if call_sign:
        call_sign = call_sign.strip().rstrip(".")

    # More specific labels first; bare "Тип:" is an alias of "Тип судна:"
    # "Т ип судна" — OCR/glue typo seen in cleaned KB
    vessel_type = _search(
        r"(?:Тип\s*судна|Т\s*ип\s*судна|Тип\s*объекта|Vessel\s*type|Type|Тип)\s*[:：]\s*"
        r"(.+?)(?=\s*Год\s*постройки|\s*Флаг|\s*Технические|\s*Размер|\s*Габарит|\s*Порт|\n|$)",
        text,
    )
    if vessel_type:
        vessel_type = _clean_spaces(vessel_type)

    built_year = _to_int(
        _search(
            r"(?:Год\s*постройки|Built|Year\s*built|Year)\s*[:：]\s*[~≈]?\s*(\d{4})",
            text,
        )
    )
    age_years = _to_int(
        _search(
            r"(?:возраст|age)\s*[:：]?\s*(\d+)\s*(?:лет|года|год|г\.?|years?|yrs?)?",
            text,
        )
    )
    if age_years is None:
        age_years = _to_int(
            _search(
                r"Год\s*постройки\s*[:：]\s*\d{4}\s*\(\s*(?:возраст\s*[:：]?\s*)?(\d+)\s*(?:лет|года|год|г\.?)?",
                text,
            )
        )
    # Corpus OSINT blocks are dated 2026 — derive age only from explicit built_year
    if age_years is None and built_year is not None and 1900 <= built_year <= 2026:
        age_years = 2026 - built_year

    flag = _search(
        r"(?:Флаг|Flag)\s*[:：]\s*(.+?)(?=\s*Технические|\s*Дедвейт|\s*Валовая|"
        r"\s*Размер|\s*Габарит|\s*Год\s*постройки|\s*Порт\s*назначения|"
        r"\s*2\.|\s*🛰️|\s*🗺️|\s*\*|\n|$)",
        text,
    )
    if flag:
        flag = _clean_spaces(flag)
        # Truncate runaway capture when source has no newlines
        flag = re.split(
            r"\s+(?:Год\s*постройки|Габарит|Дедвейт|Размер|Порт\s*назначения|"
            r"Навигационн|Осадка|2\.)\b",
            flag,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        # Keep compact flag label; drop long parenthetical OSINT lectures
        if len(flag) > 80:
            paren = re.match(r"^([^()]{2,60})\s*\(", flag)
            if paren:
                flag = _clean_spaces(paren.group(1))
            else:
                flag = _clean_spaces(flag[:80])

    dwt_tons = _to_float(
        _search(
            r"(?:Дедвейт|DWT)\s*(?:\([^)]*\))?\s*[:：—\-–]?\s*[~≈]?\s*"
            r"([\d][\d\xa0 ]*(?:[.,]\d+)?)(?![.\d])\s*(?:тонн|т\.?|tons?|t\.?)?",
            text,
        )
    )
    gt = _to_float(
        _search(
            r"(?:Валовая\s*вместимость|Gross\s*Tonnage|\bGT\b)\s*(?:\([^)]*\))?\s*[:：—\-–]?\s*[~≈]?\s*"
            r"([\d][\d\xa0 ]*(?:[.,]\d+)?)(?![.\d])",
            text,
        )
    )

    loa_m = _to_float(
        _search(
            r"(?:Длина\s*(?:общая\s*)?\(?LOA\)?|LOA|Length\s*(?:overall)?)\s*[:：—\-–]?\s*"
            r"([\d\xa0]+(?:[.,]\d+)?)\s*м?",
            text,
        )
    )
    beam_m = _to_float(
        _search(
            r"(?:ширина|beam|width)(?:\s*\([^)]*\))?\s*[:：—\-–]?\s*([\d\xa0]+(?:[.,]\d+)?)\s*м?",
            text,
        )
    )
    if loa_m is None or beam_m is None:
        # Formats:
        #   299.00 м × 50.00 м
        #   294.90 м (длина) × 46.40 м (ширина)
        #   Основные габариты: Длина общая (LOA) — 225.97 м, ширина — 36.59 м
        dims = re.search(
            r"(?:Размер(?:ения|ы)?|Dimensions?|(?:Основные\s+)?[Гг]абариты)\s*[:：]?\s*"
            r"(?:Длина\s*(?:общая\s*)?(?:\(?LOA\)?)?\s*[—\-–]?\s*)?"
            r"([\d\xa0.,]+)\s*м?(?:\s*\([^)]*\))?\s*"
            r"(?:[×xXх\*]|,?\s*ширина\s*[—\-–]?\s*)\s*([\d\xa0.,]+)\s*м?",
            text,
            re.IGNORECASE,
        )
        if dims:
            if loa_m is None:
                loa_m = _to_float(dims.group(1))
            if beam_m is None:
                beam_m = _to_float(dims.group(2))
        if loa_m is None or beam_m is None:
            dims2 = re.search(
                r"([\d]+(?:[.,]\d+)?)\s*м\s*\(\s*длина\s*\)\s*[×xXх\*]\s*"
                r"([\d]+(?:[.,]\d+)?)\s*м\s*\(\s*ширина\s*\)",
                text,
                re.IGNORECASE,
            )
            if dims2:
                if loa_m is None:
                    loa_m = _to_float(dims2.group(1))
                if beam_m is None:
                    beam_m = _to_float(dims2.group(2))

    # Prefer explicit draught near "осадка — N м" / labeled draft
    draft_m = None
    draft_note = None
    draft_labeled = re.search(
        r"(?:Текущая\s*осадка|осадка|Draft|Draught)\s*[:：—\-–]?\s*(?:—\s*)?"
        r"([\d]+(?:[.,]\d+)?)\s*м?",
        text,
        re.IGNORECASE,
    )
    if draft_labeled:
        draft_m = _to_float(draft_labeled.group(1))
    else:
        draft_raw = _search(
            r"(?:Текущая\s*осадка|Осадка|Draft|Draught)\s*[:：]?\s*(.+?)(?=\n|\*|Источник|\d\.\s*Операцион|$)",
            text,
        )
        if draft_raw:
            draft_raw = re.split(r"(?=\d+\.\s*Операцион)", draft_raw)[0]
            num_m = re.search(r"([\d]+(?:[.,]\d+)?)", draft_raw)
            if num_m:
                draft_m = _to_float(num_m.group(1))
            note_m = re.search(r"\(([^)]*)\)", draft_raw)
            if note_m:
                draft_note = _clean_spaces(note_m.group(1))
            else:
                rest = re.sub(r"^[\d\s.,]+\s*м?\s*", "", draft_raw, count=1).strip(" —–-:")
                if rest and not rest.startswith("2."):
                    draft_note = _clean_spaces(rest)

    # --- Rule 7: nav_status ONLY from "Навигационный статус:" ---
    nav_m = re.search(
        r"Навигационный статус:\s*([^\(\n]+)(?:\(([^)]*)\))?",
        text,
        re.IGNORECASE,
    )
    nav_status = None
    if nav_m:
        main = _clean_spaces(nav_m.group(1).rstrip(" .;,"))
        paren = nav_m.group(2)
        # Never capture compliance / risk wording into nav_status
        if re.search(r"риск|комплаенс", main, re.IGNORECASE):
            nav_status = None
        else:
            if paren and not re.search(r"риск|комплаенс", paren, re.IGNORECASE):
                nav_status = f"{main} ({_clean_spaces(paren)})"
            else:
                nav_status = main

    speed_knots = _to_float(
        _search(
            r"(?:Скорость|Speed)\s*[:：—\-–]?\s*(?:—\s*)?([\d]+(?:[.,]\d+)?)\s*(?:узл|knots?|kn)?",
            text,
        )
    )

    asset_status_raw = _search(r"Статус актива:\s*([^\n]+)", text)
    asset_status = _clean_spaces(asset_status_raw) if asset_status_raw else None

    compliance_risk_level = _extract_compliance_risk(text)

    destination_port = None
    destination_context = None
    departure_port = None
    departure_datetime_utc = None
    arrival_datetime = None

    # Explicit logistics labels (cleaned KB / Oracle-1001 dossier format)
    dest_labeled = _search(
        r"(?:Заявленный\s*пункт\s*назначения|Пункт\s*назначения|Порт\s*назначения|"
        r"Reported\s*destination|Destination)\s*[:：]\s*(.+?)(?=\n|Текущий статус|"
        r"Расчетное|Ожидаемое|Прибытие|Дата|Пункт\s*отправления|2\.|3\.|4\.|$)",
        text,
    )
    if dest_labeled:
        destination_port = _clean_spaces(
            re.split(r"[.(]|согласно", dest_labeled, maxsplit=1)[0]
        )
        ctx_m = re.search(r"\(([^)]+)\)", dest_labeled)
        if ctx_m:
            ctx = _clean_spaces(ctx_m.group(1))
            if not ctx.lower().startswith("согласно"):
                destination_context = ctx

    dep_labeled = _search(
        r"(?:Пункт\s*отправления|Последний\s*порт)\s*[:：]\s*(.+?)(?=\n|Время\s*выхода|"
        r"Дата\s*(?:выхода|отправления)|Пункт\s*назначения|2\.|3\.|$)",
        text,
    )
    if dep_labeled:
        departure_port = _clean_spaces(
            re.split(r"[.(]|[—\-–]", dep_labeled, maxsplit=1)[0]
        )

    dep_time = _search(
        r"(?:Время\s*выхода|Дата\s*(?:выхода|отправления)|Фактическое\s*время\s*отправления)"
        r"\s*[:：]\s*(.+?)(?=\n|Пункт|Заявленный|2\.|3\.|$)",
        text,
    )
    if dep_time:
        departure_datetime_utc = _clean_spaces(dep_time)

    loc = _search(
        r"(?:Местоположение|Локация|Координаты)\s*[:：]\s*(.+?)(?=\n|Последняя|"
        r"Кинематик|Скорость|Осадка|Навигационн|Статус:|2\.|3\.|$)",
        text,
    )
    if loc:
        loc_clean = _clean_spaces(re.split(r"\s+Статус\s*:", loc, maxsplit=1)[0])
        if len(loc_clean) > 120:
            loc_clean = loc_clean[:117] + "…"
        # Prefer explicit location over destination parenthetical
        destination_context = loc_clean

    # Destination fallback (legacy narrative blocks)
    dest_block = None
    if not destination_port:
        dest_block = _search(
            r"(?:Пункт\s*назначения|Destination|Dest)\s*[:：]?\s*(.+?)(?=\n\s*\*|\n\s*Наименование|$)",
            text,
        )
        if not dest_block:
            dest_block = _search(
                r"Локация\s*[:：]\s*(.+?)(?=\n\s*Активность|\n\s*\d+\.|$)",
                text,
            )

    if dest_block and not destination_port:
        dest_block = dest_block.strip()
        port_patterns = [
            r"(?:следует|направляется|идёт|идет)\s+в\s+порт\s+([^.(]+?)(?:\s*\(|\.|,|$)",
            r"в\s+порт\s+([^.(]+?)(?:\s*\(|\.|,|$)",
            r"^([^.(]+?)(?:\s*\(|\.|$)",
        ]
        for pat in port_patterns:
            port_m = re.search(pat, dest_block, re.IGNORECASE)
            if port_m:
                destination_port = _clean_spaces(port_m.group(1))
                break

        ctx_m = re.search(r"\(([^)]+)\)", dest_block)
        if ctx_m and not destination_context:
            destination_context = _clean_spaces(ctx_m.group(1))

        dep_m = re.search(
            r"(?:Вышл[оаи]|Departed|Left|Sailed)\s+(?:из|from)\s+(.+?)\s+"
            r"(\d{1,2}\s+\S+\s+\d{4}(?:\s+года)?(?:\s+в\s+\d{1,2}:\d{2}(?:\s*UTC)?)?)",
            dest_block,
            re.IGNORECASE,
        )
        if dep_m:
            if not departure_port:
                departure_port = _clean_spaces(dep_m.group(1))
            if not departure_datetime_utc:
                departure_datetime_utc = _clean_spaces(dep_m.group(2))
        else:
            dep_m2 = re.search(
                r"(?:Вышл[оаи]|Departed|Left|Sailed)\s+(?:из|from)\s+([^.]+)",
                dest_block,
                re.IGNORECASE,
            )
            if dep_m2 and not departure_port:
                departure_port = _clean_spaces(dep_m2.group(1))

    # Narrative logistics: "следует в порт Порт-Саид" / "направляется в Хьюстон, США"
    if not destination_port:
        port_nav = re.search(
            r"(?:следует|направляется|идёт|идет|следуя)\s+в\s+порт\s+"
            r"([^.(,\n]{2,80})(?:\s*\(|\.|,|$)",
            text,
            re.IGNORECASE,
        )
        if port_nav:
            destination_port = _clean_spaces(port_nav.group(1))
            ctx_m = re.search(
                r"(?:следует|направляется|идёт|идет|следуя)\s+в\s+порт\s+"
                + re.escape(port_nav.group(1))
                + r"\s*\(([^)]+)\)",
                text,
                re.IGNORECASE,
            )
            if ctx_m and not destination_context:
                destination_context = _clean_spaces(ctx_m.group(1).split(",")[0])
    if not destination_port:
        dest_nav = re.search(
            r"направля(?:ется|ясь)\s+в\s+([^.(,\n]{2,60})"
            r"(?:,\s*([^.(,\n]{2,40}))?",
            text,
            re.IGNORECASE,
        )
        if dest_nav:
            destination_port = _clean_spaces(dest_nav.group(1))
            if dest_nav.group(2) and not destination_context:
                destination_context = _clean_spaces(dest_nav.group(2))

    if not departure_port:
        dep_nav = re.search(
            r"(?:вышло|вышел|вышла|вышли|Departed|Left|Sailed)\s+из\s+"
            r"(?:района|порта|акватории)?\s*"
            r"([^.(,\n]{2,60})"
            r"(?:\s*\(([^)]+)\))?"
            r"(?:,\s*([^.(,\n]{2,40}))?",
            text,
            re.IGNORECASE,
        )
        if dep_nav:
            departure_port = _clean_spaces(dep_nav.group(1))
            bits = []
            if dep_nav.group(2):
                bits.append(_clean_spaces(dep_nav.group(2)))
            if dep_nav.group(3):
                bits.append(_clean_spaces(dep_nav.group(3)))
            if bits:
                departure_port = f"{departure_port} ({', '.join(bits)})"

    arr_m = re.search(
        r"(?:Ожидаемое\s*время\s*прибытия|Ожидаемое\s*прибытие|"
        r"Расчетное\s*время(?:\s*прибытия)?|"
        r"Reported\s*ETA|\bETA\b|Прибытие\s*на\s*рейд|Actual\s*Arrival|"
        r"Прибыл[оаи]?/ошвартовал(?:ся|ось)?|Прибыл[оаи]?|Прибытие|Arrived|Arrival)"
        r"(?:\s*\([^)]*\))?\s*[:：—\-–]?\s*(?:—\s*)?"
        r"(?:Указанный\s+ETA\s*)?\(?"
        r"(\d{1,2}\s+\S+(?:\s+\d{4})?[^\n.)]*?)"
        r"\)?(?:\s+уже\s+прошел)?(?:\.|$|\n)",
        text,
        re.IGNORECASE,
    )
    if arr_m:
        arrival_datetime = _clean_spaces(arr_m.group(1))
        # Drop trailing narrative crumbs / stray parens
        arrival_datetime = re.split(
            r"\s+уже\s+прошел|\s+что\s+может|\s+указывает",
            arrival_datetime,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .;()（）")

    # --- Rule 5: identity spoofing (independent) ---
    spoof_imo, spoof_note = _extract_identity_spoofing(text)

    # --- Rule 6 (iter 5): non-vessel markers only in vessel_type ---
    vessel_category = _classify_vessel_category(
        vessel_type=vessel_type,
        vessel_name=vessel_name,
        mmsi=mmsi,
        imo_from_text=imo_from_text,
        loa_m=loa_m,
        nav_status=nav_status,
    )

    # Resolve primary IMO = column A (normalized), fallback to text / UNKNOWN
    if imo_a:
        imo = imo_a
    elif imo_from_text:
        imo = imo_from_text
    else:
        imo = "UNKNOWN"

    if imo_format_error or imo == "UNKNOWN" or len(str(imo)) != 7:
        imo_valid = False
    else:
        imo_valid = imo_checksum_valid(imo)

    imo_source_match: Optional[bool] = None
    if imo_a and imo_from_text:
        imo_source_match = imo_a == imo_from_text
    elif imo_a or imo_from_text:
        imo_source_match = True

    # --- source_confidence priority ---
    # identity_conflict_flagged > non_vessel > imo_mismatch > needs_review > parsed
    source_confidence = "parsed"

    # Rule 8: format error in A is NOT imo_mismatch
    true_mismatch = (
        bool(imo_a)
        and bool(imo_from_text)
        and imo_a != imo_from_text
        and not imo_format_error
        and len(imo_a) == 7
        and len(imo_from_text) == 7
    )

    if true_mismatch:
        source_confidence = "imo_mismatch"

    if vessel_category == "non_vessel":
        source_confidence = "non_vessel"
    elif vessel_category == "unknown":
        source_confidence = "needs_review"

    if spoof_imo:
        source_confidence = "identity_conflict_flagged"

    record = VesselRecord(
        imo=imo,
        imo_valid=imo_valid,
        imo_format_error=bool(imo_format_error),
        imo_from_text=imo_from_text,
        imo_source_match=imo_source_match,
        identity_spoofing_suspected_imo=spoof_imo,
        identity_spoofing_note=spoof_note,
        vessel_category=vessel_category,
        vessel_name=vessel_name,
        mmsi=mmsi,
        call_sign=call_sign,
        vessel_type=vessel_type,
        built_year=built_year,
        age_years=age_years,
        flag=flag,
        dwt_tons=dwt_tons,
        gt=gt,
        loa_m=loa_m,
        beam_m=beam_m,
        draft_m=draft_m,
        draft_note=draft_note,
        nav_status=nav_status,
        speed_knots=speed_knots,
        asset_status=asset_status,
        compliance_risk_level=compliance_risk_level,
        destination_port=destination_port,
        destination_context=destination_context,
        departure_port=departure_port,
        departure_datetime_utc=departure_datetime_utc,
        arrival_datetime=arrival_datetime,
        source_confidence=source_confidence,
        raw_text=str(original_raw) if original_raw is not None else text,
    )

    # Sparse records → needs_review (do not override identity / non_vessel / mismatch)
    # Missing vessel_type alone does NOT force needs_review; <6 filled fields does.
    data = record.model_dump()
    filled = sum(
        1 for f in SIGNIFICANT_FIELDS if data.get(f) is not None and data.get(f) != ""
    )
    if filled < 6 and record.source_confidence == "parsed":
        record.source_confidence = "needs_review"

    if imo == "UNKNOWN" and record.source_confidence == "parsed":
        record.source_confidence = "needs_review"

    return record
