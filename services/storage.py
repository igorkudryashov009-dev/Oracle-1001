"""Async-capable AIS position storage: SQLite primary + optional PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("sentinel.storage")

ROOT = Path(__file__).resolve().parents[1]


def _default_db_path() -> Path:
    """Prefer SENTINEL_DB_PATH (container) → repo-relative история1/."""
    env = (os.getenv("SENTINEL_DB_PATH") or "").strip()
    if env:
        return Path(env)
    return ROOT / "история1" / "sentinel_ais.db"


DEFAULT_DB = _default_db_path()

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

TELEMETRY_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    mps REAL,
    matched_count INTEGER,
    unmatched_count INTEGER,
    insert_latency_ms REAL,
    ws_uptime_sec REAL,
    reconnects INTEGER,
    heartbeat_ok INTEGER
)
"""

DEAD_LETTER_DDL = """
CREATE TABLE IF NOT EXISTS dead_letter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    reason TEXT,
    payload TEXT
)
"""

PG_POSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS ais_positions (
    id BIGSERIAL PRIMARY KEY,
    imo TEXT,
    mmsi TEXT NOT NULL,
    vessel_name TEXT,
    tier TEXT,
    timestamp_utc TIMESTAMPTZ NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    sog DOUBLE PRECISION,
    cog DOUBLE PRECISION,
    heading DOUBLE PRECISION,
    nav_status TEXT,
    draft_m DOUBLE PRECISION,
    destination TEXT,
    matched INTEGER NOT NULL DEFAULT 0,
    message_type TEXT,
    received_at TIMESTAMPTZ NOT NULL,
    UNIQUE(mmsi, timestamp_utc)
)
"""


class AISStorage:
    """Unified storage facade. SQLite always works; Postgres used when DATABASE_URL set."""

    def __init__(
        self,
        sqlite_path: Path | None = None,
        database_url: str | None = None,
        batch_size: int = 50,
        flush_interval_sec: float = 1.0,
    ):
        self.sqlite_path = Path(sqlite_path or DEFAULT_DB)
        self.database_url = (database_url or os.getenv("DATABASE_URL") or "").strip()
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._conn: Optional[sqlite3.Connection] = None
        self._pg_pool = None
        self._flush_task: Optional[asyncio.Task] = None
        self.last_insert_latency_ms: float = 0.0
        self.total_inserted: int = 0
        self.total_dead_letters: int = 0

    def open(self) -> None:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.sqlite_path),
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass
        self._conn.execute(POSITIONS_DDL)
        self._conn.execute(TELEMETRY_DDL)
        self._conn.execute(DEAD_LETTER_DDL)
        try:
            from services.archive_schema import (
                VESSEL_DAILY_ARCHIVE_DDL,
                VESSEL_DAILY_ARCHIVE_INDEXES,
            )

            self._conn.execute(VESSEL_DAILY_ARCHIVE_DDL)
            for stmt in VESSEL_DAILY_ARCHIVE_INDEXES:
                self._conn.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive schema ensure skipped: %s", exc)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ais_mmsi_ts ON ais_positions(mmsi, timestamp_utc)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ais_imo_ts ON ais_positions(imo, timestamp_utc)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ais_tier ON ais_positions(tier)"
        )
        # TTF forecasting feature store (idempotent)
        try:
            from services.ttf_forecast.schema import (
                INDEX_DDL as TTF_INDEX_DDL,
                TTF_MARKET_FEATURES_DDL,
                TTF_PREDICTIONS_STORE_DDL,
            )

            self._conn.execute(TTF_MARKET_FEATURES_DDL)
            self._conn.execute(TTF_PREDICTIONS_STORE_DDL)
            for ddl in TTF_INDEX_DDL:
                self._conn.execute(ddl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TTF schema migrate skipped: %s", exc)
        self._conn.commit()
        logger.info("SQLite AIS store ready: %s", self.sqlite_path)

    async def open_async(self) -> None:
        self.open()
        if self.database_url.startswith(("postgres://", "postgresql://")):
            try:
                import asyncpg  # type: ignore

                self._pg_pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=4)
                async with self._pg_pool.acquire() as conn:
                    await conn.execute(PG_POSITIONS_DDL)
                logger.info("PostgreSQL / Timescale pool ready via DATABASE_URL")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Postgres unavailable (%s) — continuing with SQLite only", exc)
                self._pg_pool = None
        self._flush_task = asyncio.create_task(self._flush_loop(), name="ais-batch-flush")

    async def close(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush()
        if self._pg_pool:
            await self._pg_pool.close()
        if self._conn:
            self._conn.close()
            self._conn = None

    async def enqueue(self, row: dict[str, Any]) -> None:
        await self._queue.put(row)

    async def write_dead_letter(self, reason: str, payload: Any) -> None:
        self.total_dead_letters += 1
        if not self._conn:
            return
        try:
            self._conn.execute(
                "INSERT INTO dead_letter (ts_utc, reason, payload) VALUES (?, ?, ?)",
                (
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    reason,
                    json.dumps(payload, default=str)[:8000],
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("dead_letter write failed: %s", exc)

    def write_telemetry(self, row: dict[str, Any]) -> None:
        if not self._conn:
            return
        try:
            self._conn.execute(
                """
                INSERT INTO pipeline_telemetry
                (ts_utc, mps, matched_count, unmatched_count, insert_latency_ms,
                 ws_uptime_sec, reconnects, heartbeat_ok)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("ts_utc") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    float(row.get("mps") or 0.0),
                    int(row.get("matched_count") or 0),
                    int(row.get("unmatched_count") or 0),
                    float(row.get("insert_latency_ms") or 0.0),
                    float(row.get("ws_uptime_sec") or 0.0),
                    int(row.get("reconnects") or 0),
                    1 if row.get("heartbeat_ok") else 0,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("telemetry write failed: %s", exc)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval_sec)
            await self.flush()

    async def flush(self) -> int:
        batch: list[dict[str, Any]] = []
        while not self._queue.empty() and len(batch) < max(self.batch_size * 4, 200):
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            return 0
        t0 = time.perf_counter()
        n = self._insert_sqlite_batch(batch)
        if self._pg_pool:
            try:
                await self._insert_pg_batch(batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Postgres batch insert failed: %s", exc)
                await self.write_dead_letter("pg_insert_failed", {"error": str(exc), "n": len(batch)})
        self.last_insert_latency_ms = (time.perf_counter() - t0) * 1000.0
        self.total_inserted += n
        return n

    def _insert_sqlite_batch(self, batch: list[dict[str, Any]]) -> int:
        if not self._conn:
            return 0
        rows = [
            (
                r.get("imo"),
                r["mmsi"],
                r.get("vessel_name"),
                r.get("tier"),
                r["timestamp_utc"],
                r["lat"],
                r["lon"],
                r.get("sog"),
                r.get("cog"),
                r.get("heading"),
                r.get("nav_status"),
                r.get("draft_m"),
                r.get("destination"),
                1 if r.get("matched") else 0,
                r.get("message_type"),
                r.get("received_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            for r in batch
        ]
        try:
            cur = self._conn.executemany(
                """
                INSERT OR IGNORE INTO ais_positions
                (imo, mmsi, vessel_name, tier, timestamp_utc, lat, lon, sog, cog, heading,
                 nav_status, draft_m, destination, matched, message_type, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(rows)
        except sqlite3.Error as exc:
            logger.error("SQLite batch insert error: %s", exc)
            return 0

    async def _insert_pg_batch(self, batch: list[dict[str, Any]]) -> None:
        assert self._pg_pool is not None
        records = [
            (
                r.get("imo"),
                r["mmsi"],
                r.get("vessel_name"),
                r.get("tier"),
                r["timestamp_utc"],
                r["lat"],
                r["lon"],
                r.get("sog"),
                r.get("cog"),
                r.get("heading"),
                r.get("nav_status"),
                r.get("draft_m"),
                r.get("destination"),
                1 if r.get("matched") else 0,
                r.get("message_type"),
                r.get("received_at"),
            )
            for r in batch
        ]
        async with self._pg_pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO ais_positions
                (imo, mmsi, vessel_name, tier, timestamp_utc, lat, lon, sog, cog, heading,
                 nav_status, draft_m, destination, matched, message_type, received_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                ON CONFLICT (mmsi, timestamp_utc) DO NOTHING
                """,
                records,
            )

    def fetch_recent(self, limit: int = 5000, matched_only: bool = False) -> list[dict[str, Any]]:
        if not self._conn:
            self.open()
        assert self._conn is not None
        sql = "SELECT * FROM ais_positions"
        if matched_only:
            sql += " WHERE matched=1"
        sql += " ORDER BY id DESC LIMIT ?"
        cur = self._conn.execute(sql, (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def fetch_latest_positions_for_mmsis(
        self,
        mmsis: list[str],
        *,
        matched_only: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Latest row per MMSI for an explicit fleet universe (e.g. TOP-500).

        Avoids the fetch_recent(limit=N) trap where unmatched bbox noise dilutes
        the window and collapses live TOP-500 coverage to ~N≈20.
        Empty result is intentional — callers keep synthetic/stale fallback.
        """
        if not self._conn:
            self.open()
        assert self._conn is not None
        clean = [str(m).strip() for m in mmsis if str(m).strip()]
        if not clean:
            return []
        out: list[dict[str, Any]] = []
        # SQLite default max variable number is 999; chunk conservatively.
        chunk = 400
        for i in range(0, len(clean), chunk):
            part = clean[i : i + chunk]
            placeholders = ",".join("?" * len(part))
            match_clause = " AND matched=1" if matched_only else ""
            sql = f"""
                SELECT p.* FROM ais_positions p
                INNER JOIN (
                    SELECT mmsi, MAX(id) AS mid
                    FROM ais_positions
                    WHERE mmsi IN ({placeholders}){match_clause}
                    GROUP BY mmsi
                ) t ON p.id = t.mid
            """
            cur = self._conn.execute(sql, part)
            cols = [d[0] for d in cur.description]
            out.extend(dict(zip(cols, row)) for row in cur.fetchall())
        return out

    def fetch_telemetry(self, limit: int = 120) -> list[dict[str, Any]]:
        if not self._conn:
            self.open()
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT * FROM pipeline_telemetry ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def purge_older_than(self, days: int = 7) -> int:
        """Delete ais_positions older than ``days``; returns deleted row estimate."""
        if not self._conn:
            self.open()
        assert self._conn is not None
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn.execute(
            "DELETE FROM ais_positions WHERE timestamp_utc < ? OR received_at < ?",
            (cutoff, cutoff),
        )
        self._conn.commit()
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if deleted:
            try:
                self._conn.execute("PRAGMA incremental_vacuum(128)")
                self._conn.commit()
            except sqlite3.Error as exc:
                logger.warning("incremental_vacuum skipped: %s", exc)
        logger.info("Retention purge days=%d deleted≈%s cutoff=%s", days, deleted, cutoff)
        return deleted
