"""AIS live buffer worker — Contract 1.8.0-ops-gis-sot (G3-safe).

HARD RULE (AGENTS.md): does **not** open a second AISstream WebSocket.
Live ingest remains ``services.aisstream_connector`` / ``sentinel-core``
(``single_persistent``). This worker:

  1) **buffer mode (default)** — reads recent positions from the G3 SQLite
     replica and writes ``data/cache/ais_live.json`` for OOB HUD cold-start.
  2) **ingest mode (``--ingest``)** — delegates to ``aisstream_connector.main()``
     as the *sole* WS owner (for Windows host without Docker core). Exponential
     backoff lives inside the connector.

Usage::

    python -m services.ais_worker              # buffer loop
    python -m services.ais_worker --once
    python -m services.ais_worker --ingest     # ONLY if core is not already ingesting
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG = logging.getLogger("sentinel.ais_worker")
CONTRACT_VERSION = "1.8.0-ops-gis-sot"
CACHE_PATH = ROOT / "data" / "cache" / "ais_live.json"
DEFAULT_INTERVAL_SEC = float(os.getenv("AIS_WORKER_BUFFER_INTERVAL_SEC", "15"))
MAX_VESSELS = int(os.getenv("AIS_WORKER_MAX_VESSELS", "200"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_db() -> Path | None:
    try:
        from services.quant_risk_service import resolve_db_path

        p = resolve_db_path()
        if p.is_file():
            return p
    except Exception:  # noqa: BLE001
        pass
    for c in (
        ROOT / "история1" / "sentinel_ais.db",
        ROOT / "sentinel_ais.db",
        ROOT / "data" / "sentinel_ais.db",
    ):
        if c.is_file():
            return c
    return None


def snapshot_from_db(*, limit: int = MAX_VESSELS) -> dict[str, Any]:
    """Build ais_live.json payload from G3 ``ais_positions`` (no network)."""
    db = _resolve_db()
    vessels: list[dict[str, Any]] = []
    source = "empty"
    if db is not None:
        try:
            con = sqlite3.connect(str(db))
            con.row_factory = sqlite3.Row
            try:
                # Prefer latest row per MMSI when possible
                rows = con.execute(
                    """
                    SELECT mmsi, latitude, longitude, sog, cog, received_at, timestamp_utc
                    FROM ais_positions
                    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                    ORDER BY COALESCE(received_at, timestamp_utc) DESC
                    LIMIT ?
                    """,
                    (max(limit * 3, limit),),
                ).fetchall()
            except sqlite3.Error:
                rows = []
            finally:
                con.close()
            seen: set[str] = set()
            for r in rows:
                mmsi = str(r["mmsi"] if "mmsi" in r.keys() else "")
                if not mmsi or mmsi in seen:
                    continue
                seen.add(mmsi)
                try:
                    lat = float(r["latitude"])
                    lon = float(r["longitude"])
                except (TypeError, ValueError, KeyError):
                    continue
                vessels.append(
                    {
                        "mmsi": mmsi,
                        "lat": lat,
                        "lon": lon,
                        "sog": r["sog"] if "sog" in r.keys() else None,
                        "cog": r["cog"] if "cog" in r.keys() else None,
                        "received_at": r["received_at"] if "received_at" in r.keys() else None,
                    }
                )
                if len(vessels) >= limit:
                    break
            source = "g3_sqlite"
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ais_worker db snapshot failed: %s", exc)
            source = f"error:{exc}"[:80]

    return {
        "ok": True,
        "contract_version": CONTRACT_VERSION,
        "updated_at": _now_iso(),
        "count": len(vessels),
        "vessels": vessels,
        "source": source,
        "db_path": str(db).replace("\\", "/") if db else None,
        "note": (
            "Buffer-only worker — live WS owned by aisstream_connector "
            "(G3 single_persistent). No second socket."
        ),
    }


def write_live_cache(payload: dict[str, Any] | None = None) -> Path:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = payload or snapshot_from_db()
    CACHE_PATH.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return CACHE_PATH


def run_buffer_loop(*, interval_sec: float = DEFAULT_INTERVAL_SEC, once: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.info(
        "ais_worker buffer mode interval=%.1fs cache=%s (G3-safe, no extra WS)",
        interval_sec,
        CACHE_PATH,
    )
    while True:
        path = write_live_cache()
        snap = json.loads(path.read_text(encoding="utf-8"))
        LOG.info("ais_live.json count=%s source=%s", snap.get("count"), snap.get("source"))
        if once:
            return
        time.sleep(max(2.0, float(interval_sec)))


def run_ingest_delegate() -> int:
    """Sole-owner ingest path — delegates to aisstream_connector (backoff inside)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.warning(
        "ais_worker --ingest: delegating to aisstream_connector "
        "(ensure sentinel-core is NOT also subscribed — G3 ≤3 sockets)"
    )
    from services import aisstream_connector

    # Keep argv clean for connector CLI
    sys.argv = ["aisstream_connector.py"]
    return int(aisstream_connector.main() or 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="G3-safe AIS buffer / ingest delegate")
    ap.add_argument("--once", action="store_true", help="Single buffer write then exit")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC)
    ap.add_argument(
        "--ingest",
        action="store_true",
        help="Delegate to aisstream_connector (single_persistent). Do not run beside sentinel-core.",
    )
    args = ap.parse_args(argv)
    if args.ingest:
        return run_ingest_delegate()
    run_buffer_loop(interval_sec=args.interval, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
