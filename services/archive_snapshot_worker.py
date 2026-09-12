#!/usr/bin/env python3
"""Daily vessel archive snapshot worker → vessel_daily_archive (sentinel_ais.db).

Pulls the full fleet registry (~1253 vessels) from fleet_database.csv, overlays
latest live AIS telemetry from sentinel_ais.db, and upserts one immutable row
per (snapshot_date, imo) under WAL.

Usage:
  python -m services.archive_snapshot_worker
  python -m services.archive_snapshot_worker --date 2026-09-08
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.archive_schema import (  # noqa: E402
    ARCHIVE_COLUMNS,
    VESSEL_DAILY_ARCHIVE_DDL,
    VESSEL_DAILY_ARCHIVE_INDEXES,
)
from services.storage import DEFAULT_DB  # noqa: E402

FLEET_CSV = ROOT / "output" / "fleet_database.csv"
ARCHIVE_OUT = ROOT / "output" / "archive"
LIVE_AIS_FRESH_HOURS = 24.0
LIVE_AIS_STALE_HOURS = 168.0  # 7d


def _db_path() -> Path:
    env = (os.environ.get("SENTINEL_DB_PATH") or "").strip()
    return Path(env) if env else Path(DEFAULT_DB)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(VESSEL_DAILY_ARCHIVE_DDL)
    for stmt in VESSEL_DAILY_ARCHIVE_INDEXES:
        conn.execute(stmt)
    conn.commit()


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        if isinstance(v, float) and v != v:  # NaN
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    if f is None:
        return None
    try:
        return int(f)
    except (TypeError, ValueError):
        return None


def _to_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return None
    return s


def load_fleet_rows(csv_path: Path = FLEET_CSV) -> list[dict[str, Any]]:
    if not csv_path.is_file():
        raise FileNotFoundError(f"fleet registry missing: {csv_path}")
    import pandas as pd

    df = pd.read_csv(csv_path, low_memory=False)
    if "vessel_category" in df.columns:
        df = df[df["vessel_category"].astype(str).str.lower() == "vessel"].copy()
    # Prefer unique IMO; keep highest DWT on collision
    if "dwt_tons" in df.columns:
        df["dwt_tons"] = pd.to_numeric(df["dwt_tons"], errors="coerce").fillna(0.0)
        df = df.sort_values("dwt_tons", ascending=False)
    df = df.drop_duplicates(subset=["imo"], keep="first")
    return df.to_dict(orient="records")


def load_latest_ais(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Latest ais_positions row keyed by IMO string."""
    try:
        cur = conn.execute(
            """
            SELECT p.imo, p.mmsi, p.vessel_name, p.sog, p.nav_status, p.draft_m,
                   p.destination, p.timestamp_utc
            FROM ais_positions p
            INNER JOIN (
                SELECT imo AS _imo, MAX(timestamp_utc) AS mx
                FROM ais_positions
                WHERE imo IS NOT NULL AND TRIM(imo) != ''
                GROUP BY imo
            ) t ON p.imo = t._imo AND p.timestamp_utc = t.mx
            """
        )
    except sqlite3.Error:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in cur.fetchall():
        imo = str(row[0] or "").strip()
        if not imo:
            continue
        out[imo] = {
            "mmsi": row[1],
            "vessel_name": row[2],
            "sog": row[3],
            "nav_status": row[4],
            "draft_m": row[5],
            "destination": row[6],
            "timestamp_utc": row[7],
        }
    return out


def _ais_integrity(ts_utc: Optional[str], *, now: datetime) -> float:
    if not ts_utc:
        return 0.0
    try:
        raw = str(ts_utc).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_h = (now - dt.astimezone(timezone.utc)).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return 0.0
    if age_h <= LIVE_AIS_FRESH_HOURS:
        return 1.0
    if age_h <= LIVE_AIS_STALE_HOURS:
        return round(max(0.15, 1.0 - (age_h / LIVE_AIS_STALE_HOURS)), 3)
    return 0.05


def build_snapshot_rows(
    fleet: list[dict[str, Any]],
    ais_by_imo: dict[str, dict[str, Any]],
    snapshot_date: str,
    *,
    now: datetime | None = None,
) -> list[tuple[Any, ...]]:
    now = now or datetime.now(timezone.utc)
    rows: list[tuple[Any, ...]] = []
    for rec in fleet:
        imo = _to_int(rec.get("imo"))
        if imo is None:
            continue
        imo_key = str(imo)
        live = ais_by_imo.get(imo_key) or ais_by_imo.get(imo_key.zfill(7)) or {}

        dwt = _to_float(rec.get("dwt_tons"))
        gt = _to_float(rec.get("gt"))
        # Displacement not in OSINT CSV — use GT as engineering proxy when present
        displacement = gt if gt and gt > 0 else None

        speed = _to_float(live.get("sog"))
        if speed is None:
            speed = _to_float(rec.get("speed_knots"))

        draft = _to_float(live.get("draft_m"))
        if draft is None:
            draft = _to_float(rec.get("draft_m"))

        nav = _to_str(live.get("nav_status")) or _to_str(rec.get("nav_status"))
        dest = _to_str(live.get("destination")) or _to_str(rec.get("destination_port"))
        name = _to_str(live.get("vessel_name")) or _to_str(rec.get("vessel_name")) or f"IMO {imo}"

        mmsi = _to_int(live.get("mmsi"))
        if mmsi is None:
            mmsi = _to_int(rec.get("mmsi"))

        integrity = _ais_integrity(live.get("timestamp_utc"), now=now)

        rows.append(
            (
                snapshot_date,
                imo,
                dwt,
                displacement,
                _to_float(rec.get("loa_m")),
                _to_float(rec.get("beam_m")),
                draft,
                speed,
                _to_float(rec.get("age_years")),
                _to_int(rec.get("built_year")),
                _to_str(rec.get("flag")),
                _to_str(rec.get("vessel_type")),
                nav,
                _to_str(rec.get("compliance_risk_level")),
                _to_str(rec.get("departure_port")),
                dest,
                name,
                mmsi,
                _to_str(rec.get("arrival_datetime")),
                _to_str(rec.get("destination_context")),
                integrity,
            )
        )
    return rows


def upsert_snapshot(conn: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    placeholders = ",".join("?" for _ in ARCHIVE_COLUMNS)
    cols = ",".join(ARCHIVE_COLUMNS)
    # Immutable daily truth: replace same-day rows atomically
    sql = f"INSERT OR REPLACE INTO vessel_daily_archive ({cols}) VALUES ({placeholders})"
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(sql, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def export_snapshot_artifacts(
    rows: list[tuple[Any, ...]],
    snapshot_date: str,
    *,
    out_dir: Path = ARCHIVE_OUT,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    snap_dir = out_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    records = [dict(zip(ARCHIVE_COLUMNS, r)) for r in rows]
    json_path = snap_dir / f"{snapshot_date}.json"
    csv_path = snap_dir / f"{snapshot_date}.csv"
    latest_path = out_dir / "latest.json"

    payload = {
        "snapshot_date": snapshot_date,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "vessel_count": len(records),
        "columns": list(ARCHIVE_COLUMNS),
        "vessels": records,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    latest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ARCHIVE_COLUMNS))
        writer.writeheader()
        writer.writerows(records)

    # Manifest of available dates
    dates = sorted({p.stem for p in snap_dir.glob("*.json")})
    manifest = {
        "generated_at": payload["generated_at"],
        "latest": snapshot_date,
        "dates": dates,
        "vessel_count_latest": len(records),
        "json_url": f"/output/archive/snapshots/{snapshot_date}.json",
        "csv_url": f"/output/archive/snapshots/{snapshot_date}.csv",
        "latest_url": "/output/archive/latest.json",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def take_daily_snapshot(
    *,
    snapshot_date: str | None = None,
    db_path: Path | None = None,
    fleet_csv: Path | None = None,
    export: bool = True,
) -> dict[str, Any]:
    day = snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = Path(db_path or _db_path())
    db.parent.mkdir(parents=True, exist_ok=True)

    fleet = load_fleet_rows(Path(fleet_csv or FLEET_CSV))
    conn = sqlite3.connect(str(db), timeout=30.0)
    try:
        ensure_schema(conn)
        ais = load_latest_ais(conn)
        rows = build_snapshot_rows(fleet, ais, day)
        n = upsert_snapshot(conn, rows)
        # Count for day
        cur = conn.execute(
            "SELECT COUNT(*) FROM vessel_daily_archive WHERE snapshot_date = ?", (day,)
        )
        stored = int(cur.fetchone()[0])
    finally:
        conn.close()

    manifest = export_snapshot_artifacts(rows, day) if export else {}
    return {
        "ok": True,
        "snapshot_date": day,
        "db": str(db),
        "fleet_source_rows": len(fleet),
        "upserted": n,
        "stored_for_date": stored,
        "live_ais_overlay": len(ais),
        "manifest": manifest,
    }


def list_archive_dates(db_path: Path | None = None) -> list[str]:
    db = Path(db_path or _db_path())
    if not db.is_file():
        return []
    conn = sqlite3.connect(str(db), timeout=30.0)
    try:
        ensure_schema(conn)
        cur = conn.execute(
            "SELECT DISTINCT snapshot_date FROM vessel_daily_archive ORDER BY snapshot_date DESC"
        )
        return [str(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="UTC snapshot date YYYY-MM-DD (default: today)")
    parser.add_argument("--db", type=Path, default=None, help="Override SENTINEL_DB_PATH")
    parser.add_argument("--fleet-csv", type=Path, default=None)
    parser.add_argument("--no-export", action="store_true", help="Skip JSON/CSV artifact export")
    args = parser.parse_args(argv)
    try:
        result = take_daily_snapshot(
            snapshot_date=args.date,
            db_path=args.db,
            fleet_csv=args.fleet_csv,
            export=not args.no_export,
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
