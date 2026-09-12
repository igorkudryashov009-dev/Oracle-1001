"""
AISStream.io live WebSocket connector for Oracle-1001 / Sentinel.

Architecture (2026-09 docs + empirical probe):
  - Exactly ONE long-lived WebSocket per ingest process.
  - FiltersShipMMSI hard cap = 200 MMSI per subscription (aisstream.io docs).
  - Max 3 subscribed connections / account and / IP — multi-batch sockets
    are rejected at subscribe-time (socket close; sometimes HTTP 429).
  - TOP-500 therefore uses rotation of <=200-MMSI subsets on the single socket.
  - Coverage window = full rotation cycle (not a fixed 15 min).

Adapter boundary: see services.ais_source_adapter.AISSourceAdapter.
Terrestrial implementation is this module; satellite is a stub
(services.satellite_ais_adapter.SatelliteAISAdapter) — not activated.

Resilience: rate-limit-aware backoff (Retry-After / 429), token-bucket on
connect+subscribe attempts, silence watchdog, heartbeat telemetry.
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
STRUCTURED_LOG_PATH = ROOT / "logs" / "aisstream_ws_attempts.jsonl"
CONFIG_PATH = ROOT / "config.yaml"
WS_URL_DEFAULT = "wss://stream.aisstream.io/v0/stream"
WORLD_BBOX = [[[-90.0, -180.0], [90.0, 180.0]]]

# Official AISStream limits (https://aisstream.io/documentation, 2026-09):
DOC_MMSI_PER_SUBSCRIPTION = 200
DOC_MAX_CONNECTIONS_PER_ACCOUNT = 3
DOC_SUBSCRIBE_UPDATES_PER_SEC = 1

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


class RateLimitError(Exception):
    """AISStream rejected connect/subscribe due to rate / connection limits."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        http_status: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.stage = stage
        self.http_status = http_status
        self.retry_after = retry_after


def next_backoff(current: float | None, *, initial: float = 1.0, maximum: float = 60.0) -> float:
    """Exponential backoff: initial → 2× → … capped at ``maximum``."""
    if current is None:
        return float(initial)
    return min(float(current) * 2.0, float(maximum))


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def extract_http_meta(exc: BaseException) -> dict[str, Any]:
    """Pull HTTP status + Retry-After from websockets InvalidStatus / similar."""
    meta: dict[str, Any] = {
        "exc_type": type(exc).__name__,
        "exc": str(exc),
        "http_status": None,
        "retry_after": None,
        "response_headers": None,
    }
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(response, "status", None)
        meta["http_status"] = status
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                hdr = {str(k).lower(): str(v) for k, v in headers.items()}
            except Exception:  # noqa: BLE001
                hdr = {"repr": repr(headers)}
            meta["response_headers"] = hdr
            ra = hdr.get("retry-after")
            if ra is not None:
                try:
                    meta["retry_after"] = float(ra)
                except (TypeError, ValueError):
                    meta["retry_after"] = ra
    if meta["http_status"] is None:
        status_attr = getattr(exc, "status_code", None)
        if status_attr is not None:
            meta["http_status"] = status_attr
    return meta


def log_ws_attempt(event: dict[str, Any]) -> None:
    """Structured JSONL for every connect/subscribe attempt (diagnostics)."""
    payload = {"ts": _utc_ts(), **event}
    line = json.dumps(payload, ensure_ascii=False)
    STRUCTURED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STRUCTURED_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    status = payload.get("http_status")
    stage = payload.get("stage")
    ok = payload.get("ok")
    logger.info(
        "ws_attempt event=%s stage=%s batch_id=%s mmsi=%s ok=%s http_status=%s retry_after=%s",
        payload.get("event"),
        stage,
        payload.get("batch_id"),
        payload.get("mmsi_count"),
        ok,
        status,
        payload.get("retry_after"),
    )


@dataclass
class TokenBucket:
    """Global limiter for connect+subscribe attempts (below API saturation)."""

    rate_per_minute: float = 6.0
    capacity: float = 3.0
    tokens: float = 3.0
    updated_at: float = field(default_factory=time.monotonic)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self.updated_at)
        self.updated_at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * (self.rate_per_minute / 60.0))

    async def acquire(self, cost: float = 1.0) -> float:
        """Block until a token is available; return wait seconds used."""
        waited = 0.0
        while True:
            self._refill()
            if self.tokens >= cost:
                self.tokens -= cost
                return waited
            need = cost - self.tokens
            delay = need / max(self.rate_per_minute / 60.0, 1e-9)
            delay = max(0.05, min(delay, 30.0))
            await asyncio.sleep(delay)
            waited += delay


def _bbox_is_global(bbox: Any) -> bool:
    """True if BoundingBoxes covers (approx) the whole world."""
    try:
        # AISStream format: [[[lat1, lon1], [lat2, lon2]], ...]
        corners = bbox[0]
        (lat_a, lon_a), (lat_b, lon_b) = corners[0], corners[1]
        lats = sorted([float(lat_a), float(lat_b)])
        lons = sorted([float(lon_a), float(lon_b)])
        return lats[0] <= -89.0 and lats[1] >= 89.0 and lons[0] <= -179.0 and lons[1] >= 179.0
    except Exception:  # noqa: BLE001
        return False


def normalize_mmsi(value: Any) -> str:
    text = str(value or "").strip()
    if text.isdigit() and len(text) < 9:
        return text.zfill(9)
    return text


def load_api_key() -> str:
    key = (os.getenv("AISSTREAM_API_KEY") or "").strip()
    if key and not key.startswith("YOUR_"):
        return key
    raise SystemExit(
        "ERROR: Set AISSTREAM_API_KEY in .env (copy from .env.example). "
        "Get a free key at https://aisstream.io/apikeys"
    )


def load_connector_settings(config_path: Path | None = None) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "websocket_url": WS_URL_DEFAULT,
        "bounding_boxes": WORLD_BBOX,
        "reconnect_backoff_initial_seconds": 1.0,
        "reconnect_backoff_max_seconds": 60.0,
        "rate_limit_backoff_initial_seconds": 30.0,
        "rate_limit_backoff_max_seconds": 300.0,
        "reconnect_alert_after_attempts": 10,
        "silence_watchdog_seconds": 600.0,
        "silence_force_reconnect": False,
        "heartbeat_interval_seconds": 15.0,
        "coverage_log_interval_seconds": 120.0,
        "batch_size": 50,
        "flush_interval_sec": 1.0,
        "fleet_top_n": 1001,
        "filter_message_types": list(ACCEPTED_MESSAGE_TYPES),
        "db_path": str(ROOT / "история1" / "sentinel_ais.db"),
        "fleet_database": str(ROOT / "output" / "fleet_database.csv"),
        # single_persistent = one WS; rotate FiltersShipMMSI if universe > cap
        "subscription_mode": "single_persistent",
        "mmsi_per_subscription": DOC_MMSI_PER_SUBSCRIPTION,
        # Must match config.yaml / AGENTS.md (Prompt-7): 180s × 3 chunks = 540s cycle.
        # Legacy fallback was 240s (=720s window) — caused ais_health semantic drift.
        "rotation_interval_seconds": 180.0,
        "try_full_mmsi_list": True,
        "subscribe_rate_per_minute": 6.0,
        "subscribe_burst": 3.0,
    }
    env_db = (os.environ.get("SENTINEL_DB_PATH") or "").strip()
    if env_db:
        settings["db_path"] = env_db
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
        "rate_limit_backoff_initial_seconds",
        "rate_limit_backoff_max_seconds",
        "reconnect_alert_after_attempts",
        "silence_watchdog_seconds",
        "mmsi_per_subscription",
    ):
        if ais.get(key) is not None:
            settings[key] = ais[key]
    for key, val in sentinel.items():
        if val is not None:
            settings[key] = val
    paths = cfg.get("paths") or {}
    if paths.get("fleet_database"):
        settings["fleet_database"] = str(ROOT / paths["fleet_database"])
    if paths.get("sentinel_db") and not env_db:
        settings["db_path"] = str(ROOT / paths["sentinel_db"])
    if env_db:
        settings["db_path"] = env_db
    # Env overrides
    for env_key, setting_key, caster in (
        ("SENTINEL_SUBSCRIPTION_MODE", "subscription_mode", str),
        ("SENTINEL_MMSI_PER_SUBSCRIPTION", "mmsi_per_subscription", int),
        ("SENTINEL_ROTATION_INTERVAL_SECONDS", "rotation_interval_seconds", float),
        ("SENTINEL_COVERAGE_LOG_INTERVAL_SECONDS", "coverage_log_interval_seconds", float),
        ("SENTINEL_SUBSCRIBE_RATE_PER_MINUTE", "subscribe_rate_per_minute", float),
    ):
        raw = os.getenv(env_key)
        if raw not in (None, ""):
            settings[setting_key] = caster(raw)
    return settings


@dataclass
class ConnectorMetrics:
    messages_total: int = 0
    matched: int = 0
    unmatched: int = 0
    # Prompt 7: raw AIS frames matching subscribed MMSI universe (vs unique coverage+TTL).
    raw_subscribed_hits: int = 0
    unique_subscribed_mmsis: int = 0
    reconnects: int = 0
    rate_limit_hits: int = 0
    http_429_count: int = 0
    subscribe_attempts: int = 0
    last_http_429_at: float | None = None
    last_rate_limit_stage: str | None = None
    subscription_strategy: str = "unknown"
    active_chunk_index: int = 0
    active_chunk_size: int = 0
    rotation_cycle_seconds: float = 0.0
    coverage_window_seconds: float = 0.0
    last_heartbeat_at: float = field(default_factory=time.monotonic)
    connected_since: float = field(default_factory=time.monotonic)
    window_start: float = field(default_factory=time.monotonic)
    window_msgs: int = 0
    mps: float = 0.0
    _seen_subscribed: set[str] = field(default_factory=set, repr=False)

    def tick(self, matched: bool, *, mmsi: str | None = None) -> None:
        self.messages_total += 1
        self.window_msgs += 1
        if matched:
            self.matched += 1
        else:
            self.unmatched += 1
        if mmsi:
            self.raw_subscribed_hits += 1
            if mmsi not in self._seen_subscribed:
                self._seen_subscribed.add(mmsi)
                self.unique_subscribed_mmsis = len(self._seen_subscribed)
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

    @property
    def silence_sec(self) -> float:
        return max(0.0, time.monotonic() - self.last_heartbeat_at)


class AISStreamConnector:
    """Production AISStream live ingestion — single persistent WebSocket."""

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
        self._rate_limiter = TokenBucket(
            rate_per_minute=float(self.settings.get("subscribe_rate_per_minute", 6.0)),
            capacity=float(self.settings.get("subscribe_burst", 3.0)),
        )
        self._rate_limiter.tokens = float(self.settings.get("subscribe_burst", 3.0))
        # Full TOP-500 (or registry) MMSI set currently targeted by rotation.
        self._subscribed_universe: set[str] = set()

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
            mmsi = normalize_mmsi(body.get("UserID") or meta.get("MMSI") or "")
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

        body = (
            msg_body.get("PositionReport")
            or msg_body.get("StandardClassBPositionReport")
            or msg_body.get("ExtendedClassBPositionReport")
            or {}
        )
        mmsi = normalize_mmsi(body.get("UserID") or meta.get("MMSI") or "")
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

    def _load_mmsi_universe(self) -> list[str]:
        """TOP-500 MMSI list (9-char), preferred for coverage SLO."""
        mmsis: list[str] = []
        try:
            from services.analytics import select_top500_fleet

            fleet_path = Path(self.settings.get("fleet_database") or (ROOT / "output" / "fleet_database.csv"))
            top = select_top500_fleet(fleet_path, top_n=500)
            mmsis = [
                normalize_mmsi(x)
                for x in top["mmsi"].astype(str).tolist()
                if str(x).strip()
            ]
            logger.info("mmsi_universe source=select_top500_fleet n=%d", len(mmsis))
        except Exception as exc:  # noqa: BLE001
            logger.warning("TOP-500 select failed (%s) - falling back to registry order", exc)
            mmsis = [normalize_mmsi(v.mmsi) for v in self.registry.vessels if v.mmsi]
        # de-dupe preserve order
        seen: set[str] = set()
        out: list[str] = []
        for m in mmsis:
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def _chunk_plan(self, mmsis: list[str]) -> tuple[str, list[list[str]], float]:
        """Return (strategy, chunks, coverage_window_seconds)."""
        cap = int(self.settings.get("mmsi_per_subscription") or DOC_MMSI_PER_SUBSCRIPTION)
        cap = max(1, min(cap, DOC_MMSI_PER_SUBSCRIPTION))
        interval = float(self.settings.get("rotation_interval_seconds") or 180.0)
        if len(mmsis) <= cap:
            return "full_list", [mmsis], max(interval, 900.0)
        chunks = [mmsis[i : i + cap] for i in range(0, len(mmsis), cap)]
        # coverage_window_seconds == one full rotation cycle (interval × n_chunks)
        cycle = interval * len(chunks)
        return "rotation", chunks, cycle

    def _backoff_settings(self) -> tuple[float, float, int]:
        return (
            float(self.settings.get("reconnect_backoff_initial_seconds", 1.0)),
            float(self.settings.get("reconnect_backoff_max_seconds", 60.0)),
            int(self.settings.get("reconnect_alert_after_attempts", 10)),
        )

    def _rate_limit_backoff_settings(self) -> tuple[float, float]:
        return (
            float(self.settings.get("rate_limit_backoff_initial_seconds", 30.0)),
            float(self.settings.get("rate_limit_backoff_max_seconds", 300.0)),
        )

    async def _write_health(self, *, coverage: dict[str, Any] | None = None) -> None:
        try:
            from services.ais_health import (
                build_health_document,
                compute_top500_live_coverage,
                write_health_files,
            )

            window_sec = float(self.metrics.coverage_window_seconds or 0.0)
            if window_sec <= 0:
                window_sec = float(os.getenv("SENTINEL_COVERAGE_WINDOW_SECONDS") or 900)
            cov = coverage or compute_top500_live_coverage(window_seconds=window_sec)
            latency_ms = round(self.storage.last_insert_latency_ms, 2)
            write_health_files(
                build_health_document(
                    extra={
                        "top500_live_coverage": int(cov.get("top500_live_coverage") or 0),
                        "top500_coverage": cov,
                        "coverage_window_seconds": float(
                            cov.get("coverage_window_seconds") or window_sec
                        ),
                        "subscription_mode": self.metrics.subscription_strategy,
                        "connector": {
                            "mps": round(self.metrics.mps, 2),
                            "matched": self.metrics.matched,
                            "unmatched": self.metrics.unmatched,
                            "raw_subscribed_hits": self.metrics.raw_subscribed_hits,
                            "unique_subscribed_mmsis": self.metrics.unique_subscribed_mmsis,
                            "insert_latency_ms": latency_ms,
                            "reconnects": self.metrics.reconnects,
                            "rate_limit_hits": self.metrics.rate_limit_hits,
                            "http_429_count": self.metrics.http_429_count,
                            "subscribe_attempts": self.metrics.subscribe_attempts,
                            "heartbeat_ok": self.metrics.heartbeat_ok,
                            "silence_sec": round(self.metrics.silence_sec, 1),
                            "uptime_sec": round(self.metrics.uptime_sec, 1),
                            "strategy": self.metrics.subscription_strategy,
                            "chunk_index": self.metrics.active_chunk_index,
                            "chunk_size": self.metrics.active_chunk_size,
                            "rotation_cycle_seconds": self.metrics.rotation_cycle_seconds,
                            "last_rate_limit_stage": self.metrics.last_rate_limit_stage,
                        },
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("health write failed: %s", exc)

    async def _telemetry_loop(self) -> None:
        interval = float(self.settings.get("heartbeat_interval_seconds", 15.0))
        cov_every = float(self.settings.get("coverage_log_interval_seconds", 120.0))
        last_cov = 0.0
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            latency_ms = round(self.storage.last_insert_latency_ms, 2)
            self.storage.write_telemetry({
                "mps": round(self.metrics.mps, 2),
                "matched_count": self.metrics.matched,
                "unmatched_count": self.metrics.unmatched,
                "insert_latency_ms": latency_ms,
                "ws_uptime_sec": round(self.metrics.uptime_sec, 1),
                "reconnects": self.metrics.reconnects,
                "heartbeat_ok": self.metrics.heartbeat_ok,
            })
            try:
                if self.storage._conn is not None:
                    self.storage._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception as exc:  # noqa: BLE001
                logger.debug("wal_checkpoint PASSIVE skipped: %s", exc)

            now = time.monotonic()
            coverage = None
            if now - last_cov >= cov_every:
                last_cov = now
                try:
                    from services.ais_health import compute_top500_live_coverage

                    coverage = compute_top500_live_coverage(
                        window_seconds=float(self.metrics.coverage_window_seconds or 900)
                    )
                    logger.info(
                        "coverage_tick top500_live_coverage=%s window_sec=%.0f "
                        "strategy=%s chunk=%d/%s matched=%d "
                        "raw_subscribed_hits=%d unique_subscribed=%d rate_limit_hits=%d",
                        coverage.get("top500_live_coverage"),
                        float(coverage.get("coverage_window_seconds") or 0),
                        self.metrics.subscription_strategy,
                        self.metrics.active_chunk_index,
                        self.metrics.active_chunk_size,
                        self.metrics.matched,
                        self.metrics.raw_subscribed_hits,
                        self.metrics.unique_subscribed_mmsis,
                        self.metrics.rate_limit_hits,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("coverage_tick failed: %s", exc)

            await self._write_health(coverage=coverage)
            logger.info(
                "heartbeat mps=%.1f matched=%d unmatched=%d latency=%.1fms "
                "reconnects=%d rate_limits=%d heartbeat_ok=%s silence=%.0fs "
                "uptime=%.0fs strategy=%s chunk=%d",
                self.metrics.mps,
                self.metrics.matched,
                self.metrics.unmatched,
                latency_ms,
                self.metrics.reconnects,
                self.metrics.rate_limit_hits,
                self.metrics.heartbeat_ok,
                self.metrics.silence_sec,
                self.metrics.uptime_sec,
                self.metrics.subscription_strategy,
                self.metrics.active_chunk_index,
            )

    async def _send_subscribe(
        self,
        ws: Any,
        *,
        mmsi_batch: list[str],
        bbox: list,
        filter_types: list[str],
        batch_id: int,
    ) -> None:
        """Send FiltersShipMMSI subscribe and wait for SubscriptionConfirmation."""
        await self._rate_limiter.acquire(1.0)
        self.metrics.subscribe_attempts += 1
        # Full subscribe body for G2 diagnostics (API key redacted).
        sub = {
            "APIKey": "***REDACTED***",
            "BoundingBoxes": bbox,
            "FiltersShipMMSI": mmsi_batch,
            "FilterMessageTypes": filter_types,
        }
        log_ws_attempt({
            "event": "subscribe_attempt",
            "stage": "subscribe",
            "batch_id": batch_id,
            "mmsi_count": len(mmsi_batch),
            "ok": None,
            "http_status": None,
            "retry_after": None,
            "subscribe_body": sub,
            "bbox_is_global": _bbox_is_global(bbox),
        })
        sub_wire = {
            "APIKey": self.api_key,
            "BoundingBoxes": bbox,
            "FiltersShipMMSI": mmsi_batch,
            "FilterMessageTypes": filter_types,
        }
        try:
            await ws.send(json.dumps(sub_wire))
        except Exception as exc:  # noqa: BLE001
            meta = extract_http_meta(exc)
            log_ws_attempt({
                "event": "subscribe_result",
                "stage": "subscribe",
                "batch_id": batch_id,
                "mmsi_count": len(mmsi_batch),
                "ok": False,
                "http_status": meta["http_status"],
                "retry_after": meta["retry_after"],
                "error": meta["exc"],
                "exc_type": meta["exc_type"],
            })
            if meta["http_status"] == 429:
                raise RateLimitError(
                    f"HTTP 429 on subscribe send: {exc}",
                    stage="subscribe",
                    http_status=429,
                    retry_after=(
                        float(meta["retry_after"])
                        if isinstance(meta["retry_after"], (int, float))
                        else None
                    ),
                ) from exc
            raise

        # Await confirmation (or treat early AIS traffic as soft-ok)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            except Exception as exc:  # noqa: BLE001
                meta = extract_http_meta(exc)
                log_ws_attempt({
                    "event": "subscribe_result",
                    "stage": "subscribe",
                    "batch_id": batch_id,
                    "mmsi_count": len(mmsi_batch),
                    "ok": False,
                    "http_status": meta["http_status"],
                    "retry_after": meta["retry_after"],
                    "error": meta["exc"],
                    "exc_type": meta["exc_type"],
                })
                # Excess connections / oversize MMSI: server closes after subscribe
                name = type(exc).__name__
                if "ConnectionClosed" in name or meta["http_status"] == 429:
                    # Docs: 4th connection / oversize MMSI → close after subscribe.
                    # Often WITHOUT HTTP 429 (probe 2026-09-07); still rate-limit class.
                    raise RateLimitError(
                        f"subscribe rejected/closed: {exc}",
                        stage="subscribe",
                        http_status=meta["http_status"],
                        retry_after=(
                            float(meta["retry_after"])
                            if isinstance(meta["retry_after"], (int, float))
                            else None
                        ),
                    ) from exc
                raise

            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = message.get("MessageType")
            if mtype == "SubscriptionConfirmation":
                log_ws_attempt({
                    "event": "subscribe_result",
                    "stage": "subscribe",
                    "batch_id": batch_id,
                    "mmsi_count": len(mmsi_batch),
                    "ok": True,
                    "http_status": None,
                    "retry_after": None,
                    "ack": message,
                })
                self.metrics.heartbeat()
                return

            if message.get("error") or message.get("Error"):
                log_ws_attempt({
                    "event": "subscribe_result",
                    "stage": "subscribe",
                    "batch_id": batch_id,
                    "mmsi_count": len(mmsi_batch),
                    "ok": False,
                    "server_error": message,
                })
                raise RateLimitError(
                    f"subscribe server_error: {message}",
                    stage="subscribe",
                    http_status=None,
                )

            # AIS frame arrived — subscription is live
            row = self.parse_message(message)
            if row:
                hit_mmsi = str(row.get("mmsi") or "")
                in_universe = hit_mmsi in self._subscribed_universe if hit_mmsi else False
                self.metrics.tick(
                    bool(row.get("matched")),
                    mmsi=hit_mmsi if in_universe else None,
                )
                if row.get("matched") or (self.metrics.unmatched % 20 == 0):
                    await self.storage.enqueue(row)
            log_ws_attempt({
                "event": "subscribe_result",
                "stage": "subscribe",
                "batch_id": batch_id,
                "mmsi_count": len(mmsi_batch),
                "ok": True,
                "note": "ais_traffic_before_explicit_ack",
            })
            self.metrics.heartbeat()
            return

        log_ws_attempt({
            "event": "subscribe_result",
            "stage": "subscribe",
            "batch_id": batch_id,
            "mmsi_count": len(mmsi_batch),
            "ok": False,
            "error": "SubscriptionConfirmation timeout",
        })
        raise ConnectionError("SubscriptionConfirmation timeout after subscribe")

    async def _consume_socket(
        self,
        ws: Any,
        *,
        stop_at: float | None,
        chunks: list[list[str]],
        bbox: list,
        filter_types: list[str],
        rotate: bool,
    ) -> None:
        """Read loop; optionally rotate FiltersShipMMSI on the same connection."""
        silence_limit = float(self.settings.get("silence_watchdog_seconds", 60.0))
        interval = float(self.settings.get("rotation_interval_seconds") or 180.0)
        chunk_idx = 0
        next_rotate_at = time.monotonic() + interval if rotate and len(chunks) > 1 else None

        while not self._stop.is_set():
            if stop_at and time.monotonic() >= stop_at:
                self._stop.set()
                break

            if next_rotate_at is not None and time.monotonic() >= next_rotate_at:
                chunk_idx = (chunk_idx + 1) % len(chunks)
                self.metrics.active_chunk_index = chunk_idx
                self.metrics.active_chunk_size = len(chunks[chunk_idx])
                logger.info(
                    "rotation -> chunk %d/%d (%d MMSI)",
                    chunk_idx + 1,
                    len(chunks),
                    len(chunks[chunk_idx]),
                )
                await self._send_subscribe(
                    ws,
                    mmsi_batch=chunks[chunk_idx],
                    bbox=bbox,
                    filter_types=filter_types,
                    batch_id=chunk_idx,
                )
                next_rotate_at = time.monotonic() + interval
                continue

            timeout = silence_limit
            if next_rotate_at is not None:
                timeout = min(timeout, max(0.5, next_rotate_at - time.monotonic()))

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                if next_rotate_at is not None and time.monotonic() >= next_rotate_at:
                    continue
                # MMSI-filtered streams are sparse (terrestrial AIS ~200km offshore).
                # Keep the healthy WS; rely on ping/pong for dead-socket detection.
                force = bool(self.settings.get("silence_force_reconnect", False))
                if force:
                    logger.warning(
                        "silence watchdog: no AIS messages for %.0fs - forcing reconnect",
                        silence_limit,
                    )
                    raise ConnectionError(
                        f"AIS silence watchdog: no messages for {silence_limit:.0f}s"
                    )
                if self.metrics.silence_sec >= max(60.0, silence_limit * 0.5):
                    logger.info(
                        "silence %.0fs with live WS (MMSI filter sparse) - keeping connection",
                        self.metrics.silence_sec,
                    )
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                if "ConnectionClosed" in name:
                    logger.warning("WebSocket closed: %s", exc)
                else:
                    logger.exception("WebSocket recv failed: %s", exc)
                raise

            self.metrics.heartbeat()
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("json_decode error: %s", exc)
                try:
                    await self.storage.write_dead_letter(
                        "json_decode",
                        {"error": str(exc), "raw": str(raw)[:500]},
                    )
                except Exception as dl_exc:  # noqa: BLE001
                    logger.exception("dead_letter write failed: %s", dl_exc)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("message parse outer failure: %s", exc)
                continue

            try:
                if message.get("MessageType") == "SubscriptionConfirmation":
                    continue
                if message.get("error") or message.get("Error"):
                    await self.storage.write_dead_letter("server_error", message)
                    logger.warning("Server error: %s", message)
                    break

                row = self.parse_message(message)
                if not row:
                    continue
                hit_mmsi = str(row.get("mmsi") or "")
                in_universe = hit_mmsi in self._subscribed_universe if hit_mmsi else False
                self.metrics.tick(
                    bool(row.get("matched")),
                    mmsi=hit_mmsi if in_universe else None,
                )
                if row.get("matched") or (self.metrics.unmatched % 20 == 0):
                    await self.storage.enqueue(row)
            except Exception as exc:  # noqa: BLE001
                logger.exception("AIS message handling failed: %s", exc)
                try:
                    await self.storage.write_dead_letter(
                        "handler_exception",
                        {"error": str(exc), "type": type(exc).__name__},
                    )
                except Exception as dl_exc:  # noqa: BLE001
                    logger.exception("dead_letter write failed: %s", dl_exc)

    async def _connect_and_run(
        self,
        *,
        url: str,
        chunks: list[list[str]],
        bbox: list,
        filter_types: list[str],
        stop_at: float | None,
        rotate: bool,
    ) -> None:
        import websockets

        await self._rate_limiter.acquire(1.0)
        batch_id = 0
        mmsi_count = len(chunks[0]) if chunks else 0
        log_ws_attempt({
            "event": "connect_attempt",
            "stage": "handshake",
            "batch_id": batch_id,
            "mmsi_count": mmsi_count,
            "ok": None,
            "http_status": None,
            "retry_after": None,
        })
        try:
            ws = await websockets.connect(
                url,
                ping_interval=30,
                ping_timeout=60,
                close_timeout=10,
                max_size=8 * 1024 * 1024,
                compression="deflate",
            )
        except Exception as exc:  # noqa: BLE001
            meta = extract_http_meta(exc)
            log_ws_attempt({
                "event": "connect_result",
                "stage": "handshake",
                "batch_id": batch_id,
                "mmsi_count": mmsi_count,
                "ok": False,
                "http_status": meta["http_status"],
                "retry_after": meta["retry_after"],
                "error": meta["exc"],
                "exc_type": meta["exc_type"],
                "response_headers": meta["response_headers"],
            })
            if meta["http_status"] == 429:
                raise RateLimitError(
                    f"HTTP 429 on WS handshake: {exc}",
                    stage="handshake",
                    http_status=429,
                    retry_after=(
                        float(meta["retry_after"])
                        if isinstance(meta["retry_after"], (int, float))
                        else None
                    ),
                ) from exc
            raise

        log_ws_attempt({
            "event": "connect_result",
            "stage": "handshake",
            "batch_id": batch_id,
            "mmsi_count": mmsi_count,
            "ok": True,
            "http_status": 101,
            "retry_after": None,
        })

        subscribed = False
        try:
            await self._send_subscribe(
                ws,
                mmsi_batch=chunks[0],
                bbox=bbox,
                filter_types=filter_types,
                batch_id=0,
            )
            subscribed = True
            self.metrics.connected_since = time.monotonic()
            self.metrics.active_chunk_index = 0
            self.metrics.active_chunk_size = len(chunks[0])
            logger.info(
                "single WS subscribed | strategy=%s | chunk0=%d MMSI | chunks=%d | "
                "coverage_window=%.0fs",
                self.metrics.subscription_strategy,
                len(chunks[0]),
                len(chunks),
                self.metrics.coverage_window_seconds,
            )
            await self._consume_socket(
                ws,
                stop_at=stop_at,
                chunks=chunks,
                bbox=bbox,
                filter_types=filter_types,
                rotate=rotate,
            )
        except RateLimitError:
            raise
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if (not subscribed) and ("ConnectionClosed" in name):
                raise RateLimitError(
                    f"subscribe-stage close: {exc}",
                    stage="subscribe",
                    http_status=extract_http_meta(exc)["http_status"],
                ) from exc
            raise
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _run_bbox_global(self, *, url: str, bbox: list, filter_types: list[str], stop_at: float | None) -> None:
        """Legacy degraded mode: world bbox without MMSI filter."""
        import websockets

        await self._rate_limiter.acquire(1.0)
        log_ws_attempt({
            "event": "connect_attempt",
            "stage": "handshake",
            "batch_id": 0,
            "mmsi_count": 0,
            "mode": "bbox_global",
        })
        async with websockets.connect(
            url,
            ping_interval=30,
            ping_timeout=60,
            close_timeout=10,
            max_size=8 * 1024 * 1024,
            compression="deflate",
        ) as ws:
            log_ws_attempt({
                "event": "connect_result",
                "stage": "handshake",
                "batch_id": 0,
                "mmsi_count": 0,
                "ok": True,
                "http_status": 101,
                "mode": "bbox_global",
            })
            sub = {
                "APIKey": self.api_key,
                "BoundingBoxes": bbox,
                "FilterMessageTypes": filter_types,
            }
            await ws.send(json.dumps(sub))
            self.metrics.connected_since = time.monotonic()
            self.metrics.heartbeat()
            self.metrics.subscription_strategy = "bbox_global"
            logger.info("Connected | mode=bbox_global | filters=%s", ",".join(filter_types))
            await self._consume_socket(
                ws,
                stop_at=stop_at,
                chunks=[[]],
                bbox=bbox,
                filter_types=filter_types,
                rotate=False,
            )

    async def run(self, duration_seconds: float | None = None) -> dict[str, Any]:
        """Main reconnect loop. duration_seconds=None → run forever."""
        await self.storage.open_async()
        self.registry.export_targets_json(ROOT / "output" / "sentinel_targets.json")
        logger.info(
            "Fleet registry loaded: %d vessels | tiers=%s",
            len(self.registry.vessels),
            self.registry.tier_counts(),
        )
        logger.info(
            "AISStream documented limits: connections/account=%d | MMSI/subscription=%d | "
            "subscribe_updates=%d/s (https://aisstream.io/documentation)",
            DOC_MAX_CONNECTIONS_PER_ACCOUNT,
            DOC_MMSI_PER_SUBSCRIPTION,
            DOC_SUBSCRIBE_UPDATES_PER_SEC,
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
            or "single_persistent"
        ).strip().lower()

        # Legacy alias
        if mode in ("mmsi_batches", "batches"):
            logger.warning(
                "subscription_mode=%s deprecated — forcing single_persistent "
                "(multi-batch sockets violate AISStream 3-connection limit)",
                mode,
            )
            mode = "single_persistent"

        mmsis = self._load_mmsi_universe()
        self._subscribed_universe = set(mmsis)
        strategy, chunks, coverage_window = self._chunk_plan(mmsis)
        logger.info(
            "G1_rotation_math universe=%d chunks=%s interval_sec=%.0f "
            "full_cycle_sec=%.0f cycles_in_25min=%.2f bbox=%s bbox_global=%s",
            len(mmsis),
            [len(c) for c in chunks],
            float(self.settings.get("rotation_interval_seconds") or 180.0),
            float(coverage_window),
            1500.0 / float(coverage_window) if coverage_window else 0.0,
            json.dumps(self.settings.get("bounding_boxes") or WORLD_BBOX),
            _bbox_is_global(self.settings.get("bounding_boxes") or WORLD_BBOX),
        )
        try_full = bool(self.settings.get("try_full_mmsi_list", False))
        env_try = (os.getenv("SENTINEL_TRY_FULL_MMSI") or "").strip().lower()
        if env_try in ("1", "true", "yes"):
            try_full = True
        elif env_try in ("0", "false", "no"):
            try_full = False

        # Step 3 oversize probe (default OFF — already confirmed 201+/500 reject).
        if strategy == "rotation" and try_full and mode == "single_persistent":
            logger.info(
                "probing full MMSI list (%d) on single subscribe - expected reject if >%d",
                len(mmsis),
                DOC_MMSI_PER_SUBSCRIPTION,
            )
            try:
                await self._connect_and_run(
                    url=url,
                    chunks=[mmsis],
                    bbox=bbox,
                    filter_types=filter_types,
                    stop_at=time.monotonic() + 12.0,
                    rotate=False,
                )
                strategy, chunks, coverage_window = "full_list", [mmsis], max(coverage_window, 900.0)
                logger.info("full MMSI list accepted - batching/rotation disabled")
            except RateLimitError as exc:
                logger.warning(
                    "full list rejected at stage=%s http=%s - using rotation of %dx<=%d",
                    exc.stage,
                    exc.http_status,
                    len(chunks),
                    int(self.settings.get("mmsi_per_subscription") or DOC_MMSI_PER_SUBSCRIPTION),
                )
                self._rate_limiter.tokens = self._rate_limiter.capacity
                self._rate_limiter.updated_at = time.monotonic()
                logger.info("cooldown 15s after oversize probe before rotation subscribe")
                await asyncio.sleep(15.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "full list probe failed (%s) - using rotation of %d chunks",
                    exc,
                    len(chunks),
                )
                self._rate_limiter.tokens = self._rate_limiter.capacity
                self._rate_limiter.updated_at = time.monotonic()
                await asyncio.sleep(15.0)
            if self._stop.is_set() and (stop_at is None or time.monotonic() < stop_at):
                self._stop = asyncio.Event()

        self.metrics.subscription_strategy = (
            f"single_persistent:{strategy}" if mode == "single_persistent" else mode
        )
        self.metrics.rotation_cycle_seconds = float(coverage_window)
        self.metrics.coverage_window_seconds = float(coverage_window)
        os.environ["SENTINEL_COVERAGE_WINDOW_SECONDS"] = str(int(coverage_window))
        logger.info(
            "subscription_mode=%s strategy=%s chunks=%d coverage_window_seconds=%.0f",
            mode,
            strategy,
            len(chunks),
            coverage_window,
        )

        backoff: float | None = None
        rate_backoff: float | None = None
        consecutive_failures = 0
        initial, maximum, alert_after = self._backoff_settings()
        rl_initial, rl_maximum = self._rate_limit_backoff_settings()

        try:
            while not self._stop.is_set():
                if stop_at and time.monotonic() >= stop_at:
                    logger.info("Duration reached — shutting down")
                    break
                try:
                    if mode == "bbox_global":
                        await self._run_bbox_global(
                            url=url, bbox=bbox, filter_types=filter_types, stop_at=stop_at
                        )
                    else:
                        await self._connect_and_run(
                            url=url,
                            chunks=chunks,
                            bbox=bbox,
                            filter_types=filter_types,
                            stop_at=stop_at,
                            rotate=(strategy == "rotation" and len(chunks) > 1),
                        )
                    backoff = None
                    rate_backoff = None
                    consecutive_failures = 0
                except asyncio.CancelledError:
                    raise
                except RateLimitError as exc:
                    self.metrics.reconnects += 1
                    self.metrics.rate_limit_hits += 1
                    self.metrics.last_rate_limit_stage = exc.stage
                    if exc.http_status == 429:
                        self.metrics.http_429_count += 1
                        self.metrics.last_http_429_at = time.monotonic()
                    consecutive_failures += 1
                    try:
                        await self.storage.write_dead_letter(
                            "rate_limit",
                            {
                                "error": str(exc),
                                "stage": exc.stage,
                                "http_status": exc.http_status,
                                "retry_after": exc.retry_after,
                                "consecutive": consecutive_failures,
                            },
                        )
                    except Exception as dl_exc:  # noqa: BLE001
                        logger.exception("dead_letter write failed: %s", dl_exc)

                    if exc.retry_after is not None:
                        sleep_for = float(exc.retry_after)
                        logger.error(
                            "RATE_LIMIT stage=%s http=%s — honouring Retry-After=%.1fs "
                            "(hits=%d)",
                            exc.stage,
                            exc.http_status,
                            sleep_for,
                            self.metrics.rate_limit_hits,
                        )
                    else:
                        rate_backoff = next_backoff(
                            rate_backoff, initial=rl_initial, maximum=rl_maximum
                        )
                        sleep_for = rate_backoff
                        logger.error(
                            "RATE_LIMIT stage=%s http=%s — backoff %.1fs "
                            "(start=%ss cap=%ss, hits=%d)",
                            exc.stage,
                            exc.http_status,
                            sleep_for,
                            rl_initial,
                            rl_maximum,
                            self.metrics.rate_limit_hits,
                        )
                    await asyncio.sleep(sleep_for)
                except Exception as exc:  # noqa: BLE001
                    self.metrics.reconnects += 1
                    consecutive_failures += 1
                    try:
                        await self.storage.write_dead_letter(
                            "ws_disconnect",
                            {
                                "error": str(exc),
                                "consecutive": consecutive_failures,
                            },
                        )
                    except Exception as dl_exc:  # noqa: BLE001
                        logger.exception("dead_letter write failed: %s", dl_exc)
                    backoff = next_backoff(backoff, initial=initial, maximum=maximum)
                    if consecutive_failures >= alert_after:
                        logger.error(
                            "ALERT: %d consecutive reconnect failures (last=%s) — "
                            "backoff %.1fs (total reconnects=%d)",
                            consecutive_failures,
                            exc,
                            backoff,
                            self.metrics.reconnects,
                        )
                    else:
                        logger.warning(
                            "WebSocket error (%s) — reconnect in %.1fs "
                            "(consecutive=%d/#%d)",
                            exc,
                            backoff,
                            consecutive_failures,
                            self.metrics.reconnects,
                        )
                    await asyncio.sleep(backoff)
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
            "raw_subscribed_hits": self.metrics.raw_subscribed_hits,
            "unique_subscribed_mmsis": self.metrics.unique_subscribed_mmsis,
            "reconnects": self.metrics.reconnects,
            "rate_limit_hits": self.metrics.rate_limit_hits,
            "http_429_count": self.metrics.http_429_count,
            "subscribe_attempts": self.metrics.subscribe_attempts,
            "inserted": self.storage.total_inserted,
            "dead_letters": self.storage.total_dead_letters,
            "fleet": self.registry.summary(),
            "subscription_mode": self.metrics.subscription_strategy,
            "coverage_window_seconds": self.metrics.coverage_window_seconds,
            "chunks": len(chunks),
        }

    def stop(self) -> None:
        self._stop.set()


async def _amain(args: argparse.Namespace) -> int:
    settings = load_connector_settings()
    if args.db:
        settings["db_path"] = args.db

    daemon = bool(args.daemon) or args.duration is None
    outer_backoff: float | None = None
    last_result: dict[str, Any] = {}

    while True:
        connector = AISStreamConnector(settings=settings)

        def _on_signal(*_a: object) -> None:
            logger.info("signal received — stopping AIS daemon")
            connector.stop()

        try:
            loop = asyncio.get_running_loop()
            for sig_name in ("SIGINT", "SIGTERM"):
                sig = getattr(__import__("signal"), sig_name, None)
                if sig is None:
                    continue
                try:
                    loop.add_signal_handler(sig, _on_signal)
                except (NotImplementedError, RuntimeError):
                    pass
        except Exception:  # noqa: BLE001
            pass

        try:
            last_result = await connector.run(duration_seconds=args.duration)
            outer_backoff = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("AIS daemon cycle failed: %s", exc)
            outer_backoff = next_backoff(outer_backoff, initial=1.0, maximum=60.0)
            if not daemon or connector._stop.is_set():
                print(json.dumps({"error": str(exc), **last_result}, indent=2, ensure_ascii=False))
                return 1
            logger.warning("restarting AIS daemon in %.1fs", outer_backoff)
            await asyncio.sleep(outer_backoff)
            continue

        if not daemon or connector._stop.is_set() or args.duration:
            print(json.dumps(last_result, indent=2, ensure_ascii=False))
            return 0

        outer_backoff = next_backoff(outer_backoff, initial=1.0, maximum=60.0)
        logger.warning(
            "AIS run() returned unexpectedly — supervising restart in %.1fs",
            outer_backoff,
        )
        await asyncio.sleep(outer_backoff)


def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle-1001 Sentinel AISStream connector")
    parser.add_argument("--duration", type=float, default=None, help="Run for N seconds then exit")
    parser.add_argument("--db", type=str, default=None, help="Override SQLite path")
    parser.add_argument(
        "--daemon",
        action="store_true",
        default=True,
        help="Auto-restart forever on disconnect/exit (default: on)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Disable daemon supervisor (single run cycle)",
    )
    parser.add_argument(
        "--dry-registry",
        action="store_true",
        help="Only load fleet registry + export sentinel_targets.json (no WebSocket)",
    )
    args = parser.parse_args()
    if args.once:
        args.daemon = False

    if args.dry_registry:
        settings = load_connector_settings()
        reg = FleetRegistry.from_csv(
            Path(settings["fleet_database"]),
            top_n=int(settings.get("fleet_top_n", 1001)),
        )
        out = ROOT / "output" / "sentinel_targets.json"
        reg.export_targets_json(out)
        print(json.dumps({"status": "ok", "targets": out.as_posix(), **reg.summary()}, indent=2))
        return 0

    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — AIS daemon stopped")
        return 0
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
