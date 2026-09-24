#!/usr/bin/env python3
"""Daily vessel archive snapshot worker → vessel_daily_archive (sentinel_ais.db).

Layer A (free): terrestrial AIS overlay for the full gas-carrier universe (~1,260).
Every vessel gets exactly one row per UTC day. Missing telemetry → source='none'
with gap_hours set — completeness is a metric, never silent omission.

No interpolated / synthetic positions. source ∈ {terrestrial_ais, vf_api, none}.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.archive_schema import (  # noqa: E402
    ALLOWED_ARCHIVE_SOURCES,
    ARCHIVE_COLUMNS,
    PROVENANCE_COLUMNS,
    VESSEL_DAILY_ARCHIVE_DDL,
    VESSEL_DAILY_ARCHIVE_INDEXES,
)
from services.storage import DEFAULT_DB  # noqa: E402
from services.vf_budget_allocator import TARGET_FLEET_N  # noqa: E402

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
    # Idempotent column migrations for DBs created before provenance fields.
    existing = {
        str(r[1]) for r in conn.execute("PRAGMA table_info(vessel_daily_archive)").fetchall()
    }
    for col, decl in PROVENANCE_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE vessel_daily_archive ADD COLUMN {col} {decl}")
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


def load_fleet_rows(csv_path: Path = FLEET_CSV, *, target_n: int = TARGET_FLEET_N) -> list[dict[str, Any]]:
    """Unique IMO universe capped at target_n (DWT desc)."""
    if not csv_path.is_file():
        raise FileNotFoundError(f"fleet registry missing: {csv_path}")
    import pandas as pd

    df = pd.read_csv(csv_path, low_memory=False)
    if "vessel_category" in df.columns:
        df = df[df["vessel_category"].astype(str).str.lower() == "vessel"].copy()
    if "dwt_tons" in df.columns:
        df["dwt_tons"] = pd.to_numeric(df["dwt_tons"], errors="coerce").fillna(0.0)
        df = df.sort_values("dwt_tons", ascending=False)
    df = df.drop_duplicates(subset=["imo"], keep="first").head(int(target_n))
    return df.to_dict(orient="records")


def load_latest_ais(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Latest ais_positions row keyed by IMO string (real telemetry only)."""
    try:
        # Prefer lat/lon columns when present
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(ais_positions)").fetchall()}
        has_lat = "lat" in cols or "latitude" in cols
        lat_col = "lat" if "lat" in cols else ("latitude" if "latitude" in cols else None)
        lon_col = "lon" if "lon" in cols else ("longitude" if "longitude" in cols else None)
        cog_col = "cog" if "cog" in cols else ("course" if "course" in cols else None)
        select_extra = []
        if lat_col:
            select_extra.append(f"p.{lat_col} AS lat")
        else:
            select_extra.append("NULL AS lat")
        if lon_col:
            select_extra.append(f"p.{lon_col} AS lon")
        else:
            select_extra.append("NULL AS lon")
        if cog_col:
            select_extra.append(f"p.{cog_col} AS cog")
        else:
            select_extra.append("NULL AS cog")
        extra_sql = ", ".join(select_extra)
        cur = conn.execute(
            f"""
            SELECT p.imo, p.mmsi, p.vessel_name, p.sog, p.nav_status, p.draft_m,
                   p.destination, p.timestamp_utc, p.received_at, {extra_sql}
            FROM ais_positions p
            INNER JOIN (
                SELECT imo AS _imo, MAX(COALESCE(received_at, timestamp_utc)) AS mx
                FROM ais_positions
                WHERE imo IS NOT NULL AND TRIM(imo) != ''
                GROUP BY imo
            ) t ON p.imo = t._imo AND COALESCE(p.received_at, p.timestamp_utc) = t.mx
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
            "received_at": row[8],
            "lat": row[9],
            "lon": row[10],
            "cog": row[11],
            "source": "terrestrial_ais",
        }
    return out


def load_vf_overlays(conn: sqlite3.Connection, snapshot_date: str) -> dict[str, dict[str, Any]]:
    """Optional VF verification rows written by allocator runner (same-day)."""
    try:
        cur = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='vf_position_cache'
            """
        )
        if not cur.fetchone():
            return {}
        cur = conn.execute(
            """
            SELECT imo, lat, lon, sog, cog, nav_status, draught, fetched_at
            FROM vf_position_cache
            WHERE date(fetched_at) = ?
            """,
            (snapshot_date,),
        )
    except sqlite3.Error:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in cur.fetchall():
        imo = str(row[0] or "").strip()
        if not imo:
            continue
        out[imo] = {
            "lat": row[1],
            "lon": row[2],
            "sog": row[3],
            "cog": row[4],
            "nav_status": row[5],
            "draft_m": row[6],
            "timestamp_utc": row[7],
            "source": "vf_api",
            "vf_verified": 1,
        }
    return out


def _gap_hours(ts_utc: Optional[str], *, now: datetime) -> float:
    if not ts_utc:
        return 1e9
    try:
        raw = str(ts_utc).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)
    except (TypeError, ValueError):
        return 1e9


def _ais_integrity(ts_utc: Optional[str], *, now: datetime) -> float:
    age_h = _gap_hours(ts_utc, now=now)
    if age_h >= 1e8:
        return 0.0
    if age_h <= LIVE_AIS_FRESH_HOURS:
        return 1.0
    if age_h <= LIVE_AIS_STALE_HOURS:
        return round(max(0.15, 1.0 - (age_h / LIVE_AIS_STALE_HOURS)), 3)
    return 0.05


def _load_spoof_imos() -> set[str]:
    snap = ROOT / "output" / "api" / "v1" / "health.json"
    if not snap.is_file():
        return set()
    try:
        doc = json.loads(snap.read_text(encoding="utf-8"))
        spoof = doc.get("ais_spoofing") if isinstance(doc, dict) else None
        if isinstance(spoof, dict):
            return {str(x).strip() for x in (spoof.get("spoofed_imos") or []) if x}
    except (OSError, json.JSONDecodeError):
        pass
    return set()


def build_snapshot_rows(
    fleet: list[dict[str, Any]],
    ais_by_imo: dict[str, dict[str, Any]],
    snapshot_date: str,
    *,
    now: datetime | None = None,
    vf_by_imo: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[Any, ...]]:
    now = now or datetime.now(timezone.utc)
    vf_by_imo = vf_by_imo or {}
    spoofed = _load_spoof_imos()
    rows: list[tuple[Any, ...]] = []
    for rec in fleet:
        imo = _to_int(rec.get("imo"))
        if imo is None:
            continue
        imo_key = str(imo)
        live = ais_by_imo.get(imo_key) or ais_by_imo.get(imo_key.zfill(7)) or {}
        vf = vf_by_imo.get(imo_key) or {}

        # Provenance: prefer fresh terrestrial, else VF verification, else none.
        # Never invent lat/lon.
        source = "none"
        pos = {}
        if live and _gap_hours(live.get("received_at") or live.get("timestamp_utc"), now=now) <= LIVE_AIS_STALE_HOURS:
            source = "terrestrial_ais"
            pos = live
        elif vf and vf.get("lat") is not None and vf.get("lon") is not None:
            source = "vf_api"
            pos = vf
        else:
            pos = live or {}

        if source not in ALLOWED_ARCHIVE_SOURCES:
            source = "none"

        dwt = _to_float(rec.get("dwt_tons"))
        gt = _to_float(rec.get("gt"))
        displacement = gt if gt and gt > 0 else None

        speed = _to_float(pos.get("sog"))
        draft = _to_float(pos.get("draft_m"))
        if draft is None:
            draft = _to_float(rec.get("draft_m"))
        nav = _to_str(pos.get("nav_status")) or _to_str(rec.get("nav_status"))
        dest = _to_str(pos.get("destination")) or _to_str(rec.get("destination_port"))
        name = _to_str(pos.get("vessel_name")) or _to_str(rec.get("vessel_name")) or f"IMO {imo}"
        mmsi = _to_int(pos.get("mmsi")) or _to_int(rec.get("mmsi"))

        ts = pos.get("received_at") or pos.get("timestamp_utc")
        gap = _gap_hours(ts, now=now) if source != "none" else max(
            LIVE_AIS_FRESH_HOURS + 0.1,
            _gap_hours(live.get("received_at") or live.get("timestamp_utc"), now=now)
            if live
            else 1e9,
        )
        if source == "none":
            # Explicit empty day — coordinates stay NULL (not interpolated)
            lat = lon = cog = None
            speed = None
            gap = gap if gap < 1e8 else 24.0 + 1.0  # at least >24h when no position
            integrity = 0.0
        else:
            lat = _to_float(pos.get("lat"))
            lon = _to_float(pos.get("lon"))
            cog = _to_float(pos.get("cog"))
            integrity = _ais_integrity(ts, now=now)
            # If source claims position but coords missing → demote to none
            if lat is None or lon is None:
                source = "none"
                lat = lon = cog = None
                speed = None
                integrity = 0.0
                gap = max(gap, 24.1)

        tags = str(rec.get("sanctions_tags") or "").upper()
        in_sts = 1 if "STS" in tags else 0
        spoof_flag = 1 if imo_key in spoofed else 0
        vf_verified = 1 if (source == "vf_api" or int(vf.get("vf_verified") or 0) == 1) else 0

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
                lat,
                lon,
                cog,
                draft,
                source,
                round(gap, 2) if gap < 1e8 else 9999.0,
                in_sts,
                spoof_flag,
                vf_verified,
            )
        )
    return rows


def upsert_snapshot(conn: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    placeholders = ",".join("?" for _ in ARCHIVE_COLUMNS)
    cols = ",".join(ARCHIVE_COLUMNS)
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
    target_n: int = TARGET_FLEET_N,
) -> dict[str, Any]:
    day = snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = Path(db_path or _db_path())
    db.parent.mkdir(parents=True, exist_ok=True)

    fleet = load_fleet_rows(Path(fleet_csv or FLEET_CSV), target_n=target_n)
    conn = sqlite3.connect(str(db), timeout=30.0)
    try:
        ensure_schema(conn)
        ais = load_latest_ais(conn)
        vf = load_vf_overlays(conn, day)
        rows = build_snapshot_rows(fleet, ais, day, vf_by_imo=vf)
        n = upsert_snapshot(conn, rows)
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
        "vf_overlay": len(vf),
        "manifest": manifest,
    }


def compute_fleet_archive_metrics(
    *,
    db_path: Path | None = None,
    day: str | None = None,
    expected_n: int | None = None,
) -> dict[str, Any]:
    """Reporting block for /api/v1/health — does NOT feed Dual Gate sample logic."""
    from services.vf_budget_allocator import allocator_status

    db = Path(db_path or _db_path())
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    day_s = day or yesterday
    try:
        universe_n = len(load_fleet_rows(target_n=TARGET_FLEET_N))
    except Exception:  # noqa: BLE001
        universe_n = TARGET_FLEET_N
    exp = int(expected_n) if expected_n is not None else int(universe_n or TARGET_FLEET_N)

    metrics: dict[str, Any] = {
        "snapshot_date": day_s,
        "expected_n": exp,
        "archive_completeness_pct": 0.0,
        "row_count": 0,
        "terrestrial_covered_n": 0,
        "vf_verified_n": 0,
        "gap_24h_n": 0,
        "gap_48h_n": 0,
        "source_none_n": 0,
        "vf_budget": allocator_status(),
        "feeds_fleet_sample": False,
        "note": "Archive reporting only — Dual Gate fleet_sample uses live 540s window",
    }
    if not db.is_file():
        metrics["error"] = "db_missing"
        return metrics

    conn = sqlite3.connect(str(db), timeout=15.0)
    try:
        ensure_schema(conn)
        cur = conn.execute(
            "SELECT COUNT(*) FROM vessel_daily_archive WHERE snapshot_date = ?",
            (day_s,),
        )
        n = int(cur.fetchone()[0])
        metrics["row_count"] = n
        metrics["archive_completeness_pct"] = round(100.0 * n / exp, 2) if exp else 0.0

        # Prefer new provenance columns; fall back gracefully
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(vessel_daily_archive)").fetchall()}
        if "source" in cols:
            cur = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN source = 'terrestrial_ais' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN vf_verified = 1 OR source = 'vf_api' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN gap_hours IS NOT NULL AND gap_hours > 24 THEN 1 ELSE 0 END),
                  SUM(CASE WHEN gap_hours IS NOT NULL AND gap_hours > 48 THEN 1 ELSE 0 END),
                  SUM(CASE WHEN source = 'none' THEN 1 ELSE 0 END)
                FROM vessel_daily_archive
                WHERE snapshot_date = ?
                """,
                (day_s,),
            )
            row = cur.fetchone() or (0, 0, 0, 0, 0)
            metrics["terrestrial_covered_n"] = int(row[0] or 0)
            metrics["vf_verified_n"] = int(row[1] or 0)
            metrics["gap_24h_n"] = int(row[2] or 0)
            metrics["gap_48h_n"] = int(row[3] or 0)
            metrics["source_none_n"] = int(row[4] or 0)
    finally:
        conn.close()
    return metrics


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
