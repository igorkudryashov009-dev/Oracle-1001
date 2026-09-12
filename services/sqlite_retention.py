"""
AIS SQLite WAL retention + integrity recovery for sentinel_ais.db.

Usage:
  python -m services.sqlite_retention
  python -m services.sqlite_retention --days 45 --recover
  python -m services.sqlite_retention --db path/to/sentinel_ais.db
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CANDIDATES = [
    ROOT / "история1" / "sentinel_ais.db",
    Path(os.environ["SENTINEL_DB_PATH"]) if os.environ.get("SENTINEL_DB_PATH") else None,
    Path("/opt/oracle1001/ais_ingest/история1/sentinel_ais.db"),
    Path("/opt/oracle1001/analytical_engine/история1/sentinel_ais.db"),
    Path("/opt/oracle1001/analytical_engine/sentinel_ais.db"),
]

# Keep 45 days of AIS telemetry by default (P0 retention window)
DEFAULT_DAYS = 45
BATCH_DELETE = 5_000

POSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS ais_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imo TEXT,
    mmsi TEXT NOT NULL,
    vessel_name TEXT,
    tier TEXT,
    timestamp_utc TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    sog REAL,
    cog REAL,
    heading REAL,
    nav_status TEXT,
    draft_m REAL,
    destination TEXT,
    matched INTEGER NOT NULL DEFAULT 0,
    message_type TEXT,
    received_at TEXT NOT NULL,
    UNIQUE(mmsi, timestamp_utc)
)
"""


def resolve_db(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    for c in DEFAULT_CANDIDATES:
        if c is not None and c.exists():
            return c
    raise FileNotFoundError("sentinel_ais.db not found in default locations")


def apply_wal_pragmas(conn: sqlite3.Connection) -> dict[str, Any]:
    """Routine PRAGMA set — safe with concurrent ingest writers."""
    modes = {}
    modes["journal_mode"] = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    conn.execute("PRAGMA synchronous=NORMAL")
    modes["synchronous"] = conn.execute("PRAGMA synchronous").fetchone()[0]
    conn.execute("PRAGMA busy_timeout=5000")
    modes["busy_timeout"] = 5000
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        modes["wal_checkpoint"] = "PASSIVE"
    except sqlite3.Error as exc:
        modes["wal_checkpoint"] = f"skip:{exc}"
    return modes


def _table_ok(conn: sqlite3.Connection, table: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def integrity_report(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        apply_wal_pragmas(conn)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()
            ok = bool(check and check[0] == "ok")
            detail = check[0] if check else "unknown"
        except sqlite3.Error as exc:
            ok = False
            detail = str(exc)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        readable = {t: _table_ok(conn, t) for t in tables}
        return {"ok": ok, "detail": detail if ok else str(detail)[:500], "tables": readable}
    finally:
        conn.close()


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> int:
    rows = src.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        # Ensure empty table exists with schema from src
        schema = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if schema and schema[0]:
            dst.execute(schema[0])
        return 0
    cols = [d[0] for d in src.execute(f"SELECT * FROM {table} LIMIT 0").description]
    schema = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if schema and schema[0]:
        dst.execute(schema[0])
    placeholders = ",".join("?" for _ in cols)
    col_list = ",".join(cols)
    dst.executemany(
        f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
        rows,
    )
    return len(rows)


def _salvage_ais_positions(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    """Best-effort salvage of ais_positions by rowid windows (skip corrupt pages)."""
    dst.execute(POSITIONS_DDL)
    salvaged = 0
    try:
        max_row = src.execute("SELECT MAX(rowid) FROM ais_positions").fetchone()[0]
    except sqlite3.Error:
        max_row = None
    if not max_row:
        # Blind window scan
        max_row = 5_000_000
    start = 1
    window = 2_000
    while start <= int(max_row):
        end = start + window - 1
        try:
            rows = src.execute(
                "SELECT imo,mmsi,vessel_name,tier,timestamp_utc,lat,lon,sog,cog,heading,"
                "nav_status,draft_m,destination,matched,message_type,received_at "
                "FROM ais_positions WHERE rowid BETWEEN ? AND ?",
                (start, end),
            ).fetchall()
            if rows:
                dst.executemany(
                    """
                    INSERT OR IGNORE INTO ais_positions
                    (imo,mmsi,vessel_name,tier,timestamp_utc,lat,lon,sog,cog,heading,
                     nav_status,draft_m,destination,matched,message_type,received_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )
                salvaged += len(rows)
        except sqlite3.Error:
            # Corrupt page window — skip ahead
            pass
        start = end + 1
        if start % 100_000 < window:
            dst.commit()
    dst.commit()
    return salvaged


def recover_database(db_path: Path) -> dict[str, Any]:
    """Rebuild DB into a sibling file, preserving healthy tables + salvaged positions."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    corrupt_bak = db_path.with_suffix(db_path.suffix + f".corrupt-{ts}")
    recovered = db_path.with_suffix(db_path.suffix + f".recovered-{ts}")
    if recovered.exists():
        recovered.unlink()

    src = sqlite3.connect(str(db_path), timeout=60.0)
    dst = sqlite3.connect(str(recovered), timeout=60.0)
    report: dict[str, Any] = {"source": str(db_path), "recovered": str(recovered)}
    try:
        apply_wal_pragmas(dst)
        tables = [
            r[0]
            for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        copied: dict[str, int] = {}
        for table in tables:
            if table == "ais_positions":
                continue
            if not _table_ok(src, table):
                copied[table] = -1
                continue
            try:
                copied[table] = _copy_table(src, dst, table)
                dst.commit()
            except sqlite3.Error as exc:
                copied[table] = -1
                report.setdefault("errors", []).append(f"{table}:{exc}")
        salvaged = _salvage_ais_positions(src, dst)
        copied["ais_positions"] = salvaged
        dst.execute(
            "CREATE INDEX IF NOT EXISTS idx_ais_mmsi_ts ON ais_positions(mmsi, timestamp_utc)"
        )
        dst.execute("CREATE INDEX IF NOT EXISTS idx_ais_imo_ts ON ais_positions(imo, timestamp_utc)")
        apply_wal_pragmas(dst)
        dst.commit()
        report["copied"] = copied
        report["salvaged_positions"] = salvaged
    finally:
        src.close()
        dst.close()

    # Atomic swap: backup corrupt → move recovered into place
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            try:
                side.unlink()
            except OSError:
                pass
    shutil.move(str(db_path), str(corrupt_bak))
    shutil.move(str(recovered), str(db_path))
    # Ensure WAL mode on final file
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        report["pragmas"] = apply_wal_pragmas(conn)
        conn.commit()
    finally:
        conn.close()
    report["corrupt_backup"] = str(corrupt_bak)
    report["ok"] = True
    return report


def _batched_delete(
    conn: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: tuple[Any, ...],
    *,
    batch: int = BATCH_DELETE,
) -> int:
    """DELETE in small chunks to avoid long write locks against live ingest."""
    total = 0
    # Prefer rowid batching when possible
    sql = (
        f"DELETE FROM {table} WHERE rowid IN "
        f"(SELECT rowid FROM {table} WHERE {where_sql} LIMIT {int(batch)})"
    )
    while True:
        try:
            cur = conn.execute(sql, params)
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()
            apply_wal_pragmas(conn)
            if n <= 0:
                break
            total += n
            time.sleep(0.01)  # yield to writers
        except sqlite3.Error as exc:
            # Fallback: single-shot delete if subquery unsupported / schema issue
            try:
                cur = conn.execute(f"DELETE FROM {table} WHERE {where_sql}", params)
                conn.commit()
                n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                total += max(0, n)
            except sqlite3.Error as exc2:
                raise sqlite3.Error(f"{table} retention failed: {exc} / {exc2}") from exc2
            break
    return total


def retain(
    db_path: Path,
    days: int = DEFAULT_DAYS,
    max_bytes: int = 1_500_000_000,
    *,
    recover: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {"db": str(db_path), "days": days}
    integ = integrity_report(db_path)
    report["integrity"] = integ

    if not integ.get("ok") or not integ.get("tables", {}).get("ais_positions", True):
        if not recover and not integ.get("ok"):
            # Auto-recover on malformed disk image (P0)
            recover = True
        if recover:
            report["recovery"] = recover_database(db_path)
            integ = integrity_report(db_path)
            report["integrity_after_recovery"] = integ
        else:
            report["ok"] = False
            report["error"] = "database integrity failure — re-run with --recover"
            return report

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tel_cut = (datetime.now(timezone.utc) - timedelta(days=max(days, 14))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    size_before = db_path.stat().st_size
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    try:
        pragmas = apply_wal_pragmas(conn)
        report["pragmas"] = pragmas

        before = 0
        try:
            before = conn.execute("SELECT COUNT(*) FROM ais_positions").fetchone()[0]
        except sqlite3.Error as exc:
            report["error"] = f"count failed: {exc}"
            if recover:
                conn.close()
                report["recovery"] = recover_database(db_path)
                return retain(db_path, days=days, max_bytes=max_bytes, recover=False)
            raise

        deleted_pos = 0
        try:
            deleted_pos = _batched_delete(
                conn,
                "ais_positions",
                "timestamp_utc < ? OR received_at < ?",
                (cutoff, cutoff),
            )
        except sqlite3.Error as exc:
            report["delete_error"] = str(exc)
            if "malformed" in str(exc).lower() or "disk image" in str(exc).lower():
                conn.close()
                report["recovery"] = recover_database(db_path)
                return retain(db_path, days=days, max_bytes=max_bytes, recover=False)
            raise

        for table, col in (("pipeline_telemetry", "ts_utc"), ("dead_letter", "ts_utc")):
            if not _table_ok(conn, table):
                continue
            try:
                _batched_delete(conn, table, f"{col} < ?", (tel_cut,))
            except sqlite3.Error:
                pass

        after = conn.execute("SELECT COUNT(*) FROM ais_positions").fetchone()[0]
        size = db_path.stat().st_size
        if deleted_pos > 0 or size > max_bytes:
            try:
                # Never TRUNCATE while ingest may hold writers — PASSIVE only
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                # Soft reclaim; ignore if locked
                try:
                    conn.execute("PRAGMA incremental_vacuum(256)")
                except sqlite3.Error:
                    pass
                conn.commit()
            except sqlite3.Error as exc:
                report["vacuum_warning"] = str(exc)

        size_after = db_path.stat().st_size
        report.update(
            {
                "cutoff_utc": cutoff,
                "rows_before": before,
                "rows_after": after,
                "deleted": deleted_pos,
                "size_before": size_before,
                "size_after": size_after,
                "ok": True,
            }
        )
        return report
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Sentinel SQLite WAL retention (45d default)")
    parser.add_argument("--db", default=None)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--max-bytes", type=int, default=1_500_000_000)
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Force rebuild/salvage if integrity_check fails",
    )
    args = parser.parse_args()
    try:
        db = resolve_db(args.db)
    except FileNotFoundError as exc:
        print(f"SKIP: {exc}")
        return 0
    try:
        result = retain(db, days=args.days, max_bytes=args.max_bytes, recover=bool(args.recover))
    except sqlite3.Error as exc:
        print({"ok": False, "error": str(exc)})
        return 1
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
