"""Pydantic schema: 20 ТЗ columns for OSINT vessel dossiers."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

REFERENCE_YEAR = 2026
RiskLevel = Literal["LOW", "MID", "HIGH", "EXTREME"]


def strip_markdown(value: str) -> str:
    """Remove Markdown markers; keep digits, brackets, commas, colons."""
    s = str(value)
    s = s.replace("**", "").replace("__", "")
    s = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]*", "", s)
    s = s.replace("*", "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n+", " ", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def parse_numeric(value: Any) -> Optional[float]:
    """Extract clean float from messy OSINT fragments like '54 776 тонн'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = strip_markdown(str(value))
    s = s.replace("\xa0", " ").replace("~", "").replace("≈", "")
    # Prefer first number cluster (thousands with spaces/commas allowed)
    m = re.search(r"(\d{1,3}(?:[ \u00a0,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)", s)
    if not m:
        return None
    token = m.group(1).replace("\u00a0", " ").strip()
    compact = token.replace(" ", "")
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


def normalize_risk(value: Any) -> Optional[str]:
    if value is None:
        return None
    u = strip_markdown(str(value)).upper()
    if any(k in u for k in ("EXTREME", "ЭКСТРЕМАЛЬ", "GHOST", "ПРИЗРАК", "КРИТИЧЕСКИ")):
        return "EXTREME"
    if any(k in u for k in ("HIGH", "ВЫСОК", "СТРАТЕГ")):
        return "HIGH"
    if any(k in u for k in ("MID", "MEDIUM", "СРЕДН")):
        return "MID"
    if any(k in u for k in ("LOW", "НИЗК", "CLEAR", "ЧИСТ", "ЛЕГИТИМ")):
        return "LOW"
    if u in ("LOW", "MID", "HIGH", "EXTREME"):
        return u
    return None


def split_departure_location(value: str) -> tuple[str, Optional[str]]:
    """Split 'Последний порт / Локация: X (Y — отход DATE)' → (port, sail_date)."""
    s = strip_markdown(value)
    s = re.sub(
        r"^(?:Последний\s*порт\s*/\s*Локация|Пункт\s*отправления|Last\s*port)\s*[:：]\s*",
        "",
        s,
        flags=re.IGNORECASE,
    )
    sail = None
    m_sail = re.search(
        r"(?:—\s*)?отход\s+(\d{1,2}\s+\S+\s+\d{4}[^\)]*)",
        s,
        flags=re.IGNORECASE,
    )
    if m_sail:
        sail = m_sail.group(1).strip(" .;")
        s = s[: m_sail.start()].rstrip(" —–-")
    s = s.rstrip(".,; ")
    if s.count("(") > s.count(")"):
        s += ")"
    return s, sail


class VesselProfile20Cols(BaseModel):
    """Strict 20-column ТЗ profile for Oracle-1001 OSINT dossiers."""

    vessel_name: str = Field(description="Vessel name without markdown, e.g. G. PARAGON")
    imo: str = Field(description="7-digit IMO number as string")
    mmsi: str = Field(description="MMSI digits as string")
    call_sign: str = Field(description="Radio call sign, e.g. 3FTI4")
    vessel_type: str = Field(description="Full vessel type label")
    built_year: int = Field(description="Year built, e.g. 2013", ge=1900, le=2100)
    age_years: int = Field(description="Age in years = 2026 - built_year", ge=0)
    flag: str = Field(description="Flag state, e.g. Панама")
    dwt_tons: float = Field(description="Deadweight tons as pure number")
    gt: float = Field(description="Gross tonnage as pure number")
    loa_m: float = Field(description="Length overall meters")
    beam_m: float = Field(description="Beam meters")
    draft_m: float = Field(description="Current draught meters")
    nav_status: str = Field(description="Navigational status or transit description")
    speed_knots: float = Field(description="Speed in knots")
    destination_port: str = Field(description="Destination port name")
    destination_context: str = Field(description="Route/mission context, not the port name")
    departure_port: str = Field(description="Last port / departure location only")
    arrival_datetime: str = Field(description="ETA / arrival datetime string")
    compliance_risk_level: RiskLevel = Field(
        description='Risk enum only: "LOW" | "MID" | "HIGH" | "EXTREME"'
    )

    @field_validator(
        "vessel_name",
        "imo",
        "mmsi",
        "call_sign",
        "vessel_type",
        "flag",
        "nav_status",
        "destination_port",
        "destination_context",
        "departure_port",
        "arrival_datetime",
        mode="before",
    )
    @classmethod
    def clean_strings(cls, v: Any) -> Any:
        if v is None:
            return v
        if not isinstance(v, str):
            v = str(v)
        return strip_markdown(v)

    @field_validator("imo", "mmsi", mode="before")
    @classmethod
    def digits_only_ids(cls, v: Any) -> Any:
        if v is None:
            return v
        s = strip_markdown(str(v))
        digits = re.sub(r"\D", "", s)
        return digits or s

    @field_validator(
        "dwt_tons",
        "gt",
        "loa_m",
        "beam_m",
        "draft_m",
        "speed_knots",
        "built_year",
        "age_years",
        mode="before",
    )
    @classmethod
    def coerce_numbers(cls, v: Any) -> Any:
        if v is None or v == "":
            return v
        num = parse_numeric(v)
        return num if num is not None else v

    @field_validator("built_year", mode="after")
    @classmethod
    def built_year_int(cls, v: Any) -> int:
        return int(v)

    @field_validator("compliance_risk_level", mode="before")
    @classmethod
    def coerce_risk(cls, v: Any) -> Any:
        if v is None or v == "":
            return v
        return normalize_risk(v) or v

    @field_validator("departure_port", mode="before")
    @classmethod
    def split_departure(cls, v: Any) -> Any:
        if v is None or not isinstance(v, str):
            return v
        port, _sail = split_departure_location(v)
        return port

    @model_validator(mode="after")
    def compute_age_years(self) -> "VesselProfile20Cols":
        """Force age_years = REFERENCE_YEAR - built_year."""
        object.__setattr__(self, "age_years", int(REFERENCE_YEAR) - int(self.built_year))
        return self

    def to_tz_dict(self) -> dict[str, Any]:
        return self.model_dump()


# Column order for CSV / dashboards
TZ_COLUMNS: list[str] = list(VesselProfile20Cols.model_fields.keys())
