#!/usr/bin/env python3
"""Oil & gas market balance from archive telemetry (ktons).

Uses draft-ratio cargo estimate:
  ballast ≈ 0.38 × draft_max
  load_factor = (draft_curr − ballast) / (draft_max − ballast)
  cargo_kton = dwt × load_factor × density / 1000
  density ≈ 0.45 (gas) · 0.85 (oil)

Default lookback: **168 hours (7 days)** for out-of-box sample depth.

Writes (dual publish for HUD + archive sheet):
  output/daily_balance.json
  output/archive/daily_balance.json
  vessel_telemetry_history.sqlite → daily_market_balance
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
TELEMETRY_DB = ROOT / "data" / "archive" / "vessel_telemetry_history.sqlite"
BALANCE_OUT = ROOT / "output" / "daily_balance.json"
BALANCE_ARCHIVE_OUT = ROOT / "output" / "archive" / "daily_balance.json"
DEFAULT_LOOKBACK_HOURS = int(os.environ.get("BALANCE_LOOKBACK_HOURS") or "168")
MIN_ADEQUATE_SAMPLE = 150
MIN_LIMITED_SAMPLE = 30

GAS_DENSITY = 0.45
OIL_DENSITY = 0.85
BALLAST_FRAC = 0.38

BALANCE_DDL = """
CREATE TABLE IF NOT EXISTS daily_market_balance (
    balance_date TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    oil_transit_ktons REAL NOT NULL,
    gas_transit_ktons REAL NOT NULL,
    oil_vessels INTEGER NOT NULL,
    gas_vessels INTEGER NOT NULL,
    sample_n INTEGER NOT NULL,
    meta_json TEXT
)
"""

SNAPSHOT_VIEW_DDL = """
CREATE VIEW IF NOT EXISTS telemetry_snapshots AS
SELECT
    t.imo,
    t.mmsi,
    t.vessel_name,
    COALESCE(t.vessel_type, r.vessel_type) AS vessel_type,
    COALESCE(t.dwt, r.dwt) AS dwt,
    t.draft_m AS draft_current,
    COALESCE(t.draft_max, r.draft_max, t.draft_m) AS draft_max,
    t.sog AS speed,
    COALESCE(t.tier, r.tier) AS tier,
    t.timestamp_utc AS timestamp
FROM vessel_telemetry t
LEFT JOIN vessel_fleet_registry r ON r.imo = t.imo
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    d = dt or _utc_now()
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_gas(vessel_type: Any, tier: Any = None) -> bool:
    if tier is not None:
        try:
            if int(tier) == 1:
                return True
            if int(tier) == 2:
                return False
        except (TypeError, ValueError):
            pass
    s = str(vessel_type or "").upper()
    return any(g in s for g in ("LNG", "LPG", "LEG", "GAS", "СПГ", "ГАЗОВ", "FLNG"))


def _coverage_status(sample_n: int) -> str:
    if sample_n >= MIN_ADEQUATE_SAMPLE:
        return "ADEQUATE"
    if sample_n >= MIN_LIMITED_SAMPLE:
        return "LIMITED"
    return "INSUFFICIENT"


def _resolve_output_dir() -> Path:
    return Path(os.environ.get("OUTPUT_DIR") or (ROOT / "output"))


def _resolve_sentinel_db() -> Path:
    env = os.environ.get("SENTINEL_DB_PATH")
    if env:
        return Path(env)
    return ROOT / "история1" / "sentinel_ais.db"


def ensure_balance_schema(conn: sqlite3.Connection) -> None:
    conn.execute(BALANCE_DDL)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vessel_fleet_registry (
            imo TEXT PRIMARY KEY,
            mmsi TEXT,
            vessel_name TEXT,
            vessel_type TEXT,
            tier INTEGER NOT NULL,
            dwt REAL,
            draft_max REAL,
            flag TEXT,
            source TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    has_tel = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vessel_telemetry'"
    ).fetchone()
    cols = (
        {r[1] for r in conn.execute("PRAGMA table_info(vessel_telemetry)").fetchall()}
        if has_tel
        else set()
    )
    if cols:
        if "tier" not in cols:
            conn.execute("ALTER TABLE vessel_telemetry ADD COLUMN tier INTEGER")
        if "draft_max" not in cols:
            conn.execute("ALTER TABLE vessel_telemetry ADD COLUMN draft_max REAL")
    try:
        conn.execute("DROP VIEW IF EXISTS telemetry_snapshots")
        conn.execute(SNAPSHOT_VIEW_DDL)
    except sqlite3.Error:
        pass
    conn.commit()


def _rows_from_telemetry(conn: sqlite3.Connection, lookback_hours: int) -> list[tuple]:
    cur = conn.execute(
        """
        SELECT s.vessel_type, s.dwt, s.draft_current, s.draft_max, s.speed, s.tier, s.imo
        FROM telemetry_snapshots s
        INNER JOIN (
            SELECT imo AS _imo, MAX(timestamp) AS mx
            FROM telemetry_snapshots
            WHERE timestamp IS NOT NULL
              AND timestamp >= datetime('now', ?)
            GROUP BY imo
        ) t ON s.imo = t._imo AND s.timestamp = t.mx
        """,
        (f"-{int(lookback_hours)} hours",),
    )
    return list(cur.fetchall())


def _rows_from_ais_overlay(
    *,
    telemetry_conn: sqlite3.Connection,
    lookback_hours: int,
) -> list[tuple]:
    """Enrich sample from sentinel_ais.db ais_positions × fleet registry (7d window)."""
    ais_db = _resolve_sentinel_db()
    if not ais_db.is_file():
        return []
    try:
        ais = sqlite3.connect(str(ais_db), timeout=30.0)
    except sqlite3.Error:
        return []
    try:
        has = ais.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ais_positions'"
        ).fetchone()
        if not has:
            return []
        # Latest AIS row per IMO in lookback
        ais_rows = ais.execute(
            """
            SELECT p.imo, p.draft_m, p.sog
            FROM ais_positions p
            INNER JOIN (
                SELECT imo AS _imo, MAX(COALESCE(received_at, timestamp_utc)) AS mx
                FROM ais_positions
                WHERE imo IS NOT NULL AND imo != ''
                  AND COALESCE(received_at, timestamp_utc) >= datetime('now', ?)
                GROUP BY imo
            ) t ON p.imo = t._imo
               AND COALESCE(p.received_at, p.timestamp_utc) = t.mx
            """,
            (f"-{int(lookback_hours)} hours",),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        ais.close()

    if not ais_rows:
        return []

    reg = {
        str(r[0]): r
        for r in telemetry_conn.execute(
            "SELECT imo, vessel_type, dwt, draft_max, tier FROM vessel_fleet_registry"
        ).fetchall()
    }
    out: list[tuple] = []
    for imo, draft_m, sog in ais_rows:
        key = str(imo).strip()
        if not key or key not in reg:
            continue
        _imo, vtype, dwt, draft_max, tier = reg[key]
        draft_curr = draft_m
        dmax = draft_max if draft_max is not None else draft_m
        if dwt is None or draft_curr is None or dmax is None:
            continue
        out.append((vtype, dwt, draft_curr, dmax, sog, tier, key))
    return out


def _merge_unique_by_imo(primary: list[tuple], secondary: list[tuple]) -> list[tuple]:
    """Prefer primary telemetry rows; fill gaps from secondary (AIS overlay)."""
    seen: set[str] = set()
    merged: list[tuple] = []
    for row in primary:
        imo = str(row[6]) if len(row) > 6 else ""
        if not imo or imo in seen:
            continue
        seen.add(imo)
        merged.append(row)
    for row in secondary:
        imo = str(row[6]) if len(row) > 6 else ""
        if not imo or imo in seen:
            continue
        seen.add(imo)
        merged.append(row)
    return merged


def _publish_json(result: dict[str, Any]) -> list[str]:
    """Write canonical + archive copies under OUTPUT_DIR (Compose volume)."""
    out_dir = _resolve_output_dir()
    targets = [
        out_dir / "daily_balance.json",
        out_dir / "archive" / "daily_balance.json",
    ]
    # Also mirror to repo-relative paths when OUTPUT_DIR differs (host tooling)
    if out_dir.resolve() != (ROOT / "output").resolve():
        targets.extend([BALANCE_OUT, BALANCE_ARCHIVE_OUT])
    written: list[str] = []
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    for path in targets:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
            written.append(str(path))
        except OSError:
            continue
    return written


def calculate_daily_market_balance(
    *,
    db_path: Path = TELEMETRY_DB,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> dict[str, Any]:
    """Aggregate oil/gas cargo in transit (ktons) from last N hours (default 168)."""
    if not db_path.is_file():
        result = {
            "date": _utc_now().strftime("%Y-%m-%d"),
            "generated_at": _utc_iso(),
            "oil_transit_ktons": 0.0,
            "gas_transit_ktons": 0.0,
            "oil_vessels": 0,
            "gas_vessels": 0,
            "sample_n": 0,
            "lookback_hours": lookback_hours,
            "coverage_status": "INSUFFICIENT",
            "error": f"telemetry db missing: {db_path}",
        }
        _publish_json(result)
        return result

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        ensure_balance_schema(conn)
        tel_rows = _rows_from_telemetry(conn, lookback_hours)
        ais_rows = _rows_from_ais_overlay(
            telemetry_conn=conn, lookback_hours=lookback_hours
        )
        rows = _merge_unique_by_imo(tel_rows, ais_rows)
    except sqlite3.Error as exc:
        conn.close()
        result = {
            "date": _utc_now().strftime("%Y-%m-%d"),
            "generated_at": _utc_iso(),
            "oil_transit_ktons": 0.0,
            "gas_transit_ktons": 0.0,
            "oil_vessels": 0,
            "gas_vessels": 0,
            "sample_n": 0,
            "lookback_hours": lookback_hours,
            "coverage_status": "INSUFFICIENT",
            "error": str(exc),
        }
        _publish_json(result)
        return result

    oil_in_transit_ktons = 0.0
    gas_in_transit_ktons = 0.0
    oil_vessels = 0
    gas_vessels = 0

    for vessel_type, dwt, draft_curr, draft_max, speed, tier, imo in rows:
        if not dwt or not draft_curr or not draft_max or float(draft_max) == 0:
            continue
        dwt_f = float(dwt)
        draft_c = float(draft_curr)
        draft_m = float(draft_max)
        ballast_draft = draft_m * BALLAST_FRAC
        if draft_c <= ballast_draft:
            continue
        denom = draft_m - ballast_draft
        if denom <= 0:
            continue
        load_factor = min(1.0, max(0.0, (draft_c - ballast_draft) / denom))
        is_gas = _is_gas(vessel_type, tier)
        density = GAS_DENSITY if is_gas else OIL_DENSITY
        cargo_kton = (dwt_f * load_factor * density) / 1000.0
        if is_gas:
            gas_in_transit_ktons += cargo_kton
            gas_vessels += 1
        else:
            oil_in_transit_ktons += cargo_kton
            oil_vessels += 1

    sample_n = len(rows)
    coverage = _coverage_status(sample_n)
    result: dict[str, Any] = {
        "date": _utc_now().strftime("%Y-%m-%d"),
        "generated_at": _utc_iso(),
        "oil_transit_ktons": round(oil_in_transit_ktons, 1),
        "gas_transit_ktons": round(gas_in_transit_ktons, 1),
        "oil_vessels": oil_vessels,
        "gas_vessels": gas_vessels,
        "sample_n": sample_n,
        "lookback_hours": lookback_hours,
        "coverage_status": coverage,
        "coverage_thresholds": {
            "adequate_min": MIN_ADEQUATE_SAMPLE,
            "limited_min": MIN_LIMITED_SAMPLE,
        },
        "assumptions": {
            "ballast_frac": BALLAST_FRAC,
            "gas_density": GAS_DENSITY,
            "oil_density": OIL_DENSITY,
        },
        "sources": {
            "telemetry_unique": len(tel_rows),
            "ais_overlay_unique": len(ais_rows),
        },
    }

    conn.execute(
        """
        INSERT OR REPLACE INTO daily_market_balance
        (balance_date, generated_at, oil_transit_ktons, gas_transit_ktons,
         oil_vessels, gas_vessels, sample_n, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result["date"],
            result["generated_at"],
            result["oil_transit_ktons"],
            result["gas_transit_ktons"],
            oil_vessels,
            gas_vessels,
            sample_n,
            json.dumps(
                {
                    "assumptions": result["assumptions"],
                    "lookback_hours": lookback_hours,
                    "coverage_status": coverage,
                    "sources": result["sources"],
                },
                ensure_ascii=False,
            ),
        ),
    )
    conn.commit()
    conn.close()

    result["written"] = _publish_json(result)
    return result


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Oil/gas market balance engine (7d default)")
    p.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_LOOKBACK_HOURS,
        help=f"Telemetry lookback hours (default {DEFAULT_LOOKBACK_HOURS} = 7 days)",
    )
    args = p.parse_args(argv)
    out = calculate_daily_market_balance(lookback_hours=args.hours)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if "error" not in out else 2


if __name__ == "__main__":
    raise SystemExit(main())
