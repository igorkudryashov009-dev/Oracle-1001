"""
AISStream.io live WebSocket connector for Oracle-1001 / Sentinel.

Connects to wss://stream.aisstream.io/v0/stream with global bbox subscription,
matches MMSI/IMO against the strategic fleet registry (Alpha–Delta), and
persists enriched AIS records into SQLite (primary) with optional PostgreSQL.

Resilience: exponential backoff reconnect, heartbeat telemetry, dead-letter
logging, and async batch inserts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from services.chokepoints import detect_chokepoint  # noqa: E402
from services.fleet_registry import FleetRegistry  # noqa: E402
from services.storage import AISStorage  # noqa: E402

LOG_PATH = ROOT / "logs" / "aisstream_connector.log"
CONFIG_PATH = ROOT / "config.yaml"
WS_URL_DEFAULT = "wss://stream.aisstream.io/v0/stream"
WORLD_BBOX = [[[-90.0, -180.0], [90.0, 180.0]]]

ACCEPTED_MESSAGE_TYPES = frozenset({
    "PositionReport",
    "ShipStaticData",
    "StandardClassBPositionReport",
    "ExtendedClassBPositionReport",
})

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

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
logger = logging.getLogger("AISStreamConnector")


def next_backoff(current: float | None, *, initial: float = 2.0, maximum: float = 60.0) -> float:
    if current is None:
        return initial
    return min(current * 2.0, maximum)


def load_api_key() -> str:
    key = (os.getenv("AISSTREAM_API_KEY") or "").strip()
    if key and not key.startswith("YOUR_"):
        return key
    # Fallback: dotenv already loaded; try config note (never store real key in yaml)
    raise SystemExit(
        "ERROR: Set AISSTREAM_API_KEY in .env (copy from .env.example). "
        "Get a free key at https://aisstream.io/apikeys"
    )


def load_connector_settings(config_path: Path | None = None) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "websocket_url": WS_URL_DEFAULT,
        "bounding_boxes": WORLD_BBOX,
        "reconnect_backoff_initial_seconds": 2.0,
        "reconnect_backoff_max_seconds": 60.0,
        "heartbeat_interval_seconds": 15.0,
        "batch_size": 50,
        "flush_interval_sec": 1.0,
        "fleet_top_n": 1001,
        "filter_message_types": list(ACCEPTED_MESSAGE_TYPES),
        "db_path": str(ROOT / "история1" / "sentinel_ais.db"),
        "fleet_database": str(ROOT / "output" / "fleet_database.csv"),
        "subscription_mode": "mmsi_batches",
        "mmsi_batch_size": 50,
        "max_batches": 12,
        "stagger_seconds": 2.5,
        "max_concurrent_handshakes": 12,
    }
    path = config_path or CONFIG_PATH
    if not path.exists():
        return settings
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    ais = cfg.get("aisstream") or {}
    sentinel = cfg.get("sentinel") or {}
    for key in (
        "websocket_url",
        "reconnect_backoff_initial_seconds",
        "reconnect_backoff_max_seconds",
    ):
        if ais.get(key) is not None:
            settings[key] = ais[key]
    for key, val in sentinel.items():
        if val is not None:
            settings[key] = val
    paths = cfg.get("paths") or {}
    if paths.get("fleet_database"):
        settings["fleet_database"] = str(ROOT / paths["fleet_database"])
    if paths.get("sentinel_db"):
        settings["db_path"] = str(ROOT / paths["sentinel_db"])
    return settings


@dataclass
class ConnectorMetrics:
    messages_total: int = 0
    matched: int = 0
    unmatched: int = 0
    reconnects: int = 0
    last_heartbeat_at: float = field(default_factory=time.monotonic)
    connected_since: float = field(default_factory=time.monotonic)
    window_start: float = field(default_factory=time.monotonic)
    window_msgs: int = 0
    mps: float = 0.0

    def tick(self, matched: bool) -> None:
        self.messages_total += 1
        self.window_msgs += 1
        if matched:
            self.matched += 1
        else:
            self.unmatched += 1
        elapsed = time.monotonic() - self.window_start
        if elapsed >= 1.0:
            self.mps = self.window_msgs / elapsed
            self.window_msgs = 0
            self.window_start = time.monotonic()

    def heartbeat(self) -> None:
        self.last_heartbeat_at = time.monotonic()

    @property
    def uptime_sec(self) -> float:
        return max(0.0, time.monotonic() - self.connected_since)

    @property
    def heartbeat_ok(self) -> bool:
        return (time.monotonic() - self.last_heartbeat_at) < 45.0


class AISStreamConnector:
    """Production AISStream live ingestion service."""

    def __init__(
        self,
        api_key: str | None = None,
        registry: FleetRegistry | None = None,
        storage: AISStorage | None = None,
        settings: dict[str, Any] | None = None,
    ):
        self.settings = settings or load_connector_settings()
        self.api_key = api_key or load_api_key()
        fleet_path = Path(self.settings["fleet_database"])
        self.registry = registry or FleetRegistry.from_csv(
            fleet_path, top_n=int(self.settings.get("fleet_top_n", 1001))
        )
        self.storage = storage or AISStorage(
            sqlite_path=Path(self.settings["db_path"]),
            batch_size=int(self.settings.get("batch_size", 50)),
            flush_interval_sec=float(self.settings.get("flush_interval_sec", 1.0)),
        )
        self.metrics = ConnectorMetrics()
        self._stop = asyncio.Event()
        self._static_cache: dict[str, dict[str, Any]] = {}
        self._last_positions: dict[str, dict[str, Any]] = {}

    def parse_message(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Parse PositionReport / Class B / ShipStaticData into a normalized record."""
        mtype = message.get("MessageType")
        if mtype not in ACCEPTED_MESSAGE_TYPES:
            return None

        meta = message.get("Metadata") or {}
        msg_body = message.get("Message") or {}
        now = datetime.now(timezone.utc)
        received_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        ts = str(meta.get("time_utc") or meta.get("TimeUTC") or now.isoformat()).replace(" ", "T")

        if mtype == "ShipStaticData":
            body = msg_body.get("ShipStaticData") or {}
            mmsi = str(body.get("UserID") or meta.get("MMSI") or "").strip()
            if not mmsi:
                return None
            dim = body.get("Dimension") or {}
            draft = body.get("MaximumStaticDraught")
            dest = body.get("Destination") or ""
            name = (body.get("Name") or "").strip()
            imo_raw = body.get("ImoNumber")
            imo = str(imo_raw) if imo_raw and int(imo_raw or 0) > 0 else None
            self._static_cache[mmsi] = {
                "draft_m": float(draft) if draft not in (None, 0, 0.0) else None,
                "destination": str(dest).strip() or None,
                "vessel_name": name or None,
                "imo": imo,
                "loa_m": float(dim.get("A") or 0) + float(dim.get("B") or 0) or None,
                "beam_m": float(dim.get("C") or 0) + float(dim.get("D") or 0) or None,
            }
            # Static-only updates enrich cache; emit matched record if we have last pos
            last = self._last_positions.get(mmsi)
            if not last:
                return None
            row = dict(last)
            row.update({
                "draft_m": self._static_cache[mmsi].get("draft_m") or row.get("draft_m"),
                "destination": self._static_cache[mmsi].get("destination") or row.get("destination"),
                "message_type": mtype,
                "received_at": received_at,
                "timestamp_utc": ts,
            })
            return row

        # Position reports (Class A / Class B)
        body = (
            msg_body.get("PositionReport")
            or msg_body.get("StandardClassBPositionReport")
            or msg_body.get("ExtendedClassBPositionReport")
            or {}
        )
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
        if lat_f == 91 or lon_f == 181:
            return None

        sog = body.get("Sog")
        cog = body.get("Cog")
        heading = body.get("TrueHeading")
        if heading is not None:
            try:
                if float(heading) >= 511:
                    heading = cog
            except (TypeError, ValueError):
                heading = cog

        nav_code = body.get("NavigationalStatus")
        nav_status = None
        if nav_code is not None:
            try:
                nav_status = NAV_STATUS_MAP.get(int(nav_code), str(nav_code))
            except (TypeError, ValueError):
                nav_status = str(nav_code)

        static = self._static_cache.get(mmsi, {})
        target = self.registry.match(mmsi=mmsi, imo=static.get("imo"))
        matched = target is not None

        row = {
            "imo": (target.imo if target else static.get("imo")),
            "mmsi": mmsi,
            "vessel_name": (target.vessel_name if target else static.get("vessel_name")),
            "tier": (target.tier if target else None),
            "timestamp_utc": ts,
            "lat": lat_f,
            "lon": lon_f,
            "sog": None if sog is None else float(sog),
            "cog": None if cog is None else float(cog),
            "heading": None if heading is None else float(heading),
            "nav_status": nav_status,
            "draft_m": static.get("draft_m") or (target.draft_m if target else None),
            "destination": static.get("destination") or (target.destination_port if target else None),
            "matched": matched,
            "message_type": mtype,
            "received_at": received_at,
            "chokepoint": None,
        }
        cp = detect_chokepoint(lat_f, lon_f)
        if cp:
            row["chokepoint"] = cp.id
        self._last_positions[mmsi] = row
        return row

    async def _telemetry_loop(self) -> None:
        interval = float(self.settings.get("heartbeat_interval_seconds", 15.0))
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            self.storage.write_telemetry({
                "mps": round(self.metrics.mps, 2),
                "matched_count": self.metrics.matched,
                "unmatched_count": self.metrics.unmatched,
                "insert_latency_ms": round(self.storage.last_insert_latency_ms, 2),
                "ws_uptime_sec": round(self.metrics.uptime_sec, 1),
                "reconnects": self.metrics.reconnects,
                "heartbeat_ok": self.metrics.heartbeat_ok,
            })
            logger.info(
                "telemetry mps=%.1f matched=%d unmatched=%d latency=%.1fms reconnects=%d",
                self.metrics.mps,
                self.metrics.matched,
                self.metrics.unmatched,
                self.storage.last_insert_latency_ms,
                self.metrics.reconnects,
            )

    def _mmsi_batches(self, batch_size: int = 50) -> list[list[str]]:
        mmsis = [v.mmsi for v in self.registry.vessels if v.mmsi]
        return [mmsis[i : i + batch_size] for i in range(0, len(mmsis), batch_size)]

    async def _consume_socket(
        self,
        ws: Any,
        *,
        stop_at: float | None,
        batch_id: int | None = None,
    ) -> None:
        async for raw in ws:
            if self._stop.is_set():
                break
            if stop_at and time.monotonic() >= stop_at:
                self._stop.set()
                break
            self.metrics.heartbeat()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                await self.storage.write_dead_letter(
                    "json_decode", {"error": str(exc), "raw": str(raw)[:500], "batch": batch_id}
                )
                continue
            if message.get("error") or message.get("Error"):
                await self.storage.write_dead_letter("server_error", message)
                logger.warning("Server error (batch=%s): %s", batch_id, message)
                break

            row = self.parse_message(message)
            if not row:
                continue
            self.metrics.tick(bool(row.get("matched")))
            if row.get("matched") or (self.metrics.unmatched % 20 == 0):
                await self.storage.enqueue(row)

    async def _batch_worker(
        self,
        batch_id: int,
        mmsi_batch: list[str],
        *,
        url: str,
        bbox: list,
        filter_types: list[str],
        stop_at: float | None,
        stagger: float,
        handshake_sem: asyncio.Semaphore,
    ) -> None:
        import websockets

        await asyncio.sleep(batch_id * stagger)
        backoff: float | None = None
        while not self._stop.is_set():
            if stop_at and time.monotonic() >= stop_at:
                break
            try:
                async with handshake_sem:
                    ws = await websockets.connect(
                        url,
                        ping_interval=30,
                        ping_timeout=60,
                        close_timeout=10,
                        max_size=8 * 1024 * 1024,
                    )
                async with ws:
                    sub = {
                        "APIKey": self.api_key,
                        "BoundingBoxes": bbox,
                        "FiltersShipMMSI": mmsi_batch,
                        "FilterMessageTypes": filter_types,
                    }
                    await ws.send(json.dumps(sub))
                    self.metrics.connected_since = time.monotonic()
                    self.metrics.heartbeat()
                    backoff = None
                    logger.info(
                        "batch[%d] subscribed · %d MMSI · first=%s",
                        batch_id,
                        len(mmsi_batch),
                        mmsi_batch[0] if mmsi_batch else "—",
                    )
                    await self._consume_socket(ws, stop_at=stop_at, batch_id=batch_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.metrics.reconnects += 1
                await self.storage.write_dead_letter(
                    "ws_disconnect", {"error": str(exc), "batch": batch_id}
                )
                backoff = next_backoff(
                    backoff,
                    initial=float(self.settings["reconnect_backoff_initial_seconds"]),
                    maximum=float(self.settings["reconnect_backoff_max_seconds"]),
                )
                logger.warning(
                    "batch[%d] WebSocket error (%s) — reconnect in %.1fs (#%d)",
                    batch_id,
                    exc,
                    backoff,
                    self.metrics.reconnects,
                )
                await asyncio.sleep(backoff)

    async def run(self, duration_seconds: float | None = None) -> dict[str, Any]:
        """Main reconnect loop. duration_seconds=None → run forever."""
        import websockets

        await self.storage.open_async()
        self.registry.export_targets_json(ROOT / "output" / "sentinel_targets.json")
        logger.info(
            "Fleet registry loaded: %d vessels · tiers=%s",
            len(self.registry.vessels),
            self.registry.tier_counts(),
        )

        telemetry_task = asyncio.create_task(self._telemetry_loop(), name="telemetry")
        stop_at = None
        if duration_seconds:
            stop_at = time.monotonic() + float(duration_seconds)

        url = str(self.settings["websocket_url"])
        bbox = self.settings.get("bounding_boxes") or WORLD_BBOX
        filter_types = list(self.settings.get("filter_message_types") or ACCEPTED_MESSAGE_TYPES)
        mode = (
            os.getenv("SENTINEL_SUBSCRIPTION_MODE")
            or self.settings.get("subscription_mode")
            or "mmsi_batches"
        ).strip().lower()

        try:
            if mode == "bbox_global":
                backoff: float | None = None
                while not self._stop.is_set():
                    if stop_at and time.monotonic() >= stop_at:
                        logger.info("Duration reached — shutting down")
                        break
                    try:
                        async with websockets.connect(
                            url,
                            ping_interval=30,
                            ping_timeout=60,
                            close_timeout=10,
                            max_size=8 * 1024 * 1024,
                        ) as ws:
                            sub = {
                                "APIKey": self.api_key,
                                "BoundingBoxes": bbox,
                                "FilterMessageTypes": filter_types,
                            }
                            await ws.send(json.dumps(sub))
                            self.metrics.connected_since = time.monotonic()
                            self.metrics.heartbeat()
                            backoff = None
                            logger.info(
                                "Connected to %s · mode=bbox_global · filters=%s",
                                url,
                                ",".join(filter_types),
                            )
                            await self._consume_socket(ws, stop_at=stop_at)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        self.metrics.reconnects += 1
                        await self.storage.write_dead_letter("ws_disconnect", {"error": str(exc)})
                        backoff = next_backoff(
                            backoff,
                            initial=float(self.settings["reconnect_backoff_initial_seconds"]),
                            maximum=float(self.settings["reconnect_backoff_max_seconds"]),
                        )
                        logger.warning(
                            "WebSocket error (%s) — reconnect in %.1fs (attempt #%d)",
                            exc,
                            backoff,
                            self.metrics.reconnects,
                        )
                        await asyncio.sleep(backoff)
            else:
                # Production default: FiltersShipMMSI batches (AISStream hard limit = 50)
                batch_size = int(self.settings.get("mmsi_batch_size") or 50)
                # Env overrides config (VPS throttle knobs)
                env_max = os.getenv("SENTINEL_MAX_BATCHES")
                if env_max not in (None, ""):
                    max_batches = int(env_max)
                else:
                    max_batches = self.settings.get("max_batches")
                batches = self._mmsi_batches(batch_size)
                if max_batches is not None:
                    batches = batches[: max(1, int(max_batches))]
                stagger = float(self.settings.get("stagger_seconds") or 2.5)
                max_handshakes = int(self.settings.get("max_concurrent_handshakes") or 12)
                handshake_sem = asyncio.Semaphore(max_handshakes)
                logger.info(
                    "Starting MMSI batch mode · batches=%d · size=%d · stagger=%.1fs",
                    len(batches),
                    batch_size,
                    stagger,
                )
                workers = [
                    asyncio.create_task(
                        self._batch_worker(
                            i,
                            batch,
                            url=url,
                            bbox=bbox,
                            filter_types=filter_types,
                            stop_at=stop_at,
                            stagger=stagger,
                            handshake_sem=handshake_sem,
                        ),
                        name=f"ais-batch-{i}",
                    )
                    for i, batch in enumerate(batches)
                ]
                await asyncio.gather(*workers)
        finally:
            self._stop.set()
            telemetry_task.cancel()
            try:
                await telemetry_task
            except asyncio.CancelledError:
                pass
            await self.storage.close()

        return {
            "messages_total": self.metrics.messages_total,
            "matched": self.metrics.matched,
            "unmatched": self.metrics.unmatched,
            "reconnects": self.metrics.reconnects,
            "inserted": self.storage.total_inserted,
            "dead_letters": self.storage.total_dead_letters,
            "fleet": self.registry.summary(),
            "subscription_mode": mode,
        }

    def stop(self) -> None:
        self._stop.set()


async def _amain(args: argparse.Namespace) -> int:
    settings = load_connector_settings()
    if args.db:
        settings["db_path"] = args.db
    connector = AISStreamConnector(settings=settings)
    result = await connector.run(duration_seconds=args.duration)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle-1001 Sentinel AISStream connector")
    parser.add_argument("--duration", type=float, default=None, help="Run for N seconds then exit")
    parser.add_argument("--db", type=str, default=None, help="Override SQLite path")
    parser.add_argument(
        "--dry-registry",
        action="store_true",
        help="Only load fleet registry + export sentinel_targets.json (no WebSocket)",
    )
    args = parser.parse_args()

    if args.dry_registry:
        settings = load_connector_settings()
        reg = FleetRegistry.from_csv(Path(settings["fleet_database"]), top_n=int(settings.get("fleet_top_n", 1001)))
        out = ROOT / "output" / "sentinel_targets.json"
        reg.export_targets_json(out)
        print(json.dumps({"status": "ok", "targets": out.as_posix(), **reg.summary()}, indent=2))
        return 0

    try:
        return asyncio.run(_amain(args))
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
