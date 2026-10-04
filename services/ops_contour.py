"""Admin ops contour. Recomputed at most every 60 seconds from live tables."""

from __future__ import annotations

import os
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.dual_gate import compute_readiness_score
from services.i18n_catalog import LANGS, catalog_for, load_catalogs

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_TARGET_N = 1260
_TTL_SEC = 60.0
_CACHE: dict[str, Any] = {"at": 0.0, "block": None}
# One in-flight rebuild per process. Parallel misses wait for that result
# instead of each running build_ops_contour().
_BUILD_LOCK = threading.Lock()


def reset_cache() -> None:
    _CACHE["at"] = 0.0
    _CACHE["block"] = None


def _db_path() -> Path | None:
    raw = (os.getenv("SENTINEL_DB_PATH") or "").strip()
    if raw:
        return Path(raw)
    try:
        from services.storage import DEFAULT_DB

        path = Path(DEFAULT_DB)
        return path if path.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def _retention_last_run() -> str | None:
    path = ROOT / "data" / "archive" / "retention_state.json"
    if not path.is_file():
        return None
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8")).get("ts_utc")
    except (OSError, ValueError):
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _utc_day(now: datetime | None = None) -> str:
    moment = now or _now_utc()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def settled_snapshot_date(conn: sqlite3.Connection, *, now: datetime | None = None) -> str | None:
    """Latest archive day strictly before the current UTC date.

    The 00:30 job writes today with verification flags still at 0. The poll
    marks that day on the following UTC date. Readiness must not use today.
    """
    today = _utc_day(now)
    row = conn.execute(
        "SELECT MAX(snapshot_date) FROM vessel_daily_archive WHERE snapshot_date < ?",
        (today,),
    ).fetchone()
    if not row or not row[0]:
        return None
    return str(row[0])


def verification_counts(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """gfw / vf / satellite counts on the settled slice. Missing slice is zero."""
    day = settled_snapshot_date(conn, now=now)
    out: dict[str, Any] = {
        "snapshot_date": day,
        "gfw_verified_n": 0,
        "vf_verified_n": 0,
        "satellite_verified_n": 0,
    }
    if not day:
        return out
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(vessel_daily_archive)")}
    if "gfw_verified" in cols:
        counted = conn.execute(
            """
            SELECT COUNT(*) FROM vessel_daily_archive
            WHERE snapshot_date = ? AND COALESCE(gfw_verified, 0) = 1
            """,
            (day,),
        ).fetchone()
        out["gfw_verified_n"] = int(counted[0] if counted else 0)
    if "source" in cols:
        vf = conn.execute(
            """
            SELECT COUNT(*) FROM vessel_daily_archive
            WHERE snapshot_date = ? AND source = 'vf_api'
            """,
            (day,),
        ).fetchone()
        sat = conn.execute(
            """
            SELECT COUNT(*) FROM vessel_daily_archive
            WHERE snapshot_date = ? AND source = 'satellite_ais'
            """,
            (day,),
        ).fetchone()
        out["vf_verified_n"] = int(vf[0] if vf else 0)
        out["satellite_verified_n"] = int(sat[0] if sat else 0)
    return out


def _archive_block(conn: sqlite3.Connection) -> dict[str, Any]:
    day = conn.execute("SELECT MAX(snapshot_date) FROM vessel_daily_archive").fetchone()
    snapshot = day[0] if day else None
    if not snapshot:
        return {
            "snapshot_date": None,
            "rows": 0,
            "target": ARCHIVE_TARGET_N,
            "gap_hours_median": None,
            "gap_hours_p95": None,
            "sources": {},
        }
    rows = conn.execute(
        "SELECT source, gap_hours FROM vessel_daily_archive WHERE snapshot_date=?",
        (snapshot,),
    ).fetchall()
    sources: dict[str, int] = {}
    gaps: list[float] = []
    for source, gap in rows:
        key = str(source or "none")
        sources[key] = sources.get(key, 0) + 1
        if gap is not None:
            gaps.append(float(gap))
    gaps.sort()
    p95 = None
    if gaps:
        idx = min(len(gaps) - 1, int(round(0.95 * (len(gaps) - 1))))
        p95 = gaps[idx]
    gfw_verified = 0
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(vessel_daily_archive)")}
        if "gfw_verified" in cols:
            counted = conn.execute(
                "SELECT COUNT(*) FROM vessel_daily_archive WHERE snapshot_date=? AND gfw_verified=1",
                (snapshot,),
            ).fetchone()
            gfw_verified = int(counted[0] if counted else 0)
    except sqlite3.Error:
        gfw_verified = 0
    return {
        "snapshot_date": snapshot,
        "rows": len(rows),
        "target": ARCHIVE_TARGET_N,
        "gap_hours_median": statistics.median(gaps) if gaps else None,
        "gap_hours_p95": p95,
        "sources": sources,
        "gfw_verified_n": gfw_verified,
    }


def _provider_row(name: str, *, verified: int) -> dict[str, Any]:
    try:
        from services.key_activation import provider_state

        st = provider_state(name)
    except Exception:  # noqa: BLE001
        st = {}
    if st.get("paused"):
        status = "paused"
    elif st.get("last_ok") is True:
        status = "ok"
    elif st:
        status = "degraded"
    else:
        status = "unconfigured"
    return {
        "status": status,
        "verified_24h": int(verified),
        "error_streak": int(st.get("consecutive_errors") or 0),
        "probe_next": st.get("updated_at"),
    }


def _i18n_block() -> dict[str, Any]:
    cats = load_catalogs()
    keys = [set(cats[lang]) for lang in LANGS if lang in cats]
    equal = bool(keys) and all(k == keys[0] for k in keys)
    ar = catalog_for("ar")
    return {
        "locales_loaded": len(cats),
        "keys_equal": equal,
        "rtl_ok": ar.get("dir") == "rtl" and catalog_for("en").get("dir") == "ltr",
    }


def gather_readiness_facts(*, archive: dict[str, Any] | None = None) -> dict[str, Any]:
    sources = (archive or {}).get("sources") or {}
    terrestrial = int(sources.get("terrestrial_ais") or 0)
    settled = (archive or {}).get("settled")
    if isinstance(settled, dict):
        gfw = int(settled.get("gfw_verified_n") or 0)
        vf = int(settled.get("vf_verified_n") or 0)
        satellite = int(settled.get("satellite_verified_n") or 0)
        settled_day = settled.get("snapshot_date")
    else:
        gfw = int((archive or {}).get("gfw_verified_n") or sources.get("gfw_events") or 0)
        vf = int(sources.get("vf_api") or 0)
        satellite = int(sources.get("satellite_ais") or 0)
        settled_day = None
    try:
        from services.acceptance_history import green_streak_days

        streak = green_streak_days()
    except Exception:  # noqa: BLE001
        streak = 0
    pilot_n = 0
    try:
        from services.pilot_register import count_clients

        pilot_n = count_clients(status="active")
    except Exception:  # noqa: BLE001
        pilot_n = 0
    llm_n = 0
    try:
        from services.llm_router import llm_health_block

        llm = llm_health_block()
        if llm.get("status") == "ok":
            llm_n = 1
    except Exception:  # noqa: BLE001
        llm_n = 0
    pipeline = (os.getenv("SENTINEL_PIPELINE_STATUS") or "UNKNOWN").upper()
    return {
        "pipeline_health_status": pipeline,
        "terrestrial_verified_n": terrestrial,
        "gfw_verified_n": gfw,
        "vf_verified_n": vf,
        "llm_verified_n": llm_n,
        "satellite_verified_n": satellite,
        "settled_snapshot_date": settled_day,
        "green_streak_days": streak,
        "pilot_active_n": pilot_n,
    }


def build_ops_contour() -> dict[str, Any]:
    from services.disk_forecast import disk_forecast_block
    from services.job_log import scheduler_health_block
    from services.satellite_adapter import public_status

    sched = scheduler_health_block()
    forecast = disk_forecast_block()
    archive = {"rows": 0, "target": ARCHIVE_TARGET_N, "sources": {}}
    path = _db_path()
    if path is not None and path.is_file():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                archive = _archive_block(conn)
                archive["settled"] = verification_counts(conn, now=_now_utc())
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    sources = archive.get("sources") or {}
    settled = archive.get("settled") if isinstance(archive.get("settled"), dict) else {}
    providers = {
        "terrestrial": _provider_row("aisstream", verified=int(sources.get("terrestrial_ais") or 0)),
        "gfw": _provider_row("gfw", verified=int(settled.get("gfw_verified_n") or 0)),
        "vesselfinder": _provider_row("vesselfinder", verified=int(settled.get("vf_verified_n") or 0)),
        "anthropic": _provider_row("anthropic", verified=0),
        "satellite": public_status(verified_24h=int(settled.get("satellite_verified_n") or 0)),
    }
    facts = gather_readiness_facts(archive=archive)
    # Pipeline status for the score is the live gate when the caller already knows it.
    # gather uses env/acceptance; build_health overwrites pipeline before scoring.
    return {
        "scheduler": sched,
        "providers": providers,
        "disk": {
            "free_pct": None,
            "forecast_h": forecast.get("hours_to_critical"),
            "retention_last_run": _retention_last_run(),
            "forecast_status": forecast.get("status"),
        },
        "archive": archive,
        "i18n": _i18n_block(),
        "readiness": compute_readiness_score(facts),
        "facts": facts,
    }


def ops_contour_block(*, force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _CACHE.get("block")
    if not force and cached is not None and (now - float(_CACHE.get("at") or 0)) < _TTL_SEC:
        return cached
    with _BUILD_LOCK:
        now = time.monotonic()
        cached = _CACHE.get("block")
        if not force and cached is not None and (now - float(_CACHE.get("at") or 0)) < _TTL_SEC:
            return cached
        block = build_ops_contour()
        _CACHE["at"] = time.monotonic()
        _CACHE["block"] = block
        return block
