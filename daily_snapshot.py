"""Daily AIS snapshot writer (run every 24h, e.g. 00:05 UTC via Task Scheduler).

Writes:
  история1/daily/YYYY-MM-DD.csv
  история1/by_vessel/{imo}.csv  (append)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from distance_calc import haversine_km

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "история1"
DB_PATH = HISTORY / "raw_positions.db"
DAILY_DIR = HISTORY / "daily"
BY_VESSEL_DIR = HISTORY / "by_vessel"
TARGETS = ROOT / "targets.json"
LOG = ROOT / "logs" / "snapshot_log.txt"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def path_km(points: list[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(points)):
        d = haversine_km(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
        if d <= 2500:  # drop teleports
            total += d
    return round(total, 2)


def last_known_before(conn: sqlite3.Connection, imo: str, before_iso: str) -> dict | None:
    cur = conn.execute(
        """
        SELECT imo, mmsi, timestamp_utc, lat, lon, speed_knots, heading, nav_status
        FROM positions
        WHERE imo = ? AND timestamp_utc < ?
        ORDER BY timestamp_utc DESC LIMIT 1
        """,
        (imo, before_iso),
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["imo", "mmsi", "timestamp_utc", "lat", "lon", "speed_knots", "heading", "nav_status"]
    return dict(zip(keys, row))


def points_in_window(
    conn: sqlite3.Connection, imo: str, start_iso: str, end_iso: str
) -> list[dict]:
    cur = conn.execute(
        """
        SELECT imo, mmsi, timestamp_utc, lat, lon, speed_knots, heading, nav_status
        FROM positions
        WHERE imo = ? AND timestamp_utc >= ? AND timestamp_utc < ?
        ORDER BY timestamp_utc ASC
        """,
        (imo, start_iso, end_iso),
    )
    keys = ["imo", "mmsi", "timestamp_utc", "lat", "lon", "speed_knots", "heading", "nav_status"]
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def append_by_vessel(imo: str, row: dict) -> None:
    """Append one daily row per vessel, idempotently.

    Re-running the snapshot for a date that was already written (manual
    re-run, or a Task Scheduler double-trigger) must NOT duplicate rows:
    any existing row with the same snapshot_date is replaced, not appended.
    """
    BY_VESSEL_DIR.mkdir(parents=True, exist_ok=True)
    path = BY_VESSEL_DIR / f"{imo}.csv"
    new_row = pd.DataFrame([row])

    if path.exists():
        existing = pd.read_csv(path, encoding="utf-8-sig")
        if "snapshot_date" in existing.columns:
            existing = existing[existing["snapshot_date"] != row["snapshot_date"]]
        combined = pd.concat([existing, new_row], ignore_index=True)
    else:
        combined = new_row

    combined.to_csv(path, index=False, encoding="utf-8-sig")


def run_snapshot(as_of: datetime | None = None) -> Path:
    if not TARGETS.exists():
        raise FileNotFoundError("targets.json missing — run prepare_targets.py")

    HISTORY.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    BY_VESSEL_DIR.mkdir(parents=True, exist_ok=True)

    as_of = as_of or datetime.now(timezone.utc)
    end = as_of
    start = end - timedelta(hours=24)
    day_str = end.strftime("%Y-%m-%d")
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    targets = json.loads(TARGETS.read_text(encoding="utf-8"))["targets"]
    conn = sqlite3.connect(DB_PATH) if DB_PATH.exists() else None

    rows = []
    n_live = 0
    n_gap = 0

    for t in targets:
        imo = str(t["imo"])
        name = t.get("vessel_name")
        mmsi = t.get("mmsi")

        if conn is None:
            pts = []
        else:
            pts = points_in_window(conn, imo, start_iso, end_iso)

        if pts:
            last = pts[-1]
            km = path_km([(p["lat"], p["lon"]) for p in pts])
            row = {
                "imo": imo,
                "vessel_name": name,
                "mmsi": last["mmsi"] or mmsi,
                "lat": last["lat"],
                "lon": last["lon"],
                "timestamp": last["timestamp_utc"],
                "speed": last["speed_knots"],
                "heading": last["heading"],
                "nav_status": last["nav_status"],
                "km_last_24h": km,
                "num_pings_24h": len(pts),
                "data_source": "aisstream_live",
                "snapshot_date": day_str,
            }
            n_live += 1
        else:
            prev = last_known_before(conn, imo, end_iso) if conn else None
            row = {
                "imo": imo,
                "vessel_name": name,
                "mmsi": (prev or {}).get("mmsi") or mmsi,
                "lat": (prev or {}).get("lat"),
                "lon": (prev or {}).get("lon"),
                "timestamp": (prev or {}).get("timestamp_utc"),
                "speed": (prev or {}).get("speed_knots"),
                "heading": (prev or {}).get("heading"),
                "nav_status": (prev or {}).get("nav_status"),
                "km_last_24h": 0.0,
                "num_pings_24h": 0,
                "data_source": "no_signal_24h",
                "snapshot_date": day_str,
            }
            n_gap += 1

        rows.append(row)
        append_by_vessel(imo, row)

    if conn:
        conn.close()

    out = DAILY_DIR / f"{day_str}.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    log(f"Snapshot {day_str}: live={n_live} no_signal={n_gap} → {out}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--as-of",
        default=None,
        help="UTC datetime ISO for end of window (default: now)",
    )
    args = parser.parse_args()
    as_of = None
    if args.as_of:
        as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
    run_snapshot(as_of)
    return 0


if __name__ == "__main__":
    sys.exit(main())
