#!/usr/bin/env python3
"""VesselFinder Fleet Positions archive sync with 500-slot rotation.

Premium fleet subscription is capped at FLEET_SLOT_LIMIT (default 500) while the
local OSINT registry is ~1253 vessels. This service rotates the remote ListManager
fleet in batches, pulls VesselsList telemetry, persists history locally, and
upserts fresh ais_positions into sentinel_ais.db so replica lag can clear.

Credentials: never hardcode. Reads (in order):
  VESSELFINDER_API_KEY → PROVIDER_API_KEY → VESSEL_TRACKING_API_KEY

Usage:
  python -m services.archive_service --status
  python -m services.archive_service --force-sync
  python -m services.archive_service --force-sync --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.archive_snapshot_worker import (  # noqa: E402
    load_fleet_rows,
    take_daily_snapshot,
)
from services.key_manager import (  # noqa: E402
    mask_key,
    resolve_commercial_key,
    validate_userkey,
)
from services.storage import DEFAULT_DB  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

LOG = logging.getLogger("sentinel.archive_service")

FLEET_CSV = ROOT / "output" / "fleet_database.csv"
ARCHIVE_DATA_DIR = ROOT / "data" / "archive"
TELEMETRY_DB = ARCHIVE_DATA_DIR / "vessel_telemetry_history.sqlite"
ROTATION_STATE = ARCHIVE_DATA_DIR / "rotation_state.json"
API_STATUS_OUT = ROOT / "output" / "archive" / "api_status.json"

LISTMANAGER_URL = "https://api.vesselfinder.com/listmanager"
VESSELSLIST_URL = "https://api.vesselfinder.com/vesselslist"

DEFAULT_SLOT_LIMIT = 500
DEFAULT_CYCLE_DAYS = 3
DEFAULT_TIMEOUT_SEC = 45.0
DEFAULT_RATE_SLEEP_SEC = 1.1
DEFAULT_POST_PUT_WAIT_SEC = 3.0
MAX_RETRIES = 4


TELEMETRY_DDL = """
CREATE TABLE IF NOT EXISTS vessel_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at TEXT NOT NULL,
    batch_index INTEGER NOT NULL,
    imo TEXT,
    mmsi TEXT,
    vessel_name TEXT,
    timestamp_utc TEXT,
    lat REAL,
    lon REAL,
    sog REAL,
    cog REAL,
    heading REAL,
    nav_status TEXT,
    draft_m REAL,
    destination TEXT,
    eta TEXT,
    flag TEXT,
    vessel_type TEXT,
    dwt REAL,
    loa_m REAL,
    beam_m REAL,
    built_year INTEGER,
    departure_port TEXT,
    raw_json TEXT,
    tier INTEGER,
    draft_max REAL,
    UNIQUE(imo, timestamp_utc, batch_index)
)
"""

TELEMETRY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_vt_imo_ts ON vessel_telemetry(imo, timestamp_utc)",
    "CREATE INDEX IF NOT EXISTS idx_vt_synced ON vessel_telemetry(synced_at)",
    "CREATE INDEX IF NOT EXISTS idx_vt_batch ON vessel_telemetry(batch_index)",
    "CREATE INDEX IF NOT EXISTS idx_vt_tier ON vessel_telemetry(tier)",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    d = dt or _utc_now()
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_api_key(*, argv: Optional[list[str]] = None) -> str:
    """Resolve commercial userkey via key_manager cascade (never hardcode)."""
    resolved = resolve_commercial_key(argv=argv, auto_persist=True, validate=False)
    if resolved and resolved.key:
        return resolved.key
    return ""


def handshake_commercial(key: str) -> dict[str, Any]:
    """Ping ListManager; never raises — returns ok False on Invalid Userkey."""
    if not key:
        return {"ok": False, "error": "missing_key"}
    return validate_userkey(key)


def slot_limit() -> int:
    try:
        return max(1, int(os.getenv("VESSELFINDER_FLEET_SLOT_LIMIT", DEFAULT_SLOT_LIMIT)))
    except ValueError:
        return DEFAULT_SLOT_LIMIT


def cycle_days() -> int:
    try:
        return max(1, int(os.getenv("VESSELFINDER_ROTATION_CYCLE_DAYS", DEFAULT_CYCLE_DAYS)))
    except ValueError:
        return DEFAULT_CYCLE_DAYS


def slot_interval_hours() -> float:
    """Wall-clock hours between tiered slot advances (default 1h)."""
    try:
        return max(0.1, float(os.getenv("ARCHIVE_SLOT_INTERVAL_HOURS", "1")))
    except ValueError:
        return 1.0


def sentinel_db_path() -> Path:
    env = (os.environ.get("SENTINEL_DB_PATH") or "").strip()
    return Path(env) if env else Path(DEFAULT_DB)


def ensure_archive_dirs() -> None:
    ARCHIVE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    API_STATUS_OUT.parent.mkdir(parents=True, exist_ok=True)


def open_telemetry_db(path: Path = TELEMETRY_DB) -> sqlite3.Connection:
    ensure_archive_dirs()
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(TELEMETRY_DDL)
    for stmt in TELEMETRY_INDEXES:
        conn.execute(stmt)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vessel_telemetry)").fetchall()}
    if "tier" not in cols:
        conn.execute("ALTER TABLE vessel_telemetry ADD COLUMN tier INTEGER")
    if "draft_max" not in cols:
        conn.execute("ALTER TABLE vessel_telemetry ADD COLUMN draft_max REAL")
    conn.commit()
    return conn


def load_rotation_state() -> dict[str, Any]:
    ensure_archive_dirs()
    if not ROTATION_STATE.is_file():
        return {
            "batch_index": 0,
            "last_sync_at": None,
            "last_success_at": None,
            "last_error": None,
            "total_batches": 0,
            "slots_limit": slot_limit(),
            "monitored_total": 0,
            "last_batch_size": 0,
            "last_positions": 0,
        }
    try:
        return json.loads(ROTATION_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"batch_index": 0, "last_sync_at": None}


def save_rotation_state(state: dict[str, Any]) -> None:
    ensure_archive_dirs()
    ROTATION_STATE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_api_status(
    state: dict[str, Any], *, plan: str = "OSINT REGISTRY (HYBRID LOCAL FALLBACK)"
) -> dict[str, Any]:
    limit = int(state.get("slots_limit") or slot_limit())
    used = int(state.get("last_batch_size") or 0)
    total = int(state.get("monitored_total") or 0)
    ingest_mode = state.get("ingest_mode") or state.get("auth_mode") or "unknown"
    commercial = ingest_mode in {"commercial_rest", "vesselfinder_commercial"}
    key_masked = state.get("key_masked")
    if commercial:
        ui_api = "COMMERCIAL REST API"
        ui_key = f"AUTO-RESOLVED ({key_masked})" if key_masked else "AUTO-RESOLVED"
        ui_status = "ONLINE"
        ui_tone = "ok"
        api_plan = "COMMERCIAL REST API (LIVE)"
    else:
        ui_api = "HYBRID LOCAL FALLBACK · SNAPSHOT / DEMO MODE"
        ui_key = f"DETECTED ({key_masked}) · REST PENDING" if key_masked else "—"
        ui_status = "SNAPSHOT / DEMO"
        ui_tone = "hybrid"
        api_plan = "OSINT REGISTRY (HYBRID LOCAL FALLBACK · SNAPSHOT / DEMO)"
    payload = {
        "generated_at": _utc_iso(),
        "api_plan": api_plan,
        "provider": "vesselfinder" if commercial else "osint_static_registry",
        "ingest_mode": ingest_mode,
        "auth_mode": state.get("auth_mode"),
        "key_masked": key_masked,
        "key_source": state.get("key_source"),
        "is_synthetic": not commercial,
        "demo_mode": not commercial,
        "registry_source": "OSINT static snapshot (fleet_database.csv) · VesselFinder REST inactive (Invalid Userkey)",
        "live_ais_source": "terrestrial_g3_aisstream",
        "known_fleet_count": total if total > 0 else 1253,
        "ui_api": ui_api,
        "ui_key": ui_key,
        "ui_status": ui_status,
        "ui_tone": ui_tone,
        "slots_used": used,
        "slots_limit": limit,
        "slots_label": f"{used}/{limit} (ROTATING)",
        "snapshot_cycle_days": cycle_days(),
        "total_monitored": total if total > 0 else 1253,
        "batch_index": int(state.get("batch_index") or 0),
        "total_batches": int(state.get("total_batches") or 0),
        "last_sync_at": state.get("last_sync_at"),
        "last_success_at": state.get("last_success_at"),
        "last_error": state.get("last_error"),
        "last_positions": int(state.get("last_positions") or 0),
        "active_tier": state.get("active_tier"),
        "slot_phase": state.get("slot_phase"),
        "rotation_label": state.get("rotation_label"),
        "tier1_gas_count": state.get("tier1_gas_count"),
        "tier2_oil_count": state.get("tier2_oil_count"),
        "slot_interval_hours": slot_interval_hours(),
        "disclaimer": (
            "ARCHIVE REGISTRY: SNAPSHOT / DEMO MODE. "
            "Known fleet ≈1,253 gas carriers (OSINT / VesselFinder hybrid snapshot) — NOT live VF REST. "
            "LIVE G3 AIS N is terrestrial AISstream only and is the sole input to Dual Gate fleet_sample_status. "
            "Archive never feeds fleet_sample_status and is NOT satellite coverage."
        ),
        "status": "OK"
        if commercial and state.get("last_success_at") and not state.get("last_error")
        else ("HYBRID" if ingest_mode == "hybrid_local" else "DEGRADED"),
    }
    API_STATUS_OUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def fleet_imo_batches(
    fleet_csv: Path = FLEET_CSV, *, limit: Optional[int] = None
) -> list[list[str]]:
    """Legacy flat batches (gas-only CSV). Prefer ``select_tiered_rotation_batch``."""
    limit = limit or slot_limit()
    rows = load_fleet_rows(fleet_csv)
    imos: list[str] = []
    seen: set[str] = set()
    for rec in rows:
        imo = str(rec.get("imo") or "").strip()
        if not imo or imo in seen:
            continue
        try:
            imo = str(int(float(imo)))
        except (TypeError, ValueError):
            continue
        seen.add(imo)
        imos.append(imo)
    batches: list[list[str]] = []
    for i in range(0, len(imos), limit):
        batches.append(imos[i : i + limit])
    return batches


def select_tiered_rotation_batch(
    state: dict[str, Any],
    *,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """Pick next IMOs using 3-slot cycle: hours 0–1 = gas (tier1), hour 2 = oil (tier2)."""
    from services.fleet_tiers import rebuild_fleet_tiers

    limit = limit or slot_limit()
    rebuilt = rebuild_fleet_tiers(slot_limit=limit)
    gas_batches: list[list[str]] = rebuilt["gas_batches"]
    oil_batches: list[list[str]] = rebuilt["oil_batches"]
    # Drop empty placeholder oil batch
    oil_batches = [b for b in oil_batches if b]

    phase = int(state.get("slot_phase") or 0) % 3
    gas_idx = int(state.get("gas_batch_index") or 0)
    oil_idx = int(state.get("oil_batch_index") or 0)

    if phase in (0, 1):
        tier = 1
        n = max(1, len(gas_batches))
        batch_index = gas_idx % n
        imos = gas_batches[batch_index] if gas_batches else []
        next_gas = (gas_idx + 1) % n
        next_oil = oil_idx
        label = f"TIER1_GAS batch {batch_index + 1}/{n}"
    else:
        tier = 2
        if not oil_batches:
            # No oil registry yet — fall back to gas so slots stay utilized
            tier = 1
            n = max(1, len(gas_batches))
            batch_index = gas_idx % n
            imos = gas_batches[batch_index] if gas_batches else []
            next_gas = (gas_idx + 1) % n
            next_oil = oil_idx
            label = f"TIER2_OIL unavailable → fallback TIER1_GAS {batch_index + 1}/{n}"
        else:
            n = max(1, len(oil_batches))
            batch_index = oil_idx % n
            imos = oil_batches[batch_index]
            next_gas = gas_idx
            next_oil = (oil_idx + 1) % n
            label = f"TIER2_OIL batch {batch_index + 1}/{n}"

    return {
        "imos": imos,
        "tier": tier,
        "slot_phase": phase,
        "batch_index": batch_index,
        "next_gas_batch_index": next_gas,
        "next_oil_batch_index": next_oil,
        "next_slot_phase": (phase + 1) % 3,
        "label": label,
        "meta": rebuilt["meta"],
        "gas_batches": len(gas_batches),
        "oil_batches": len(oil_batches),
        "monitored_total": int(rebuilt["meta"].get("tier1_gas_count") or 0)
        + int(rebuilt["meta"].get("tier2_oil_count") or 0),
    }


class VesselFinderFleetClient:
    """Thin VesselFinder ListManager + VesselsList client with rate limits."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        rate_sleep: float = DEFAULT_RATE_SLEEP_SEC,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.rate_sleep = rate_sleep
        self.session = session or requests.Session()
        self._last_call = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.rate_sleep:
            time.sleep(self.rate_sleep - elapsed)
        self._last_call = time.monotonic()

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    timeout=self.timeout,
                    headers={"User-Agent": "Oracle-1001-Sentinel-Archive/1.0"},
                )
                if resp.status_code == 429:
                    wait = min(60.0, self.rate_sleep * (2**attempt))
                    LOG.warning("rate-limited 429 — sleep %.1fs (attempt %d)", wait, attempt)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    wait = min(30.0, 1.5 * attempt)
                    LOG.warning("server %s — retry in %.1fs", resp.status_code, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"VesselFinder HTTP {resp.status_code}: {resp.text[:400]}"
                    )
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "json" in ctype or resp.text.strip().startswith(("[", "{")):
                    return resp.json()
                return {"raw": resp.text, "status_code": resp.status_code}
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_err = exc
                wait = min(30.0, 1.5 * attempt)
                LOG.warning("network error %s — retry in %.1fs", exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"VesselFinder request failed after retries: {last_err}")

    def get_fleet_list(self) -> Any:
        return self._request(
            "GET", LISTMANAGER_URL, params={"userkey": self.api_key}
        )

    def replace_fleet(self, imos: list[str]) -> Any:
        """PUT replaces the entire remote fleet list (slot rotation)."""
        body = {"userkey": self.api_key, "imo": ",".join(imos)}
        return self._request("PUT", LISTMANAGER_URL, data=body)

    def probe_auth(self) -> dict[str, Any]:
        """Validate commercial userkey via ListManager GET (no fleet mutation)."""
        data = self._request("GET", LISTMANAGER_URL, params={"userkey": self.api_key})
        if isinstance(data, dict) and data.get("error"):
            return {"ok": False, "error": str(data.get("error")), "mode": "invalid_userkey"}
        return {"ok": True, "mode": "vesselfinder_commercial", "payload_type": type(data).__name__}

    def fetch_vesselslist(
        self, *, interval_min: Optional[int] = None, extradata: str = "voyage,master"
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "userkey": self.api_key,
            "format": "json",
            "extradata": extradata,
        }
        if interval_min is not None:
            params["interval"] = int(interval_min)
        data = self._request("GET", VESSELSLIST_URL, params=params)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"VesselFinder vesselslist error: {data.get('error')}")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("vessels"), list):
            return data["vessels"]
        raise RuntimeError(
            f"Unexpected vesselslist payload type: {type(data).__name__} keys="
            f"{list(data.keys())[:8] if isinstance(data, dict) else []}"
        )


def _parse_vf_timestamp(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Common: "2017-08-11 11:43:42 UTC" or ISO
    s2 = s.replace(" UTC", "").replace("Z", "").strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            dt = datetime.strptime(s2[:19], fmt.replace("Z", "")).replace(
                tzinfo=timezone.utc
            )
            return _utc_iso(dt)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _utc_iso(dt)
    except ValueError:
        return None


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        if x != x:
            return None
        return x
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    f = _f(v)
    if f is None:
        return None
    try:
        return int(f)
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def normalize_vessel_record(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    ais = item.get("AIS") if isinstance(item.get("AIS"), dict) else item
    master = item.get("MASTER") if isinstance(item.get("MASTER"), dict) else {}
    voyage = item.get("VOYAGE") if isinstance(item.get("VOYAGE"), dict) else {}
    if not isinstance(ais, dict):
        return None

    imo = _s(ais.get("IMO") or master.get("IMO"))
    mmsi = _s(ais.get("MMSI") or master.get("MMSI"))
    if not imo and not mmsi:
        return None

    # VesselFinder SPEED is typically knots (float). Keep as-is.
    sog = _f(ais.get("SPEED"))
    lat = _f(ais.get("LATITUDE"))
    lon = _f(ais.get("LONGITUDE"))
    draft = _f(ais.get("DRAUGHT"))
    # Draught sometimes in 0.1 m
    if draft is not None and draft > 40:
        draft = draft / 10.0

    loa = None
    beam = None
    a, b, c, d = _f(ais.get("A")), _f(ais.get("B")), _f(ais.get("C")), _f(ais.get("D"))
    if None not in (a, b):
        loa = (a or 0) + (b or 0)
    if None not in (c, d):
        beam = (c or 0) + (d or 0)
    loa = _f(master.get("LENGTH")) or loa
    beam = _f(master.get("BREADTH")) or beam

    return {
        "imo": imo,
        "mmsi": mmsi,
        "vessel_name": _s(ais.get("NAME") or master.get("NAME")),
        "timestamp_utc": _parse_vf_timestamp(ais.get("TIMESTAMP")) or _utc_iso(),
        "lat": lat,
        "lon": lon,
        "sog": sog,
        "cog": _f(ais.get("COURSE")),
        "heading": _f(ais.get("HEADING")),
        "nav_status": _s(ais.get("NAVSTAT") or ais.get("NAV_STATUS")),
        "draft_m": draft,
        "destination": _s(ais.get("DESTINATION") or voyage.get("DESTINATION")),
        "eta": _s(ais.get("ETA") or ais.get("ETA_AIS")),
        "flag": _s(master.get("FLAG") or master.get("COUNTRY")),
        "vessel_type": _s(master.get("TYPE") or ais.get("TYPE")),
        "dwt": _f(master.get("DWT")),
        "loa_m": loa,
        "beam_m": beam,
        "built_year": _i(master.get("YEAR_BUILT") or master.get("BUILT")),
        "departure_port": _s(voyage.get("LASTPORT") or voyage.get("DEPARTURE")),
        "raw_json": json.dumps(item, ensure_ascii=False)[:8000],
    }


def persist_telemetry(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    *,
    batch_index: int,
    synced_at: str,
    tier: Optional[int] = None,
) -> int:
    if not records:
        return 0
    rows = [
        (
            synced_at,
            batch_index,
            r.get("imo"),
            r.get("mmsi"),
            r.get("vessel_name"),
            r.get("timestamp_utc"),
            r.get("lat"),
            r.get("lon"),
            r.get("sog"),
            r.get("cog"),
            r.get("heading"),
            r.get("nav_status"),
            r.get("draft_m"),
            r.get("destination"),
            r.get("eta"),
            r.get("flag"),
            r.get("vessel_type"),
            r.get("dwt"),
            r.get("loa_m"),
            r.get("beam_m"),
            r.get("built_year"),
            r.get("departure_port"),
            r.get("raw_json"),
            r.get("tier") if r.get("tier") is not None else tier,
            r.get("draft_max") or r.get("draft_m"),
        )
        for r in records
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO vessel_telemetry (
            synced_at, batch_index, imo, mmsi, vessel_name, timestamp_utc,
            lat, lon, sog, cog, heading, nav_status, draft_m, destination, eta,
            flag, vessel_type, dwt, loa_m, beam_m, built_year, departure_port, raw_json,
            tier, draft_max
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_sentinel_ais(records: list[dict[str, Any]], db_path: Path) -> int:
    """Write VesselFinder positions into sentinel_ais.db to refresh replica lag."""
    if not records:
        return 0
    db_path.parent.mkdir(parents=True, exist_ok=True)
    received = _utc_iso()
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        # Ensure table exists (idempotent)
        from services.storage import POSITIONS_DDL

        conn.execute(POSITIONS_DDL)
        rows = []
        for r in records:
            mmsi = _s(r.get("mmsi"))
            lat = r.get("lat")
            lon = r.get("lon")
            ts = r.get("timestamp_utc") or received
            rec_at = r.get("received_at") or ts
            if not mmsi or lat is None or lon is None:
                continue
            rows.append(
                (
                    _s(r.get("imo")),
                    mmsi,
                    r.get("vessel_name"),
                    "vf_fleet",
                    ts,
                    float(lat),
                    float(lon),
                    r.get("sog"),
                    r.get("cog"),
                    r.get("heading"),
                    str(r.get("nav_status")) if r.get("nav_status") is not None else None,
                    r.get("draft_m"),
                    r.get("destination"),
                    1,
                    "vesselfinder_vesselslist",
                    rec_at,
                )
            )
        if not rows:
            return 0
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO ais_positions
            (imo, mmsi, vessel_name, tier, timestamp_utc, lat, lon, sog, cog, heading,
             nav_status, draft_m, destination, matched, message_type, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        return int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(rows))
    finally:
        conn.close()


def due_for_rotation(state: dict[str, Any], *, force: bool = False) -> bool:
    if force:
        return True
    last = state.get("last_success_at") or state.get("last_sync_at")
    if not last:
        return True
    try:
        raw = str(last).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    # Tiered ingest advances one slot every ARCHIVE_SLOT_INTERVAL_HOURS (default 1h)
    return _utc_now() - dt.astimezone(timezone.utc) >= timedelta(hours=slot_interval_hours())


def export_rotation_batch(imos: list[str], batch_index: int) -> dict[str, str]:
    """Write active rotation batch for VesselFinder My Fleet paste / ops."""
    ensure_archive_dirs()
    out_dir = ROOT / "output" / "archive"
    out_dir.mkdir(parents=True, exist_ok=True)
    txt = out_dir / f"rotation_batch_{batch_index}.txt"
    js = out_dir / f"rotation_batch_{batch_index}.json"
    txt.write_text("\n".join(imos) + "\n", encoding="utf-8")
    js.write_text(
        json.dumps(
            {
                "batch_index": batch_index,
                "slot_limit": slot_limit(),
                "vessel_count": len(imos),
                "imos": imos,
                "generated_at": _utc_iso(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # Compatibility aliases used by prior My Fleet tooling
    (ROOT / "output" / "vesselfinder_myfleet_500.txt").write_text(
        "\n".join(imos) + "\n", encoding="utf-8"
    )
    return {"txt": str(txt), "json": str(js)}


def load_local_ais_for_imos(imos: list[str], db_path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Hybrid fallback: pull latest real ais_positions for the active rotation batch."""
    db = Path(db_path or sentinel_db_path())
    if not db.is_file() or not imos:
        return []
    want = {str(i).strip() for i in imos if str(i).strip()}
    conn = sqlite3.connect(str(db), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute(
            """
            SELECT p.imo, p.mmsi, p.vessel_name, p.timestamp_utc, p.lat, p.lon,
                   p.sog, p.cog, p.heading, p.nav_status, p.draft_m, p.destination
            FROM ais_positions p
            INNER JOIN (
                SELECT imo AS _imo, MAX(timestamp_utc) AS mx
                FROM ais_positions
                WHERE imo IS NOT NULL AND TRIM(imo) != ''
                GROUP BY imo
            ) t ON p.imo = t._imo AND p.timestamp_utc = t.mx
            """
        )
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            imo = str(row[0] or "").strip()
            if imo not in want:
                # also accept zero-padded / int-normalized
                try:
                    if str(int(float(imo))) not in want:
                        continue
                    imo = str(int(float(imo)))
                except (TypeError, ValueError):
                    continue
            lat, lon = row[4], row[5]
            if lat is None or lon is None:
                continue
            out.append(
                {
                    "imo": imo,
                    "mmsi": _s(row[1]),
                    "vessel_name": _s(row[2]),
                    "timestamp_utc": _parse_vf_timestamp(row[3]) or _s(row[3]) or _utc_iso(),
                    "lat": _f(lat),
                    "lon": _f(lon),
                    "sog": _f(row[6]),
                    "cog": _f(row[7]),
                    "heading": _f(row[8]),
                    "nav_status": _s(row[9]),
                    "draft_m": _f(row[10]),
                    "destination": _s(row[11]),
                    "eta": None,
                    "flag": None,
                    "vessel_type": None,
                    "dwt": None,
                    "loa_m": None,
                    "beam_m": None,
                    "built_year": None,
                    "departure_port": None,
                    "raw_json": json.dumps({"source": "local_core_ais", "imo": imo}),
                }
            )
        return out
    except sqlite3.Error as exc:
        LOG.warning("local AIS fallback query failed: %s", exc)
        return []
    finally:
        conn.close()


def hybrid_local_sync(
    *,
    imos: list[str],
    batch_index: int,
    synced_at: str,
    exports: dict[str, str],
    state: dict[str, Any],
    refresh_snapshot: bool,
    auth_note: str,
    tier: int = 1,
    rotation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """My Fleet export + local sentinel_ais.db overlay when commercial REST is unavailable."""
    records = load_local_ais_for_imos(imos)
    for r in records:
        r["tier"] = tier
    written = 0
    ais_n = 0
    if records:
        tconn = open_telemetry_db()
        try:
            written = persist_telemetry(
                tconn,
                records,
                batch_index=batch_index,
                synced_at=synced_at,
                tier=tier,
            )
        finally:
            tconn.close()
        ais_n = upsert_sentinel_ais(records, sentinel_db_path())

    rot = rotation or {}
    state["slot_phase"] = rot.get("next_slot_phase", (int(state.get("slot_phase") or 0) + 1) % 3)
    state["gas_batch_index"] = rot.get(
        "next_gas_batch_index", int(state.get("gas_batch_index") or 0)
    )
    state["oil_batch_index"] = rot.get(
        "next_oil_batch_index", int(state.get("oil_batch_index") or 0)
    )
    state["batch_index"] = batch_index
    state["active_tier"] = tier
    state["rotation_label"] = rot.get("label")
    state["auth_mode"] = "hybrid_local"
    state["ingest_mode"] = "hybrid_local"
    state["last_positions"] = len(records)
    state["last_batch_size"] = len(imos)
    state["last_ais_upserts"] = ais_n
    state["last_telemetry_rows"] = written
    if rot.get("meta"):
        state["tier1_gas_count"] = rot["meta"].get("tier1_gas_count")
        state["tier2_oil_count"] = rot["meta"].get("tier2_oil_count")
        state["monitored_total"] = int(rot.get("monitored_total") or 0)
    if records:
        state["last_success_at"] = synced_at
        state["last_error"] = None
    else:
        state["last_error"] = (
            f"{auth_note} Hybrid local fallback found 0 live ais_positions "
            f"for tier={tier} batch {batch_index} ({len(imos)} IMOs)."
        )[:500]
    save_rotation_state(state)
    status = write_api_status(state)
    status["ok"] = True
    status["ok_api"] = False
    status["ingest_mode"] = "hybrid_local"
    status["auth_note"] = auth_note
    status["exports"] = exports
    status["positions"] = len(records)
    status["ais_upserts"] = ais_n
    status["telemetry_rows"] = written
    status["rotated_from_batch"] = batch_index
    status["active_tier"] = tier
    status["rotation_label"] = rot.get("label")
    if refresh_snapshot:
        try:
            snap = take_daily_snapshot(export=True)
            status["snapshot"] = {
                "date": snap.get("snapshot_date"),
                "vessels": snap.get("vessel_count"),
            }
        except Exception as exc:  # noqa: BLE001
            LOG.warning("daily snapshot refresh failed: %s", exc)
            status["snapshot_error"] = str(exc)
    try:
        from services.balance_engine import calculate_daily_market_balance

        status["daily_balance"] = calculate_daily_market_balance()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("daily balance failed: %s", exc)
        status["daily_balance_error"] = str(exc)
    return status


def sync_archive(
    *,
    force: bool = False,
    dry_run: bool = False,
    refresh_snapshot: bool = True,
) -> dict[str, Any]:
    ensure_archive_dirs()
    state = load_rotation_state()
    state["slots_limit"] = slot_limit()

    if not due_for_rotation(state, force=force):
        status = write_api_status(state)
        status["skipped"] = True
        status["reason"] = (
            f"next tier slot in <{slot_interval_hours()}h (use --force-sync)"
        )
        return status

    rotation = select_tiered_rotation_batch(state)
    imos = rotation["imos"]
    batch_index = int(rotation["batch_index"])
    tier = int(rotation["tier"])
    if not imos:
        raise FileNotFoundError(
            "No IMOs for tiered rotation — check fleet_database.csv / fleet_oil_tankers.csv"
        )

    synced_at = _utc_iso()
    state["last_sync_at"] = synced_at
    state["last_batch_size"] = len(imos)
    state["batch_index"] = batch_index
    state["active_tier"] = tier
    state["slot_phase"] = rotation["slot_phase"]
    state["rotation_label"] = rotation["label"]
    state["tier1_gas_count"] = (rotation.get("meta") or {}).get("tier1_gas_count")
    state["tier2_oil_count"] = (rotation.get("meta") or {}).get("tier2_oil_count")
    state["monitored_total"] = rotation.get("monitored_total")
    state["total_batches"] = int(rotation.get("gas_batches") or 0) + int(
        rotation.get("oil_batches") or 0
    )
    exports = export_rotation_batch(imos, batch_index)

    if dry_run:
        state["last_error"] = None
        state["last_positions"] = 0
        # Advance pointers even in dry-run so ops can preview the cycle
        state["slot_phase"] = rotation["next_slot_phase"]
        state["gas_batch_index"] = rotation["next_gas_batch_index"]
        state["oil_batch_index"] = rotation["next_oil_batch_index"]
        save_rotation_state(state)
        status = write_api_status(state)
        status["dry_run"] = True
        status["would_put_imos"] = len(imos)
        status["batch_index"] = batch_index
        status["active_tier"] = tier
        status["rotation_label"] = rotation["label"]
        status["exports"] = exports
        return status

    resolved = resolve_commercial_key(auto_persist=True, validate=False)
    api_key = (resolved.key if resolved else "") or ""
    if resolved:
        state["key_masked"] = resolved.masked()
        state["key_source"] = resolved.source
    LOG.info(
        "%s · slots %d/%d · monitored %d · key=%s src=%s",
        rotation["label"],
        len(imos),
        slot_limit(),
        state.get("monitored_total") or 0,
        mask_key(api_key) if api_key else "none",
        (resolved.source if resolved else "none"),
    )

    def _advance_commercial(records: list[dict[str, Any]], written: int, ais_n: int) -> dict[str, Any]:
        state["slot_phase"] = rotation["next_slot_phase"]
        state["gas_batch_index"] = rotation["next_gas_batch_index"]
        state["oil_batch_index"] = rotation["next_oil_batch_index"]
        state["last_success_at"] = synced_at
        state["last_error"] = None
        state["auth_mode"] = "vesselfinder_commercial"
        state["ingest_mode"] = "commercial_rest"
        state["last_positions"] = len(records)
        state["last_batch_size"] = len(imos)
        state["last_ais_upserts"] = ais_n
        state["last_telemetry_rows"] = written
        save_rotation_state(state)
        status = write_api_status(state)
        status["ok_api"] = True
        status["ingest_mode"] = "commercial_rest"
        status["positions"] = len(records)
        status["ais_upserts"] = ais_n
        status["telemetry_rows"] = written
        status["rotated_from_batch"] = batch_index
        status["active_tier"] = tier
        status["rotation_label"] = rotation["label"]
        status["exports"] = exports
        if refresh_snapshot:
            try:
                snap = take_daily_snapshot(export=True)
                status["snapshot"] = {
                    "date": snap.get("snapshot_date"),
                    "vessels": snap.get("vessel_count"),
                }
            except Exception as exc:  # noqa: BLE001
                LOG.warning("daily snapshot refresh failed: %s", exc)
                status["snapshot_error"] = str(exc)
        try:
            from services.balance_engine import calculate_daily_market_balance

            status["daily_balance"] = calculate_daily_market_balance()
        except Exception as exc:  # noqa: BLE001
            status["daily_balance_error"] = str(exc)
        return status

    # Path A — commercial VesselFinder REST after handshake
    if api_key:
        auth = handshake_commercial(api_key)
        if auth.get("ok"):
            client = VesselFinderFleetClient(api_key)
            try:
                client.replace_fleet(imos)
                time.sleep(DEFAULT_POST_PUT_WAIT_SEC)
                raw_list = client.fetch_vesselslist()
                records = [r for r in (normalize_vessel_record(x) for x in raw_list) if r]
                for r in records:
                    r["tier"] = tier
                tconn = open_telemetry_db()
                try:
                    written = persist_telemetry(
                        tconn,
                        records,
                        batch_index=batch_index,
                        synced_at=synced_at,
                        tier=tier,
                    )
                finally:
                    tconn.close()
                ais_n = upsert_sentinel_ais(records, sentinel_db_path())
                return _advance_commercial(records, written, ais_n)
            except Exception as exc:
                LOG.warning("commercial REST path failed, falling back to hybrid: %s", exc)
                return hybrid_local_sync(
                    imos=imos,
                    batch_index=batch_index,
                    synced_at=synced_at,
                    exports=exports,
                    state=state,
                    refresh_snapshot=refresh_snapshot,
                    auth_note=f"commercial_rest_error: {exc}",
                    tier=tier,
                    rotation=rotation,
                )

        note = (
            f"VesselFinder commercial handshake rejected ({auth.get('error')}). "
            "Auto-fallback: OSINT REGISTRY HYBRID_LOCAL."
        )
        LOG.warning(note)
        return hybrid_local_sync(
            imos=imos,
            batch_index=batch_index,
            synced_at=synced_at,
            exports=exports,
            state=state,
            refresh_snapshot=refresh_snapshot,
            auth_note=note,
            tier=tier,
            rotation=rotation,
        )

    # Path B — no key: still rotate + local overlay
    note = "No commercial userkey resolved — hybrid My Fleet export + local core AIS only."
    LOG.warning(note)
    return hybrid_local_sync(
        imos=imos,
        batch_index=batch_index,
        synced_at=synced_at,
        exports=exports,
        state=state,
        refresh_snapshot=refresh_snapshot,
        auth_note=note,
        tier=tier,
        rotation=rotation,
    )


def status_report() -> dict[str, Any]:
    state = load_rotation_state()
    from services.fleet_tiers import rebuild_fleet_tiers

    rebuilt = rebuild_fleet_tiers(slot_limit=slot_limit())
    state.setdefault("slots_limit", slot_limit())
    state["tier1_gas_count"] = rebuilt["meta"].get("tier1_gas_count")
    state["tier2_oil_count"] = rebuilt["meta"].get("tier2_oil_count")
    state["monitored_total"] = int(rebuilt["meta"].get("tier1_gas_count") or 0) + int(
        rebuilt["meta"].get("tier2_oil_count") or 0
    )
    state["total_batches"] = int(rebuilt["meta"].get("gas_batches") or 0) + int(
        rebuilt["meta"].get("oil_batches") or 0
    )
    resolved = resolve_commercial_key(auto_persist=False, validate=False)
    if resolved:
        state["key_masked"] = resolved.masked()
        state["key_source"] = resolved.source
    status = write_api_status(state)
    status["due"] = due_for_rotation(state, force=False)
    status["api_key_configured"] = bool(resolved and resolved.key)
    status["telemetry_db"] = str(TELEMETRY_DB)
    status["sentinel_db"] = str(sentinel_db_path())
    status["tiers_meta"] = rebuilt["meta"]
    return status


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    p = argparse.ArgumentParser(description="VesselFinder 500-slot archive rotation")
    p.add_argument("--force-sync", action="store_true", help="Ignore 3-day cycle gate")
    p.add_argument("--dry-run", action="store_true", help="Plan batch without API calls")
    p.add_argument("--status", action="store_true", help="Print rotation / API status JSON")
    p.add_argument("--no-snapshot", action="store_true", help="Skip daily archive snapshot")
    p.add_argument("--userkey", default=None, help="Inject commercial userkey (auto-persist)")
    p.add_argument("--api-key", dest="api_key", default=None, help="Alias for --userkey")
    args = p.parse_args(argv)

    # Cascade resolve + optional CLI inject before sync
    resolve_commercial_key(
        argv=argv,
        explicit=args.userkey or args.api_key,
        auto_persist=True,
        validate=False,
    )

    if args.status and not args.force_sync:
        print(json.dumps(status_report(), ensure_ascii=False, indent=2))
        return 0

    try:
        result = sync_archive(
            force=bool(args.force_sync or args.dry_run),
            dry_run=bool(args.dry_run),
            refresh_snapshot=not args.no_snapshot,
        )
    except Exception as exc:
        LOG.error("sync failed: %s", exc)
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
