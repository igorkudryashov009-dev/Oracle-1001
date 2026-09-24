"""Immutable daily vessel archive schema for sentinel_ais.db.

Provenance contract (1.8.0):
  source ∈ {terrestrial_ais, vf_api, none} — never interpolated / synthetic positions.
"""

from __future__ import annotations

VESSEL_DAILY_ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS vessel_daily_archive (
    snapshot_date TEXT NOT NULL,
    imo INTEGER NOT NULL,
    dwt REAL,
    displacement REAL,
    loa REAL,
    beam REAL,
    draft REAL,
    speed REAL,
    age REAL,
    build_year INTEGER,
    flag TEXT,
    vessel_type TEXT,
    nav_status TEXT,
    risk_level TEXT,
    origin_port TEXT,
    destination_port TEXT,
    vessel_name TEXT,
    mmsi INTEGER,
    eta TEXT,
    route_context TEXT,
    ais_integrity REAL,
    lat REAL,
    lon REAL,
    cog REAL,
    draught REAL,
    source TEXT NOT NULL DEFAULT 'none',
    gap_hours REAL,
    in_sts_zone INTEGER NOT NULL DEFAULT 0,
    spoof_flag INTEGER NOT NULL DEFAULT 0,
    vf_verified INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_date, imo)
)
"""

VESSEL_DAILY_ARCHIVE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_vda_date ON vessel_daily_archive(snapshot_date)",
    "CREATE INDEX IF NOT EXISTS idx_vda_imo ON vessel_daily_archive(imo)",
    "CREATE INDEX IF NOT EXISTS idx_vda_flag ON vessel_daily_archive(flag)",
    "CREATE INDEX IF NOT EXISTS idx_vda_risk ON vessel_daily_archive(risk_level)",
    "CREATE INDEX IF NOT EXISTS idx_vda_source ON vessel_daily_archive(source)",
    "CREATE INDEX IF NOT EXISTS idx_vda_gap ON vessel_daily_archive(gap_hours)",
)

# Columns added after initial DDL — applied via ALTER TABLE IF needed.
PROVENANCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("lat", "REAL"),
    ("lon", "REAL"),
    ("cog", "REAL"),
    ("draught", "REAL"),
    ("source", "TEXT NOT NULL DEFAULT 'none'"),
    ("gap_hours", "REAL"),
    ("in_sts_zone", "INTEGER NOT NULL DEFAULT 0"),
    ("spoof_flag", "INTEGER NOT NULL DEFAULT 0"),
    ("vf_verified", "INTEGER NOT NULL DEFAULT 0"),
)

ARCHIVE_COLUMNS = (
    "snapshot_date",
    "imo",
    "dwt",
    "displacement",
    "loa",
    "beam",
    "draft",
    "speed",
    "age",
    "build_year",
    "flag",
    "vessel_type",
    "nav_status",
    "risk_level",
    "origin_port",
    "destination_port",
    "vessel_name",
    "mmsi",
    "eta",
    "route_context",
    "ais_integrity",
    "lat",
    "lon",
    "cog",
    "draught",
    "source",
    "gap_hours",
    "in_sts_zone",
    "spoof_flag",
    "vf_verified",
)

ALLOWED_ARCHIVE_SOURCES = frozenset({"terrestrial_ais", "vf_api", "none"})
