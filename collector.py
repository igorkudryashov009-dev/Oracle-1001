"""
Continuous AISstream.io collector → SQLite raw log.

RISK AUDIT (2026-07-28, re-checked against https://aisstream.io/documentation):
  - MMSI filter hard limit: 50 per WebSocket subscription (documented, exact).
  - Concurrent-connections-per-key limit: NOT a published number. Docs only say
    "throttling at the api key and user level... may be throttled if... many
    concurrent connections with the same api key." No numeric threshold given.
  - Subscription-update rate limit: max 1 subscribe message per second per
    connection (documented, exact) — irrelevant here since each batch sends
    its subscribe message exactly once at connect time.
  - Coverage is land-based AIS stations (~200km offshore); open-ocean gaps are
    expected and are a data-source limitation, not a collector bug.
  - Service is BETA, no uptime SLA.

Because there is NO published concurrent-connection number, this collector
does NOT assume 55 simultaneous sockets are safe by default. It staggers
batch startup and caps concurrent handshakes via a semaphore (see
STAGGER_SECONDS / MAX_CONCURRENT_HANDSHAKES below). If AISstream starts
rejecting connections after some batch N in practice, that is the empirical
real-world limit — it will show up as repeated handshake failures in
logs\\collector_log.txt and in the "unreachable this session" summary; do not
silently ignore that signal.

Requires AISSTREAM_API_KEY in .env (never commit the key).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import websockets
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

HISTORY = ROOT / "история1"
DB_PATH = HISTORY / "raw_positions.db"
LOG_PATH = ROOT / "logs" / "collector_log.txt"
TARGETS_PATH = ROOT / "targets.json"
BATCHES_PATH = ROOT / "targets_batches.json"
UNREACHABLE_PATH = HISTORY / "collector_unreachable_mmsi.json"
CONFIG_PATH = ROOT / "config.yaml"

WS_URL = "wss://stream.aisstream.io/v0/stream"
WORLD_BBOX = [[[-90, -180], [90, 180]]]
MMSI_BATCH_SIZE = 50

# Preventive controls — no published limit exists, so we don't assume 55-at-once is safe.
STAGGER_SECONDS_DEFAULT = 2.5
MAX_CONCURRENT_HANDSHAKES_DEFAULT = 20
# A batch is declared "unreachable this session" after this many consecutive
# handshake failures (connect+subscribe never confirmed alive).
CONSECUTIVE_FAILURES_UNREACHABLE = 3

# WebSocket disconnect / retry backoff: 2s → 4s → 8s → … capped at 30s.
BACKOFF_INITIAL_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 30.0

# SQLite: WAL for concurrent batch writers + busy timeout (seconds).
SQLITE_TIMEOUT_SECONDS = 30.0


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def next_backoff(
    current: float | None,
    *,
    initial: float = BACKOFF_INITIAL_SECONDS,
    maximum: float = BACKOFF_MAX_SECONDS,
) -> float:
    """Exponential backoff: initial → 2× → … capped at maximum (default 2s→30s)."""
    if current is None:
        return initial
    return min(current * 2.0, maximum)


def load_aisstream_settings(config_path: Path | None = None) -> dict[str, Any]:
    """Merge aisstream.* from config.yaml with module defaults."""
    path = config_path or CONFIG_PATH
    settings: dict[str, Any] = {
        "websocket_url": WS_URL,
        "mmsi_batch_size": MMSI_BATCH_SIZE,
        "stagger_seconds": STAGGER_SECONDS_DEFAULT,
        "max_concurrent_handshakes": MAX_CONCURRENT_HANDSHAKES_DEFAULT,
        "unreachable_after_consecutive_failures": CONSECUTIVE_FAILURES_UNREACHABLE,
        "max_batches": None,
        "reconnect_backoff_initial_seconds": BACKOFF_INITIAL_SECONDS,
        "reconnect_backoff_max_seconds": BACKOFF_MAX_SECONDS,
        "sqlite_timeout_seconds": SQLITE_TIMEOUT_SECONDS,
    }
    if not path.exists():
        return settings
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    ais = cfg.get("aisstream") or {}
    for key in settings:
        if key in ais and ais[key] is not None:
            settings[key] = ais[key]
    paths = cfg.get("paths") or {}
    if paths.get("db"):
        settings["db_path"] = paths["db"]
    if paths.get("targets"):
        settings["targets_path"] = paths["targets"]
    return settings


def open_db(db_path: Path, timeout_seconds: float = SQLITE_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Open SQLite with WAL journal and busy timeout for multi-batch writers."""
    conn = sqlite3.connect(
        str(db_path),
        timeout=timeout_seconds,
        check_same_thread=False,
    )
    busy_ms = max(1, int(timeout_seconds * 1000))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imo TEXT,
            mmsi TEXT NOT NULL,
            timestamp_utc TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            speed_knots REAL,
            heading REAL,
            nav_status TEXT,
            received_at TEXT NOT NULL,
            UNIQUE(mmsi, timestamp_utc)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pos_imo_ts ON positions(imo, timestamp_utc)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pos_mmsi_ts ON positions(mmsi, timestamp_utc)"
    )
    conn.commit()


def load_mmsi_to_imo(targets_path: Path = TARGETS_PATH) -> dict[str, str]:
    data = json.loads(targets_path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for t in data["targets"]:
        mapping[str(t["mmsi"])] = str(t["imo"])
    return mapping


def load_batches(
    max_batches: int | None = None,
    *,
    targets_path: Path = TARGETS_PATH,
    batches_path: Path = BATCHES_PATH,
    mmsi_batch_size: int = MMSI_BATCH_SIZE,
) -> list[list[str]]:
    if batches_path.exists():
        data = json.loads(batches_path.read_text(encoding="utf-8"))
        batches = data["batches"]
    else:
        data = json.loads(targets_path.read_text(encoding="utf-8"))
        mmsis = sorted({str(t["mmsi"]) for t in data["targets"]})
        batches = [
            mmsis[i : i + mmsi_batch_size]
            for i in range(0, len(mmsis), mmsi_batch_size)
        ]
    if max_batches is not None:
        batches = batches[: max(1, max_batches)]
    return batches


NAV_STATUS_MAP = {
    0: "Under way using engine",
    1: "At anchor",
    2: "Not under command",
    3: "Restricted manoeuvrability",
    4: "Constrained by her draught",
    5: "Moored",
    6: "Aground",
    7: "Engaged in fishing",
    8: "Under way sailing",
    14: "AIS-SART",
    15: "Not defined",
}


def insert_position(conn: sqlite3.Connection, row: dict) -> bool:
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO positions
            (imo, mmsi, timestamp_utc, lat, lon, speed_knots, heading, nav_status, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["imo"],
                row["mmsi"],
                row["timestamp_utc"],
                row["lat"],
                row["lon"],
                row["speed_knots"],
                row["heading"],
                row["nav_status"],
                row["received_at"],
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error as e:
        log(f"DB insert error: {e}")
        return False


async def safe_insert_position(
    conn: sqlite3.Connection,
    row: dict,
    db_write_lock: asyncio.Lock,
) -> bool:
    async with db_write_lock:
        return insert_position(conn, row)


def parse_position(message: dict, mmsi_to_imo: dict[str, str]) -> dict | None:
    mtype = message.get("MessageType")
    if mtype not in (
        "PositionReport",
        "StandardClassBPositionReport",
        "ExtendedClassBPositionReport",
    ):
        return None

    body = message.get("Message", {}).get("PositionReport") or {}
    meta = message.get("Metadata", {}) or {}

    mmsi = str(body.get("UserID") or meta.get("MMSI") or "").strip()
    if not mmsi:
        return None

    lat = body.get("Latitude", meta.get("Latitude"))
    lon = body.get("Longitude", meta.get("Longitude"))
    if lat is None or lon is None:
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None
    if lat_f == 91 or lon_f == 181:  # AIS not available sentinel
        return None

    sog = body.get("Sog")
    heading = body.get("TrueHeading")
    if heading is not None and float(heading) >= 511:
        heading = body.get("Cog")
    nav_code = body.get("NavigationalStatus")
    nav_status = None
    if nav_code is not None:
        try:
            nav_status = NAV_STATUS_MAP.get(int(nav_code), str(nav_code))
        except (TypeError, ValueError):
            nav_status = str(nav_code)

    now = datetime.now(timezone.utc)
    ts = meta.get("time_utc") or meta.get("TimeUTC") or now.isoformat()

    return {
        "imo": mmsi_to_imo.get(mmsi),
        "mmsi": mmsi,
        "timestamp_utc": str(ts).replace(" ", "T"),
        "lat": lat_f,
        "lon": lon_f,
        "speed_knots": None if sog is None else float(sog),
        "heading": None if heading is None else float(heading),
        "nav_status": nav_status,
        "received_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


async def batch_worker(
    batch_id: int,
    mmsi_batch: list[str],
    api_key: str,
    mmsi_to_imo: dict[str, str],
    db_path: Path,
    stop_at: datetime | None,
    stats: dict,
    stagger_seconds: float,
    handshake_sem: asyncio.Semaphore,
    db_write_lock: asyncio.Lock,
    all_batches: list[list[str]],
    *,
    ws_url: str = WS_URL,
    unreachable_threshold: int = CONSECUTIVE_FAILURES_UNREACHABLE,
    backoff_initial: float = BACKOFF_INITIAL_SECONDS,
    backoff_max: float = BACKOFF_MAX_SECONDS,
    sqlite_timeout: float = SQLITE_TIMEOUT_SECONDS,
) -> None:
    start_offset = batch_id * stagger_seconds
    await asyncio.sleep(start_offset)
    log(f"batch[{batch_id}] scheduled start (offset={start_offset:.1f}s from launch)")

    reconnect_delay: float | None = None
    consecutive_handshake_failures = 0
    conn = open_db(db_path, timeout_seconds=sqlite_timeout)

    while True:
        if stop_at and datetime.now(timezone.utc) >= stop_at:
            log(f"batch[{batch_id}] stop time reached")
            break

        connect_started_at = datetime.now(timezone.utc)
        subscribed_ok = False
        ws = None
        try:
            async with handshake_sem:
                log(f"batch[{batch_id}] connect attempt @ {connect_started_at.isoformat()}")
                ws = await websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=8 * 1024 * 1024,
                )
                sub = {
                    "APIKey": api_key,
                    "BoundingBoxes": WORLD_BBOX,
                    "FiltersShipMMSI": mmsi_batch,
                    "FilterMessageTypes": ["PositionReport"],
                }
                await ws.send(json.dumps(sub))
                try:
                    first = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    first_msg = json.loads(first)
                    if first_msg.get("error") or first_msg.get("Error"):
                        raise RuntimeError(f"server rejected subscription: {first_msg}")
                    subscribed_ok = True
                    log(
                        f"batch[{batch_id}] subscribed OK ({len(mmsi_batch)} MMSI), "
                        f"first={mmsi_batch[0]}, first_msg_type={first_msg.get('MessageType')}"
                    )
                except asyncio.TimeoutError:
                    subscribed_ok = True
                    log(
                        f"batch[{batch_id}] subscribed OK ({len(mmsi_batch)} MMSI), "
                        f"first={mmsi_batch[0]}, no traffic within 5s (normal)"
                    )
                    first_msg = None

            reconnect_delay = None
            consecutive_handshake_failures = 0
            stats.setdefault("failed_batches", {}).pop(batch_id, None)

            if first_msg is not None:
                row = parse_position(first_msg, mmsi_to_imo)
                if row:
                    inserted = await safe_insert_position(conn, row, db_write_lock)
                    stats["messages"] += 1
                    if inserted:
                        stats["inserted"] += 1

            async for raw in ws:
                if stop_at and datetime.now(timezone.utc) >= stop_at:
                    log(f"batch[{batch_id}] stop time reached (in-loop)")
                    await ws.close()
                    conn.close()
                    return
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("error") or msg.get("Error"):
                    log(f"batch[{batch_id}] server error: {msg}")
                    continue
                row = parse_position(msg, mmsi_to_imo)
                if not row:
                    continue
                inserted = await safe_insert_position(conn, row, db_write_lock)
                stats["messages"] += 1
                if inserted:
                    stats["inserted"] += 1
                    if stats["inserted"] <= 30 or stats["inserted"] % 50 == 0:
                        log(
                            f"POS imo={row['imo']} mmsi={row['mmsi']} "
                            f"lat={row['lat']:.4f} lon={row['lon']:.4f} "
                            f"sog={row['speed_knots']}"
                        )
        except asyncio.CancelledError:
            if ws is not None:
                await ws.close()
            conn.close()
            raise
        except Exception as e:
            if not subscribed_ok:
                consecutive_handshake_failures += 1
                stats.setdefault("failed_batches", {})[batch_id] = {
                    "mmsi_batch": mmsi_batch,
                    "consecutive_failures": consecutive_handshake_failures,
                    "last_error": repr(e),
                    "last_attempt_utc": datetime.now(timezone.utc).isoformat(),
                    "status": "handshake_failed",
                }
                reconnect_delay = next_backoff(
                    reconnect_delay, initial=backoff_initial, maximum=backoff_max
                )
                log(
                    f"batch[{batch_id}] handshake failure "
                    f"{consecutive_handshake_failures}/{unreachable_threshold}: {e!r}; "
                    f"retry in {reconnect_delay:.0f}s"
                )
                if consecutive_handshake_failures >= unreachable_threshold:
                    stats["failed_batches"][batch_id]["status"] = "unreachable_this_session"
                    log(
                        f"batch[{batch_id}] UNREACHABLE this session after "
                        f"{consecutive_handshake_failures} consecutive handshake failures "
                        f"({len(mmsi_batch)} MMSI affected): {e!r}"
                    )
                    write_unreachable_report(all_batches, stats)
                    break
            else:
                stats.setdefault("failed_batches", {}).pop(batch_id, None)
                reconnect_delay = next_backoff(
                    reconnect_delay, initial=backoff_initial, maximum=backoff_max
                )
                log(
                    f"batch[{batch_id}] WebSocket disconnect after subscribe: {e!r}; "
                    f"backoff retry in {reconnect_delay:.0f}s"
                )
            log(traceback.format_exc().splitlines()[-1])
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            await asyncio.sleep(reconnect_delay or backoff_initial)
        else:
            stats.setdefault("failed_batches", {}).pop(batch_id, None)
            reconnect_delay = next_backoff(
                reconnect_delay, initial=backoff_initial, maximum=backoff_max
            )
            log(
                f"batch[{batch_id}] stream ended cleanly; "
                f"backoff retry in {reconnect_delay:.0f}s"
            )
            await asyncio.sleep(reconnect_delay)

    conn.close()


def write_unreachable_report(batches: list[list[str]], stats: dict) -> None:
    failed = stats.get("failed_batches", {})
    unreachable_threshold = stats.get("unreachable_threshold", CONSECUTIVE_FAILURES_UNREACHABLE)
    unreachable = {
        bid: info
        for bid, info in failed.items()
        if info.get("consecutive_failures", 0) >= unreachable_threshold
        or info.get("status") == "unreachable_this_session"
    }
    if not unreachable:
        return
    total_mmsi = sum(len(info["mmsi_batch"]) for info in unreachable.values())
    HISTORY.mkdir(parents=True, exist_ok=True)
    UNREACHABLE_PATH.write_text(
        json.dumps(
            {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "unreachable_batch_count": len(unreachable),
                "total_batches": len(batches),
                "unreachable_mmsi_count": total_mmsi,
                "honest_degradation": True,
                "batches": unreachable,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log(
        f"WARNING: {len(unreachable)}/{len(batches)} batch(es) unreachable this "
        f"session ({total_mmsi} MMSI) — see {UNREACHABLE_PATH}"
    )


async def run_collector(
    test_minutes: float | None = None,
    max_batches: int | None = None,
    stagger_seconds: float | None = None,
    max_concurrent_handshakes: int | None = None,
    *,
    config_path: Path | None = None,
) -> None:
    settings = load_aisstream_settings(config_path)
    stagger_seconds = (
        stagger_seconds
        if stagger_seconds is not None
        else float(settings["stagger_seconds"])
    )
    max_concurrent_handshakes = (
        max_concurrent_handshakes
        if max_concurrent_handshakes is not None
        else int(settings["max_concurrent_handshakes"])
    )
    if max_batches is None and settings.get("max_batches") is not None:
        max_batches = int(settings["max_batches"])

    unreachable_threshold = int(settings["unreachable_after_consecutive_failures"])
    backoff_initial = float(settings["reconnect_backoff_initial_seconds"])
    backoff_max = float(settings["reconnect_backoff_max_seconds"])
    sqlite_timeout = float(settings["sqlite_timeout_seconds"])
    ws_url = str(settings["websocket_url"])
    mmsi_batch_size = int(settings["mmsi_batch_size"])

    db_path = ROOT / settings["db_path"] if settings.get("db_path") else DB_PATH
    targets_path = (
        ROOT / settings["targets_path"] if settings.get("targets_path") else TARGETS_PATH
    )

    api_key = os.getenv("AISSTREAM_API_KEY", "").strip()
    if not api_key or api_key.startswith("YOUR_"):
        log("ERROR: Set AISSTREAM_API_KEY in .env (see .env.example)")
        raise SystemExit(1)

    if not targets_path.exists():
        log("ERROR: targets.json missing — run prepare_targets.py first")
        raise SystemExit(1)

    HISTORY.mkdir(parents=True, exist_ok=True)
    mmsi_to_imo = load_mmsi_to_imo(targets_path)
    batches = load_batches(
        max_batches=max_batches,
        targets_path=targets_path,
        mmsi_batch_size=mmsi_batch_size,
    )
    ramp_up_seconds = max(0, len(batches) - 1) * stagger_seconds
    log(
        f"Starting collector: {len(batches)} WebSocket batch(es) x <={mmsi_batch_size} MMSI "
        f"(mapped IMO={len(mmsi_to_imo)}) | stagger={stagger_seconds}s "
        f"| max_concurrent_handshakes={max_concurrent_handshakes} "
        f"| backoff={backoff_initial}s→{backoff_max}s "
        f"| sqlite_wal timeout={sqlite_timeout}s "
        f"| full ramp-up ~{ramp_up_seconds:.0f}s"
    )
    log(
        "RISK NOTE: AISstream.io publishes no numeric limit on concurrent "
        "connections per key (only unspecified 'throttling'). If repeated "
        "handshake failures appear below after some batch N, that N is the "
        "real empirical limit — check logs and targets.json accordingly."
    )

    stop_at = None
    if test_minutes:
        stop_at = datetime.now(timezone.utc) + timedelta(minutes=test_minutes)
        log(f"TEST MODE: will stop at {stop_at.isoformat()}")

    stats: dict = {
        "messages": 0,
        "inserted": 0,
        "failed_batches": {},
        "unreachable_threshold": unreachable_threshold,
    }
    db_write_lock = asyncio.Lock()
    handshake_sem = asyncio.Semaphore(max(1, max_concurrent_handshakes))
    tasks = [
        asyncio.create_task(
            batch_worker(
                i,
                batch,
                api_key,
                mmsi_to_imo,
                db_path,
                stop_at,
                stats,
                stagger_seconds,
                handshake_sem,
                db_write_lock,
                batches,
                ws_url=ws_url,
                unreachable_threshold=unreachable_threshold,
                backoff_initial=backoff_initial,
                backoff_max=backoff_max,
                sqlite_timeout=sqlite_timeout,
            )
        )
        for i, batch in enumerate(batches)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
            log(f"batch[{i}] CRASHED (unexpected, not retried): {r!r}")

    write_unreachable_report(batches, stats)
    log(f"Collector finished. messages={stats['messages']} inserted={stats['inserted']}")


def main() -> int:
    settings = load_aisstream_settings()
    parser = argparse.ArgumentParser(description="AISstream continuous collector")
    parser.add_argument(
        "--test-minutes",
        type=float,
        default=None,
        help="Run for N minutes then exit (smoke test)",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=settings.get("max_batches"),
        help="Limit number of parallel WS batches (default: all). Use 1–2 for smoke tests.",
    )
    parser.add_argument(
        "--stagger-seconds",
        type=float,
        default=float(settings["stagger_seconds"]),
        help=f"Delay between batch startups (default: {settings['stagger_seconds']}s)",
    )
    parser.add_argument(
        "--max-concurrent-handshakes",
        type=int,
        default=int(settings["max_concurrent_handshakes"]),
        help=(
            "Max simultaneous connect+subscribe handshakes in flight "
            f"(default: {settings['max_concurrent_handshakes']}). Does not limit "
            "steady-state open sockets, only the risky connect burst."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="Path to config.yaml (aisstream section)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(
            run_collector(
                args.test_minutes,
                args.max_batches,
                args.stagger_seconds,
                args.max_concurrent_handshakes,
                config_path=args.config,
            )
        )
    except KeyboardInterrupt:
        log("Interrupted by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
