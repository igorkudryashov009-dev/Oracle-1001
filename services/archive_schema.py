"""Immutable daily vessel archive schema for sentinel_ais.db."""

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
    PRIMARY KEY (snapshot_date, imo)
)
"""

VESSEL_DAILY_ARCHIVE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_vda_date ON vessel_daily_archive(snapshot_date)",
    "CREATE INDEX IF NOT EXISTS idx_vda_imo ON vessel_daily_archive(imo)",
    "CREATE INDEX IF NOT EXISTS idx_vda_flag ON vessel_daily_archive(flag)",
    "CREATE INDEX IF NOT EXISTS idx_vda_risk ON vessel_daily_archive(risk_level)",
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
)
