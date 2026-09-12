"""
ETL: migrate legacy collector tables into unified Sentinel schema `ais_positions`.

Supports legacy schemas with flexible column aliases (positions / ais_raw / etc.).
Idempotent: INSERT OR IGNORE inside a single transaction.

Usage:
  python -m services.migrate_legacy_to_sentinel
  python services/migrate_legacy_to_sentinel.py
  python -m services.migrate_legacy_to_sentinel --legacy история1/raw_positions.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.storage import AISStorage, POSITIONS_DDL  # noqa: E402
from services.utils.path_sanitizer import sanitize_structure  # noqa: E402

# Column aliases: sentinel_field → possible legacy names (case-insensitive match)
_ALIASES: dict[str, tuple[str, ...]] = {
    "imo": ("imo", "imo_number", "imonumber"),
    "mmsi": ("mmsi", "userid", "user_id", "ship_mmsi"),
    "vessel_name": ("vessel_name", "name", "shipname", "ship_name"),
    "tier": ("tier", "fleet_tier"),
    "timestamp_utc": ("timestamp_utc", "ts_utc", "time_utc", "timestamp", "ts", "msgtime"),
    "lat": ("lat", "latitude", "y"),
    "lon": ("lon", "longitude", "lng", "x"),
    "sog": ("sog", "speed_knots", "speed", "sog_kn", "speedoverground"),
    "cog": ("cog", "course", "course_over_ground"),
    "heading": ("heading", "trueheading", "true_heading", "hdg"),
    "nav_status": ("nav_status", "navigationalstatus", "navstatus", "status"),
    "draft_m": ("draft_m", "draught", "draft", "maximumstaticdraught"),
    "destination": ("destination", "dest", "destination_port"),
    "matched": ("matched",),
    "message_type": ("message_type", "msg_type", "type"),
    "received_at": ("received_at", "ingested_at", "created_at", "recv_at"),
}

_LEGACY_TABLE_CANDIDATES = (
    "positions",
    "ais_positions_legacy",
    "raw_positions",
    "ais_raw",
    "collector_positions",
)


def _norm_cols(cols: Iterable[str]) -> dict[str, str]:
    """Map lower-case column name → actual column name."""
    return {c.lower(): c for c in cols}


def _pick(d: dict[str, Any], cols_map: dict[str, str], field: str) -> Any:
    for alias in _ALIASES.get(field, (field,)):
        real = cols_map.get(alias.lower())
        if real is not None and d.get(real) is not None:
            return d.get(real)
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def discover_legacy_table(src: sqlite3.Connection) -> Optional[str]:
    tables = {
        r[0]
        for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    for name in _LEGACY_TABLE_CANDIDATES:
        if name in tables:
            return name
    # Fallback: first table that has lat/lon-like columns
    for name in sorted(tables):
        cols = {r[1].lower() for r in src.execute(f"PRAGMA table_info({name})").fetchall()}
        if ("lat" in cols or "latitude" in cols) and ("lon" in cols or "longitude" in cols or "lng" in cols):
            return name
    return None


def _row_to_tuple(d: dict[str, Any], cols_map: dict[str, str]) -> Optional[tuple[Any, ...]]:
    lat = _to_float(_pick(d, cols_map, "lat"))
    lon = _to_float(_pick(d, cols_map, "lon"))
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None

    ts = _to_str(_pick(d, cols_map, "timestamp_utc"))
    received = _to_str(_pick(d, cols_map, "received_at")) or ts
    mmsi = _to_str(_pick(d, cols_map, "mmsi"))
    if not mmsi and not ts:
        return None

    imo = _pick(d, cols_map, "imo")
    imo_s = _to_str(imo) if imo not in (None, "", 0, "0") else None
    matched_raw = _pick(d, cols_map, "matched")
    if matched_raw is None:
        matched = 1 if imo_s else 0
    else:
        try:
            matched = 1 if int(matched_raw) else 0
        except (TypeError, ValueError):
            matched = 1 if imo_s else 0

    msg_type = _to_str(_pick(d, cols_map, "message_type")) or "legacy_positions"

    return (
        imo_s,
        mmsi,
        _to_str(_pick(d, cols_map, "vessel_name")) or None,
        _to_str(_pick(d, cols_map, "tier")) or None,
        ts,
        float(lat),
        float(lon),
        _to_float(_pick(d, cols_map, "sog")),
        _to_float(_pick(d, cols_map, "cog")),
        _to_float(_pick(d, cols_map, "heading")),
        _to_str(_pick(d, cols_map, "nav_status")) or None,
        _to_float(_pick(d, cols_map, "draft_m")),
        _to_str(_pick(d, cols_map, "destination")) or None,
        matched,
        msg_type,
        received,
    )


def migrate(legacy: Path, dest: Path, batch_size: int = 2000) -> dict:
    if not legacy.exists():
        return sanitize_structure({
            "status": "skip",
            "reason": f"legacy missing: {legacy}",
            "legacy": str(legacy),
            "dest": str(dest),
        })

    store = AISStorage(sqlite_path=dest)
    store.open()
    assert store._conn is not None
    dest_conn = store._conn

    src = sqlite3.connect(str(legacy))
    src.row_factory = sqlite3.Row
    try:
        table = discover_legacy_table(src)
        if not table:
            return {
                "status": "skip",
                "reason": "no migratable legacy table found",
                "legacy": str(legacy),
            }

        info = src.execute(f"PRAGMA table_info({table})").fetchall()
        cols = [r[1] for r in info]
        if not cols:
            return {"status": "skip", "reason": f"empty schema on {table}"}
        cols_map = _norm_cols(cols)

        # Require lat/lon mapping
        if _ALIASES["lat"][0] not in cols_map and not any(a in cols_map for a in _ALIASES["lat"]):
            return {"status": "skip", "reason": f"{table} missing lat/lon columns", "columns": cols}

        total = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        migrated = 0
        skipped_rows = 0
        offset = 0

        # Idempotent single transaction — rollback on any hard failure
        prev_isolation = dest_conn.isolation_level
        dest_conn.isolation_level = None  # manual BEGIN/COMMIT
        try:
            dest_conn.execute("BEGIN IMMEDIATE")
            dest_conn.execute(POSITIONS_DDL)

            order_col = "id" if "id" in cols_map else cols[0]
            while True:
                rows = src.execute(
                    f"SELECT * FROM {table} ORDER BY {order_col} LIMIT ? OFFSET ?",
                    (batch_size, offset),
                ).fetchall()
                if not rows:
                    break
                payload: list[tuple[Any, ...]] = []
                for r in rows:
                    tup = _row_to_tuple(dict(r), cols_map)
                    if tup is None:
                        skipped_rows += 1
                        continue
                    payload.append(tup)
                if payload:
                    dest_conn.executemany(
                        """
                        INSERT OR IGNORE INTO ais_positions
                        (imo, mmsi, vessel_name, tier, timestamp_utc, lat, lon, sog, cog, heading,
                         nav_status, draft_m, destination, matched, message_type, received_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        payload,
                    )
                    migrated += len(payload)
                offset += batch_size

            dest_conn.execute("COMMIT")
        except Exception:
            try:
                dest_conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            dest_conn.isolation_level = prev_isolation

        result = {
            "status": "ok",
            "legacy": str(legacy),
            "dest": str(dest),
            "legacy_table": table,
            "legacy_rows": total,
            "attempted": migrated,
            "skipped_invalid": skipped_rows,
            "note": "INSERT OR IGNORE — duplicates skipped; fleet tier filled at analytics time",
        }
        return sanitize_structure(result)
    finally:
        src.close()
        try:
            store._conn.close()
        except Exception:
            pass


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
    try:
        result = migrate(Path(args.legacy), Path(args.dest))
    except Exception as exc:  # noqa: BLE001
        print({"status": "error", "error": str(exc)})
        return 1
    print(result)
    return 0 if result.get("status") in ("ok", "skip") else 1


if __name__ == "__main__":
    raise SystemExit(main())
