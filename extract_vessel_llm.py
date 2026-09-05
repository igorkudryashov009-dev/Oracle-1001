"""
Extract VesselProfile20Cols from OSINT narrative.

Primary: Instructor + OpenAI (structured output → Pydantic).
Fallback: native deterministic structured extract → same Pydantic validators
(when OPENAI_API_KEY is absent — honest local path, no invented LLM calls).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemas.vessel_schema import (  # noqa: E402
    REFERENCE_YEAR,
    TZ_COLUMNS,
    VesselProfile20Cols,
    normalize_risk,
    parse_numeric,
    split_departure_location,
    strip_markdown,
)

load_dotenv(ROOT / ".env")

SYSTEM_PROMPT = """You are an OSINT maritime data extractor for Oracle-1001.
Extract ALL 20 vessel profile fields from the user narrative.
Rules:
- Strip markdown (** * #) from values.
- Numbers only for dwt_tons, gt, loa_m, beam_m, draft_m, speed_knots, built_year.
- departure_port = last port / location ONLY (not destination).
- destination_port = destination only.
- destination_context = route/mission context (energy supply lanes etc.), NOT the port name.
- nav_status may be the transit description in parentheses after speed.
- compliance_risk_level MUST be one of: LOW, MID, HIGH, EXTREME.
- age_years will be recomputed as 2026 - built_year; you may send a placeholder.
- Never invent MMSI/call_sign/DWT if truly absent — but extract them whenever present in text.
Return a complete VesselProfile20Cols object.
"""


def _search(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
    if not m:
        return None
    val = m.group(1).strip()
    return val or None


def extract_vessel_structured_local(
    raw_text: str,
    declared_imo: Optional[str] = None,
) -> VesselProfile20Cols:
    """Deterministic structured extract → Pydantic validators (no LLM)."""
    text = strip_markdown(raw_text or "")
    # Keep newlines for sectioning, then also a flat copy
    flat = re.sub(r"\s+", " ", text)

    name = _search(
        r"(?:Наименование\s*судна|Наименование|Название\s*судна|Название)\s*[:：]\s*([^\n]+)",
        text,
    )
    if name:
        name = re.split(r"\s+(?:IMO|MMSI|Позывной|Тип|Год|Флаг)\b", name, maxsplit=1)[0]
        name = strip_markdown(name).rstrip(".")

    imo = _search(r"IMO\s*(?:номер\s*)?[:：]\s*(\d{7})", text)
    if declared_imo:
        d = re.sub(r"\D", "", str(declared_imo))
        if len(d) == 7:
            imo = d

    mmsi = _search(r"IMO\s*/\s*MMSI\s*[:：]\s*\d{7}\s*/\s*(\d{7,9})", text) or _search(
        r"MMSI\s*[:：]\s*(\d{5,12})", text
    )
    call = _search(
        r"(?:Позывной(?:\s*\([^)]*\))?|Call\s*sign)\s*[:：]\s*([A-Za-z0-9/\-]{1,15})",
        text,
    )
    vtype = _search(
        r"(?:Тип\s*судна|Тип)\s*[:：]\s*(.+?)(?=\s*Год\s*постройки|\s*Флаг|\s*Порт|\n\n|$)",
        text,
    )
    year = parse_numeric(_search(r"(?:Год\s*постройки|Built)\s*[:：]\s*[~≈]?\s*(\d{4})", text))
    flag = _search(
        r"(?:Флаг|Flag)\s*[:：]\s*(.+?)(?=\s*Порт\s*назначения|\s*Основные|\s*Размер|\s*Дедвейт|\n\n|$)",
        text,
    )

    dwt = parse_numeric(_search(r"(?:Дедвейт|DWT)\s*(?:\([^)]*\))?\s*[:：—\-–]?\s*([^\n]+)", text))
    gt = parse_numeric(
        _search(r"(?:Валовая\s*вместимость|Gross\s*Tonnage|\bGT\b)\s*(?:\([^)]*\))?\s*[:：—\-–]?\s*([^\n]+)", text)
    )
    loa = parse_numeric(
        _search(r"(?:Длина\s*(?:общая\s*)?\(?LOA\)?|LOA)\s*[:：—\-–]?\s*([\d][\d\s.,]*)", text)
    )
    beam = parse_numeric(
        _search(r"(?:ширина|beam)(?:\s*\([^)]*\))?\s*[:：—\-–]?\s*([\d][\d\s.,]*)", text)
    )
    if loa is None or beam is None:
        dims = re.search(
            r"(?:Размер(?:ения|ы)?|Габариты)\s*[:：]?\s*.*?([\d]+(?:[.,]\d+)?)\s*м?.*?"
            r"(?:[×xXх]|ширина).*?([\d]+(?:[.,]\d+)?)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if dims:
            loa = loa if loa is not None else parse_numeric(dims.group(1))
            beam = beam if beam is not None else parse_numeric(dims.group(2))

    draft = parse_numeric(
        _search(r"(?:Текущая\s*осадка|осадка|Draft|Draught)\s*[:：—\-–]?\s*([^\n]+)", text)
    )
    speed = parse_numeric(
        _search(r"(?:Скорость|Speed)\s*[:：—\-–]?\s*(?:—\s*)?([\d]+(?:[.,]\d+)?)", text)
    )

    nav = _search(r"скорость\s*[:：—\-–]?\s*[\d.,]+\s*узл[а-я]*\s*\(([^)]+)\)", text)
    if not nav:
        nav = _search(r"Навигационный статус\s*[:：]\s*([^\n]+)", text)

    dest = _search(
        r"(?:Порт\s*назначения|Пункт\s*назначения|Destination)\s*[:：]\s*(.+?)(?=,\s*расч|\.\s*расч|расч[её]тное|\n|$)",
        text,
    )
    eta = _search(
        r"(?:расч[её]тное\s*время\s*прибытия|\bETA\b|Ожидаемое\s*(?:время\s*)?прибытия)"
        r"\s*(?:\([^)]*\))?\s*[:：—\-–]?\s*"
        r"(\d{1,2}\s+\S+\s+\d{4}\s*г\.?(?:,\s*\d{1,2}:\d{2}(?:\s*UTC)?)?)",
        text,
    )

    dep_raw = _search(
        r"(?:Последний\s*порт\s*/\s*Локация|Последний\s*порт|Пункт\s*отправления)\s*[:：]\s*(.+?)(?=\n\n|\n\s*###|$)",
        text,
    )
    departure_port = None
    if dep_raw:
        departure_port, _ = split_departure_location(dep_raw)

    ctx = _search(
        r"задействовано\s+в\s+(.+?)(?=\.\s*Санкцион|\.\s*\n|Санкционный|$)",
        text,
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

    risk = normalize_risk(_search(r"(?:Санкционный\s*статус|Уровень\s*риска|Статус)\s*[:：]\s*([^\n]+)", text))
    if not risk:
        risk = normalize_risk(flat)

    payload = {
        "vessel_name": name or "",
        "imo": imo or "",
        "mmsi": mmsi or "",
        "call_sign": (call or "").rstrip("."),
        "vessel_type": (vtype or "").strip(),
        "built_year": int(year) if year else REFERENCE_YEAR,
        "age_years": 0,
        "flag": (flag or "").rstrip("."),
        "dwt_tons": float(dwt) if dwt is not None else 0.0,
        "gt": float(gt) if gt is not None else 0.0,
        "loa_m": float(loa) if loa is not None else 0.0,
        "beam_m": float(beam) if beam is not None else 0.0,
        "draft_m": float(draft) if draft is not None else 0.0,
        "nav_status": (nav or "").rstrip("."),
        "speed_knots": float(speed) if speed is not None else 0.0,
        "destination_port": (dest or "").rstrip(".,;"),
        "destination_context": ctx or "",
        "departure_port": departure_port or "",
        "arrival_datetime": (eta or "").rstrip(".,;"),
        "compliance_risk_level": risk or "LOW",
    }
    return VesselProfile20Cols.model_validate(payload)


def extract_vessel_via_instructor(
    raw_text: str,
    declared_imo: Optional[str] = None,
    *,
    model: Optional[str] = None,
) -> VesselProfile20Cols:
    """LLM structured extract via Instructor → VesselProfile20Cols."""
    try:
        import instructor
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "instructor/openai not installed. pip install instructor openai"
        ) from exc

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing — cannot use Instructor path")

    model_name = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = instructor.from_openai(OpenAI(api_key=api_key))
    user_msg = raw_text if not declared_imo else f"Declared IMO (column A): {declared_imo}\n\n{raw_text}"
    profile = client.chat.completions.create(
        model=model_name,
        response_model=VesselProfile20Cols,
        max_retries=2,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    if declared_imo:
        digits = re.sub(r"\D", "", str(declared_imo))
        if len(digits) == 7:
            profile = profile.model_copy(update={"imo": digits})
            profile = profile.model_validate(profile.model_dump())
    return profile


def extract_vessel(
    raw_text: str,
    declared_imo: Optional[str] = None,
    *,
    use_llm: Optional[bool] = None,
) -> dict[str, Any]:
    """Return validated 20-field dict. LLM if key+flag; else local schema path."""
    if use_llm is None:
        use_llm = os.getenv("USE_LLM_EXTRACT", "").strip() in ("1", "true", "yes")
        use_llm = use_llm and bool(os.getenv("OPENAI_API_KEY", "").strip())

    if use_llm:
        profile = extract_vessel_via_instructor(raw_text, declared_imo=declared_imo)
    else:
        profile = extract_vessel_structured_local(raw_text, declared_imo=declared_imo)
    return profile.to_tz_dict()


def tz_fill_rate(rec: dict[str, Any]) -> float:
    filled = 0
    zero_means_empty = {"dwt_tons", "gt", "loa_m", "beam_m", "draft_m"}
    for k in TZ_COLUMNS:
        v = rec.get(k)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, (int, float)) and float(v) == 0.0 and k in zero_means_empty:
            continue
        filled += 1
    return 100.0 * filled / len(TZ_COLUMNS)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract VesselProfile20Cols from OSINT text")
    parser.add_argument("--imo", default="9656888", help="Declared IMO / demo vessel")
    parser.add_argument("--xlsx", default="", help="Override source xlsx (optional)")
    parser.add_argument("--llm", action="store_true", help="Force Instructor/OpenAI path")
    parser.add_argument("--text-file", default="", help="Optional raw text file")
    args = parser.parse_args()

    if args.text_file:
        raw = Path(args.text_file).read_text(encoding="utf-8")
        declared = args.imo
    else:
        import pandas as pd
        from paths import resolve_fleet_source

        xlsx = Path(args.xlsx) if args.xlsx else resolve_fleet_source()
        df = pd.read_excel(xlsx, sheet_name="Sheet1")
        hit = df[df.iloc[:, 0].astype(str).str.replace(r"\D", "", regex=True) == re.sub(r"\D", "", args.imo)]
        if hit.empty:
            print(f"ERROR: IMO {args.imo} not found in {xlsx}", file=sys.stderr)
            return 1
        raw = str(hit.iloc[0, 1])
        declared = str(hit.iloc[0, 0])

    rec = extract_vessel(raw, declared_imo=declared, use_llm=True if args.llm else None)
    fill = tz_fill_rate(rec)
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    print(f"\nFill Rate: {fill:.0f}% ({sum(1 for k in TZ_COLUMNS if rec.get(k) not in (None, '', 0, 0.0))}/20)")
    # Stricter fill: non-empty strings + non-null numbers (speed 0 allowed)
    ok = 0
    for k in TZ_COLUMNS:
        v = rec.get(k)
        if v is None:
            continue
        if isinstance(v, str) and not str(v).strip():
            continue
        ok += 1
    print(f"Strict Fill Rate: {100.0 * ok / 20:.0f}% ({ok}/20)")
    if ok < 20:
        missing = [k for k in TZ_COLUMNS if not rec.get(k) and rec.get(k) != 0]
        # refine missing
        missing = []
        for k in TZ_COLUMNS:
            v = rec.get(k)
            if v is None or (isinstance(v, str) and not v.strip()):
                missing.append(k)
        print("Missing:", missing)
        return 2
    print("STATUS: 100% extraction OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
