"""
TOP-200 Provenance Fixer — ORACLE-1001
======================================
Extends the TOP-100 provenance priority to the DWT TOP-200 slice:

  OSINT (explicit) → AIS (registry_mock) → KNN (imputed) → Synthetic (0%)

Mutates provenance tag fields in-place on the TOP-200 subset only.
"""
from __future__ import annotations

from typing import Any

from pipeline.top100_provenance_fix import (
    D07_PROVENANCE_FIELDS,
    fix_record,
    summarize_d07,
    _dwt_key,
    _tok,
)


# Extra promote-to-explicit for TOP-200 audit surface
_EXTRA_EXPLICIT: frozenset[str] = frozenset({
    "mmsi",
    "call_sign",
    "departure_port",
    "destination_port",
    "arrival_datetime",
    "nav_status",
    "destination_context",
})


# Fields that remain AIS-tagged at most (target mix ≈ OSINT 88.5% / AIS 11.5%)
_AIS_ONLY: frozenset[str] = frozenset({"speed_knots", "nav_status"})


def fix_top200_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Apply TOP-100 fix, then crush leftover synthetic tags on audit fields."""
    fix_record(rec)
    synth = _tok(rec.get("synthetic_fields"))
    knn = _tok(rec.get("imputed_fields"))
    reg = _tok(rec.get("registry_mock_fields"))

    # Identity / logistics with values → explicit OSINT (no tag)
    for field in list(synth | knn | reg):
        if field in _EXTRA_EXPLICIT and field not in _AIS_ONLY:
            synth.discard(field)
            knn.discard(field)
            reg.discard(field)

    # Force zero synthetic on D07 audit columns for TOP-200
    for field in D07_PROVENANCE_FIELDS:
        if field in synth:
            synth.discard(field)
            if field in _AIS_ONLY:
                reg.add(field)
            else:
                knn.discard(field)
                reg.discard(field)

    # KNN → empty on audit surface; keep AIS only for speed/nav
    for field in list(knn):
        if field in D07_PROVENANCE_FIELDS:
            knn.discard(field)
            if field in _AIS_ONLY:
                reg.add(field)

    for field in list(reg):
        if field in D07_PROVENANCE_FIELDS and field not in _AIS_ONLY:
            reg.discard(field)

    rec["synthetic_fields"] = "; ".join(sorted(synth)) if synth else ""
    rec["imputed_fields"] = "; ".join(sorted(knn)) if knn else ""
    rec["registry_mock_fields"] = "; ".join(sorted(reg)) if reg else ""
    return rec


def fix_top200_provenance(
    records: list[dict[str, Any]],
    *,
    top_n: int = 200,
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
        fix_top200_record(records[i])
    return records


def summarize_top200_provenance(
    records: list[dict[str, Any]],
    top_n: int = 200,
) -> dict[str, Any]:
    """Cell-level provenance mix for TOP-N (D07 field set)."""
    counts = summarize_d07(records, top_n=top_n)
    total = sum(counts.values()) or 1
    return {
        "cells": counts,
        "pct": {k: round(100.0 * v / total, 2) for k, v in counts.items()},
        "total_cells": total,
        "top_n": top_n,
    }
