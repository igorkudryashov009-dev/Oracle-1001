#!/usr/bin/env python3
"""TimescaleDB migration blueprint. Does not switch the live Node A database.

Backfill copies vessel_daily_archive into a staging SQLite file whose columns
match the hypertable mapping. A real Timescale cutover is the runbook, after
DB_BACKEND=timescale and an operator-created data/archive/timescale_cutover.json.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

# SQLite column -> Timescale column. snapshot_date + observed time become timestamptz.
SCHEMA_MAP = (
    ("snapshot_date", "snapshot_date", "date"),
    ("imo", "imo", "bigint"),
    ("lat", "lat", "double precision"),
    ("lon", "lon", "double precision"),
    ("source", "source", "text"),
    ("gap_hours", "gap_hours", "double precision"),
)

STAGING_DDL = """
CREATE TABLE IF NOT EXISTS vessel_daily_archive (
    snapshot_date TEXT NOT NULL,
    imo INTEGER NOT NULL,
    lat REAL,
    lon REAL,
    source TEXT NOT NULL,
    gap_hours REAL,
    row_checksum TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, imo)
)
"""


def _checksum(row: tuple[Any, ...]) -> str:
    blob = "|".join("" if value is None else str(value) for value in row)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def backfill(source: Path, dest: Path) -> dict[str, int]:
    """Idempotent copy. A second run inserts nothing new and keeps checksums."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(dest))
    try:
        dst.execute(STAGING_DDL)
        rows = src.execute(
            "SELECT snapshot_date, imo, lat, lon, source, gap_hours FROM vessel_daily_archive"
        ).fetchall()
        inserted = 0
        for row in rows:
            digest = _checksum(row)
            cur = dst.execute(
                """
                INSERT OR IGNORE INTO vessel_daily_archive
                    (snapshot_date, imo, lat, lon, source, gap_hours, row_checksum)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (*row, digest),
            )
            inserted += int(cur.rowcount or 0)
        dst.commit()
        return {"source_rows": len(rows), "inserted": inserted}
    finally:
        src.close()
        dst.close()


def verify(source: Path, dest: Path, *, sample: int = 5) -> dict[str, Any]:
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(dest))
    try:
        src_n = src.execute("SELECT COUNT(*) FROM vessel_daily_archive").fetchone()[0]
        dst_n = dst.execute("SELECT COUNT(*) FROM vessel_daily_archive").fetchone()[0]
        checked = 0
        mismatched = 0
        for row in src.execute(
            "SELECT snapshot_date, imo, lat, lon, source, gap_hours FROM vessel_daily_archive LIMIT ?",
            (max(1, sample),),
        ):
            digest = _checksum(row)
            found = dst.execute(
                "SELECT row_checksum FROM vessel_daily_archive WHERE snapshot_date=? AND imo=?",
                (row[0], row[1]),
            ).fetchone()
            checked += 1
            if found is None or found[0] != digest:
                mismatched += 1
        return {
            "source_rows": int(src_n),
            "dest_rows": int(dst_n),
            "counts_match": int(src_n) == int(dst_n),
            "sample_checked": checked,
            "sample_mismatched": mismatched,
        }
    finally:
        src.close()
        dst.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage a Timescale-shaped backfill (no live cutover)")
    parser.add_argument("--source", required=True)
    parser.add_argument("--dest", required=True)
    args = parser.parse_args(argv)
    stats = backfill(Path(args.source), Path(args.dest))
    report = verify(Path(args.source), Path(args.dest))
    print({"backfill": stats, "verify": report, "schema": [item[1] for item in SCHEMA_MAP]})
    return 0 if report["counts_match"] and report["sample_mismatched"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
