"""
Robust OSINT narrative parser → exactly 20 ТЗ columns (NASA / Wet-Glass).

Algorithm:
  1) Preprocess — strip Markdown, normalize `Key: Value` lines
  2) Flexible Regex Engine — case/space tolerant extractors
  3) Normalize → 20 UPPERCASE keys (also snake_case for CSV)
  4) Fill-rate report helper
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from pipeline.synthetic_enrichment import enrich_missing_fields

REFERENCE_YEAR = 2026

# Canonical 20 ТЗ keys (UPPERCASE) as required by the dashboard contract
TZ_KEYS_UPPER: list[str] = [
    "VESSEL_NAME",
    "IMO",
    "MMSI",
    "CALL_SIGN",
    "VESSEL_TYPE",
    "BUILT_YEAR",
    "AGE_YEARS",
    "FLAG",
    "DWT_TONS",
    "GT",
    "LOA_M",
    "BEAM_M",
    "DRAFT_M",
    "NAV_STATUS",
    "SPEED_KNOTS",
    "DESTINATION_PORT",
    "DESTINATION_CONTEXT",
    "DEPARTURE_PORT",
    "ARRIVAL_DATETIME",
    "COMPLIANCE_RISK_LEVEL",
]

# snake_case aliases used by fleet_database.csv / osint_layers.html
UPPER_TO_SNAKE: dict[str, str] = {
    "VESSEL_NAME": "vessel_name",
    "IMO": "imo",
    "MMSI": "mmsi",
    "CALL_SIGN": "call_sign",
    "VESSEL_TYPE": "vessel_type",
    "BUILT_YEAR": "built_year",
    "AGE_YEARS": "age_years",
    "FLAG": "flag",
    "DWT_TONS": "dwt_tons",
    "GT": "gt",
    "LOA_M": "loa_m",
    "BEAM_M": "beam_m",
    "DRAFT_M": "draft_m",
    "NAV_STATUS": "nav_status",
    "SPEED_KNOTS": "speed_knots",
    "DESTINATION_PORT": "destination_port",
    "DESTINATION_CONTEXT": "destination_context",
    "DEPARTURE_PORT": "departure_port",
    "ARRIVAL_DATETIME": "arrival_datetime",
    "COMPLIANCE_RISK_LEVEL": "compliance_risk_level",
}

TZ_COLUMNS: list[str] = [UPPER_TO_SNAKE[k] for k in TZ_KEYS_UPPER]

# L8 Compliance — sanctions / dark-fleet signal tags (extra field, not in 20 ТЗ core)
SANCTION_TAG_SPECS: list[tuple[str, re.Pattern[str]]] = [
    ("OFAC", re.compile(r"\bOFAC\b|\bSDN\b", re.IGNORECASE)),
    (
        "STS Risk",
        re.compile(
            r"\bSTS\b|ship[\s\-]?to[\s\-]?ship|рейдов[аоы][яйе]?\s+перевалк|"
            r"перевалк\w*\s+на\s+рейде",
            re.IGNORECASE,
        ),
    ),
    (
        "Shadow Fleet",
        re.compile(
            r"shadow\s*fleet|тенев\w*\s+флот|«?тенев\w*»?\s+актив|"
            r"серы\w*\s+схем",
            re.IGNORECASE,
        ),
    ),
    (
        "Dark Activity",
        re.compile(
            r"dark\s*activ|AIS\s*(?:off|gap|dark)|выключ\w*\s+AIS|"
            r"судно[\-\s]?призрак|ghost\s*vessel|тёмн\w*\s+активност",
            re.IGNORECASE,
        ),
    ),
    (
        "Flag Risk",
        re.compile(
            r"flag\s*risk|флаг\w*\s+сомнител|удобн\w*\s+флаг|"
            r"не\s+имеет\s+выхода\s+к\s+(?:морю|океану)|landlocked|"
            r"флага\s+сомнительной\s+юрисдикции",
            re.IGNORECASE,
        ),
    ),
]


# ---------------------------------------------------------------------------
# Heuristic Risk Engine — deterministic fallback for UNLABELED vessels
# ---------------------------------------------------------------------------

# Flags with weak/absent maritime administration — elevated scrutiny
_GRAY_REGISTRY: frozenset[str] = frozenset({
    "comoros", "коморские", "comoro",
    "cameroon", "камерун",
    "gabon", "габон",
    "palau", "палау",
    "eswatini", "свазиленд",
    "tuvalu", "тувалу",
    "niue", "ниуэ",
    "cook islands", "острова кука",
    "sao tome", "сан-томе",
    "togo", "того",
    "djibouti", "джибути",
    "kiribati", "кирибати",
    "mali", "мали",
    "landlocked",
    "belize", "белиз",
    "sierra leone", "сьерра-леоне",
    "cambodia", "камбоджа",
    "moldova", "молдова",
    "guinea", "гвинея",
    "vanuatu", "вануату",
    "st kitts", "сент-китс",
})

_OOS_HEUR: re.Pattern[str] = re.compile(
    r"out[\s\-]of[\s\-]service|not\s+in\s+service|"
    r"выведено\s+из\s+эксплуатации|scrapped|утилизир\w+|"
    r"судно\s+мертво|AIS\s*активность\s*:\s*(?:отсутствует|нет\s+данных)",
    re.IGNORECASE,
)


def heuristic_risk_eval(
    nav_status: Optional[str],
    age_years: Optional[int],
    flag: Optional[str],
    sanctions_tags: list[str],
    vessel_type: Optional[str] = None,
) -> tuple[str, str]:
    """Deterministic fallback risk labeler.

    Returns (risk_level, "heuristic").
    Decision tree (highest priority first):
      EXTREME: OOS + OFAC/DarkActivity | OFAC/DarkActivity alone | landlocked flag + OOS
      HIGH:    OOS alone | Shadow/STS/FlagRisk tags | age > threshold
      MID:     gray-registry flag (any age)
      LOW:     clean default
    """
    tags_u = {t.upper() for t in (sanctions_tags or [])}
    nav_u  = (nav_status or "").upper()
    flag_l = (flag or "").lower()
    is_oos = bool(_OOS_HEUR.search(nav_u)) if nav_u else False
    is_gas = bool(re.search(r"LNG|LPG|GAS|ГАЗО", (vessel_type or "").upper()))

    # EXTREME
    if "OFAC" in tags_u or "DARK ACTIVITY" in tags_u:
        return ("EXTREME", "heuristic")
    if is_oos and any(kw in flag_l for kw in {"mali", "мали", "comoros", "коморские", "landlocked"}):
        return ("EXTREME", "heuristic")

    # HIGH
    if is_oos:
        return ("HIGH", "heuristic")
    if any(t in tags_u for t in {"SHADOW FLEET", "STS RISK", "FLAG RISK"}):
        return ("HIGH", "heuristic")
    age = int(age_years) if age_years else 0
    age_threshold = 30 if is_gas else 35
    if age > age_threshold:
        return ("HIGH", "heuristic")

    # MID
    if any(kw in flag_l for kw in _GRAY_REGISTRY):
        return ("MID", "heuristic")

    # LOW — clean default
    return ("LOW", "heuristic")


def extract_sanctions_tags(text: str) -> list[str]:
    """Pull L8 sanctions tags from cleaned OSINT narrative (order preserved)."""
    if not text:
        return []
    found: list[str] = []
    for label, pat in SANCTION_TAG_SPECS:
        if pat.search(text) and label not in found:
            found.append(label)
    return found


def sanctions_tags_csv(tags: list[str]) -> Optional[str]:
    return "; ".join(tags) if tags else None


# ---------------------------------------------------------------------------
# Stage 1 — Preprocess
# ---------------------------------------------------------------------------

_MD_BOLD = re.compile(r"\*\*|__")
_MD_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]*")
_MD_ITALIC_STAR = re.compile(r"\*")
_MD_ITALIC_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9])_(?![A-Za-z0-9])")
_BULLET = re.compile(r"(?m)^[ \t]*[-•]\s+")
_HR = re.compile(r"(?m)^[ \t]*-{3,}[ \t]*$")


def strip_markdown(text: str) -> str:
    """Full Markdown scrub: **, *, _, # — keep digits/brackets/commas/colons."""
    if not text:
        return ""
    s = str(text)
    s = _MD_BOLD.sub("", s)
    s = _MD_HEADING.sub("", s)
    s = _MD_ITALIC_STAR.sub("", s)
    s = _MD_ITALIC_UNDERSCORE.sub("", s)
    s = _BULLET.sub("", s)
    s = _HR.sub("", s)
    return s


def preprocess(raw_text: str) -> str:
    """Stage 1: clean markdown and normalize toward `Key: Value` lines."""
    s = strip_markdown(raw_text)
    # Normalize fancy dashes / colons
    s = s.replace("：", ":").replace("—", "—").replace("–", "—")
    # Ensure list-like "Label: value" stays on its own conceptual lines
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def _block(text: str, title: str) -> str:
    """Extract subsection body after a heading-like title until next heading/EOF."""
    pat = re.compile(
        rf"(?:^|\n)\s*{re.escape(title)}\s*\n+(.*?)(?=\n\s*[А-ЯA-Z][^:\n]{{0,40}}\n|\n\s*\d+\.\s|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    m = pat.search(text)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Stage 2 — Flexible Regex Engine
# ---------------------------------------------------------------------------

def _search(pattern: str, text: str, flags: int = re.IGNORECASE | re.MULTILINE) -> Optional[str]:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    val = m.group(1).strip()
    return val or None


def _to_float(token: str) -> Optional[float]:
    if token is None:
        return None
    s = str(token).strip().replace("\xa0", " ").replace("~", "").replace("≈", "")
    if not s:
        return None
    m = re.search(r"(\d{1,3}(?:[ \u00a0,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)", s)
    if not m:
        return None
    raw = m.group(1).replace("\u00a0", " ").strip()
    compact = raw.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", compact):
        compact = compact.replace(",", "")
    elif re.fullmatch(r"\d+,\d{1,2}", compact):
        compact = compact.replace(",", ".")
    else:
        compact = compact.replace(",", "")
    try:
        return float(compact)
    except ValueError:
        return None


def _to_int(token: str) -> Optional[int]:
    f = _to_float(token)
    return int(f) if f is not None else None


_FLAG_BRACKET_RE = re.compile(r"\s*\(([^)]{3,})\)\s*$")


def _clean_flag(raw: str) -> tuple[str, Optional[str]]:
    """Strip any parenthetical explanation from a FLAG string.

    Returns (clean_country_name, note_or_None).
    E.g.: "Коморские Острова (типичный флаг для «серого» флота)"
          → ("Коморские Острова", "типичный флаг для «серого» флота")
    """
    s = re.sub(r"\s+", " ", raw).strip().rstrip(".")
    m = _FLAG_BRACKET_RE.search(s)
    if m:
        note = m.group(1).strip()
        clean = s[: m.start()].strip()
        return clean, note if len(note) > 4 else None
    return s, None


def _normalize_risk(text: str) -> Optional[str]:
    u = text.upper()
    # Prefer explicit labeled risk phrases — avoid FP on «высокоэффективный»
    if re.search(
        r"ЭКСТРЕМАЛЬ\w*\s+РИСК|EXTREME|\bGHOST\s*VESSEL\b|СУДНО[\-\s]?ПРИЗРАК|КРИТИЧЕСКИ\s+ВЫСОК",
        u,
    ):
        return "EXTREME"
    if re.search(r"ВЫСОКИЙ\s+РИСК|\bHIGH\s*RISK\b|\bHIGH\b(?!\w)|СТРАТЕГИЧЕСКИЙ\s+АКТИВ", u):
        return "HIGH"
    if re.search(r"СРЕДНИЙ\s+РИСК|\bMEDIUM\b|\bMID\b(?!\w)", u):
        return "MID"
    if re.search(
        r"НИЗКИЙ\s+РИСК|\bLOW\s*RISK\b|\bLOW\b(?!\w)|CLEARED|ЧИСТЫЙ\s+ПРОФИЛЬ|ЛЕГИТИМНЫЙ\s+АКТИВ",
        u,
    ):
        return "LOW"
    return None


def extract_fields(text: str, declared_imo: Optional[str] = None) -> dict[str, Any]:
    """Stage 2: flexible, case/space-tolerant extractors — full multi-alias engine."""
    tech = _block(text, "Технические характеристики") or text
    ops  = _block(text, "Операционный профиль и комплаенс") or text

    # ── Identity ──────────────────────────────────────────────────────────────
    vessel_name = _search(
        r"(?:Наименование\s*судна|Наименование|Название\s*судна|Название)\s*:\s*(.+)",
        text,
    )
    if vessel_name:
        vessel_name = re.split(
            r"\s+(?:IMO|MMSI|Позывной|Тип|Год|Флаг)\b",
            vessel_name, maxsplit=1, flags=re.IGNORECASE,
        )[0]
        vessel_name = re.sub(r"\s+", " ", vessel_name).strip().rstrip(".")

    imo = _search(r"IMO\s*(?:номер\s*)?:\s*(\d{7})", text)
    if declared_imo:
        d = re.sub(r"\D", "", str(declared_imo))
        if len(d) == 7:
            imo = d

    # MMSI — 9-digit MID codes; multiple aliases + inline slash pattern
    mmsi = (
        _search(r"MMSI\s*:\s*(\d{9})\b", text)
        or _search(r"MMSI\s*:\s*(\d{7,12})", text)
        or _search(r"(?:ММСИ|МMSI|Идентификатор\s+AIS|AIS[\s\-]ID)\s*[:\-–]?\s*(\d{9})\b", text)
        or _search(r"IMO\s*/\s*MMSI\s*:\s*\d{7}\s*/\s*(\d{7,12})", text)
        or _search(r"IMO\s*/\s*MMSI\s*:\s*\d{7}\s*/\s*(\d{9})\b", text)
        or _search(r"(?:^|\s)([2-7]\d{8})\b(?=[^\d])", text)  # bare 9-digit MID
    )

    # CALL_SIGN — positional + keyword variants
    call_sign = (
        _search(r"(?:Позывной(?:\s*\([^)]*\))?|Call\s*sign|CS)\s*:\s*([A-Za-z0-9/\-]{2,15})", text)
        or _search(r"(?:позывной|позывн[оы][йи])\s+(?:сигнал\s+)?:?\s*([A-Z0-9]{3,8})\b", text)
        or _search(r"\bCS\s*[:\-]\s*([A-Z0-9]{3,8})\b", text)
    )
    if call_sign:
        call_sign = call_sign.rstrip(".")

    vessel_type = _search(
        r"(?:Тип\s*судна|Тип)\s*:\s*(.+?)(?=\s*Год\s*постройки|\s*Флаг|\s*Порт|\n\n|$)",
        text,
    )
    if vessel_type:
        vessel_type = re.sub(r"\s+", " ", vessel_type).strip()

    # ── Year of build / age — multi-pattern ───────────────────────────────────
    built_year = (
        _to_int(_search(r"Год\s*постройки\s*:\s*[~≈]?\s*(\d{4})", text))
        or _to_int(_search(r"(\d{4})\s*года?\s*постройки", text))
        or _to_int(_search(r"построен\w*\s+в\s+(\d{4})", text))
        or _to_int(_search(r"введ[её]н\w*\s+в\s+эксплуатацию\s+в?\s*(\d{4})", text))
        or _to_int(_search(r"\bBuilt\s*[:\-–]?\s*(\d{4})\b", text))
    )
    # Age-based: "(N года)" / "(N лет)" → back-calculate
    if not built_year:
        for age_pat in [r"\((\d{1,2})\s*(?:год|лет|года)\)", r"возраст\s*[:\-–]?\s*(\d{1,2})\s*(?:год|лет|года)"]:
            age_raw = _to_int(_search(age_pat, text))
            if age_raw and 1 <= age_raw <= 80:
                built_year = REFERENCE_YEAR - age_raw
                break
    # IMO-based estimate mention: "~1993 (на основе IMO)"
    if not built_year:
        built_year = _to_int(_search(r"[~≈]\s*(\d{4})\s*\(на\s+основе", text))

    flag_raw = _search(
        r"Флаг\s*:\s*(.+?)(?=\s*Порт\s*назначения|\s*Основные|\s*Размер|\s*Дедвейт|\n\n|$)",
        text,
    )
    flag: Optional[str] = None
    flag_note: Optional[str] = None
    if flag_raw:
        flag, flag_note = _clean_flag(flag_raw)

    # ── Technical block — DWT / GT ────────────────────────────────────────────
    def _extract_dwt(src: str) -> Optional[float]:
        raw = _search(r"(?:Дедвейт|DWT|дедвейтом)\s*(?:\([^)]*\))?\s*[:\-–]?\s*([^\n]+)", src)
        if not raw:
            return None
        # Range "~3 500 – 4 500" → average
        rng = re.search(r"([\d][\d\s,\.]+)\s*[–—\-]+\s*([\d][\d\s,\.]+)", raw)
        if rng:
            v1, v2 = _to_float(rng.group(1)), _to_float(rng.group(2))
            if v1 and v2:
                return round((v1 + v2) / 2, 0)
        return _to_float(raw)

    def _extract_gt(src: str) -> Optional[float]:
        return _to_float(
            _search(r"(?:Валовая\s*вместимость|Gross\s*Tonnage|\bGT\b)\s*(?:\([^)]*\))?\s*[:\-–]?\s*([^\n]+)", src)
        )

    dwt = _extract_dwt(tech) or _extract_dwt(text)
    # Extra GT aliases
    gt = (
        _extract_gt(tech) or _extract_gt(text)
        or _to_float(_search(r"(?:валовая|брутто)\s*регистровая\s*вместимость\s*[:\-–]?\s*([^\n]+)", tech or text))
        or _to_float(_search(r"\bBRT\b\s*[:\-–]?\s*([^\n]+)", text))
    )

    loa = _to_float(
        _search(r"(?:Длина\s*(?:общая\s*)?\(?LOA\)?|LOA)\s*[—:\-–]?\s*([\d][\d\s.,]*)", tech)
    ) or _to_float(
        _search(r"(?:Длина\s*(?:общая\s*)?\(?LOA\)?|LOA)\s*[—:\-–]?\s*([\d][\d\s.,]*)", text)
    )

    beam = _to_float(
        _search(r"(?:ширина|beam)(?:\s*\([^)]*\))?\s*[—:\-–]?\s*([\d][\d\s.,]*)", tech)
    ) or _to_float(
        _search(r"(?:ширина|beam)(?:\s*\([^)]*\))?\s*[—:\-–]?\s*([\d][\d\s.,]*)", text)
    )

    if loa is None or beam is None:
        dims = re.search(
            r"(?:Размер(?:ения|ы)?|Габариты)\s*:?\s*.*?([\d]+(?:[.,]\d+)?)\s*м?.*?"
            r"(?:[×xXх]|ширина).*?([\d]+(?:[.,]\d+)?)",
            tech or text, re.IGNORECASE | re.DOTALL,
        )
        if dims:
            loa  = loa  if loa  is not None else _to_float(dims.group(1))
            beam = beam if beam is not None else _to_float(dims.group(2))

    draft = _to_float(
        _search(r"(?:Текущая\s*осадка|осадка|Draft|Draught)\s*[—:\-–]?\s*([^\n]+)", tech)
    ) or _to_float(
        _search(r"(?:Текущая\s*осадка|осадка|Draft|Draught)\s*[—:\-–]?\s*([^\n]+)", text)
    )

    # ── Speed ─────────────────────────────────────────────────────────────────
    speed = (
        _to_float(_search(r"скорость\s*[—:\-–]?\s*([\d]+(?:[.,]\d+)?)\s*узл", text))
        or _to_float(_search(r"([\d]+(?:[.,]\d+)?)\s*(?:узл[а-я]*|kn\b|knots)", text))
        or _to_float(_search(r"(?:Speed|Скорость)\s*[:\-–]?\s*([\d]+(?:[.,]\d+)?)\b", tech or text))
        or _to_float(_search(r"ход[уе]\s+(?:со\s+скоростью\s+)?([\d]+(?:[.,]\d+)?)\s*узл", text))
    )

    # ── Nav status — decommissioned / out-of-service first ────────────────────
    _OOS_RE = re.compile(
        r"выведено\s+из\s+эксплуатации|вывод\w*\s+из\s+эксплуатации|"
        r"not\s+in\s+service|out[\s\-]of[\s\-]service|"
        r"AIS\s*активность\s*:\s*(?:отсутствует|нет\s+данных|не\s+поступ\w+)|"
        r"судно\s+мертво\s+в\s+системе|"
        r"исключ\w+\s+из\s+(?:реестра|регистра|флота)|"
        r"\bSCRAPPED\b|утилизир\w+|списан\w+\s+(?:судно|из|с)",
        re.IGNORECASE,
    )
    nav: Optional[str] = None
    oos_match = _OOS_RE.search(text)
    if oos_match:
        u = oos_match.group(0).upper()
        nav = "Scrapped / Утилизировано" if re.search(r"SCRAPPED|УТИЛИЗИР|СПИСАН", u) \
              else "Out of Service / Выведено из эксплуатации"
    else:
        nav = (
            _search(r"Навигационный статус\s*:\s*([^\n]+)", text)
            or _search(r"скорость\s*[—:\-–]?\s*[\d.,]+\s*узл[а-я]*\s*\(([^)]+)\)", text)
            or _search(r"(?:Navigational\s+status|Nav\.?\s+status)\s*[:\-–]?\s*([^\n]+)", text)
        )
        if nav:
            nav = re.sub(r"\s+", " ", nav).strip().rstrip(".")
        # AIS activity note as fallback
        if not nav:
            ais_note = _search(r"AIS\s*активность\s*:\s*([^\n.]+)", text)
            if ais_note:
                nav = f"AIS: {ais_note.strip()}"

    # ── Destination port — multi-alias ────────────────────────────────────────
    dest = (
        _search(r"(?:Порт\s*назначения|Пункт\s*назначения|Destination\s*port|Destination)\s*:\s*"
                r"(.+?)(?=,\s*расч|\.\s*расч|расч[её]тное|ETA|\n|$)", text)
        or _search(r"(?:следует\s+в|направляется\s+в|идёт\s+в|идет\s+в|Заход\s+в)\s+(.+?)(?=[,\n]|$)", text)
        or _search(r"(?:порт\s*прибытия|прибытие\s+в)\s*[:\-–]?\s*(.+?)(?=[,\n]|$)", text)
        or _search(r"рейс\s+(?:из\s+[^\s]+\s+)?(?:в|до)\s+(.+?)(?=\s+ETA|\s+\d{1,2}\s|\n|$)", text)
        or _search(r"Reported\s+destination\s*[:\-–]?\s*(.+?)(?=[,\n]|$)", text)
        or _search(r"направление\s*[:\-–]?\s*(.+?)(?=[,\n]|$)", text)
    )
    if dest:
        dest = re.sub(r"\s+", " ", dest).strip().rstrip(".,;")

    # ── ETA / ARRIVAL_DATETIME — multi-pattern date search ───────────────────
    _RDATE = (
        r"\d{1,2}\s+"
        r"(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря|"
        r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
        r"\w*\s+\d{4}\s*г\.?(?:\s*(?:в\s*)?\d{1,2}:\d{2}(?:\s*\(UTC[^\)]*\)|\s*UTC[+-]?\d*)?)?"
    )
    _NDATE = r"\d{1,2}[./]\d{1,2}[./]\d{4}(?:\s+\d{1,2}:\d{2}(?:\s*UTC[+-]?\d*)?)?"
    eta = (
        _search(rf"(?:расч[её]тное\s*время\s*прибытия|ETA|Ожидаемое\s*(?:время\s*)?прибытия)"
                rf"\s*(?:\([^)]*\))?\s*[—:\-–]?\s*({_RDATE})", text)
        or _search(rf"(?:расч[её]тное|ETA)\s*[:\-–]?\s*({_NDATE})", text)
        or _search(rf"(?:прибытие|Actual\s+Arrival|прибыл\w*)\s*(?:\([^)]*\))?\s*[:\-–]?\s*({_RDATE})", text)
        or _search(rf"прибытие\s+(?:на\s+рейд\s+[^,]+,\s*)?\s*(?:\([^)]*\)\s*)?[:\-–]?\s*({_RDATE})", text)
        or _search(rf"зафиксировано\s+(?:по\s+состоянию\s+на\s+)?({_RDATE})", text)
        or _search(rf"Данные\s+(?:на|по\s+состоянию\s+на)\s+({_RDATE})", text)
        or _search(rf"(?:последний\s+сигнал|последняя\s+фиксация)\s*[:\-–]?[^,\n]*?({_RDATE})", text)
        or _search(rf"(?:отход|отправление|вышел)\s*[:\-–]?\s*({_RDATE})", text)
        or _search(rf"({_NDATE})(?=\s*(?:UTC|г\.|,))", text)
    )
    if eta:
        eta = re.sub(r"\s+", " ", eta).strip().rstrip(".,;")

    # ── Departure port — multi-alias ─────────────────────────────────────────
    dep = (
        _search(r"Последний\s*порт\s*/?\s*Локация\s*:\s*(.+?)(?=\s*[—–]\s*отход|\s*отход\s+\d|\n\n|$)", text)
        or _search(r"(?:Пункт\s*отправления|Последний\s*порт|Порт\s*отхода|Порт\s*отправления)\s*:\s*"
                   r"(.+?)(?=\n\n|Дата|Время|$)", text)
        or _search(r"(?:Last\s+port|Port\s+of\s+departure|From\s+port)\s*[:\-–]?\s*(.+?)(?=[,\n]|$)", text)
        or _search(r"(?:отход\s+из|вышло?\s+из|выш[еа]дш\w+\s+из|отбыло?\s+из)\s+(.+?)(?=[,\n]|$)", text)
        or _search(r"рейс\s+из\s+(.+?)\s+(?:в|до)\s+", text)
    )
    if dep:
        dep = re.sub(r"\s+", " ", dep).strip().rstrip(".,;")
        if dep.count("(") > dep.count(")"):
            dep += ")"

    # ── Destination context ───────────────────────────────────────────────────
    ctx = _search(
        r"задействовано\s+в\s+(.+?)(?=\.\s*Санкцион|Санкционный|\n\n|$)",
        ops if ops else text,
    )
    if ctx:
        m_ctx = re.search(
            r"((?:трансконтинентальных\s+)?поставк[а-я]+\s+энергоносителей[^.]{0,160})",
            ctx, re.IGNORECASE,
        )
        if m_ctx:
            ctx = m_ctx.group(1)
        ctx = re.sub(r"^трансконтинентальных\s+поставках\s+", "поставки ", ctx, flags=re.I)
        ctx = re.sub(r"^поставках\s+", "поставки ", ctx, flags=re.I)
        ctx = re.sub(r"\s*/\s*Восточной\s+Азии\s*$", "", ctx, flags=re.I)
        ctx = re.sub(r"\s*\(LPG\)\s*", " ", ctx)
        ctx = re.sub(r"\s+", " ", ctx).strip(" .,;")

    if not ctx and flag_note:
        ctx = flag_note

    # ── Risk ──────────────────────────────────────────────────────────────────
    risk_line = _search(
        r"(?:Санкционный\s*статус|Уровень\s*риска|Статус\s*комплаенса)\s*:\s*([^\n]+)",
        ops if ops else text,
    )
    risk = _normalize_risk(risk_line or text)

    return {
        "vessel_name": vessel_name,
        "imo": imo,
        "mmsi": mmsi,
        "call_sign": call_sign,
        "vessel_type": vessel_type,
        "built_year": built_year,
        "flag": flag,
        "dwt_tons": dwt,
        "gt": gt,
        "loa_m": loa,
        "beam_m": beam,
        "draft_m": draft,
        "nav_status": nav,
        "speed_knots": speed,
        "destination_port": dest,
        "destination_context": ctx,
        "departure_port": dep,
        "arrival_datetime": eta,
        "compliance_risk_level": risk,
        # internal: passed to _apply_inferences
        "_flag_note": flag_note,
    }
    if vessel_name:
        vessel_name = re.split(
            r"\s+(?:IMO|MMSI|Позывной|Тип|Год|Флаг)\b",
            vessel_name,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        vessel_name = re.sub(r"\s+", " ", vessel_name).strip().rstrip(".")

    imo = _search(r"IMO\s*(?:номер\s*)?:\s*(\d{7})", text)
    if declared_imo:
        d = re.sub(r"\D", "", str(declared_imo))
        if len(d) == 7:
            imo = d

    # MMSI: key + 9 digits (also accept 7–12)
    mmsi = _search(r"MMSI\s*:\s*(\d{9})\b", text) or _search(r"MMSI\s*:\s*(\d{7,12})", text)
    if not mmsi:
        mmsi = _search(r"IMO\s*/\s*MMSI\s*:\s*\d{7}\s*/\s*(\d{7,9})", text)

    call_sign = _search(
        r"(?:Позывной(?:\s*\([^)]*\))?|Call\s*sign)\s*:\s*([A-Za-z0-9/\-]{1,15})",
        text,
    )
    if call_sign:
        call_sign = call_sign.rstrip(".")

    vessel_type = _search(
        r"(?:Тип\s*судна|Тип)\s*:\s*(.+?)(?=\s*Год\s*постройки|\s*Флаг|\s*Порт|\n\n|$)",
        text,
    )
    if vessel_type:
        vessel_type = re.sub(r"\s+", " ", vessel_type).strip()

    built_year = _to_int(_search(r"Год\s*постройки\s*:\s*[~≈]?\s*(\d{4})", text))

    flag_raw = _search(
        r"Флаг\s*:\s*(.+?)(?=\s*Порт\s*назначения|\s*Основные|\s*Размер|\s*Дедвейт|\n\n|$)",
        text,
    )
    flag: Optional[str] = None
    flag_note: Optional[str] = None
    if flag_raw:
        flag, flag_note = _clean_flag(flag_raw)

    # Technical block numbers (thousand spaces: 54 776)
    dwt = _to_float(_search(r"(?:Дедвейт|DWT)\s*(?:\([^)]*\))?\s*:\s*([^\n]+)", tech))
    if dwt is None:
        dwt = _to_float(_search(r"(?:Дедвейт|DWT)\s*(?:\([^)]*\))?\s*:\s*([^\n]+)", text))

    gt = _to_float(
        _search(r"(?:Валовая\s*вместимость|Gross\s*Tonnage|\bGT\b)\s*(?:\([^)]*\))?\s*:\s*([^\n]+)", tech)
    )
    if gt is None:
        gt = _to_float(
            _search(r"(?:Валовая\s*вместимость|Gross\s*Tonnage|\bGT\b)\s*(?:\([^)]*\))?\s*:\s*([^\n]+)", text)
        )

    loa = _to_float(
        _search(r"(?:Длина\s*(?:общая\s*)?\(?LOA\)?|LOA)\s*[—:\-–]?\s*([\d][\d\s.,]*)", tech)
    )
    if loa is None:
        loa = _to_float(
            _search(r"(?:Длина\s*(?:общая\s*)?\(?LOA\)?|LOA)\s*[—:\-–]?\s*([\d][\d\s.,]*)", text)
        )

    beam = _to_float(
        _search(r"(?:ширина|beam)(?:\s*\([^)]*\))?\s*[—:\-–]?\s*([\d][\d\s.,]*)", tech)
    )
    if beam is None:
        beam = _to_float(
            _search(r"(?:ширина|beam)(?:\s*\([^)]*\))?\s*[—:\-–]?\s*([\d][\d\s.,]*)", text)
        )

    if loa is None or beam is None:
        dims = re.search(
            r"(?:Размер(?:ения|ы)?|Габариты)\s*:?\s*.*?([\d]+(?:[.,]\d+)?)\s*м?.*?"
            r"(?:[×xXх]|ширина).*?([\d]+(?:[.,]\d+)?)",
            tech or text,
            re.IGNORECASE | re.DOTALL,
        )
        if dims:
            loa = loa if loa is not None else _to_float(dims.group(1))
            beam = beam if beam is not None else _to_float(dims.group(2))

    draft = _to_float(
        _search(r"(?:Текущая\s*осадка|осадка|Draft|Draught)\s*[—:\-–]?\s*([^\n]+)", tech)
    )
    if draft is None:
        draft = _to_float(
            _search(r"(?:Текущая\s*осадка|осадка|Draft|Draught)\s*[—:\-–]?\s*([^\n]+)", text)
        )

    # Speed — number before «узла» / after «скорость —»
    speed = _to_float(_search(r"скорость\s*[—:\-–]?\s*([\d]+(?:[.,]\d+)?)\s*узл", text))
    if speed is None:
        speed = _to_float(_search(r"([\d]+(?:[.,]\d+)?)\s*узл", tech))

    # Nav status — decommissioned / out-of-service states have PRIORITY
    _OOS_RE = re.compile(
        r"выведено\s+из\s+эксплуатации|вывод\w*\s+из\s+эксплуатации|"
        r"not\s+in\s+service|out[\s\-]of[\s\-]service|"
        r"AIS\s*активность\s*:\s*(?:отсутствует|нет\s+данных|не\s+поступ\w+)|"
        r"судно\s+мертво\s+в\s+системе|"
        r"исключ\w+\s+из\s+(?:реестра|регистра|флота)|"
        r"\bSCRAPPED\b|утилизир\w+|списан\w+\s+(?:судно|из|с)",
        re.IGNORECASE,
    )
    oos_match = _OOS_RE.search(text)
    nav: Optional[str] = None
    if oos_match:
        # Normalise to bilingual canonical label
        raw_oos = oos_match.group(0).strip()
        u = raw_oos.upper()
        if re.search(r"SCRAPPED|УТИЛИЗИР|СПИСАН", u):
            nav = "Scrapped / Утилизировано"
        else:
            nav = "Out of Service / Выведено из эксплуатации"
    else:
        nav = _search(r"скорость\s*[—:\-–]?\s*[\d.,]+\s*узл[а-я]*\s*\(([^)]+)\)", text)
        if not nav:
            nav = _search(r"Навигационный статус\s*:\s*([^\n]+)", text)
        if nav:
            nav = re.sub(r"\s+", " ", nav).strip().rstrip(".")

    # Also pull AIS-status lines into nav_status when no explicit nav found
    if not nav:
        ais_note = _search(
            r"AIS\s*активность\s*:\s*([^\n.]+)", text
        )
        if ais_note:
            nav = f"AIS: {ais_note.strip()}"

    # Destination
    dest = _search(
        r"(?:Порт\s*назначения|Пункт\s*назначения|Destination)\s*:\s*(.+?)(?=,\s*расч|\.\s*расч|расч[её]тное|\n|$)",
        text,
    )
    if dest:
        dest = re.sub(r"\s+", " ", dest).strip().rstrip(".,;")

    eta = _search(
        r"(?:расч[её]тное\s*время\s*прибытия|\bETA\b|Ожидаемое\s*(?:время\s*)?прибытия)"
        r"\s*(?:\([^)]*\))?\s*[—:\-–]?\s*"
        r"(\d{1,2}\s+\S+\s+\d{4}\s*г\.?(?:,\s*\d{1,2}:\d{2}(?:\s*UTC)?)?)",
        text,
    )
    if eta:
        eta = re.sub(r"\s+", " ", eta).strip().rstrip(".,;")

    # Departure — Последний порт / Локация (NOT destination)
    dep = _search(
        r"Последний\s*порт\s*/\s*Локация\s*:\s*(.+?)(?=\s*—\s*отход|\s*отход\s+\d|\n\n|$)",
        text,
    )
    if not dep:
        dep = _search(
            r"(?:Пункт\s*отправления|Последний\s*порт)\s*:\s*(.+?)(?=\n\n|$)",
            text,
        )
    if dep:
        dep = re.sub(r"\s+", " ", dep).strip().rstrip(".,;")
        if dep.count("(") > dep.count(")"):
            dep += ")"

    # Destination context from operational profile
    ctx = _search(
        r"задействовано\s+в\s+(.+?)(?=\.\s*Санкцион|Санкционный|\n\n|$)",
        ops if ops else text,
    )
    if ctx:
        m = re.search(
            r"((?:трансконтинентальных\s+)?поставк[а-я]+\s+энергоносителей[^.]{0,160})",
            ctx,
            re.IGNORECASE,
        )
        if m:
            ctx = m.group(1)
        ctx = re.sub(r"^трансконтинентальных\s+поставках\s+", "поставки ", ctx, flags=re.I)
        ctx = re.sub(r"^поставках\s+", "поставки ", ctx, flags=re.I)
        ctx = re.sub(r"\s*/\s*Восточной\s+Азии\s*$", "", ctx, flags=re.I)
        ctx = re.sub(r"\s*\(LPG\)\s*", " ", ctx)
        ctx = re.sub(r"\s+", " ", ctx).strip(" .,;")

    # If ctx is still empty and FLAG had a parenthetical note, use that as context
    if not ctx and flag_note:
        ctx = flag_note

    risk_line = _search(
        r"(?:Санкционный\s*статус|Уровень\s*риска|Статус\s*комплаенса)\s*:\s*([^\n]+)",
        ops if ops else text,
    )
    risk = _normalize_risk(risk_line or text)

    return {
        "vessel_name": vessel_name,
        "imo": imo,
        "mmsi": mmsi,
        "call_sign": call_sign,
        "vessel_type": vessel_type,
        "built_year": built_year,
        "flag": flag,
        "dwt_tons": dwt,
        "gt": gt,
        "loa_m": loa,
        "beam_m": beam,
        "draft_m": draft,
        "nav_status": nav,
        "speed_knots": speed,
        "destination_port": dest,
        "destination_context": ctx,
        "departure_port": dep,
        "arrival_datetime": eta,
        "compliance_risk_level": risk,
    }


# ---------------------------------------------------------------------------
# Stage 3 — Normalize → 20 columns
# ---------------------------------------------------------------------------

def normalize_to_20(raw_fields: dict[str, Any]) -> dict[str, Any]:
    """Guarantee exactly 20 UPPERCASE keys."""
    built = raw_fields.get("built_year")
    age = None
    if isinstance(built, (int, float)) and built:
        age = int(REFERENCE_YEAR) - int(built)

    upper = {
        "VESSEL_NAME": raw_fields.get("vessel_name"),
        "IMO": raw_fields.get("imo"),
        "MMSI": raw_fields.get("mmsi"),
        "CALL_SIGN": raw_fields.get("call_sign"),
        "VESSEL_TYPE": raw_fields.get("vessel_type"),
        "BUILT_YEAR": int(built) if built is not None else None,
        "AGE_YEARS": age,
        "FLAG": raw_fields.get("flag"),
        "DWT_TONS": raw_fields.get("dwt_tons"),
        "GT": raw_fields.get("gt"),
        "LOA_M": raw_fields.get("loa_m"),
        "BEAM_M": raw_fields.get("beam_m"),
        "DRAFT_M": raw_fields.get("draft_m"),
        "NAV_STATUS": raw_fields.get("nav_status"),
        "SPEED_KNOTS": raw_fields.get("speed_knots"),
        "DESTINATION_PORT": raw_fields.get("destination_port"),
        "DESTINATION_CONTEXT": raw_fields.get("destination_context"),
        "DEPARTURE_PORT": raw_fields.get("departure_port"),
        "ARRIVAL_DATETIME": raw_fields.get("arrival_datetime"),
        "COMPLIANCE_RISK_LEVEL": raw_fields.get("compliance_risk_level"),
    }
    # Exact key set / order
    return {k: upper.get(k) for k in TZ_KEYS_UPPER}


def to_snake(upper_rec: dict[str, Any]) -> dict[str, Any]:
    return {UPPER_TO_SNAKE[k]: upper_rec[k] for k in TZ_KEYS_UPPER}


def _val_present(v: Any) -> bool:
    """True when a value is non-empty and non-sentinel."""
    if v is None:
        return False
    import math as _math
    if isinstance(v, float) and (_math.isnan(v) or _math.isinf(v)):
        return False
    s = str(v).strip().lower()
    return s not in {"", "—", "-", "none", "не извлечено", "nan", "n/a"}


def _infer_nav_status(speed: Optional[float], text: str) -> str:
    """Infer NAV_STATUS from kinematic + textual signals."""
    tl = text.lower()
    if re.search(r"рейд|якор|anchorage|at\s+anchor|на\s+якор|стоянк", tl):
        return "At Anchor / На якорной стоянке"
    if re.search(r"выведено|not\s+in\s+service|out\s+of\s+service|scrapped|утилизир", tl):
        return "Out of Service / Выведено из эксплуатации"
    if speed is not None:
        if speed <= 0.3:
            return "At Anchor / На якорной стоянке"
        if speed > 0.5:
            return "Underway using Engine / В пути"
    if re.search(r"в\s+пути|ход[уе]\s|transit|underway|следует\s|переход\s|перевозк", tl):
        return "Underway using Engine / В пути"
    return "Underway / В плавании"


def _apply_inferences(upper: dict[str, Any], cleaned_text: str) -> dict[str, Any]:
    """Stage 3.5 — cross-field inference: fill remaining gaps with logical defaults.

    Runs AFTER normalize_to_20.  Mutates a copy of ``upper``.
    """
    u = dict(upper)

    # 1. AGE_YEARS ↔ BUILT_YEAR cross-calculation
    built = u.get("BUILT_YEAR")
    age   = u.get("AGE_YEARS")
    if built and not age:
        try:
            u["AGE_YEARS"] = REFERENCE_YEAR - int(built)
        except (TypeError, ValueError):
            pass
    elif age and not built:
        try:
            u["BUILT_YEAR"] = REFERENCE_YEAR - int(age)
        except (TypeError, ValueError):
            pass

    # 2. NAV_STATUS — infer from speed / text if missing
    if not _val_present(u.get("NAV_STATUS")):
        u["NAV_STATUS"] = _infer_nav_status(u.get("SPEED_KNOTS"), cleaned_text)

    # 3. DESTINATION_CONTEXT — auto-generate from available fields if empty
    if not _val_present(u.get("DESTINATION_CONTEXT")):
        parts: list[str] = []
        vt  = u.get("VESSEL_TYPE")
        fl  = u.get("FLAG")
        dp  = u.get("DEPARTURE_PORT")
        dst = u.get("DESTINATION_PORT")
        nav = u.get("NAV_STATUS")
        age_v = u.get("AGE_YEARS")

        if vt:
            parts.append(vt.split("(")[0].strip().rstrip())
        if fl:
            parts.append(f"флаг: {fl}")
        if dp and dst:
            parts.append(f"маршрут: {dp} → {dst}")
        elif dst:
            parts.append(f"следует в: {dst}")
        elif dp:
            parts.append(f"отход: {dp}")
        if nav:
            parts.append(nav.split("/")[0].strip())
        if age_v:
            parts.append(f"возраст: {age_v} лет")

        if parts:
            u["DESTINATION_CONTEXT"] = " · ".join(parts)

    # 4. SPEED_KNOTS — if vessel is underway but speed missing: estimated transit speed
    if not _val_present(u.get("SPEED_KNOTS")):
        nav_v = str(u.get("NAV_STATUS") or "").lower()
        if re.search(r"underway|в\s+пути|в\s+плавании", nav_v):
            vt_v = str(u.get("VESSEL_TYPE") or "").upper()
            # LNG/LPG/VLGC: ~14 kn; bulk/tanker: ~12 kn; general: ~10 kn
            if re.search(r"LNG|LPG|VLGC|GAS", vt_v):
                u["SPEED_KNOTS"] = 14.0
            elif re.search(r"TANKER|TANKER|BULK|OIL", vt_v):
                u["SPEED_KNOTS"] = 12.0
            else:
                u["SPEED_KNOTS"] = 10.0

    return u


def parse_osint_narrative(
    raw_text: str,
    declared_imo: Optional[str] = None,
    *,
    as_snake: bool = True,
) -> dict[str, Any]:
    """Full pipeline Stages 1–3.5 + L8 SANCTIONS_TAGS + heuristic risk fallback."""
    cleaned = preprocess(raw_text or "")
    fields  = extract_fields(cleaned, declared_imo=declared_imo)
    upper   = normalize_to_20(fields)
    # Stage 3.5 — cross-field inference (fills NAV_STATUS, DESTINATION_CONTEXT, AGE, SPEED)
    upper   = _apply_inferences(upper, cleaned)
    tags    = extract_sanctions_tags(cleaned)

    # ── Risk resolution ──────────────────────────────────────────────────────
    risk_level  = upper.get("COMPLIANCE_RISK_LEVEL")
    risk_source = "explicit"

    if not risk_level:
        # Compute AGE from built_year if needed
        built = upper.get("BUILT_YEAR")
        age   = upper.get("AGE_YEARS")
        if age is None and built:
            try:
                age = REFERENCE_YEAR - int(built)
            except (TypeError, ValueError):
                age = None

        risk_level, risk_source = heuristic_risk_eval(
            nav_status   = upper.get("NAV_STATUS"),
            age_years    = age,
            flag         = upper.get("FLAG"),
            sanctions_tags = tags,
            vessel_type  = upper.get("VESSEL_TYPE"),
        )
        upper["COMPLIANCE_RISK_LEVEL"] = risk_level
    # ────────────────────────────────────────────────────────────────────────

    if as_snake:
        out = to_snake(upper)
        out["sanctions_tags"]    = sanctions_tags_csv(tags)
        out["risk_source"]       = risk_source
        out["synthetic_fields"]  = ""           # will be filled by enrichment
        out = enrich_missing_fields(out)         # Stage 4: synthetic gap-fill
        return out
    upper["SANCTIONS_TAGS"]     = sanctions_tags_csv(tags)
    upper["RISK_SOURCE"]        = risk_source
    upper["SYNTHETIC_FIELDS"]   = ""
    # convert to snake for enrichment then back
    snake = to_snake(upper)
    snake["sanctions_tags"]  = upper["SANCTIONS_TAGS"]
    snake["risk_source"]     = upper["RISK_SOURCE"]
    snake["synthetic_fields"] = ""
    snake = enrich_missing_fields(snake)
    # rebuild upper from enriched snake
    for k in TZ_KEYS_UPPER:
        upper[k] = snake.get(UPPER_TO_SNAKE[k])
    upper["SYNTHETIC_FIELDS"] = snake.get("synthetic_fields", "")
    return upper


def fill_rate(rec: dict[str, Any], *, uppercase: bool = False) -> float:
    keys = TZ_KEYS_UPPER if uppercase else TZ_COLUMNS
    # Map snake↔upper if needed
    if uppercase and any(k in rec for k in TZ_COLUMNS) and "VESSEL_NAME" not in rec:
        rec = {k: rec.get(UPPER_TO_SNAKE[k]) for k in TZ_KEYS_UPPER}
        keys = TZ_KEYS_UPPER
    filled = 0
    zero_empty = {
        "DWT_TONS",
        "GT",
        "LOA_M",
        "BEAM_M",
        "DRAFT_M",
        "dwt_tons",
        "gt",
        "loa_m",
        "beam_m",
        "draft_m",
    }
    for k in keys:
        v = rec.get(k)
        if v is None:
            continue
        if isinstance(v, float) and v != v:  # NaN
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, (int, float)) and float(v) == 0.0 and k in zero_empty:
            continue
        filled += 1
    return 100.0 * filled / len(keys)


def format_report_table(upper_rec: dict[str, Any]) -> str:
    """ASCII table for console acceptance report."""
    lines = [
        f"{'#':<3} {'COLUMN':<24} {'VALUE'}",
        f"{'-'*3} {'-'*24} {'-'*60}",
    ]
    for i, k in enumerate(TZ_KEYS_UPPER, start=1):
        v = upper_rec.get(k)
        shown = "—" if v is None or v == "" else str(v)
        if len(shown) > 70:
            shown = shown[:67] + "..."
        lines.append(f"{i:<3} {k:<24} {shown}")
    rate = fill_rate(upper_rec, uppercase=True)
    lines.append("")
    lines.append(f"Fill Rate: {rate:.0f}% ({sum(1 for k in TZ_KEYS_UPPER if upper_rec.get(k) not in (None, ''))}/20)")
    return "\n".join(lines)
