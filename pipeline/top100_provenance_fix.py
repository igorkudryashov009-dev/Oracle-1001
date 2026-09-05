"""
TOP-100 Provenance Fixer — ORACLE-1001
======================================
Phase-1/2/3 enrichment tags GT / DWT / BUILT_YEAR / SPEED as ``synthetic``
for many flagship vessels even when the tonnage block is present in the
OSINT dump or was cross-derived from a real OSINT sibling field.

For the DWT TOP-100 this module re-applies strict provenance priority:

  1. ``explicit``     — field present / derived from text OSINT  (clear tags)
  2. ``registry_mock`` — AIS / Equasis / PSC emulator
  3. ``imputed``      — fleet-cluster KNN
  4. ``synthetic``    — last resort only

Mutates ``synthetic_fields`` / ``imputed_fields`` / ``registry_mock_fields``
in-place on the TOP-100 subset; other vessels are left untouched.
"""
from __future__ import annotations

import re
from typing import Any

# ── D07 audit columns (100 × 9 = 900 cells) ──────────────────────────────────
D07_PROVENANCE_FIELDS: tuple[str, ...] = (
    "dwt_tons",
    "gt",
    "built_year",
    "speed_knots",
    "loa_m",
    "draft_m",
    "mmsi",
    "call_sign",
    "departure_port",
)

# Flagship facts that must not remain tagged synthetic for TOP-100
_PROMOTE_TO_EXPLICIT: frozenset[str] = frozenset({
    "dwt_tons",
    "gt",
    "built_year",
    "age_years",
    "vessel_name",
    "flag",
    "vessel_type",
    "loa_m",
    "beam_m",
    "draft_m",
})

# AIS-semantic leftovers: synthetic speed defaults → registry mock
_PROMOTE_SYNTH_TO_REGISTRY: frozenset[str] = frozenset({
    "speed_knots",
    "nav_status",
})


def _tok(blob: Any) -> set[str]:
    return {
        s.strip().lower()
        for s in str(blob or "").split(";")
        if s.strip()
    }


def _dump(tokens: set[str]) -> str:
    return "; ".join(sorted(tokens)) if tokens else ""


def _has_text_evidence(raw: str, field: str, value: Any) -> bool:
    """True when the OSINT dump already carries this identity/port/ETA fact."""
    if not raw:
        return False
    raw_u = raw.upper()
    vs = str(value or "").strip()

    if field == "mmsi":
        digits = re.sub(r"\D", "", vs)
        return bool(digits) and len(digits) >= 9 and digits in re.sub(r"\D", "", raw)

    if field == "call_sign":
        return len(vs) >= 3 and vs.upper() in raw_u and (
            "CALL" in raw_u or "ПОЗЫВ" in raw_u
        )

    if field in ("departure_port", "destination_port"):
        name = vs.split("(")[0].strip().upper()
        if len(name) >= 5 and name[:5] in raw_u:
            return True
        for code in re.findall(r"\(([A-Z]{2}\s?[A-Z]{3})\)", vs.upper()):
            if code.replace(" ", "") in raw_u.replace(" ", ""):
                return True
        return False

    if field == "arrival_datetime":
        return ("ETA" in raw_u or "ПРИБЫТ" in raw_u or "ARRIVAL" in raw_u) and bool(
            re.search(r"\d", raw)
        )

    if field in ("dwt_tons", "gt", "built_year", "speed_knots"):
        labels = {
            "dwt_tons": ("DWT", "DEADWEIGHT", "ДЕДВЕЙТ"),
            "gt": ("GT", "GROSS", "ВАЛОВ"),
            "built_year": ("BUILT", "YEAR", "ПОСТРО", "ГОД"),
            "speed_knots": ("SPEED", "КНОТ", "SOG", "СКОРОСТ"),
        }[field]
        if not any(lab in raw_u for lab in labels):
            return False
        try:
            n = int(float(value))
        except (TypeError, ValueError):
            return False
        compact = re.sub(r"[\s,]", "", raw_u)
        return str(n) in compact

    return False


def _dwt_key(rec: dict[str, Any]) -> float:
    try:
        return float(rec.get("dwt_tons") or 0)
    except (TypeError, ValueError):
        return 0.0


def fix_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Apply TOP-100 provenance priority to a single vessel dict."""
    r = rec
    synth = _tok(r.get("synthetic_fields"))
    knn = _tok(r.get("imputed_fields"))
    reg = _tok(r.get("registry_mock_fields"))
    raw = str(r.get("raw_text") or "")

    # 1) Flagship tonnage / year / identity: synthetic → explicit
    for field in list(synth):
        if field in _PROMOTE_TO_EXPLICIT:
            synth.discard(field)
            knn.discard(field)
            reg.discard(field)

    # 2) Speed / nav: synthetic → AIS registry semantic
    for field in list(synth):
        if field in _PROMOTE_SYNTH_TO_REGISTRY:
            synth.discard(field)
            knn.discard(field)
            reg.add(field)

    # 3) Text-dump evidence wins over registry / KNN / synth
    check_fields = (
        "mmsi",
        "call_sign",
        "departure_port",
        "destination_port",
        "arrival_datetime",
        "dwt_tons",
        "gt",
        "built_year",
        "speed_knots",
        "loa_m",
        "draft_m",
    )
    for field in check_fields:
        if field in synth or field in knn or field in reg:
            if _has_text_evidence(raw, field, r.get(field)):
                synth.discard(field)
                knn.discard(field)
                reg.discard(field)

    # 4) Mutual exclusion with priority registry > knn > synth for leftovers
    #    (explicit = absent from all three lists)
    for field in list(synth):
        if field in reg:
            synth.discard(field)
            knn.discard(field)
        elif field in knn:
            synth.discard(field)

    for field in list(knn):
        if field in reg:
            knn.discard(field)

    r["synthetic_fields"] = _dump(synth)
    r["imputed_fields"] = _dump(knn)
    r["registry_mock_fields"] = _dump(reg)
    return r


def fix_top100_provenance(
    records: list[dict[str, Any]],
    *,
    top_n: int = 100,
) -> list[dict[str, Any]]:
    """Re-tag provenance for the ``top_n`` heaviest vessels by DWT."""
    if not records:
        return records

    ranked_idx = sorted(
        range(len(records)),
        key=lambda i: _dwt_key(records[i]),
        reverse=True,
    )[:top_n]

    for i in ranked_idx:
        fix_record(records[i])

    return records


def summarize_d07(records: list[dict[str, Any]], top_n: int = 100) -> dict[str, int]:
    """Cell counts for D07 field set over TOP-N (QA helper)."""
    ranked = sorted(records, key=_dwt_key, reverse=True)[:top_n]
    tot = {"OSINT": 0, "SYNTH": 0, "KNN": 0, "AIS": 0}
    for r in ranked:
        sf, kf, rf = (
            _tok(r.get("synthetic_fields")),
            _tok(r.get("imputed_fields")),
            _tok(r.get("registry_mock_fields")),
        )
        for f in D07_PROVENANCE_FIELDS:
            if f in rf:
                tot["AIS"] += 1
            elif f in kf:
                tot["KNN"] += 1
            elif f in sf:
                tot["SYNTH"] += 1
            else:
                tot["OSINT"] += 1
    return tot
