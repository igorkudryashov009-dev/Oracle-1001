"""Vessel record schema and IMO checksum validation."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


def imo_checksum_valid(imo: str) -> bool:
    """Validate IMO number using the standard check-digit algorithm.

    d = list of 7 digits; valid if sum(d[i] * (7-i) for i in 0..5) % 10 == d[6]
    """
    digits = "".join(c for c in str(imo) if c.isdigit())
    if len(digits) != 7:
        return False
    d = [int(c) for c in digits]
    checksum = sum(d[i] * (7 - i) for i in range(6)) % 10
    return checksum == d[6]


class VesselRecord(BaseModel):
    """Normalized vessel record from OSINT block."""

    imo: str
    imo_valid: Optional[bool] = None
    imo_format_error: Optional[bool] = None
    imo_from_text: Optional[str] = None
    imo_source_match: Optional[bool] = None

    identity_spoofing_suspected_imo: Optional[str] = None
    identity_spoofing_note: Optional[str] = None

    vessel_category: Optional[str] = Field(
        default=None,
        description='One of: "vessel" | "non_vessel" | "unknown"',
    )

    vessel_name: Optional[str] = None
    mmsi: Optional[str] = None
    call_sign: Optional[str] = None
    vessel_type: Optional[str] = None
    built_year: Optional[int] = None
    age_years: Optional[int] = None
    flag: Optional[str] = None
    dwt_tons: Optional[float] = None
    gt: Optional[float] = None
    loa_m: Optional[float] = None
    beam_m: Optional[float] = None
    draft_m: Optional[float] = None
    draft_note: Optional[str] = None
    nav_status: Optional[str] = None
    speed_knots: Optional[float] = None
    asset_status: Optional[str] = None
    compliance_risk_level: Optional[str] = None
    destination_port: Optional[str] = None
    destination_context: Optional[str] = None
    departure_port: Optional[str] = None
    departure_datetime_utc: Optional[str] = None
    arrival_datetime: Optional[str] = None
    source_confidence: Optional[str] = Field(
        default=None,
        description=(
            'One of: "parsed" | "needs_review" | "imo_mismatch" | '
            '"identity_conflict_flagged" | "non_vessel"'
        ),
    )
    raw_text: Optional[str] = None


FIELD_ORDER = [
    "imo",
    "imo_valid",
    "imo_format_error",
    "imo_from_text",
    "imo_source_match",
    "identity_spoofing_suspected_imo",
    "identity_spoofing_note",
    "vessel_category",
    "vessel_name",
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
    "draft_note",
    "nav_status",
    "speed_knots",
    "asset_status",
    "compliance_risk_level",
    "destination_port",
    "destination_context",
    "departure_port",
    "departure_datetime_utc",
    "arrival_datetime",
    "source_confidence",
    "raw_text",
]

# Fields counted toward "successfully extracted" threshold
SIGNIFICANT_FIELDS = [
    "vessel_name",
    "mmsi",
    "call_sign",
    "vessel_type",
    "built_year",
    "flag",
    "dwt_tons",
    "gt",
    "loa_m",
    "beam_m",
    "draft_m",
    "nav_status",
    "asset_status",
    "compliance_risk_level",
    "destination_port",
]

NON_VESSEL_MARKERS = (
    "aid to navigation",
    "aton",
    "буй",
    "навигационный знак",
    "маяк",
)
