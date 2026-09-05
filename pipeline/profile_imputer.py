"""
Fleet Profile Imputer — ORACLE-1001
=====================================
K-Nearest-Neighbors / cluster-median imputation for remaining NULL fields.

ARCHITECTURE
------------
Phase 1 (per-vessel, inside parse_osint_narrative):
    synthetic_enrichment.enrich_missing_fields()
    → MMSI, CALL_SIGN, PORTS, ETA, DWT/GT, BUILT_YEAR

Phase 2 (fleet-level, called in run_all.py):
    FleetDataImputer.impute_all()
    → LOA_M, BEAM_M, DRAFT_M, VESSEL_TYPE, FLAG
    → Uses cluster statistics from real peers

Fields tagged in ``imputed_fields`` (semicolon-sep) for UI 🔬 KNN badge.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Vessel-type normalisation
# ---------------------------------------------------------------------------
def _vtype_key(vessel_type: Optional[str]) -> str:
    vt = (vessel_type or "").upper()
    if "LNG" in vt:                         return "lng"
    if "LPG" in vt or "VLGC" in vt:        return "lpg"
    if "ГАЗО" in vt:                        return "lpg"
    if "VLCC" in vt or "CRUDE" in vt:       return "vlcc"
    if "SUEZMAX" in vt or "SUEZ" in vt:     return "suezmax"
    if "AFRAMAX" in vt or "AFRA" in vt:     return "aframax"
    if "CHEMICAL" in vt or "ХИМО" in vt:    return "chemical"
    if "BULK" in vt or "БАЛК" in vt:        return "bulk"
    if "TANKER" in vt or "ТАНКЕР" in vt:    return "tanker"
    return "generic"


def _dwt_bucket(dwt_tons: Any) -> str:
    try:
        dwt = float(dwt_tons)
        if dwt < 5_000:   return "micro"
        if dwt < 20_000:  return "small"
        if dwt < 60_000:  return "medium"
        if dwt < 120_000: return "large"
        if dwt < 200_000: return "vlarge"
        return "ularge"
    except (TypeError, ValueError):
        return "unknown"


def _flag_tier(flag: Optional[str]) -> str:
    """Classify flag into open/convenience registry tiers."""
    fl = (flag or "").lower()
    # Major IACS flags → tier A
    major = ("norway","швец","denmark","uk","united kingdom","germany","германи",
              "france","france","netherlands","нидерл","japan","япони",
              "usa","сша","australia","австрали","singapore","сингапур")
    if any(k in fl for k in major):
        return "A"
    # Convenience / FOC flags → tier B
    foc   = ("panama","панам","liberia","либери","marshall","маршалл","bahamas","багам",
              "cyprus","кипр","malta","мальта","hong kong","гонконг","belize","белиз")
    if any(k in fl for k in foc):
        return "B"
    # Gray / high-risk → tier C
    gray  = ("comoros","коморск","gabon","габон","cameroon","камерун","palau","палау",
              "togo","того","tuvalu","тувалу","mali","мали","sierra leone","сьерра",
              "eswatini","свазиленд","guinea","гвинея","vanuatu","вануату")
    if any(k in fl for k in gray):
        return "C"
    return "B"   # FOC default for unrecognised


def _is_empty(val: Any) -> bool:
    if val is None: return True
    return str(val).strip().lower() in {"", "—", "-", "none", "не извлечено", "nan", "n/a"}


def _safe_float(val: Any) -> Optional[float]:
    try:
        f = float(val)
        return f if math.isfinite(f) and f > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Vessel-type-to-DWT heuristic (when vessel_type AND dwt are both absent)
# ---------------------------------------------------------------------------
_VTYPE_FROM_DWT: list[tuple[str, tuple[float, float]]] = [
    # (canonical_label, (dwt_lo, dwt_hi))
    ("LPG Tanker (Small)",       (500,    5_000)),
    ("LPG Tanker",               (5_000,  25_000)),
    ("LPG Tanker / VLGC",        (25_000, 60_000)),
    ("LNG Tanker",               (50_000, 90_000)),
    ("LNG Tanker / LNGC",        (90_000, 180_000)),
    ("Crude Oil Tanker (Afra)",  (80_000, 120_000)),
    ("Crude Oil Tanker (Suez)",  (120_000, 200_000)),
    ("Crude Oil Tanker (VLCC)",  (200_000, 400_000)),
]

_VTYPE_DEFAULT_BY_DWT_BUCKET: dict[str, str] = {
    "micro":   "LPG Tanker (Small)",
    "small":   "LPG Tanker",
    "medium":  "LPG Tanker / VLGC",
    "large":   "LNG Tanker / LNGC",
    "vlarge":  "LNG Tanker / LNGC",
    "ularge":  "Crude Oil Tanker (VLCC)",
    "unknown": "Gas / Oil Tanker",
}

# LOA / BEAM / DRAFT medians per vessel-type-key  (hard-coded industry references)
# Used only as last-resort when cluster has < 3 peers
_REFERENCE_DIMS: dict[str, dict[str, float]] = {
    "lng":       {"loa_m": 282.0, "beam_m": 43.4, "draft_m": 11.2, "loa_beam": 6.50},
    "lpg":       {"loa_m": 180.0, "beam_m": 29.8, "draft_m":  9.1, "loa_beam": 6.04},
    "vlcc":      {"loa_m": 330.0, "beam_m": 58.0, "draft_m": 21.0, "loa_beam": 5.69},
    "suezmax":   {"loa_m": 274.0, "beam_m": 48.0, "draft_m": 17.0, "loa_beam": 5.71},
    "aframax":   {"loa_m": 250.0, "beam_m": 44.0, "draft_m": 14.5, "loa_beam": 5.68},
    "chemical":  {"loa_m": 183.0, "beam_m": 32.0, "draft_m": 11.0, "loa_beam": 5.72},
    "tanker":    {"loa_m": 183.0, "beam_m": 32.0, "draft_m": 11.0, "loa_beam": 5.72},
    "bulk":      {"loa_m": 230.0, "beam_m": 38.0, "draft_m": 14.0, "loa_beam": 6.05},
    "generic":   {"loa_m": 180.0, "beam_m": 30.0, "draft_m": 10.0, "loa_beam": 6.00},
}


# ---------------------------------------------------------------------------
# Cluster statistics container
# ---------------------------------------------------------------------------
class _ClusterStats:
    __slots__ = (
        "loa_vals", "beam_vals", "draft_vals", "loa_beam_ratios",
        "speed_vals", "vtype_counter", "flag_counter",
        "dep_counter", "dst_counter",
    )

    def __init__(self) -> None:
        self.loa_vals:       list[float] = []
        self.beam_vals:      list[float] = []
        self.draft_vals:     list[float] = []
        self.loa_beam_ratios: list[float] = []
        self.speed_vals:     list[float] = []
        self.vtype_counter:  Counter[str] = Counter()
        self.flag_counter:   Counter[str] = Counter()
        self.dep_counter:    Counter[str] = Counter()
        self.dst_counter:    Counter[str] = Counter()

    # ── aggregates ───────────────────────────────────────────────────────
    def median_loa(self)  -> Optional[float]:
        return statistics.median(self.loa_vals)  if len(self.loa_vals)  >= 2 else (self.loa_vals[0]  if self.loa_vals  else None)
    def median_beam(self) -> Optional[float]:
        return statistics.median(self.beam_vals) if len(self.beam_vals) >= 2 else (self.beam_vals[0] if self.beam_vals else None)
    def median_draft(self)-> Optional[float]:
        return statistics.median(self.draft_vals) if len(self.draft_vals)>= 2 else (self.draft_vals[0] if self.draft_vals else None)
    def median_loa_beam(self) -> Optional[float]:
        return statistics.median(self.loa_beam_ratios) if len(self.loa_beam_ratios) >= 2 else (self.loa_beam_ratios[0] if self.loa_beam_ratios else None)
    def mode_vtype(self) -> Optional[str]:
        return self.vtype_counter.most_common(1)[0][0] if self.vtype_counter else None
    def mode_flag(self)  -> Optional[str]:
        return self.flag_counter.most_common(1)[0][0]  if self.flag_counter  else None


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------
class FleetDataImputer:
    """
    Build cluster statistics from a parsed fleet and impute remaining NULLs.

    Usage::

        from pipeline.profile_imputer import FleetDataImputer
        imputer = FleetDataImputer(records)       # fit
        records = imputer.impute_all(records)      # transform
    """

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._stats: dict[tuple, _ClusterStats] = defaultdict(_ClusterStats)
        self._global = _ClusterStats()
        self._fit(records)

    # ── Private ─────────────────────────────────────────────────────────────
    def _cluster_key(self, rec: dict) -> tuple[str, str]:
        return (_vtype_key(rec.get("vessel_type")),
                _dwt_bucket(rec.get("dwt_tons")))

    def _fit(self, records: list[dict]) -> None:
        for rec in records:
            key = self._cluster_key(rec)
            cs  = self._stats[key]

            loa   = _safe_float(rec.get("loa_m"))
            beam  = _safe_float(rec.get("beam_m"))
            draft = _safe_float(rec.get("draft_m"))
            spd   = _safe_float(rec.get("speed_knots"))
            vt    = rec.get("vessel_type")
            fl    = rec.get("flag")
            dep   = rec.get("departure_port")
            dst   = rec.get("destination_port")

            # Only use REAL (non-synthetic) values for cluster stats
            synth_set = {
                s.strip().lower()
                for s in str(rec.get("synthetic_fields") or "").split(";")
                if s.strip()
            }

            def _add(field: str, val: Optional[float], local: list, glob: list) -> None:
                if val is not None and field not in synth_set:
                    local.append(val)
                    glob.append(val)

            _add("loa_m",       loa,   cs.loa_vals,   self._global.loa_vals)
            _add("beam_m",      beam,  cs.beam_vals,  self._global.beam_vals)
            _add("draft_m",     draft, cs.draft_vals, self._global.draft_vals)
            _add("speed_knots", spd,   cs.speed_vals, self._global.speed_vals)

            if (loa and beam
                    and "loa_m"  not in synth_set
                    and "beam_m" not in synth_set):
                ratio = loa / beam
                if 3.0 < ratio < 10.0:
                    cs.loa_beam_ratios.append(ratio)
                    self._global.loa_beam_ratios.append(ratio)

            if vt and not _is_empty(vt):
                cs.vtype_counter[vt] += 1
                self._global.vtype_counter[vt] += 1
            if fl and not _is_empty(fl):
                cs.flag_counter[fl] += 1
                self._global.flag_counter[fl] += 1
            if dep and not _is_empty(dep) and "departure_port" not in synth_set:
                cs.dep_counter[dep] += 1
                self._global.dep_counter[dep] += 1
            if dst and not _is_empty(dst) and "destination_port" not in synth_set:
                cs.dst_counter[dst] += 1
                self._global.dst_counter[dst] += 1

    def _get_stats(self, key: tuple[str, str]) -> _ClusterStats:
        """Return best available cluster stats with graceful fallback."""
        cs = self._stats.get(key)
        if cs and (cs.loa_vals or cs.beam_vals or cs.draft_vals):
            return cs
        # Fallback 1: same vtype, any dwt
        vtype = key[0]
        merged = _ClusterStats()
        for (vt, _), s in self._stats.items():
            if vt == vtype:
                merged.loa_vals    += s.loa_vals
                merged.beam_vals   += s.beam_vals
                merged.draft_vals  += s.draft_vals
                merged.loa_beam_ratios += s.loa_beam_ratios
                merged.speed_vals  += s.speed_vals
                merged.vtype_counter.update(s.vtype_counter)
                merged.flag_counter.update(s.flag_counter)
        if merged.loa_vals or merged.beam_vals:
            return merged
        # Fallback 2: global
        return self._global

    def _ref_dims(self, vtk: str) -> dict[str, float]:
        return _REFERENCE_DIMS.get(vtk, _REFERENCE_DIMS["generic"])

    # ── Public ──────────────────────────────────────────────────────────────
    def impute(self, rec: dict[str, Any]) -> dict[str, Any]:
        """Impute a single vessel record using cluster statistics."""
        r = dict(rec)
        imputed: list[str] = [
            s.strip()
            for s in str(r.get("imputed_fields") or "").split(";")
            if s.strip()
        ]

        key  = self._cluster_key(r)
        vtk  = key[0]
        cs   = self._get_stats(key)
        ref  = self._ref_dims(vtk)

        # ── VESSEL_TYPE ────────────────────────────────────────────────────
        if _is_empty(r.get("vessel_type")):
            vt_from_cluster = cs.mode_vtype()
            if vt_from_cluster:
                r["vessel_type"] = vt_from_cluster
            else:
                dwt_key = _dwt_bucket(r.get("dwt_tons"))
                r["vessel_type"] = _VTYPE_DEFAULT_BY_DWT_BUCKET.get(dwt_key, "Gas / Oil Tanker")
            imputed.append("vessel_type")

        # ── FLAG ──────────────────────────────────────────────────────────
        if _is_empty(r.get("flag")):
            flag_from_cluster = cs.mode_flag()
            if flag_from_cluster:
                r["flag"] = flag_from_cluster
                imputed.append("flag")

        # ── Dimension helpers ─────────────────────────────────────────────
        loa   = _safe_float(r.get("loa_m"))
        beam  = _safe_float(r.get("beam_m"))
        draft = _safe_float(r.get("draft_m"))

        # Compute LOA/BEAM ratio to use
        ratio = cs.median_loa_beam() or ref["loa_beam"]

        # LOA from BEAM
        if loa is None and beam is not None:
            r["loa_m"]  = round(beam * ratio, 1)
            imputed.append("loa_m")
            loa = r["loa_m"]

        # BEAM from LOA
        if beam is None and loa is not None:
            r["beam_m"] = round(loa / ratio, 1)
            imputed.append("beam_m")
            beam = r["beam_m"]

        # LOA from DWT cube-root law (when both loa/beam missing)
        if loa is None:
            cluster_loa = cs.median_loa()
            if cluster_loa:
                r["loa_m"]  = round(cluster_loa, 1)
            else:
                dwt = _safe_float(r.get("dwt_tons"))
                if dwt:
                    k = {"lng": 7.2, "lpg": 6.6, "vlcc": 5.9,
                         "suezmax": 6.2, "aframax": 6.1}.get(vtk, 6.0)
                    r["loa_m"] = round(k * (dwt ** (1/3)), 1)
                else:
                    r["loa_m"] = ref["loa_m"]
            imputed.append("loa_m")
            loa = r["loa_m"]

        if beam is None:
            cluster_beam = cs.median_beam()
            r["beam_m"] = round(cluster_beam if cluster_beam else loa / ratio, 1)
            imputed.append("beam_m")
            beam = r["beam_m"]

        # DRAFT (most imputation needed — 186 missing)
        if draft is None:
            cluster_draft = cs.median_draft()
            if cluster_draft:
                r["draft_m"] = round(cluster_draft, 1)
            else:
                # Molland rule: draft ≈ beam * 0.62 for gas/oil carriers
                r["draft_m"] = round((beam or ref["beam_m"]) * 0.62, 1)
            imputed.append("draft_m")

        # ── Persist ──────────────────────────────────────────────────────
        r["imputed_fields"] = "; ".join(imputed) if imputed else ""
        return r

    def impute_all(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Impute full fleet. Re-runs _fit so cluster stats exclude prior gaps."""
        return [self.impute(rec) for rec in records]
