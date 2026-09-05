"""
7-day rolling retention + incremental vacuum for sentinel_ais.db.

Usage:
  python -m services.sqlite_retention
  python -m services.sqlite_retention --days 7 --db path/to/sentinel_ais.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CANDIDATES = [
    ROOT / "история1" / "sentinel_ais.db",
    Path("/opt/oracle1001/ais_ingest/история1/sentinel_ais.db"),
    Path("/opt/oracle1001/analytical_engine/история1/sentinel_ais.db"),
    Path("/opt/oracle1001/analytical_engine/sentinel_ais.db"),
]


def resolve_db(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    for c in DEFAULT_CANDIDATES:
        if c.exists():
            return c
    raise FileNotFoundError("sentinel_ais.db not found in default locations")


def retain(db_path: Path, days: int = 7, max_bytes: int = 1_500_000_000) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        before = conn.execute("SELECT COUNT(*) FROM ais_positions").fetchone()[0]
        cur = conn.execute(
            "DELETE FROM ais_positions WHERE timestamp_utc < ? OR received_at < ?",
            (cutoff, cutoff),
        )
        deleted_pos = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        # Telemetry / dead-letter: keep 14 days
        tel_cut = (datetime.now(timezone.utc) - timedelta(days=max(days, 14))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            conn.execute("DELETE FROM pipeline_telemetry WHERE ts_utc < ?", (tel_cut,))
            conn.execute("DELETE FROM dead_letter WHERE ts_utc < ?", (tel_cut,))
        except sqlite3.Error:
            pass
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM ais_positions").fetchone()[0]

        # incremental vacuum if file oversized or deletes happened
        size = db_path.stat().st_size
        if deleted_pos > 0 or size > max_bytes:
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            # reclaim pages without long write locks (1000 pages ≈ several MB)
            conn.execute("PRAGMA incremental_vacuum(1000)")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        size_after = db_path.stat().st_size
        return {
            "db": str(db_path),
            "cutoff_utc": cutoff,
            "rows_before": before,
            "rows_after": after,
            "deleted": deleted_pos,
            "size_before": size,
            "size_after": size_after,
            "ok": size_after < max_bytes,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sentinel SQLite 7-day retention")
    parser.add_argument("--db", default=None)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--max-bytes", type=int, default=1_500_000_000)
    args = parser.parse_args()
    try:
        db = resolve_db(args.db)
    except FileNotFoundError as exc:
        print(f"SKIP: {exc}")
        return 0
    result = retain(db, days=args.days, max_bytes=args.max_bytes)
    print(result)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
