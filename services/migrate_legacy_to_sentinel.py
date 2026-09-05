"""
ETL: migrate legacy collector `positions` table (raw_positions.db)
into unified Sentinel schema `ais_positions` (sentinel_ais.db).

Usage:
  python -m services.migrate_legacy_to_sentinel
  python -m services.migrate_legacy_to_sentinel --legacy история1/raw_positions.db --dest история1/sentinel_ais.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from services.storage import AISStorage  # noqa: E402


def migrate(legacy: Path, dest: Path, batch_size: int = 2000) -> dict:
    if not legacy.exists():
        return {"status": "skip", "reason": f"legacy missing: {legacy}"}

    store = AISStorage(sqlite_path=dest)
    store.open()
    assert store._conn is not None

    src = sqlite3.connect(str(legacy))
    src.row_factory = sqlite3.Row
    try:
        # Discover columns present in legacy schema
        cols = {r[1] for r in src.execute("PRAGMA table_info(positions)").fetchall()}
        if not cols:
            return {"status": "skip", "reason": "no positions table"}

        total = src.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        migrated = 0
        offset = 0
        while True:
            rows = src.execute(
                "SELECT * FROM positions ORDER BY id LIMIT ? OFFSET ?",
                (batch_size, offset),
            ).fetchall()
            if not rows:
                break
            payload = []
            for r in rows:
                d = dict(r)
                payload.append(
                    (
                        d.get("imo"),
                        str(d.get("mmsi") or ""),
                        None,  # vessel_name
                        None,  # tier — enriched later from fleet registry on read
                        str(d.get("timestamp_utc") or ""),
                        float(d["lat"]),
                        float(d["lon"]),
                        d.get("speed_knots"),
                        None,  # cog
                        d.get("heading"),
                        d.get("nav_status"),
                        None,  # draft
                        None,  # destination
                        1 if d.get("imo") else 0,
                        "legacy_positions",
                        str(d.get("received_at") or d.get("timestamp_utc") or ""),
                    )
                )
            store._conn.executemany(
                """
                INSERT OR IGNORE INTO ais_positions
                (imo, mmsi, vessel_name, tier, timestamp_utc, lat, lon, sog, cog, heading,
                 nav_status, draft_m, destination, matched, message_type, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            store._conn.commit()
            migrated += len(payload)
            offset += batch_size
        return {
            "status": "ok",
            "legacy": str(legacy),
            "dest": str(dest),
            "legacy_rows": total,
            "attempted": migrated,
            "note": "INSERT OR IGNORE — duplicates skipped; fleet tier filled at analytics time",
        }
    finally:
        src.close()
        store._conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy AIS archive → Sentinel SoT")
    parser.add_argument(
        "--legacy",
        default=str(ROOT / "история1" / "raw_positions.db"),
    )
    parser.add_argument(
        "--dest",
        default=str(ROOT / "история1" / "sentinel_ais.db"),
    )
    args = parser.parse_args()
    result = migrate(Path(args.legacy), Path(args.dest))
    print(result)
    return 0 if result.get("status") in ("ok", "skip") else 1


if __name__ == "__main__":
    raise SystemExit(main())
