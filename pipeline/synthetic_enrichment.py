"""
Synthetic Enrichment Engine — ORACLE-1001
==========================================
Deterministic (IMO-hash-seeded) gap-filler for 20 ТЗ columns.

⚠  SYNTHETIC DATA ≠ REAL OSINT.
   Every fabricated field is listed in vessel['synthetic_fields'] (semicolon-sep string)
   and rendered with the ⚡ synth badge in osint_layers.html.

Design:
  - hashlib.md5(IMO + field_name) seeds a random.Random → same IMO always yields
    the same values across multiple pipeline runs.
  - ITU MID / call-sign prefix tables for MMSI & CALL_SIGN.
  - Vessel-type-aware port pool selection (LNG / LPG / oil / generic).
  - ETA: reference date ± deterministic offset.
  - DWT ↔ GT: physics-based conversion ratios.
"""

from __future__ import annotations

import hashlib
import random
import re
from datetime import datetime, timedelta
from typing import Any, Optional

# ── Reference date ────────────────────────────────────────────────────────────
_REF_DATE = datetime(2026, 9, 4)

_ALPHA    = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DIGITS   = "0123456789"
_ALPHANUM = _ALPHA + _DIGITS

_VESSEL_PREFIXES: dict[str, list[str]] = {
    "lng":     ["GAS", "LNG", "ENERGY", "ARCTIC", "PACIFIC", "AZURE"],
    "lpg":     ["NAVIGATOR", "GAS", "CARGO", "PACIFIC", "PIONEER", "LPG"],
    "crude":   ["TANKER", "CRUDE", "ATLANTIC", "PACIFIC", "OCEAN"],
    "tanker":  ["TANKER", "EAGLE", "HORIZON", "STAR", "MARINE"],
    "generic": ["VESSEL", "PACIFIC", "OCEAN", "MARITIME", "ATLAS"],
}

# DWT ranges (min, max) by vessel type key
_DWT_RANGES: dict[str, tuple[int, int]] = {
    "lng":    (40_000,  174_000),
    "lpg":    (3_000,   84_000),
    "crude":  (80_000,  320_000),
    "tanker": (10_000,  120_000),
    "generic":(5_000,   60_000),
}
# GT ≈ DWT * ratio (used when generating GT standalone)
_GT_RATIO: dict[str, float] = {
    "lng": 1.22, "lpg": 1.08, "crude": 0.90, "tanker": 0.92, "generic": 0.95,
}

# ── ITU Maritime tables ───────────────────────────────────────────────────────
# Each entry: (flag_keywords_tuple, MID_list, callsign_prefix_list)
_ITU_TABLE: list[tuple[tuple[str, ...], list[int], list[str]]] = [
    (("panama", "панам"),
     [351, 352, 353, 355, 370, 371, 372, 373], ["3E", "3F", "H3", "HP", "HQ", "HO"]),
    (("liberia", "либери"),
     [636], ["5L", "5M", "A8", "EL"]),
    (("marshall", "маршалл"),
     [538], ["V7"]),
    (("bahamas", "багам"),
     [308, 309], ["C6"]),
    (("singapore", "сингапур"),
     [563, 564, 565, 566, 567], ["9V"]),
    (("hong kong", "гонконг"),
     [477], ["VR"]),
    (("cyprus", "кипр"),
     [209, 210, 212], ["5B", "P3"]),
    (("malta", "мальта"),
     [215, 229, 248], ["9H"]),
    (("norway", "норвег"),
     [257, 258, 259], ["LA", "LB", "LG", "LM", "LN"]),
    (("greece", "греци"),
     [237, 239, 240], ["SV", "J4", "SW"]),
    (("china", "китай"),
     [412, 413, 414, 416], ["BO", "BY", "BZ", "XU"]),
    (("japan", "япони"),
     [431, 432, 433], ["JA", "JH", "JL", "JS"]),
    (("korea", "корея"),
     [440, 441], ["DS", "HL", "6K", "6L", "6M"]),
    (("russia", "росси"),
     [273], ["UA", "UB", "UC", "UD", "UE", "RA", "RG"]),
    (("comoros", "коморск"),
     [616, 620], ["D6"]),
    (("gabon", "габон"),
     [626], ["TR"]),
    (("cameroon", "камерун"),
     [613], ["TJ"]),
    (("palau", "палау"),
     [511], ["T8"]),
    (("belize", "белиз"),
     [312], ["V3"]),
    (("togo", "того"),
     [671], ["5V"]),
    (("tuvalu", "тувалу"),
     [572], ["T2"]),
    (("cambodia", "камбодж"),
     [514, 515], ["XU"]),
    (("united kingdom", "uk", "великобритани"),
     [232, 233, 234, 235], ["G", "2E", "M", "MF"]),
    (("united states", "usa", "сша"),
     [338, 366, 367, 368, 369], ["WA", "WB", "WD", "KC"]),
    (("india", "инди"),
     [419], ["VT", "AT"]),
    (("uae", "emirat", "оаэ"),
     [470, 471], ["A6"]),
    (("saudi", "саудов"),
     [403], ["HZ"]),
    (("iran", "иран"),
     [422], ["EP"]),
    (("kuwait", "кувейт"),
     [447], ["9K"]),
    (("qatar", "катар"),
     [466], ["A7"]),
    (("turkey", "турци"),
     [271], ["TC", "YM"]),
    (("france", "франци"),
     [226, 228], ["FA", "FG", "FN"]),
    (("germany", "германи", "deutsch"),
     [211, 218], ["DA", "DC", "DL", "DP"]),
    (("italy", "итали"),
     [247], ["IB", "IK", "IP"]),
    (("spain", "испани"),
     [224], ["EA", "EC", "EG"]),
    (("denmark", "дании"),
     [219, 220], ["OU", "OZ", "XP"]),
    (("netherlands", "нидерланд", "голланд"),
     [244, 245, 246], ["PA", "PB", "PD", "PI"]),
    (("sweden", "шведи"),
     [265, 266], ["SA", "SB", "SC", "SD"]),
    (("finland", "финлянд"),
     [230], ["OF", "OG", "OH"]),
    (("indonesia", "индонези"),
     [525], ["YB", "YC", "YH"]),
    (("malaysia", "малайзи"),
     [533], ["9M"]),
    (("thailand", "таиланд"),
     [567], ["HS"]),
    (("vietnam", "вьетнам"),
     [574], ["XV"]),
    (("philippines", "филиппин"),
     [548], ["DU", "DX", "4D"]),
    (("australia", "австрали"),
     [503], ["VH", "VK"]),
    (("canada", "канад"),
     [316], ["CF", "CG", "VA", "VE"]),
    (("brazil", "бразили"),
     [710], ["PP", "PY"]),
    (("argentina", "аргентин"),
     [701], ["LO", "LW"]),
    (("mexico", "мексик"),
     [345], ["XA", "XB", "XC"]),
    (("eswatini", "свазиленд"),
     [629], ["3DA"]),
    (("cook islands", "острова кука"),
     [518], ["ZK"]),
    (("niue", "ниуэ"),
     [542], ["ZM"]),
    (("mali", "мали"),
     [625], ["TZ"]),
    (("sierra leone", "сьерра-леоне"),
     [619], ["9L"]),
    (("guinea", "гвинея"),
     [627], ["3X"]),
    (("vanuatu", "вануату"),
     [576], ["YJ"]),
    (("st kitts", "сент-китс"),
     [341], ["V4"]),
    (("moldova", "молдов"),
     [214], ["ER"]),
    (("djibouti", "джибути"),
     [621], ["J2"]),
    (("kiribati", "кирибати"),
     [529], ["T3"]),
    (("sao tome", "сан-томе"),
     [626], ["S9"]),
]
_ITU_DEFAULT: tuple[list[int], list[str]] = ([636], ["5L"])  # Liberia as universal fallback

# ── Port pools ────────────────────────────────────────────────────────────────
_LNG_EXPORT = [
    "Ras Laffan, Qatar (QA RLF)",
    "Sabine Pass LNG, USA (US SAB)",
    "Corpus Christi LNG, USA (US CRP)",
    "Sabetta, Russia (RU SAB)",
    "Darwin LNG, Australia (AU DRW)",
    "Bintulu LNG, Malaysia (MY BTU)",
    "Gladstone LNG, Australia (AU GLA)",
    "Bonny LNG, Nigeria (NG BNL)",
    "Tangguh LNG, Indonesia (ID TGH)",
    "Angola LNG, Angola (AO LNG)",
    "Cove Point LNG, USA (US COV)",
    "Hammerfest LNG, Norway (NO HAM)",
]
_LNG_IMPORT = [
    "Incheon, South Korea (KR INC)",
    "Yokohama, Japan (JP YOK)",
    "Gate Terminal Rotterdam, Netherlands (NL RTM)",
    "Barcelona Regas, Spain (ES BCN)",
    "Zeebrugge LNG, Belgium (BE ZEE)",
    "Grain LNG, UK (GB GRA)",
    "Shanghai LNG, China (CN SHA)",
    "Revithoussa, Greece (GR REV)",
    "Fos-sur-Mer, France (FR FOS)",
    "Tianjin, China (CN TJN)",
    "Ningbo, China (CN NGB)",
]
_LPG_EXPORT = [
    "Ras Laffan, Qatar (QA RLF)",
    "Yanbu, Saudi Arabia (SA YNB)",
    "Jubail, Saudi Arabia (SA JUB)",
    "Houston, USA (US HOU)",
    "Marcus Hook, USA (US MHK)",
    "Chiba, Japan (JP CHB)",
    "Yeosu, South Korea (KR YOS)",
    "Ain Sukhna, Egypt (EG ASU)",
    "Rotterdam, Netherlands (NL RTM)",
    "Primorsk, Russia (RU PRI)",
]
_LPG_IMPORT = [
    "Incheon, South Korea (KR INC)",
    "Chiba, Japan (JP CHB)",
    "Shanghai, China (CN SHA)",
    "Zhangpu, China (CN ZAP)",
    "Ningbo, China (CN NGB)",
    "Ulsan, South Korea (KR USN)",
    "Rotterdam, Netherlands (NL RTM)",
    "Tianjin, China (CN TJN)",
    "Huizhou, China (CN HUI)",
    "Oita, Japan (JP OIT)",
]
_OIL_EXPORT = [
    "Kharg Island, Iran (IR KHG)",
    "Ras Tanura, Saudi Arabia (SA RAT)",
    "Fujairah, UAE (AE FUJ)",
    "Novorossiysk, Russia (RU NVS)",
    "Primorsk, Russia (RU PRI)",
    "Sidi Kerir, Egypt (EG SDK)",
    "Bonny, Nigeria (NG BON)",
    "Ceyhan, Turkey (TR CEY)",
]
_OIL_IMPORT = [
    "Rotterdam, Netherlands (NL RTM)",
    "Qingdao, China (CN TAO)",
    "Ulsan, South Korea (KR USN)",
    "Yokohama, Japan (JP YOK)",
    "Houston, USA (US HOU)",
    "Trieste, Italy (IT TRS)",
    "Wilhelmshaven, Germany (DE WHV)",
    "Ain Sukhna, Egypt (EG ASU)",
]
_GENERIC = [
    "Singapore (SG SIN)",
    "Rotterdam, Netherlands (NL RTM)",
    "Houston, USA (US HOU)",
    "Shanghai, China (CN SHA)",
    "Incheon, South Korea (KR INC)",
    "Yokohama, Japan (JP YOK)",
    "Fujairah, UAE (AE FUJ)",
    "Piraeus, Greece (GR PIR)",
]

# ── Year ranges by vessel type ────────────────────────────────────────────────
_YEAR_RANGES: list[tuple[tuple[str, ...], tuple[int, int]]] = [
    (("lng",),       (2000, 2024)),
    (("lpg", "vlgc"), (1990, 2024)),
    (("vlcc", "suezmax", "aframax"), (1995, 2022)),
    (("tanker",),    (1990, 2022)),
    (("bulk",),      (1988, 2021)),
]
_DEFAULT_YEAR_RANGE = (1992, 2022)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _rng(imo: str, field: str) -> random.Random:
    """Deterministic RNG seeded by IMO + field name."""
    raw = f"{imo}:{field}".encode()
    seed = int(hashlib.md5(raw).hexdigest(), 16) % (2 ** 32)
    return random.Random(seed)


def _is_empty(val: Any) -> bool:
    if val is None:
        return True
    s = str(val).strip().lower()
    return s in {"", "—", "-", "none", "не извлечено", "nan", "n/a"}


def _get_itu(flag: Optional[str]) -> tuple[list[int], list[str]]:
    fl = (flag or "").lower()
    for keywords, mids, cs_list in _ITU_TABLE:
        if any(kw in fl for kw in keywords):
            return mids, cs_list
    return _ITU_DEFAULT


def _vessel_type_key(vt: Optional[str]) -> str:
    u = (vt or "").upper()
    if "LNG" in u:
        return "lng"
    if "LPG" in u or "VLGC" in u or "ГАЗО" in u:
        return "lpg"
    if "CRUDE" in u or "VLCC" in u or "SUEZ" in u or "AFRA" in u:
        return "crude"
    if "OIL" in u or "ТАНКЕР" in u or "TANKER" in u:
        return "tanker"
    return "generic"


# ── Synthetic field generators ────────────────────────────────────────────────
def _synth_mmsi(imo: str, flag: Optional[str]) -> str:
    r = _rng(imo, "MMSI")
    mids, _ = _get_itu(flag)
    mid = r.choice(mids)
    suffix = r.randint(0, 999_999)
    return f"{mid}{suffix:06d}"


def _synth_call_sign(imo: str, flag: Optional[str]) -> str:
    r = _rng(imo, "CALL_SIGN")
    _, cs_list = _get_itu(flag)
    prefix = r.choice(cs_list)
    # Pad to 4–5 total alphanumeric chars
    need = max(2, 5 - len(prefix))
    suffix = "".join(r.choice(_ALPHANUM) for _ in range(need))
    return (prefix + suffix)[:7]  # ITU max 7 chars


def _synth_port(imo: str, field: str, vt_key: str, pool_type: str) -> str:
    r = _rng(imo, field)
    if pool_type == "export":
        pool = {"lng": _LNG_EXPORT, "lpg": _LPG_EXPORT,
                "crude": _OIL_EXPORT, "tanker": _OIL_EXPORT}.get(vt_key, _GENERIC)
    else:
        pool = {"lng": _LNG_IMPORT, "lpg": _LPG_IMPORT,
                "crude": _OIL_IMPORT, "tanker": _OIL_IMPORT}.get(vt_key, _GENERIC)
    return r.choice(pool)


def _synth_eta(imo: str) -> str:
    r = _rng(imo, "ARRIVAL_DATETIME")
    offset = r.randint(-7, 60)  # −7..+60 days around reference
    hour   = r.randint(0, 23)
    minute = r.choice([0, 15, 30, 45])
    dt = _REF_DATE + timedelta(days=offset)
    dt = dt.replace(hour=hour, minute=minute)
    return f"{dt.day} {_MONTHS_RU[dt.month - 1]} {dt.year} г., {hour:02d}:{minute:02d} UTC"


def _synth_gt_from_dwt(dwt: float, vt_key: str) -> float:
    ratios = {"lng": 1.22, "lpg": 1.08, "crude": 0.92, "tanker": 0.90}
    return round(dwt * ratios.get(vt_key, 0.92))


def _synth_dwt_from_gt(gt: float, vt_key: str) -> float:
    ratios = {"lng": 0.82, "lpg": 0.93, "crude": 1.08, "tanker": 1.10}
    return round(gt * ratios.get(vt_key, 1.05))


_MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля",   "августа", "сентября", "октября", "ноября", "декабря",
]


def _synth_vessel_name(imo: str, vessel_type: Optional[str]) -> str:
    r   = _rng(imo, "VESSEL_NAME")
    vtk = _vessel_type_key(vessel_type)
    prefix = r.choice(_VESSEL_PREFIXES.get(vtk, _VESSEL_PREFIXES["generic"]))
    tail   = imo[-4:] if imo.isdigit() else "".join(r.choice(_ALPHA) for _ in range(4))
    return f"{prefix} {tail}"


def _synth_dwt(imo: str, vessel_type: Optional[str]) -> int:
    r   = _rng(imo, "DWT_TONS")
    vtk = _vessel_type_key(vessel_type)
    lo, hi = _DWT_RANGES.get(vtk, _DWT_RANGES["generic"])
    # Round to nearest 50
    raw = r.randint(lo, hi)
    return int(round(raw / 50) * 50)


def _synth_gt_standalone(imo: str, vessel_type: Optional[str]) -> int:
    dwt = _synth_dwt(imo, vessel_type)
    vtk = _vessel_type_key(vessel_type)
    return int(round(dwt * _GT_RATIO.get(vtk, 0.92)))


def _synth_built_year(imo: str, vessel_type: Optional[str]) -> int:
    r = _rng(imo, "BUILT_YEAR")
    vt = (vessel_type or "").lower()
    lo, hi = _DEFAULT_YEAR_RANGE
    for keys, rng_pair in _YEAR_RANGES:
        if any(k in vt for k in keys):
            lo, hi = rng_pair
            break
    return r.randint(lo, hi)


# ── Main enrichment function ─────────────────────────────────────────────────
def enrich_missing_fields(rec: dict[str, Any]) -> dict[str, Any]:
    """Fill remaining NULL fields with deterministic synthetic values.

    Args:
        rec: snake_case dict (output of ``parse_osint_narrative``).

    Returns:
        Enriched copy.  Mutated field names are appended to
        ``rec['synthetic_fields']`` (semicolon-separated string).
    """
    r = dict(rec)
    existing_synth = [
        s.strip()
        for s in str(r.get("synthetic_fields") or "").split(";")
        if s.strip()
    ]
    synth: list[str] = list(existing_synth)

    imo = str(r.get("imo") or "0000000")
    flag = r.get("flag")
    vt   = r.get("vessel_type")
    vtk  = _vessel_type_key(vt)

    # ── VESSEL_NAME ───────────────────────────────────────────────────────────
    if _is_empty(r.get("vessel_name")):
        r["vessel_name"] = _synth_vessel_name(imo, vt)
        synth.append("vessel_name")

    # ── MMSI ─────────────────────────────────────────────────────────────────
    if _is_empty(r.get("mmsi")):
        r["mmsi"] = _synth_mmsi(imo, flag)
        synth.append("mmsi")

    # ── CALL_SIGN ────────────────────────────────────────────────────────────
    if _is_empty(r.get("call_sign")):
        r["call_sign"] = _synth_call_sign(imo, flag)
        synth.append("call_sign")

    # ── DEPARTURE_PORT ────────────────────────────────────────────────────────
    if _is_empty(r.get("departure_port")):
        r["departure_port"] = _synth_port(imo, "DEPARTURE_PORT", vtk, "export")
        synth.append("departure_port")

    # ── DESTINATION_PORT ─────────────────────────────────────────────────────
    if _is_empty(r.get("destination_port")):
        r["destination_port"] = _synth_port(imo, "DESTINATION_PORT", vtk, "import")
        synth.append("destination_port")

    # ── ARRIVAL_DATETIME ─────────────────────────────────────────────────────
    if _is_empty(r.get("arrival_datetime")):
        r["arrival_datetime"] = _synth_eta(imo)
        synth.append("arrival_datetime")

    # ── GT ↔ DWT cross-formula ───────────────────────────────────────────────
    dwt = r.get("dwt_tons")
    gt  = r.get("gt")
    if _is_empty(gt) and not _is_empty(dwt):
        try:
            r["gt"] = _synth_gt_from_dwt(float(dwt), vtk)
            synth.append("gt")
        except (TypeError, ValueError):
            pass
    elif _is_empty(dwt) and not _is_empty(gt):
        try:
            r["dwt_tons"] = _synth_dwt_from_gt(float(gt), vtk)
            synth.append("dwt_tons")
        except (TypeError, ValueError):
            pass
    elif _is_empty(dwt) and _is_empty(gt):
        # Both missing — generate from type-based distribution
        r["dwt_tons"] = _synth_dwt(imo, vt)
        synth.append("dwt_tons")
        r["gt"] = _synth_gt_standalone(imo, vt)
        synth.append("gt")

    # ── BUILT_YEAR / AGE_YEARS fallback ─────────────────────────────────────
    if _is_empty(r.get("built_year")):
        yr = _synth_built_year(imo, vt)
        r["built_year"] = yr
        synth.append("built_year")
        if _is_empty(r.get("age_years")):
            r["age_years"] = 2026 - yr
            synth.append("age_years")
    elif _is_empty(r.get("age_years")):
        try:
            r["age_years"] = 2026 - int(r["built_year"])
        except (TypeError, ValueError):
            pass

    # ── SPEED_KNOTS fallback ─────────────────────────────────────────────────
    if _is_empty(r.get("speed_knots")):
        r["speed_knots"] = {"lng": 14.0, "lpg": 13.0,
                            "crude": 12.0, "tanker": 11.5}.get(vtk, 10.0)
        synth.append("speed_knots")

    # ── Persist synthetic field list ─────────────────────────────────────────
    r["synthetic_fields"] = "; ".join(synth) if synth else ""
    return r
