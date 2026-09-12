"""Live AIS freshness + Truth Contract health document (DASHBOARD_PORT)."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]

LIVE_OK_LAG_SEC = 300  # < 5 minutes → ais: live_ok / source_mode=live_ais
STALE_LAG_SEC = 600
# Display helper only — rolling coverage window is derived from rotation cycle
# (see coverage_window_from_config). Legacy hardcode 720 (= 3×240s) removed:
# Prompt-7 locked interval=180s → full cycle 540s for TOP-500 @ 200 MMSI/chunk.
DEFAULT_COVERAGE_WINDOW_MIN = 9

DB_CANDIDATES = [
    ROOT / "история1" / "sentinel_ais.db",
    ROOT / "sentinel_ais.db",
    ROOT / "data" / "sentinel_ais.db",
    Path("/opt/oracle1001/ais_ingest/история1/sentinel_ais.db"),
    Path("/opt/oracle1001/analytical_engine/история1/sentinel_ais.db"),
]


def coverage_window_from_config(
    config_path: Path | None = None,
    *,
    universe: int | None = None,
) -> float:
    """Derive coverage_window_seconds = rotation_interval × n_chunks from config.

    Single source of truth with connector ``_chunk_plan``: do not hardcode 540/720.
    Historical drift: ais_health once defaulted to 720 (= 3×240s) after config
    moved to rotation_interval_seconds=180 (cycle 540s) — that was unexplained
    legacy, not an intentional buffer.
    """
    path = config_path or (ROOT / "config.yaml")
    interval = 180.0
    cap = 200
    uni = 500 if universe is None else int(universe)
    if path.exists():
        try:
            import yaml

            with path.open(encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            sentinel = cfg.get("sentinel") or {}
            gas = cfg.get("gas_weekly") or {}
            if sentinel.get("rotation_interval_seconds") is not None:
                interval = float(sentinel["rotation_interval_seconds"])
            if sentinel.get("mmsi_per_subscription") is not None:
                cap = int(sentinel["mmsi_per_subscription"])
            if universe is None and gas.get("top_n_target") is not None:
                uni = int(gas["top_n_target"])
        except Exception:  # noqa: BLE001
            pass
    env_iv = (os.environ.get("SENTINEL_ROTATION_INTERVAL_SECONDS") or "").strip()
    if env_iv:
        try:
            interval = float(env_iv)
        except ValueError:
            pass
    env_cap = (os.environ.get("SENTINEL_MMSI_PER_SUBSCRIPTION") or "").strip()
    if env_cap:
        try:
            cap = int(env_cap)
        except ValueError:
            pass
    cap = max(1, cap)
    uni = max(1, uni)
    if uni <= cap:
        # Matches connector full_list branch floor (stable coverage window).
        return float(max(interval, 900.0))
    n_chunks = int(math.ceil(uni / float(cap)))
    return float(interval * n_chunks)


# Resolved at import from config.yaml (typically 180×3=540 for TOP-500).
DEFAULT_COVERAGE_WINDOW_SEC = coverage_window_from_config()


def resolve_coverage_window_seconds(
    *,
    window_seconds: float | None = None,
    window_minutes: float | None = None,
) -> float:
    """Prefer explicit seconds → env → minutes arg → config-derived rotation cycle."""
    if window_seconds is not None and float(window_seconds) > 0:
        return float(window_seconds)
    env = (os.environ.get("SENTINEL_COVERAGE_WINDOW_SECONDS") or "").strip()
    if env:
        try:
            val = float(env)
            if val > 0:
                return val
        except ValueError:
            pass
    if window_minutes is not None and float(window_minutes) > 0:
        return float(window_minutes) * 60.0
    # Re-read config each call so interval/chunk edits apply without process restart.
    return float(coverage_window_from_config())


def compute_top500_live_coverage(
    db_path: Path | None = None,
    *,
    window_minutes: float | None = None,
    window_seconds: float | None = None,
    fleet_csv: Path | None = None,
) -> dict[str, Any]:
    """Unique TOP-500 MMSIs seen inside the coverage rolling window.

    Window MUST equal one full MMSI-rotation cycle when the connector rotates
    subsets — otherwise coverage is artificially capped by architecture.
    """
    from datetime import timedelta

    window_sec = resolve_coverage_window_seconds(
        window_seconds=window_seconds,
        window_minutes=window_minutes,
    )
    db = resolve_db_path(db_path)
    csv_path = fleet_csv or (ROOT / "output" / "fleet_database.csv")
    out: dict[str, Any] = {
        "top500_live_coverage": 0,
        "top500_universe": 0,
        "window_minutes": round(window_sec / 60.0, 3),
        "coverage_window_seconds": float(window_sec),
        "cutoff_utc": None,
        "fleet_csv": str(csv_path) if csv_path else None,
        "ok": False,
        "error": None,
    }
    if db is None:
        out["error"] = "db_missing"
        return out
    try:
        from services.analytics import select_top500_fleet

        top = select_top500_fleet(csv_path, top_n=500)
        mmsis = [str(x) for x in top["mmsi"].astype(str).tolist()]
        out["top500_universe"] = len(mmsis)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"fleet:{exc}"
        return out
    if not mmsis:
        out["error"] = "empty_top500"
        return out

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=float(window_sec))
    cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    out["cutoff_utc"] = cutoff_s
    try:
        conn = sqlite3.connect(str(db), timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            seen: set[str] = set()
            chunk = 400
            for i in range(0, len(mmsis), chunk):
                part = mmsis[i : i + chunk]
                ph = ",".join("?" * len(part))
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT mmsi FROM ais_positions
                    WHERE mmsi IN ({ph})
                      AND (received_at >= ? OR timestamp_utc >= ?)
                    """,
                    [*part, cutoff_s, cutoff_s],
                ).fetchall()
                seen.update(str(r[0]) for r in rows if r and r[0] is not None)
            out["top500_live_coverage"] = len(seen)
            out["ok"] = True
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["error"] = f"sqlite:{exc}"
    return out


def strip_stale_tag(mode: str | None) -> str:
    """Remove sticky +STALE suffixes so transitions stay automatic."""
    text = str(mode or "live_ais").strip() or "live_ais"
    while text.lower().endswith("+stale"):
        text = text[: -len("+STALE")].rstrip("+")
    return text or "live_ais"


def resolve_source_mode(
    freshness: dict[str, Any],
    *,
    base_mode: str | None = None,
) -> str:
    """Truth Contract source_mode transitions (no manual sticky STALE flag).

    - lag < LIVE_OK_LAG_SEC (300) → live_ais (auto clears +STALE)
    - lag > STALE_LAG_SEC (600)  → <base>+STALE
    - otherwise                  → base without +STALE (WARMING)
    """
    base = strip_stale_tag(base_mode) if base_mode else None
    if freshness.get("status") == "MISSING_REPLICA":
        return "synthetic_seed+STALE"
    if freshness.get("live_ok"):
        return base or "live_ais"
    if freshness.get("stale"):
        root = base or "live_ais"
        return f"{root}+STALE"
    return base or "live_ais"


def resolve_db_path(explicit: str | Path | None = None) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    env = (os.environ.get("SENTINEL_DB_PATH") or "").strip()
    if env:
        p = Path(env)
        if p.exists():
            return p
    for c in DB_CANDIDATES:
        if c.exists():
            return c
    return None


def _parse_ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "T")
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _query_latest_epoch(conn: sqlite3.Connection) -> tuple[Optional[float], Optional[str], str]:
    """Return (epoch, raw_ts, source) from the freshest available signal."""
    queries = (
        ("SELECT MAX(received_at) FROM ais_positions", "ais_positions.received_at"),
        ("SELECT MAX(timestamp_utc) FROM ais_positions", "ais_positions.timestamp_utc"),
        ("SELECT MAX(ts_utc) FROM pipeline_telemetry", "pipeline_telemetry.ts_utc"),
    )
    best_epoch: Optional[float] = None
    best_raw: Optional[str] = None
    best_src = "none"
    for sql, src in queries:
        try:
            row = conn.execute(sql).fetchone()
        except sqlite3.Error:
            continue
        raw = row[0] if row else None
        epoch = _parse_ts(raw)
        if epoch is None:
            continue
        if best_epoch is None or epoch > best_epoch:
            best_epoch, best_raw, best_src = epoch, str(raw), src
    return best_epoch, best_raw, best_src


def compute_ais_freshness(
    db_path: Path | None = None,
    *,
    live_ok_lag_sec: float = LIVE_OK_LAG_SEC,
    stale_after_sec: float = STALE_LAG_SEC,
) -> dict[str, Any]:
    """Compute ingest lag from SQL timestamps (WAL-safe; not file mtime)."""
    db = resolve_db_path(db_path)
    now = time.time()
    if db is None:
        return {
            "stale": True,
            "live_ok": False,
            "db_path": None,
            "age_sec": None,
            "lag_minutes": None,
            "status": "MISSING_REPLICA",
            "ais_truth": "missing",
            "signal_source": None,
            "latest_ts": None,
            "banner": "[STALE AIS REPLICA DETECTED - DATABASE FILE MISSING]",
            "mtime_epoch": None,
        }

    mtime = db.stat().st_mtime
    latest_epoch: Optional[float] = None
    latest_raw: Optional[str] = None
    signal_source = "mtime"
    integrity_ok = True

    try:
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            latest_epoch, latest_raw, signal_source = _query_latest_epoch(conn)
            if latest_epoch is None:
                signal_source = "mtime"
                latest_epoch = mtime
        finally:
            conn.close()
    except sqlite3.Error as exc:
        integrity_ok = False
        signal_source = "mtime"
        latest_epoch = mtime
        latest_raw = f"sqlite_error:{exc}"

    age = max(0.0, now - float(latest_epoch or mtime))
    lag_min = round(age / 60.0, 1)
    live_ok = age < float(live_ok_lag_sec) and integrity_ok
    stale = age > float(stale_after_sec) or not integrity_ok

    if live_ok:
        ais_truth = "live_ok"
        status = "FRESH"
        banner = None
    elif not integrity_ok:
        ais_truth = "db_corrupt"
        status = "CORRUPT"
        banner = "[AIS DB INTEGRITY FAILURE — RUN sqlite_retention --recover]"
    elif stale:
        ais_truth = "stale"
        status = "STALE"
        banner = f"[STALE AIS REPLICA DETECTED - DATA LAG > {lag_min} MINS]"
    else:
        ais_truth = "warming"
        status = "WARMING"
        banner = None

    return {
        "stale": stale,
        "live_ok": live_ok,
        "db_path": str(db),
        "mtime_epoch": mtime,
        "age_sec": round(age, 1),
        "lag_minutes": lag_min,
        "status": status,
        "ais_truth": ais_truth,
        "signal_source": signal_source,
        "latest_ts": latest_raw,
        "integrity_ok": integrity_ok,
        "banner": banner,
        "live_ok_lag_sec": live_ok_lag_sec,
    }


def build_health_document(
    *,
    freshness: dict[str, Any] | None = None,
    source_mode: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Truth-contract health payload for /output/api/v1/health (HTTP 200)."""
    fr = freshness or compute_ais_freshness()
    ais_truth = fr.get("ais_truth") or "unknown"
    mode = resolve_source_mode(fr, base_mode=source_mode)

    truth = {
        "ais": "live_ok" if fr.get("live_ok") else ais_truth,
        "top10": (extra or {}).get("top10", "unknown"),
        "route": (extra or {}).get("route", "unknown"),
        "ttf": (extra or {}).get("ttf", "unknown"),
    }

    # Continuous availability: HTTP layer returns 200; status reflects AIS plane
    status = "ok" if fr.get("live_ok") else ("degraded" if not fr.get("stale") else "unhealthy")

    doc: dict[str, Any] = {
        "service": "sentinel_dashboard",
        "status": status,
        "http": "200",
        "port": int(os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or 8765),
        "operational_status": "NOMINAL" if fr.get("live_ok") else (
            "DEGRADED_STALE_REPLICA" if fr.get("stale") else "WARMING"
        ),
        "replica": fr,
        "source_mode": mode,
        "truth_contract": truth,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # Merge last release snapshot (spoofing / alerts / balance / quant) if present
    snap_path = ROOT / "output" / "api" / "v1" / "health.json"
    if snap_path.exists():
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            if isinstance(snap, dict):
                for key in (
                    "ais_spoofing",
                    "reactive_alerts",
                    "top500_balance_status",
                    "quant_pipeline",
                    "top10",
                    "route",
                    "ttf_spot",
                    "ttf_integrity",
                    "live_vessel_count",
                    "top500_live_coverage",
                    "top500_coverage",
                    "coverage_window_seconds",
                    "subscription_mode",
                    "published_at",
                    "build_mode",
                    "truth_contract",
                    # Dual-gate (Prompt 8/10) — must survive connector heartbeat overwrites
                    "pipeline_health_status",
                    "pipeline_health",
                    "fleet_sample_status",
                    "fleet_sample",
                    "sample_size_caveat",
                ):
                    if key in snap and key not in doc:
                        doc[key] = snap[key]
                    elif key in snap and key == "truth_contract" and isinstance(snap[key], dict):
                        # Prefer live AIS truth; keep sheet truths from snapshot
                        merged = dict(snap[key])
                        merged["ais"] = doc["truth_contract"]["ais"]
                        doc["truth_contract"] = merged
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    if extra:
        for k, v in extra.items():
            if k not in ("top10", "route", "ttf"):
                doc[k] = v

    # Always recompute dual-gate from live freshness + coverage (never drop these fields).
    try:
        from services.dual_gate import (
            compute_fleet_sample_status,
            compute_pipeline_health_status,
        )

        cov_n = int(doc.get("top500_live_coverage") or 0)
        if not cov_n and isinstance(doc.get("top500_coverage"), dict):
            cov_n = int((doc.get("top500_coverage") or {}).get("top500_live_coverage") or 0)
        sample = compute_fleet_sample_status(
            cov_n,
            universe=int(
                ((doc.get("top500_coverage") or {}) if isinstance(doc.get("top500_coverage"), dict) else {}).get(
                    "top500_universe"
                )
                or 500
            ),
        )
        connector = doc.get("connector") if isinstance(doc.get("connector"), dict) else {}
        pipe = compute_pipeline_health_status(
            freshness=fr,
            connector=connector,
            port_ok=True,
            port_drift_8478=False,
        )
        doc["pipeline_health_status"] = pipe["pipeline_health_status"]
        doc["pipeline_health"] = pipe
        doc["fleet_sample_status"] = sample["fleet_sample_status"]
        doc["fleet_sample"] = sample
        doc["sample_size_caveat"] = sample.get("sample_size_caveat")
        doc["top500_live_coverage"] = cov_n
    except Exception:  # noqa: BLE001
        pass

    return doc


def write_health_files(doc: dict[str, Any] | None = None, root: Path | None = None) -> Path:
    root = root or ROOT
    health_dir = root / "output" / "api" / "v1"
    health_dir.mkdir(parents=True, exist_ok=True)
    payload = doc or build_health_document()
    try:
        from services.utils.path_sanitizer import sanitize_structure

        payload = sanitize_structure(payload, root=root)
    except Exception:
        pass
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    path = health_dir / "health"
    path.write_text(text, encoding="utf-8")
    (health_dir / "health.json").write_text(text, encoding="utf-8")
    return path
