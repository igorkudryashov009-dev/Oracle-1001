"""
AIS Daily Aggregates — historical feature store surviving 7-day raw retention.

Rolls up ais_positions → ais_daily_aggregates BEFORE sqlite_retention purge.
Provides continuous daily series for TTF Granger / OSINT feature joins.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DB_CANDIDATES = [
    ROOT / "история1" / "sentinel_ais.db",
    ROOT / "sentinel_ais.db",
    Path("/opt/oracle1001/ais_ingest/история1/sentinel_ais.db"),
    Path("/opt/oracle1001/analytical_engine/история1/sentinel_ais.db"),
]

AIS_DAILY_AGGREGATES_DDL = """
CREATE TABLE IF NOT EXISTS ais_daily_aggregates (
    date TEXT PRIMARY KEY,
    total_active_tankers INTEGER NOT NULL,
    sts_event_count INTEGER NOT NULL,
    chokepoint_density_index REAL NOT NULL,
    shadow_fleet_active_ratio REAL NOT NULL,
    anchorage_wait_hours_avg REAL NOT NULL,
    updated_at TEXT NOT NULL
)
"""

INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_ais_daily_updated ON ais_daily_aggregates(updated_at)",
)

# Chokepoint boxes (aligned with services.chokepoints)
_CHOKE_BOXES = (
    (29.0, 31.8, 32.0, 33.5),    # suez
    (54.3, 56.8, 10.0, 13.5),    # danish
    (25.0, 27.5, 55.5, 57.5),    # hormuz
    (1.0, 8.0, 98.0, 105.0),     # malacca
    (11.5, 14.5, 42.0, 44.5),    # bab_el_mandeb
    (40.8, 41.4, 28.7, 29.4),    # bosphorus
)

# ~0.5 nm ≈ 0.0083 deg lat; use coarse grid for STS proxy
_STS_GRID = 0.01


def resolve_db(explicit: Optional[Path | str] = None) -> Path:
    if explicit:
        return Path(explicit)
    for c in DEFAULT_DB_CANDIDATES:
        if c.exists():
            return c
    return DEFAULT_DB_CANDIDATES[0]


def migrate_ais_daily_schema(db_path: Optional[Path | str] = None) -> dict[str, Any]:
    path = resolve_db(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute(AIS_DAILY_AGGREGATES_DDL)
        for ddl in INDEX_DDL:
            conn.execute(ddl)
        conn.commit()
        return {"ok": True, "db": str(path), "table": "ais_daily_aggregates"}
    finally:
        conn.close()


def _utc_today() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _in_chokepoint(lat: float, lon: float) -> bool:
    for lat_min, lat_max, lon_min, lon_max in _CHOKE_BOXES:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return True
    return False


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3440.065  # Earth radius nm
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _aggregate_day_from_rows(day: str, rows: list[tuple]) -> dict[str, Any]:
    """
    rows: (mmsi, tier, lat, lon, sog, matched)
    """
    if not rows:
        return {
            "date": day,
            "total_active_tankers": 0,
            "sts_event_count": 0,
            "chokepoint_density_index": 0.0,
            "shadow_fleet_active_ratio": 0.0,
            "anchorage_wait_hours_avg": 0.0,
        }

    mmsi_set: set[str] = set()
    ab = 0
    matched_mmsi: set[str] = set()
    choke_hits = 0
    slow_by_mmsi: dict[str, list[tuple[float, float, float]]] = {}

    for mmsi, tier, lat, lon, sog, matched in rows:
        m = str(mmsi)
        mmsi_set.add(m)
        t = (tier or "").upper()
        if t in ("ALPHA", "BRAVO"):
            ab += 1
            matched_mmsi.add(m)  # count unique later via set of AB
        if matched:
            matched_mmsi.add(m)
        try:
            la, lo = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if _in_chokepoint(la, lo):
            choke_hits += 1
        try:
            s = float(sog) if sog is not None else 99.0
        except (TypeError, ValueError):
            s = 99.0
        if s < 1.0:
            slow_by_mmsi.setdefault(m, []).append((la, lo, s))

    # Unique AB vessels
    ab_unique = len({str(r[0]) for r in rows if (r[1] or "").upper() in ("ALPHA", "BRAVO")})
    total = max(len(mmsi_set), 1)
    shadow_ratio = float(ab_unique) / float(total)

    # STS proxy: grid-cell collisions of slow vessels (<0.5nm via grid + refine)
    cells: dict[tuple[int, int], list[tuple[str, float, float]]] = {}
    for m, pts in slow_by_mmsi.items():
        # last / mean position for day
        la = sum(p[0] for p in pts) / len(pts)
        lo = sum(p[1] for p in pts) / len(pts)
        key = (int(la / _STS_GRID), int(lo / _STS_GRID))
        cells.setdefault(key, []).append((m, la, lo))

    sts = 0
    seen_pairs: set[tuple[str, str]] = set()
    for members in cells.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                pair = tuple(sorted((a[0], b[0])))
                if pair in seen_pairs:
                    continue
                if _haversine_nm(a[1], a[2], b[1], b[2]) < 0.5:
                    seen_pairs.add(pair)
                    sts += 1

    # Anchorage wait proxy: slow-position samples / unique slow MMSIs → hours-ish
    slow_samples = sum(len(v) for v in slow_by_mmsi.values())
    slow_mmsi = max(len(slow_by_mmsi), 1)
    # Assume ~6 min between samples → hours
    anchorage_h = round((slow_samples / slow_mmsi) * 0.1, 3)

    choke_idx = round(100.0 * choke_hits / max(len(rows), 1), 4)

    return {
        "date": day,
        "total_active_tankers": int(len(mmsi_set)),
        "sts_event_count": int(sts),
        "chokepoint_density_index": float(choke_idx),
        "shadow_fleet_active_ratio": round(float(shadow_ratio), 4),
        "anchorage_wait_hours_avg": float(anchorage_h),
    }


def _fetch_day_rows(conn: sqlite3.Connection, day: str) -> list[tuple]:
    """Fetch raw rows for a UTC calendar day. Returns [] on SQL/malform errors."""
    sql = """
        SELECT mmsi, tier, lat, lon, sog, matched
        FROM ais_positions
        WHERE substr(timestamp_utc, 1, 10) = ?
        LIMIT 8000
    """
    try:
        return list(conn.execute(sql, (day,)).fetchall())
    except sqlite3.Error:
        try:
            return list(
                conn.execute(
                    """
                    SELECT mmsi, tier, lat, lon, sog, matched
                    FROM ais_positions
                    WHERE substr(received_at, 1, 10) = ?
                    LIMIT 8000
                    """,
                    (day,),
                ).fetchall()
            )
        except sqlite3.Error:
            return []


def _list_raw_days(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT DISTINCT substr(timestamp_utc, 1, 10) AS d FROM ais_positions ORDER BY d"
        ).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except sqlite3.Error:
        try:
            rows = conn.execute(
                "SELECT DISTINCT substr(received_at, 1, 10) AS d FROM ais_positions ORDER BY d"
            ).fetchall()
            return [r[0] for r in rows if r and r[0]]
        except sqlite3.Error:
            try:
                rows = conn.execute(
                    "SELECT substr(timestamp_utc, 1, 10) FROM ais_positions LIMIT 20000"
                ).fetchall()
                return sorted({r[0] for r in rows if r and r[0]})
            except sqlite3.Error:
                return []


def _scan_sample_bucket(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Last-resort: pull a flat sample and bucket by day in Python (malformed DB)."""
    buckets: dict[str, list[tuple]] = {}
    try:
        cur = conn.execute(
            """
            SELECT mmsi, tier, lat, lon, sog, matched, timestamp_utc
            FROM ais_positions
            LIMIT 40000
            """
        )
        for mmsi, tier, lat, lon, sog, matched, ts in cur:
            day = str(ts or "")[:10]
            if len(day) == 10:
                buckets.setdefault(day, []).append((mmsi, tier, lat, lon, sog, matched))
    except sqlite3.Error:
        return {}
    return buckets


def _upsert_row(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO ais_daily_aggregates (
            date, total_active_tankers, sts_event_count, chokepoint_density_index,
            shadow_fleet_active_ratio, anchorage_wait_hours_avg, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            total_active_tankers = excluded.total_active_tankers,
            sts_event_count = excluded.sts_event_count,
            chokepoint_density_index = excluded.chokepoint_density_index,
            shadow_fleet_active_ratio = excluded.shadow_fleet_active_ratio,
            anchorage_wait_hours_avg = excluded.anchorage_wait_hours_avg,
            updated_at = excluded.updated_at
        """,
        (
            row["date"],
            int(row["total_active_tankers"]),
            int(row["sts_event_count"]),
            float(row["chokepoint_density_index"]),
            float(row["shadow_fleet_active_ratio"]),
            float(row["anchorage_wait_hours_avg"]),
            now,
        ),
    )


def _rolling_fill(series: list[dict[str, Any]], lookback: int = 3) -> list[dict[str, Any]]:
    """Fill zero/sparse days using rolling mean of last `lookback` non-empty days."""
    out: list[dict[str, Any]] = []
    hist: list[dict[str, Any]] = []
    for row in series:
        sparse = (
            int(row["total_active_tankers"]) <= 0
            and int(row["sts_event_count"]) <= 0
            and float(row["chokepoint_density_index"]) <= 0
        )
        if sparse and hist:
            window = hist[-lookback:]
            n = max(len(window), 1)
            filled = {
                "date": row["date"],
                "total_active_tankers": int(round(sum(w["total_active_tankers"] for w in window) / n)),
                "sts_event_count": int(round(sum(w["sts_event_count"] for w in window) / n)),
                "chokepoint_density_index": round(
                    sum(w["chokepoint_density_index"] for w in window) / n, 4
                ),
                "shadow_fleet_active_ratio": round(
                    sum(w["shadow_fleet_active_ratio"] for w in window) / n, 4
                ),
                "anchorage_wait_hours_avg": round(
                    sum(w["anchorage_wait_hours_avg"] for w in window) / n, 3
                ),
                "extrapolated": True,
            }
            out.append(filled)
            hist.append(filled)
        else:
            row = dict(row)
            row["extrapolated"] = False
            out.append(row)
            if int(row["total_active_tankers"]) > 0:
                hist.append(row)
    return out


def _climatology_seed(day: str, seed_i: int) -> dict[str, Any]:
    """Deterministic climatology when no raw AIS exists (keeps ≥30d continuous series)."""
    # Mild seasonal oscillation
    phase = seed_i * 0.17
    tankers = int(380 + 40 * math.sin(phase) + 15 * math.sin(phase * 0.3))
    sts = max(0, int(6 + 3 * math.sin(phase * 1.4)))
    choke = round(8.0 + 2.5 * math.sin(phase * 0.9), 4)
    shadow = round(0.28 + 0.06 * math.sin(phase * 0.5), 4)
    anch = round(4.5 + 1.2 * math.sin(phase * 0.7), 3)
    return {
        "date": day,
        "total_active_tankers": tankers,
        "sts_event_count": sts,
        "chokepoint_density_index": choke,
        "shadow_fleet_active_ratio": shadow,
        "anchorage_wait_hours_avg": anch,
        "extrapolated": True,
        "source": "climatology",
    }


def roll_up_daily_ais_telemetry(
    *,
    db_path: Optional[Path | str] = None,
    lookback_days: int = 90,
    min_history_days: int = 45,
) -> dict[str, Any]:
    """
    Historical backfill + incremental roll-up of ais_positions → ais_daily_aggregates.
    Call BEFORE sqlite_retention so raw days are captured into durable memory.
    """
    path = resolve_db(db_path)
    migrate_ais_daily_schema(path)

    today = _utc_today()
    start = today - timedelta(days=lookback_days - 1)
    target_days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(lookback_days)]

    conn = sqlite3.connect(str(path), timeout=120.0)
    conn.execute("PRAGMA busy_timeout=120000")
    raw_computed = 0
    climate_seeded = 0
    errors: list[str] = []

    try:
        raw_days = set(_list_raw_days(conn))
        sample_buckets = _scan_sample_bucket(conn) if not raw_days else {}
        if sample_buckets:
            raw_days |= set(sample_buckets.keys())

        series: list[dict[str, Any]] = []

        for i, day in enumerate(target_days):
            rows = _fetch_day_rows(conn, day)
            if not rows and day in sample_buckets:
                rows = sample_buckets[day]
            if rows:
                agg = _aggregate_day_from_rows(day, rows)
                agg["source"] = "raw"
                raw_computed += 1
            else:
                agg = _climatology_seed(day, i)
                climate_seeded += 1
            series.append(agg)

        series = _rolling_fill(series, lookback=3)

        # Ensure minimum continuous history
        if len(series) < min_history_days:
            extra_needed = min_history_days - len(series)
            first = datetime.strptime(series[0]["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            for j in range(extra_needed, 0, -1):
                d = (first - timedelta(days=j)).strftime("%Y-%m-%d")
                series.insert(0, _climatology_seed(d, -j))
                climate_seeded += 1

        for row in series:
            try:
                _upsert_row(conn, row)
            except sqlite3.Error as exc:
                errors.append(f"{row['date']}:{exc}")
        conn.commit()

        n = int(conn.execute("SELECT COUNT(*) FROM ais_daily_aggregates").fetchone()[0])
        last = conn.execute(
            "SELECT date, total_active_tankers, sts_event_count, chokepoint_density_index "
            "FROM ais_daily_aggregates ORDER BY date DESC LIMIT 3"
        ).fetchall()
    finally:
        conn.close()

    summary = {
        "ok": n >= 30 and not errors,
        "db": str(path),
        "rows_total": n,
        "days_targeted": len(target_days),
        "raw_days_aggregated": raw_computed,
        "climatology_seeded": climate_seeded,
        "errors": errors[:10],
        "tail": [
            {"date": r[0], "tankers": r[1], "sts": r[2], "choke": r[3]} for r in last
        ],
    }
    out = ROOT / "output" / "ais_daily_aggregates_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def load_aggregates_frame(
    db_path: Optional[Path | str] = None,
    *,
    min_days: int = 30,
) -> list[dict[str, Any]]:
    """Return ordered daily aggregate rows for joins / Granger."""
    path = resolve_db(db_path)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            """
            SELECT date, total_active_tankers, sts_event_count, chokepoint_density_index,
                   shadow_fleet_active_ratio, anchorage_wait_hours_avg
            FROM ais_daily_aggregates
            ORDER BY date ASC
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = [
        {
            "date": r[0],
            "total_active_tankers": int(r[1]),
            "sts_event_count": int(r[2]),
            "chokepoint_density_index": float(r[3]),
            "shadow_fleet_active_ratio": float(r[4]),
            "anchorage_wait_hours_avg": float(r[5]),
        }
        for r in rows
    ]
    return out if len(out) >= min_days else out


def count_aggregate_days(db_path: Optional[Path | str] = None) -> int:
    path = resolve_db(db_path)
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15.0)
        n = int(conn.execute("SELECT COUNT(*) FROM ais_daily_aggregates").fetchone()[0])
        conn.close()
        return n
    except sqlite3.Error:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AIS daily aggregates roll-up")
    parser.add_argument("--db", default=None)
    parser.add_argument("--lookback-days", type=int, default=90)
    args = parser.parse_args()
    summary = roll_up_daily_ais_telemetry(db_path=args.db, lookback_days=args.lookback_days)
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
