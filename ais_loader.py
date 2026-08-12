"""Flexible AIS position-history loader.

Auto-detects columns by name aliases (case-insensitive). Supports .csv, .xlsx, .json.
Does NOT guess blindly — raises with found columns if required fields are missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

IMO_ALIASES = ("imo", "vessel_imo")
TIME_ALIASES = ("timestamp", "datetime", "date", "position_time", "utc_time")
LAT_ALIASES = ("lat", "latitude", "lat_deg")
LON_ALIASES = ("lon", "lng", "longitude", "lon_deg")
SPEED_ALIASES = ("speed", "sog", "speed_knots")
HEADING_ALIASES = ("heading", "cog")

REQUIRED = {
    "imo": IMO_ALIASES,
    "timestamp": TIME_ALIASES,
    "lat": LAT_ALIASES,
    "lon": LON_ALIASES,
}
OPTIONAL = {
    "speed_knots": SPEED_ALIASES,
    "heading": HEADING_ALIASES,
}


def _normalize_col(name: str) -> str:
    return str(name).strip().lower().replace(" ", "_")


def _resolve_column(columns: list[str], aliases: tuple[str, ...]) -> Optional[str]:
    normalized = {_normalize_col(c): c for c in columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


def _read_raw(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        # Skip leading comment lines starting with #
        return pd.read_csv(path, comment="#")
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path, engine="openpyxl")
    if suffix == ".json":
        return pd.read_json(path)
    raise ValueError(
        f"Unsupported AIS file format '{suffix}'. Expected .csv, .xlsx, or .json."
    )


def load_ais_history(path: str | Path) -> pd.DataFrame:
    """Load AIS history into canonical DataFrame.

    Returns columns: imo, timestamp (UTC datetime), lat, lon,
    speed_knots (nullable), heading (nullable).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"AIS source not found: {path}")

    raw = _read_raw(path)
    if raw.empty:
        raise ValueError(f"AIS file is empty: {path}")

    cols = list(raw.columns)
    mapping: dict[str, str] = {}
    missing: list[str] = []

    for canonical, aliases in REQUIRED.items():
        found = _resolve_column(cols, aliases)
        if found is None:
            missing.append(f"{canonical} (aliases: {', '.join(aliases)})")
        else:
            mapping[canonical] = found

    if missing:
        raise ValueError(
            "Cannot map required AIS columns.\n"
            f"  Missing: {missing}\n"
            f"  Found columns: {cols}\n"
            "Do not guess — confirm mapping before re-running."
        )

    for canonical, aliases in OPTIONAL.items():
        found = _resolve_column(cols, aliases)
        if found is not None:
            mapping[canonical] = found

    out = pd.DataFrame()
    out["imo"] = (
        raw[mapping["imo"]]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.strip()
    )
    out["timestamp"] = pd.to_datetime(raw[mapping["timestamp"]], utc=True, errors="coerce")
    out["lat"] = pd.to_numeric(raw[mapping["lat"]], errors="coerce")
    out["lon"] = pd.to_numeric(raw[mapping["lon"]], errors="coerce")

    if "speed_knots" in mapping:
        out["speed_knots"] = pd.to_numeric(raw[mapping["speed_knots"]], errors="coerce")
    else:
        out["speed_knots"] = pd.NA

    if "heading" in mapping:
        out["heading"] = pd.to_numeric(raw[mapping["heading"]], errors="coerce")
    else:
        out["heading"] = pd.NA

    before = len(out)
    out = out.dropna(subset=["imo", "timestamp", "lat", "lon"])
    out = out[(out["lat"].between(-90, 90)) & (out["lon"].between(-180, 180))]
    out = out.sort_values(["imo", "timestamp"]).reset_index(drop=True)

    if out.empty:
        raise ValueError(
            f"No valid AIS rows after cleaning ({before} raw rows). "
            f"Check timestamp/lat/lon parsing. Columns: {cols}"
        )
    return out
