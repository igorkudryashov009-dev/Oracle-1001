"""
External Registry Emulator — ORACLE-1001  (Phase 3 enrichment)
================================================================
Simulates lookups against closed maritime databases:
  • AIS Hub          → MMSI, NAV_STATUS, SPEED, ETA
  • Lloyd's / Equasis → CALL_SIGN, IMO cross-reference
  • Port State Control → DEPARTURE_PORT, DESTINATION_PORT, ARRIVAL_DATETIME

DESIGN
------
Phase 1 (synthetic_enrichment)  — deterministic hash-based gap-fill
Phase 2 (profile_imputer)        — fleet-cluster KNN for dimensions
Phase 3 (THIS MODULE)            — registry-semantic re-tagging + final safety net

What Phase 3 does:
  1. Re-classifies Phase-1 synthetic fields that are conceptually
     "registry-sourced" (MMSI, CALL_SIGN, PORTS, ETA, NAV_STATUS)
     by moving them from ``synthetic_fields`` → ``registry_mock_fields``.
     This gives the UI a separate 🌐 AIS/Equasis badge for those values.
  2. Runs ``mock_lookup(imo, field)`` as a last-resort safety net for any
     field that is still empty after Phase 1 + Phase 2 (should be 0 on a
     healthy run, but guards against future schema additions).
  3. Guarantees 100 % fill rate is preserved on every run.

All registry-mocked fields are tracked in ``registry_mock_fields``
(semicolon-separated string).
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
_ALPHANUM = _ALPHA + "0123456789"

# ── Fields that semantically originate from REGISTRIES / AIS ─────────────────
# Phase 1 may have filled these deterministically; Phase 3 re-tags them.
REGISTRY_OWNED_FIELDS: frozenset[str] = frozenset({
    "mmsi",
    "call_sign",
    "departure_port",
    "destination_port",
    "arrival_datetime",
    "nav_status",
})

# ── NAV_STATUS pool (AIS standard values) ────────────────────────────────────
_NAV_STATUS_POOL = [
    "Underway using Engine",
    "At Anchor",
    "Moored",
    "Underway using Engine",   # weighted twice (most common)
    "Underway using Engine",
    "At Anchor",
    "Restricted Manoeuvrability",
    "Not Under Command",
    "Engaged in Fishing",
    "Constrained by her Draught",
]

# ── ITU tables (MID + call-sign prefix) ──────────────────────────────────────
# Mirrors pipeline/synthetic_enrichment.py for safety-net use
_ITU_TABLE: list[tuple[tuple[str, ...], list[int], list[str]]] = [
    (("panama", "панам"),                [351,352,353,370,371,373], ["3E","3F","H3","HP","HQ"]),
    (("liberia", "либери"),              [636],                     ["5L","A8","EL"]),
    (("marshall", "маршалл"),            [538],                     ["V7"]),
    (("bahamas", "багам"),               [308,309],                 ["C6"]),
    (("singapore", "сингапур"),          [563,564,565,566],         ["9V"]),
    (("hong kong", "гонконг"),           [477],                     ["VR"]),
    (("cyprus", "кипр"),                 [209,210,212],             ["5B"]),
    (("malta", "мальта"),                [215,229,248],             ["9H"]),
    (("norway", "норвег"),               [257,258,259],             ["LA","LB","LM"]),
    (("greece", "греци"),                [237,239,240],             ["SV","J4"]),
    (("china", "китай"),                 [412,413,414],             ["BO","BY","XU"]),
    (("japan", "япони"),                 [431,432,433],             ["JA","JH","JL"]),
    (("korea", "корея"),                 [440,441],                 ["DS","HL"]),
    (("russia", "росси"),                [273],                     ["UA","UB","UC","RA"]),
    (("comoros", "коморск"),             [616,620],                 ["D6"]),
    (("gabon", "габон"),                 [626],                     ["TR"]),
    (("cameroon", "камерун"),            [613],                     ["TJ"]),
    (("palau", "палау"),                 [511],                     ["T8"]),
    (("togo", "того"),                   [671],                     ["5V"]),
    (("tuvalu", "тувалу"),               [572],                     ["T2"]),
    (("cambodia", "камбодж"),            [514,515],                 ["XU"]),
    (("uk", "united kingdom","великобр"),[232,233,234,235],         ["G","2E","M"]),
    (("usa","united states","сша"),      [338,366,367,368,369],     ["WA","WB","KC"]),
    (("india", "инди"),                  [419],                     ["VT","AT"]),
    (("uae", "emirat","оаэ"),            [470,471],                 ["A6"]),
    (("saudi", "саудов"),                [403],                     ["HZ"]),
    (("iran", "иран"),                   [422],                     ["EP"]),
    (("qatar", "катар"),                 [466],                     ["A7"]),
    (("turkey", "турци"),                [271],                     ["TC","YM"]),
    (("france", "франци"),               [226,228],                 ["FG","FN"]),
    (("germany", "германи"),             [211,218],                 ["DA","DC","DL"]),
    (("italy", "итали"),                 [247],                     ["IB","IK"]),
    (("spain", "испани"),                [224],                     ["EA","EC"]),
    (("denmark", "дании"),               [219,220],                 ["OU","OZ"]),
    (("netherlands", "нидерланд"),       [244,245,246],             ["PA","PB","PD"]),
    (("mali", "мали"),                   [625],                     ["TZ"]),
    (("belize", "белиз"),                [312],                     ["V3"]),
    (("eswatini","свазиленд"),           [629],                     ["3DA"]),
    (("vanuatu","вануату"),              [576],                     ["YJ"]),
    (("niue","ниуэ"),                    [542],                     ["ZM"]),
]
_ITU_DEFAULT = ([636], ["5L"])

# ── Port pools by vessel type ─────────────────────────────────────────────────
_PORT_POOLS: dict[str, dict[str, list[str]]] = {
    "lng": {
        "export": [
            "Ras Laffan LNG, Qatar (QA RLF)",
            "Sabine Pass LNG, USA (US SAB)",
            "Yamal LNG Terminal, Russia (RU SAB)",
            "Corpus Christi LNG, USA (US CRP)",
            "Bintulu LNG, Malaysia (MY BTU)",
            "Gladstone LNG, Australia (AU GLA)",
            "Freeport LNG, USA (US FPT)",
        ],
        "import": [
            "Incheon Regasification, South Korea (KR INC)",
            "Gate Terminal Rotterdam (NL RTM)",
            "Yokohama LNG, Japan (JP YOK)",
            "Tianjin LNG, China (CN TJN)",
            "Grain LNG, UK (GB GRA)",
            "Barcelona Regas, Spain (ES BCN)",
            "Zeebrugge LNG, Belgium (BE ZEE)",
        ],
    },
    "lpg": {
        "export": [
            "Jubail Petrochemical Terminal, Saudi Arabia (SA JUB)",
            "Ras Laffan, Qatar (QA RLF)",
            "Marcus Hook, USA (US MHK)",
            "Houston Ship Channel, USA (US HOU)",
            "Yeosu LPG Terminal, South Korea (KR YOS)",
            "Ain Sukhna, Egypt (EG ASU)",
        ],
        "import": [
            "Chiba LPG Terminal, Japan (JP CHB)",
            "Zhangpu, China (CN ZAP)",
            "Ningbo LPG, China (CN NGB)",
            "Ulsan, South Korea (KR USN)",
            "Rotterdam LPG, Netherlands (NL RTM)",
            "Huizhou, China (CN HUI)",
        ],
    },
    "crude": {
        "export": [
            "Ras Tanura, Saudi Arabia (SA RAT)",
            "Kharg Island, Iran (IR KHG)",
            "Novorossiysk, Russia (RU NVS)",
            "Basrah Oil Terminal, Iraq (IQ BSR)",
            "Bonny Terminal, Nigeria (NG BON)",
            "Ceyhan, Turkey (TR CEY)",
        ],
        "import": [
            "Rotterdam Crude, Netherlands (NL RTM)",
            "Qingdao, China (CN TAO)",
            "Ulsan, South Korea (KR USN)",
            "Chiba, Japan (JP CHB)",
            "Houston, USA (US HOU)",
            "Trieste, Italy (IT TRS)",
        ],
    },
    "generic": {
        "export": [
            "Singapore Strait Terminal (SG SIN)",
            "Fujairah, UAE (AE FUJ)",
            "Piraeus, Greece (GR PIR)",
            "Houston, USA (US HOU)",
            "Rotterdam, Netherlands (NL RTM)",
        ],
        "import": [
            "Singapore (SG SIN)",
            "Shanghai, China (CN SHA)",
            "Incheon, South Korea (KR INC)",
            "Yokohama, Japan (JP YOK)",
            "Rotterdam, Netherlands (NL RTM)",
        ],
    },
}

_MONTHS_RU = [
    "января","февраля","марта","апреля","мая","июня",
    "июля","августа","сентября","октября","ноября","декабря",
]


# ── Helpers ───────────────────────────────────────────────────────────────────
def _rng(imo: str, field: str) -> random.Random:
    seed = int(hashlib.md5(f"REG:{imo}:{field}".encode()).hexdigest(), 16) % (2**32)
    return random.Random(seed)


def _is_empty(val: Any) -> bool:
    if val is None: return True
    return str(val).strip().lower() in {"", "—", "-", "none", "не извлечено", "nan", "n/a"}


def _vtype_key(vessel_type: Optional[str]) -> str:
    vt = (vessel_type or "").upper()
    if "LNG" in vt:                       return "lng"
    if "LPG" in vt or "VLGC" in vt:      return "lpg"
    if "ГАЗО" in vt:                      return "lpg"
    if any(k in vt for k in ("VLCC","CRUDE","SUEZMAX","AFRAMAX","TANKER","ТАНКЕР")):
        return "crude"
    return "generic"


def _get_itu(flag: Optional[str]) -> tuple[list[int], list[str]]:
    fl = (flag or "").lower()
    for keywords, mids, cs_list in _ITU_TABLE:
        if any(kw in fl for kw in keywords):
            return mids, cs_list
    return _ITU_DEFAULT


# ── ExternalRegistryEnricher ──────────────────────────────────────────────────
class ExternalRegistryEnricher:
    """
    Simulates external registry API calls (AIS Hub, Lloyd's, Equasis,
    Port State Control records) to fill or re-tag registry-owned fields.

    Usage::

        enricher = ExternalRegistryEnricher()
        record   = enricher.enrich(record)
        # or individual lookup:
        value    = enricher.mock_lookup("9780005", "call_sign", flag="Hong Kong")
    """

    # ── Public API ────────────────────────────────────────────────────────────
    def mock_lookup(
        self,
        imo: str,
        field_name: str,
        flag: Optional[str] = None,
        vessel_type: Optional[str] = None,
        speed_knots: Optional[float] = None,
    ) -> Optional[str]:
        """
        Simulate a registry API call for a single field.

        Returns the looked-up / generated value, or None if unsupported.
        """
        fn = field_name.lower()
        r  = _rng(imo, fn)

        if fn == "mmsi":
            mids, _ = _get_itu(flag)
            mid     = r.choice(mids)
            return f"{mid}{r.randint(0, 999_999):06d}"

        if fn == "call_sign":
            _, cs_list = _get_itu(flag)
            prefix = r.choice(cs_list)
            need   = max(2, 5 - len(prefix))
            suffix = "".join(r.choice(_ALPHANUM) for _ in range(need))
            return (prefix + suffix)[:7]

        if fn in ("departure_port", "destination_port"):
            vtk  = _vtype_key(vessel_type)
            pool_key = "export" if fn == "departure_port" else "import"
            pool = _PORT_POOLS.get(vtk, _PORT_POOLS["generic"])[pool_key]
            return r.choice(pool)

        if fn == "arrival_datetime":
            offset = r.randint(-7, 60)
            hour   = r.randint(0, 23)
            minute = r.choice([0, 15, 30, 45])
            dt = _REF_DATE + timedelta(days=offset)
            return f"{dt.day} {_MONTHS_RU[dt.month-1]} {dt.year} г., {hour:02d}:{minute:02d} UTC"

        if fn == "nav_status":
            return r.choice(_NAV_STATUS_POOL)

        return None  # field not handled by this registry

    def enrich(self, rec: dict[str, Any]) -> dict[str, Any]:
        """
        Phase 3 enrichment:
          1. Re-tag registry-owned fields from synthetic_fields → registry_mock_fields.
          2. Fill any still-empty registry fields (safety net).
        """
        r = dict(rec)
        imo        = str(r.get("imo") or "0000000")
        flag       = r.get("flag")
        vt         = r.get("vessel_type")
        spd        = r.get("speed_knots")

        # Parse existing provenance lists
        synth_set: set[str] = {
            s.strip().lower()
            for s in str(r.get("synthetic_fields") or "").split(";")
            if s.strip()
        }
        reg_mock: list[str] = [
            s.strip()
            for s in str(r.get("registry_mock_fields") or "").split(";")
            if s.strip()
        ]

        # ── Step 1: re-tag registry-owned fields from synthetic → registry_mock ──
        for field in REGISTRY_OWNED_FIELDS:
            if field in synth_set:
                synth_set.discard(field)
                if field not in reg_mock:
                    reg_mock.append(field)

        # ── Step 2: safety-net fill for any still-empty registry fields ───────────
        for field in REGISTRY_OWNED_FIELDS:
            if _is_empty(r.get(field)):
                val = self.mock_lookup(
                    imo, field,
                    flag=flag, vessel_type=vt, speed_knots=spd,
                )
                if val is not None:
                    r[field]   = val
                    if field not in reg_mock:
                        reg_mock.append(field)

        # ── Step 3: persist updated provenance ────────────────────────────────────
        r["synthetic_fields"]    = "; ".join(sorted(synth_set)) if synth_set else ""
        r["registry_mock_fields"] = "; ".join(reg_mock) if reg_mock else ""
        return r

    def enrich_all(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply Phase 3 enrichment to every vessel in the fleet."""
        return [self.enrich(rec) for rec in records]
