#!/usr/bin/env python3
"""GIS AIS tracker — Contract 1.8.0-ops-gis-sot.

Cache-first fleet positions for Edge (:8765) and FastAPI (:8766).

Honesty rules (AGENTS.md):
  - Does **not** invent satellite provider endpoints/credentials.
  - Default source = local G3 terrestrial replica (``ais_positions``).
  - ``SatelliteAISAdapter`` remains stub; commercial sat feed stays inactive
    until Architect supplies a real provider + key.
  - Never labels output as PREMIUM SATELLITE without a live commercial feed.
  - This module does **not** feed Dual Gate ``fleet_sample_status``.

Routes:
  GET /api/v1/gis/ais/status
  GET /api/v1/gis/ais/positions   (GeoJSON FeatureCollection)
  GET /api/v1/gis/ais/vessel/{imo}
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.ais_tracker")

CONTRACT_VERSION = "1.8.0-ops-gis-sot"
CACHE_TTL_SEC = int(os.getenv("AIS_TRACKER_CACHE_TTL_SEC", str(6 * 3600)))  # 6h
MONTHLY_BUDGET_USD = float(os.getenv("AIS_TRACKER_MONTHLY_BUDGET_USD", "30"))
# Soft accounting: estimated USD per paid upstream call (when sat feed wired)
USD_PER_PAID_CALL = float(os.getenv("AIS_TRACKER_USD_PER_CALL", "0.02"))

# Live AIS WebSocket ingest remains owned by ``services.aisstream_connector``
# (G3 single_persistent). This tracker is cache-first and must NOT open a
# second AISstream socket. Key is resolved via unified registry for status.
def resolve_aisstream_key_configured() -> bool:
    try:
        from services.config_keys import get_key

        return bool(get_key("AISSTREAM_KEY"))
    except Exception:  # noqa: BLE001
        return bool((os.getenv("AISSTREAM_API_KEY") or os.getenv("AISSTREAM_KEY") or "").strip())

DB_PATH = Path(
    os.getenv("AIS_HISTORY_DB") or (ROOT / "data" / "ais_history.db")
)
USAGE_DB = Path(
    os.getenv("AIS_TRACKER_USAGE_DB")
    or (ROOT / "data" / "archive" / "ais_tracker_budget.sqlite")
)

_LOCK = threading.RLock()
_DB_INIT = False

_AIS_PATH_RE = re.compile(
    r"^/api/v1/gis/ais(?:/(?P<action>status|positions)|/vessel/(?P<imo>\d+))?/?$",
    re.IGNORECASE,
)


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _now_epoch() -> int:
    return int(time.time())


def _resolve_live_ais_db() -> Optional[Path]:
    try:
        from services.quant_risk_service import resolve_db_path

        p = resolve_db_path()
        return p if p.is_file() else None
    except Exception:  # noqa: BLE001
        candidates = [
            ROOT / "история1" / "sentinel_ais.db",
            ROOT / "sentinel_ais.db",
        ]
        for c in candidates:
            if c.is_file():
                return c
        return None


def _satellite_key() -> str:
    return (
        os.getenv("SATELLITE_AIS_API_KEY")
        or os.getenv("SPIRE_AIS_API_KEY")
        or os.getenv("ORBCOMM_AIS_API_KEY")
        or ""
    ).strip()


def _ensure_db() -> None:
    global _DB_INIT
    with _LOCK:
        if _DB_INIT and DB_PATH.is_file():
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(DB_PATH))
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS vessel_positions (
                    imo INTEGER,
                    mmsi INTEGER,
                    name TEXT,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    speed REAL,
                    heading REAL,
                    updated_at INTEGER NOT NULL,
                    source TEXT,
                    PRIMARY KEY (mmsi)
                );
                CREATE INDEX IF NOT EXISTS idx_vessel_positions_imo
                    ON vessel_positions(imo);
                CREATE TABLE IF NOT EXISTS vessel_track (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mmsi INTEGER NOT NULL,
                    imo INTEGER,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    speed REAL,
                    heading REAL,
                    timestamp INTEGER NOT NULL,
                    source TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_vessel_track_imo_ts
                    ON vessel_track(imo, timestamp);
                CREATE INDEX IF NOT EXISTS idx_vessel_track_mmsi_ts
                    ON vessel_track(mmsi, timestamp);
                CREATE TABLE IF NOT EXISTS tracker_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            con.commit()
        finally:
            con.close()
        _ensure_budget_db()
        _DB_INIT = True


def _ensure_budget_db() -> None:
    USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(USAGE_DB))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS ais_tracker_budget (
                month TEXT PRIMARY KEY,
                paid_calls INTEGER NOT NULL DEFAULT 0,
                spent_usd REAL NOT NULL DEFAULT 0,
                updated_at TEXT
            )
            """
        )
        con.commit()
    finally:
        con.close()


def get_budget_snapshot() -> dict[str, Any]:
    _ensure_db()
    m = _month_key()
    with _LOCK:
        con = sqlite3.connect(str(USAGE_DB))
        try:
            row = con.execute(
                "SELECT paid_calls, spent_usd FROM ais_tracker_budget WHERE month=?",
                (m,),
            ).fetchone()
        finally:
            con.close()
    paid = int(row[0]) if row else 0
    spent = float(row[1]) if row else 0.0
    hard_stop = spent >= MONTHLY_BUDGET_USD
    return {
        "month": m,
        "paid_calls": paid,
        "spent_usd": round(spent, 4),
        "budget_usd": MONTHLY_BUDGET_USD,
        "hard_stop": hard_stop,
        "remaining_usd": round(max(0.0, MONTHLY_BUDGET_USD - spent), 4),
    }


def _record_paid_call() -> None:
    """Account a commercial upstream call toward the $30/mo hard-stop."""
    _ensure_db()
    m = _month_key()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _LOCK:
        con = sqlite3.connect(str(USAGE_DB))
        try:
            con.execute(
                """
                INSERT INTO ais_tracker_budget(month, paid_calls, spent_usd, updated_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(month) DO UPDATE SET
                    paid_calls = paid_calls + 1,
                    spent_usd = spent_usd + excluded.spent_usd,
                    updated_at = excluded.updated_at
                """,
                (m, USD_PER_PAID_CALL, now),
            )
            con.commit()
        finally:
            con.close()


def _upsert_position(
    *,
    mmsi: int,
    imo: Optional[int],
    name: Optional[str],
    lat: float,
    lon: float,
    speed: Optional[float],
    heading: Optional[float],
    updated_at: int,
    source: str,
    write_track: bool = True,
) -> None:
    _ensure_db()
    with _LOCK:
        con = sqlite3.connect(str(DB_PATH))
        try:
            con.execute(
                """
                INSERT INTO vessel_positions(
                    imo, mmsi, name, lat, lon, speed, heading, updated_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mmsi) DO UPDATE SET
                    imo = COALESCE(excluded.imo, vessel_positions.imo),
                    name = COALESCE(excluded.name, vessel_positions.name),
                    lat = excluded.lat,
                    lon = excluded.lon,
                    speed = excluded.speed,
                    heading = excluded.heading,
                    updated_at = excluded.updated_at,
                    source = excluded.source
                """,
                (imo, mmsi, name, lat, lon, speed, heading, updated_at, source),
            )
            if write_track:
                con.execute(
                    """
                    INSERT INTO vessel_track(
                        mmsi, imo, lat, lon, speed, heading, timestamp, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mmsi, imo, lat, lon, speed, heading, updated_at, source),
                )
            con.commit()
        finally:
            con.close()


def _parse_ts_epoch(value: Any) -> int:
    if value is None:
        return _now_epoch()
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return _now_epoch()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return _now_epoch()


def sync_from_local_g3(*, max_age_sec: int | None = None) -> dict[str, Any]:
    """Refresh ais_history from local terrestrial replica (zero-cost).

    Default window: 90 days (env ``AIS_TRACKER_SYNC_MAX_AGE_SEC``). Local soak
    replicas may lag wall-clock; a tight 7d window would empty the GIS layer.
    """
    if max_age_sec is None:
        max_age_sec = int(os.getenv("AIS_TRACKER_SYNC_MAX_AGE_SEC", str(90 * 24 * 3600)))
    max_vessels = int(os.getenv("AIS_TRACKER_SYNC_MAX_VESSELS", "2000"))
    live = _resolve_live_ais_db()
    if live is None:
        return {"ok": False, "error": "sentinel_ais_db_missing", "synced": 0}
    cutoff = _now_epoch() - int(max_age_sec)
    try:
        src = sqlite3.connect(str(live))
        try:
            rows = src.execute(
                """
                SELECT mmsi, imo, vessel_name, lat, lon, sog, heading, cog,
                       received_at, timestamp_utc
                FROM ais_positions
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                ORDER BY received_at DESC
                LIMIT 80000
                """
            ).fetchall()
        finally:
            src.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"sqlite_{exc}", "synced": 0}

    batch: list[tuple[Any, ...]] = []
    track_batch: list[tuple[Any, ...]] = []
    seen: set[int] = set()
    for r in rows:
        if len(batch) >= max_vessels:
            break
        try:
            mmsi = int(str(r[0]).strip())
        except (TypeError, ValueError):
            continue
        if mmsi in seen:
            continue
        ts = _parse_ts_epoch(r[8] or r[9])
        if ts < cutoff:
            continue
        seen.add(mmsi)
        imo: Optional[int] = None
        if r[1] not in (None, ""):
            try:
                imo = int(str(r[1]).strip())
            except ValueError:
                imo = None
        heading = r[6] if r[6] is not None else r[7]
        try:
            lat = float(r[3])
            lon = float(r[4])
        except (TypeError, ValueError):
            continue
        speed = float(r[5]) if r[5] is not None else None
        hdg = float(heading) if heading is not None else None
        name = str(r[2]) if r[2] else None
        batch.append((imo, mmsi, name, lat, lon, speed, hdg, ts, "local_g3"))
        track_batch.append((mmsi, imo, lat, lon, speed, hdg, ts, "local_g3"))

    _ensure_db()
    with _LOCK:
        dst = sqlite3.connect(str(DB_PATH))
        try:
            dst.executemany(
                """
                INSERT INTO vessel_positions(
                    imo, mmsi, name, lat, lon, speed, heading, updated_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mmsi) DO UPDATE SET
                    imo = COALESCE(excluded.imo, vessel_positions.imo),
                    name = COALESCE(excluded.name, vessel_positions.name),
                    lat = excluded.lat,
                    lon = excluded.lon,
                    speed = excluded.speed,
                    heading = excluded.heading,
                    updated_at = excluded.updated_at,
                    source = excluded.source
                """,
                batch,
            )
            # Track: one point per vessel per sync (avoid exploding history)
            dst.executemany(
                """
                INSERT INTO vessel_track(
                    mmsi, imo, lat, lon, speed, heading, timestamp, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                track_batch,
            )
            dst.commit()
        finally:
            dst.close()
    _meta_set("last_sync_epoch", str(_now_epoch()))
    return {
        "ok": True,
        "synced": len(batch),
        "source_db": str(live).replace("\\", "/"),
    }


def _cache_fresh(updated_at: int) -> bool:
    return (_now_epoch() - int(updated_at or 0)) <= CACHE_TTL_SEC


def _sync_cache_fresh() -> bool:
    raw = _meta_get("last_sync_epoch")
    if not raw:
        return False
    try:
        return _cache_fresh(int(raw))
    except ValueError:
        return False


def _meta_get(key: str) -> Optional[str]:
    _ensure_db()
    with _LOCK:
        con = sqlite3.connect(str(DB_PATH))
        try:
            row = con.execute(
                "SELECT value FROM tracker_meta WHERE key=?", (key,)
            ).fetchone()
            return str(row[0]) if row else None
        finally:
            con.close()


def _meta_set(key: str, value: str) -> None:
    _ensure_db()
    with _LOCK:
        con = sqlite3.connect(str(DB_PATH))
        try:
            con.execute(
                """
                INSERT INTO tracker_meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            con.commit()
        finally:
            con.close()


def _try_commercial_refresh(imo: int) -> Optional[dict[str, Any]]:
    """Commercial satellite refresh — blocked until real provider is wired.

    Never invents endpoints. Returns None and leaves cache as SoT.
    """
    budget = get_budget_snapshot()
    if budget["hard_stop"]:
        LOG.info("ais_tracker hard-stop: monthly budget $%.2f exhausted", MONTHLY_BUDGET_USD)
        return None
    key = _satellite_key()
    if not key:
        return None
    # Key present but SatelliteAISAdapter is stub — do not fabricate HTTP calls.
    LOG.info(
        "ais_tracker: SAT key present but SatelliteAISAdapter not activated "
        "(imo=%s) — serving cache only",
        imo,
    )
    return None


def list_positions(*, refresh: bool = True) -> tuple[dict[str, Any], str]:
    """Return GeoJSON FeatureCollection + X-AIS-Source value."""
    _ensure_db()
    source = "cache"
    if refresh:
        # 6h sync throttle (independent of vessel position age in soak replica)
        if _sync_cache_fresh():
            source = "cache"
        else:
            result = sync_from_local_g3()
            if result.get("ok") and int(result.get("synced") or 0) > 0:
                source = "local_g3"
            else:
                source = "cache"

    with _LOCK:
        con = sqlite3.connect(str(DB_PATH))
        try:
            rows = con.execute(
                """
                SELECT imo, mmsi, name, lat, lon, speed, heading, updated_at, source
                FROM vessel_positions
                ORDER BY updated_at DESC
                """
            ).fetchall()
        finally:
            con.close()

    features = []
    for r in rows:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r[4], r[3]]},
                "properties": {
                    "imo": r[0],
                    "mmsi": r[1],
                    "name": r[2],
                    "speed": r[5],
                    "heading": r[6],
                    "updated_at": r[7],
                    "source": r[8] or source,
                },
            }
        )
    payload = {
        "type": "FeatureCollection",
        "features": features,
        "contract_version": CONTRACT_VERSION,
        "count": len(features),
        "ais_source": source,
        "note": (
            "Positions from local G3 cache / terrestrial replica. "
            "Not commercial satellite AIS."
        ),
    }
    return payload, source


def get_vessel_by_imo(imo: int) -> tuple[dict[str, Any], str]:
    _ensure_db()
    source = "cache"
    # Attempt commercial refresh only if cache stale — still no invented API
    with _LOCK:
        con = sqlite3.connect(str(DB_PATH))
        try:
            latest = con.execute(
                """
                SELECT imo, mmsi, name, lat, lon, speed, heading, updated_at, source
                FROM vessel_positions
                WHERE imo = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (imo,),
            ).fetchone()
        finally:
            con.close()

    if latest is None or not _cache_fresh(int(latest[7])):
        # Prefer local G3 sync for this IMO
        sync_from_local_g3()
        with _LOCK:
            con = sqlite3.connect(str(DB_PATH))
            try:
                latest = con.execute(
                    """
                    SELECT imo, mmsi, name, lat, lon, speed, heading, updated_at, source
                    FROM vessel_positions
                    WHERE imo = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (imo,),
                ).fetchone()
            finally:
                con.close()
        if latest is not None:
            source = latest[8] or "local_g3"
        else:
            _try_commercial_refresh(imo)
            source = "cache"
    else:
        source = latest[8] or "cache"

    track: list[dict[str, Any]] = []
    with _LOCK:
        con = sqlite3.connect(str(DB_PATH))
        try:
            track_rows = con.execute(
                """
                SELECT lat, lon, speed, heading, timestamp, source
                FROM vessel_track
                WHERE imo = ?
                ORDER BY timestamp ASC
                LIMIT 500
                """,
                (imo,),
            ).fetchall()
        finally:
            con.close()
    for t in track_rows:
        track.append(
            {
                "lat": t[0],
                "lon": t[1],
                "speed": t[2],
                "heading": t[3],
                "timestamp": t[4],
                "source": t[5],
            }
        )

    if latest is None:
        payload = {
            "ok": False,
            "imo": imo,
            "error": "vessel_not_found",
            "position": None,
            "trajectory": track,
            "contract_version": CONTRACT_VERSION,
            "ais_source": source,
        }
        return payload, source

    payload = {
        "ok": True,
        "imo": latest[0],
        "mmsi": latest[1],
        "name": latest[2],
        "position": {
            "lat": latest[3],
            "lon": latest[4],
            "speed": latest[5],
            "heading": latest[6],
            "updated_at": latest[7],
        },
        "trajectory": track,
        "contract_version": CONTRACT_VERSION,
        "ais_source": source,
        "cache_ttl_seconds": CACHE_TTL_SEC,
        "note": "Not commercial satellite AIS unless provider activated.",
    }
    return payload, source


def public_status() -> dict[str, Any]:
    _ensure_db()
    with _LOCK:
        con = sqlite3.connect(str(DB_PATH))
        try:
            count = con.execute("SELECT COUNT(*) FROM vessel_positions").fetchone()[0]
            freshest = con.execute(
                "SELECT MAX(updated_at) FROM vessel_positions"
            ).fetchone()[0]
        finally:
            con.close()
    key_present = bool(_satellite_key())
    aisstream_cfg = resolve_aisstream_key_configured()
    return {
        "status": "active_local_cache" if count else "active_empty",
        "contract_version": CONTRACT_VERSION,
        "tracked_vessels": int(count or 0),
        "cache_ttl_seconds": CACHE_TTL_SEC,
        "freshest_updated_at": freshest,
        "provider": {
            "mode": "local_g3_cache",
            "satellite_adapter": "stub_not_activated",
            "api_key_configured": key_present,
            "aisstream_key_configured": aisstream_cfg,
            "ingest_owner": "services.aisstream_connector",
            "spatial_index": "services.spatial_index",
            "commercial_feed": False,
            "note": (
                "Zero-cost path: sync from sentinel_ais.db. "
                "Live WS is G3 single_persistent via aisstream_connector — "
                "this tracker does not open a second AISstream socket. "
                "Commercial satellite requires Architect-selected provider + real key."
            ),
        },
        "budget": get_budget_snapshot(),
        "db_path": str(DB_PATH).replace("\\", "/"),
        "route_prefix": "/api/v1/gis/ais",
    }


def parse_ais_path(path: str) -> Optional[dict[str, Any]]:
    """Parse /api/v1/gis/ais/... for Edge BaseHTTP handler."""
    raw = (path or "").split("?", 1)[0]
    if raw.startswith("/output/"):
        raw = raw[len("/output") :]
    m = _AIS_PATH_RE.match(raw)
    if not m:
        # Also accept /api/v1/gis/ais exactly
        if raw.rstrip("/") in {"/api/v1/gis/ais"}:
            return {"action": "status"}
        return None
    if m.group("imo"):
        return {"action": "vessel", "imo": int(m.group("imo"))}
    action = (m.group("action") or "status").lower()
    if action not in {"status", "positions"}:
        return None
    return {"action": action}


def handle_ais_request(path: str) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Edge-friendly dispatcher. Returns (status, body_dict, extra_headers)."""
    spec = parse_ais_path(path)
    headers = {
        "X-Contract-Version": CONTRACT_VERSION,
    }
    if not spec:
        return 404, {"ok": False, "error": "not_found", "contract_version": CONTRACT_VERSION}, headers

    action = spec["action"]
    if action == "status":
        body = {"ok": True, **public_status()}
        headers["X-AIS-Source"] = "local_g3_cache"
        return 200, body, headers
    if action == "positions":
        payload, source = list_positions()
        headers["X-AIS-Source"] = source
        return 200, payload, headers
    if action == "vessel":
        payload, source = get_vessel_by_imo(int(spec["imo"]))
        headers["X-AIS-Source"] = source
        code = 200 if payload.get("ok") else 404
        return code, payload, headers
    return 404, {"ok": False, "error": "not_found"}, headers


# ---------------------------------------------------------------------------
# Optional FastAPI router
# ---------------------------------------------------------------------------
try:
    from fastapi import APIRouter, Response as FastAPIResponse

    ais_router = APIRouter(prefix="/api/v1/gis/ais", tags=["GIS AIS Tracker"])
    router = ais_router  # alias for include_router convenience

    @ais_router.get("/status")
    async def ais_status() -> FastAPIResponse:
        body = {"ok": True, **public_status()}
        return FastAPIResponse(
            content=json.dumps(body),
            media_type="application/json",
            headers={
                "X-Contract-Version": CONTRACT_VERSION,
                "X-AIS-Source": "local_g3_cache",
            },
        )

    @ais_router.get("/positions")
    async def ais_positions() -> FastAPIResponse:
        payload, source = list_positions()
        return FastAPIResponse(
            content=json.dumps(payload),
            media_type="application/json",
            headers={
                "X-Contract-Version": CONTRACT_VERSION,
                "X-AIS-Source": source,
            },
        )

    @ais_router.get("/vessel/{imo}")
    async def ais_vessel(imo: int) -> FastAPIResponse:
        payload, source = get_vessel_by_imo(int(imo))
        code = 200 if payload.get("ok") else 404
        return FastAPIResponse(
            content=json.dumps(payload),
            status_code=code,
            media_type="application/json",
            headers={
                "X-Contract-Version": CONTRACT_VERSION,
                "X-AIS-Source": source,
            },
        )

except ImportError:  # pragma: no cover
    ais_router = None  # type: ignore[assignment]
    router = None  # type: ignore[assignment]


__all__ = (
    "CONTRACT_VERSION",
    "ais_router",
    "router",
    "parse_ais_path",
    "handle_ais_request",
    "list_positions",
    "get_vessel_by_imo",
    "public_status",
    "sync_from_local_g3",
    "get_budget_snapshot",
)
