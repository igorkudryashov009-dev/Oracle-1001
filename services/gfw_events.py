#!/usr/bin/env python3
"""Global Fishing Watch Events client — free non-commercial verification plane.

Contract 1.8.0-ops-gis-sot:
  - Auth via GFW_API_TOKEN (Bearer). Absence ⇒ configured=false, no HTTP.
  - Events only from real API responses (raw_json provenance). Empty = no events
    (not an error). Never invents positions into vessel_daily_archive.
  - Rate ≤2 rps; daily_cap 500; response cache TTL 24h.
  - Does NOT feed Dual Gate fleet_sample_status.

Docs: https://globalfishingwatch.org/our-apis/
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.gfw_events")

GFW_GATEWAY = os.getenv(
    "GFW_API_BASE", "https://gateway.api.globalfishingwatch.org"
).rstrip("/")
GFW_EVENTS_URL = f"{GFW_GATEWAY}/v3/events"
GFW_VESSELS_SEARCH_URL = f"{GFW_GATEWAY}/v3/vessels/search"

EVENT_DATASETS = (
    "public-global-gaps-events:latest",
    "public-global-encounters-events:latest",
    "public-global-loitering-events:latest",
    "public-global-port-visits-events:latest",
)
VESSEL_IDENTITY_DATASET = "public-global-vessel-identity:latest"

DAILY_CAP = 500
MAX_RPS = 2.0
MIN_INTERVAL_SEC = 1.0 / MAX_RPS
CACHE_TTL_SEC = 86400.0
BATCH_DAILY_MAX = 20
TRAIL_DAYS = 7

BUDGET_PATH = ROOT / "data" / "archive" / "gfw_budget.json"
CACHE_DIR = ROOT / "data" / "cache" / "gfw"
STATE_PATH = ROOT / "data" / "archive" / "gfw_poll_state.json"

_LOCK = threading.RLock()
_LAST_CALL_MONO = 0.0

VESSEL_GFW_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS vessel_gfw_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_imo INTEGER NOT NULL,
    event_id TEXT,
    event_type TEXT NOT NULL,
    start_utc TEXT,
    end_utc TEXT,
    lat REAL,
    lon REAL,
    fetched_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(vessel_imo, event_id)
)
"""
VESSEL_GFW_EVENTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_gfw_imo ON vessel_gfw_events(vessel_imo)",
    "CREATE INDEX IF NOT EXISTS idx_gfw_type ON vessel_gfw_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_gfw_fetched ON vessel_gfw_events(fetched_at)",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mask(token: str) -> str:
    t = (token or "").strip()
    if len(t) < 4:
        return "****" if t else ""
    return f"****{t[-4:]}"


def resolve_token() -> str:
    try:
        from services.runtime_env import getenv_secret

        v = getenv_secret("GFW_API_TOKEN", "GFW_API_KEY", "GLOBAL_FISHING_WATCH_TOKEN")
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return (
        os.getenv("GFW_API_TOKEN")
        or os.getenv("GFW_API_KEY")
        or os.getenv("GLOBAL_FISHING_WATCH_TOKEN")
        or ""
    ).strip()


def _day_key(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).strftime("%Y-%m-%d")


def _empty_budget(day: Optional[str] = None) -> dict[str, Any]:
    d = day or _day_key()
    return {
        "day": d,
        "daily_cap": DAILY_CAP,
        "used": 0,
        "remaining": DAILY_CAP,
        "last_ok": None,
        "last_call_at": None,
        "updated_at": _utc_iso(),
        "history": [],
    }


def _load_budget() -> dict[str, Any]:
    BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    day = _day_key()
    if not BUDGET_PATH.is_file():
        return _empty_budget(day)
    try:
        data = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_budget(day)
    if not isinstance(data, dict) or str(data.get("day") or "") != day:
        return _empty_budget(day)
    data.setdefault("daily_cap", DAILY_CAP)
    data["remaining"] = max(0, int(data.get("daily_cap") or DAILY_CAP) - int(data.get("used") or 0))
    return data


def _save_budget(st: dict[str, Any]) -> None:
    BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = _utc_iso()
    st["remaining"] = max(0, int(st.get("daily_cap") or DAILY_CAP) - int(st.get("used") or 0))
    tmp = BUDGET_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(BUDGET_PATH)


def get_budget_status() -> dict[str, Any]:
    with _LOCK:
        st = _load_budget()
        return {
            "day": st.get("day"),
            "daily_cap": int(st.get("daily_cap") or DAILY_CAP),
            "used": int(st.get("used") or 0),
            "remaining": int(st.get("remaining") or 0),
            "last_ok": st.get("last_ok"),
            "last_call_at": st.get("last_call_at"),
            "updated_at": st.get("updated_at"),
        }


def _record_call(*, ok: bool, detail: str | None = None) -> dict[str, Any]:
    with _LOCK:
        st = _load_budget()
        st["used"] = int(st.get("used") or 0) + 1
        st["last_ok"] = bool(ok)
        st["last_call_at"] = _utc_iso()
        hist = list(st.get("history") or [])
        hist.append(
            {
                "at": st["last_call_at"],
                "ok": bool(ok),
                "detail": (detail or "")[:160] or None,
            }
        )
        st["history"] = hist[-50:]
        _save_budget(st)
        return get_budget_status()


def _rate_limit() -> None:
    global _LAST_CALL_MONO
    with _LOCK:
        now = time.monotonic()
        wait = MIN_INTERVAL_SEC - (now - _LAST_CALL_MONO)
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL_MONO = time.monotonic()


def _cache_path(imo: str, start: str, end: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = f"{imo}_{start}_{end}".replace(":", "").replace("/", "")
    return CACHE_DIR / f"{safe}.json"


def _read_cache(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > CACHE_TTL_SEC:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        LOG.warning("gfw cache write failed: %s", exc)


def gfw_status() -> dict[str, Any]:
    """Public health blob — never includes raw token."""
    token = resolve_token()
    budget = get_budget_status()
    events_7d = 0
    try:
        from services.storage import DEFAULT_DB

        db = Path(os.environ.get("SENTINEL_DB_PATH") or DEFAULT_DB)
        if db.is_file():
            cut = (_utc_now() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM vessel_gfw_events WHERE fetched_at >= ?",
                    (cut,),
                ).fetchone()
                events_7d = int(row[0] or 0) if row else 0
            except sqlite3.Error:
                events_7d = 0
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        events_7d = 0
    return {
        "provider": "global_fishing_watch",
        "configured": bool(token),
        "token_masked": _mask(token) if token else None,
        "last_ok": budget.get("last_ok"),
        "events_7d_n": events_7d,
        "budget": budget,
        "note": (
            "Free non-commercial GFW Events — does not feed Dual Gate; "
            "set GFW_API_TOKEN in .env to activate"
            if not token
            else "GFW Events verification plane active"
        ),
    }


def ensure_gfw_schema(conn: sqlite3.Connection) -> None:
    conn.execute(VESSEL_GFW_EVENTS_DDL)
    for stmt in VESSEL_GFW_EVENTS_INDEXES:
        conn.execute(stmt)
    # Optional columns on vessel_daily_archive
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(vessel_daily_archive)").fetchall()}
        if "gfw_verified" not in cols:
            conn.execute(
                "ALTER TABLE vessel_daily_archive ADD COLUMN gfw_verified INTEGER NOT NULL DEFAULT 0"
            )
        if "gfw_events_n" not in cols:
            conn.execute(
                "ALTER TABLE vessel_daily_archive ADD COLUMN gfw_events_n INTEGER NOT NULL DEFAULT 0"
            )
    except sqlite3.Error:
        pass
    conn.commit()


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Oracle-1001-Sentinel-GFW/1.0",
    }


def _resolve_vessel_id(imo: str, token: str, *, timeout: float = 30.0) -> tuple[Optional[str], Optional[str]]:
    """Map IMO → GFW vessel id. Returns (vessel_id|None, error|None).

    error is ``auth_http_401`` / ``auth_http_403`` on auth failures (must not
    be treated as honest empty / vessel-not-found).
    """
    _rate_limit()
    try:
        resp = requests.get(
            GFW_VESSELS_SEARCH_URL,
            params={
                "query": imo,
                "datasets": VESSEL_IDENTITY_DATASET,
                "limit": 5,
            },
            headers=_auth_headers(token),
            timeout=timeout,
        )
        _record_call(ok=resp.status_code < 400, detail=f"vessels_search http_{resp.status_code}")
        if resp.status_code in (401, 403):
            LOG.warning("GFW vessel search imo=%s http=%s (auth)", imo, resp.status_code)
            return None, f"auth_http_{resp.status_code}"
        if resp.status_code >= 400:
            LOG.warning("GFW vessel search imo=%s http=%s", imo, resp.status_code)
            return None, f"http_{resp.status_code}"
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        _record_call(ok=False, detail=f"vessels_search:{exc}"[:160])
        return None, f"vessels_search:{type(exc).__name__}"
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None, None
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        vid = ent.get("id") or ent.get("vesselId")
        if not vid:
            vessel = ent.get("vessel") if isinstance(ent.get("vessel"), dict) else {}
            vid = vessel.get("id")
        if vid:
            return str(vid), None
    return None, None


def fetch_events_for_imo(
    imo: str,
    *,
    trail_days: int = TRAIL_DAYS,
    token: str | None = None,
    timeout: float = 45.0,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Fetch trailing events for one IMO. Empty entries = no events (ok).

    Auth failures (401/403) return ok=False — never flag gfw_verified.
    """
    imo_s = str(imo).strip()
    if not imo_s.isdigit():
        raise ValueError(f"invalid IMO: {imo!r}")
    tok = (token or resolve_token()).strip()
    if not tok:
        return {
            "ok": False,
            "configured": False,
            "imo": imo_s,
            "events": [],
            "error": "not_configured",
        }

    end = _utc_now()
    start = end - timedelta(days=max(1, int(trail_days)))
    start_s = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_s = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    cache_p = _cache_path(imo_s, start_s[:10], end_s[:10])
    if use_cache:
        cached = _read_cache(cache_p)
        if cached is not None:
            cached["cache_hit"] = True
            return cached

    with _LOCK:
        bud = _load_budget()
        if int(bud.get("remaining") or 0) < 1:
            return {
                "ok": False,
                "configured": True,
                "imo": imo_s,
                "events": [],
                "error": "daily_cap_exhausted",
                "budget": get_budget_status(),
            }

    vessel_id, resolve_err = _resolve_vessel_id(imo_s, tok, timeout=timeout)
    if resolve_err and str(resolve_err).startswith("auth_http_"):
        return {
            "ok": False,
            "configured": True,
            "imo": imo_s,
            "events": [],
            "error": resolve_err,
            "auth_failed": True,
        }
    if not vessel_id:
        # Honest empty — vessel unknown to GFW (not auth); do NOT set verified here
        payload = {
            "ok": True,
            "configured": True,
            "imo": imo_s,
            "events": [],
            "vessel_id": None,
            "note": "vessel_not_found_in_gfw_identity",
            "fetched_at": _utc_iso(),
            "cache_hit": False,
            "verify": False,  # identity miss ≠ verification success
        }
        _write_cache(cache_p, payload)
        return payload

    body = {
        "datasets": list(EVENT_DATASETS),
        "vessels": [{"id": vessel_id}],
        "startDate": start_s,
        "endDate": end_s,
    }
    _rate_limit()
    try:
        resp = requests.post(
            f"{GFW_EVENTS_URL}?limit=50&offset=0",
            headers=_auth_headers(tok),
            json=body,
            timeout=timeout,
        )
        status = resp.status_code
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": (resp.text or "")[:400]}
        ok = status < 400 and not (
            isinstance(data, dict) and data.get("error")
        )
        _record_call(
            ok=ok,
            detail=None if ok else f"events http_{status}",
        )
        if not ok:
            err = None
            if isinstance(data, dict):
                err = data.get("error") or data.get("message")
            auth_failed = status in (401, 403)
            return {
                "ok": False,
                "configured": True,
                "imo": imo_s,
                "vessel_id": vessel_id,
                "events": [],
                "http_status": status,
                "error": str(err or f"http_{status}")[:200],
                "token_masked": _mask(tok),
                "auth_failed": auth_failed,
            }
    except requests.RequestException as exc:
        _record_call(ok=False, detail=f"events_network:{exc}"[:160])
        return {
            "ok": False,
            "configured": True,
            "imo": imo_s,
            "events": [],
            "error": f"network:{exc}"[:200],
        }

    entries = []
    if isinstance(data, dict):
        entries = data.get("entries") or data.get("events") or []
    if not isinstance(entries, list):
        entries = []

    normalized: list[dict[str, Any]] = []
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        pos = ent.get("position") if isinstance(ent.get("position"), dict) else {}
        start_e = ent.get("start") or ent.get("startDate") or ent.get("start_utc")
        end_e = ent.get("end") or ent.get("endDate") or ent.get("end_utc")
        etype = (
            ent.get("type")
            or ent.get("eventType")
            or (ent.get("dataset") or "").split(":")[0]
            or "unknown"
        )
        normalized.append(
            {
                "event_id": str(ent.get("id") or ent.get("eventId") or "") or None,
                "event_type": str(etype),
                "start_utc": start_e,
                "end_utc": end_e,
                "lat": pos.get("lat") if pos else ent.get("lat"),
                "lon": pos.get("lon") if pos else ent.get("lon"),
                "raw_json": json.dumps(ent, ensure_ascii=False),
            }
        )

    payload = {
        "ok": True,
        "configured": True,
        "imo": imo_s,
        "vessel_id": vessel_id,
        "events": normalized,
        "events_n": len(normalized),
        "fetched_at": _utc_iso(),
        "window": {"start": start_s, "end": end_s},
        "cache_hit": False,
        "token_masked": _mask(tok),
        "verify": True,  # real GFW events call succeeded (0 events still ok)
    }
    _write_cache(cache_p, payload)
    return payload


def persist_events(
    imo: str,
    events: list[dict[str, Any]],
    *,
    db_path: Path | None = None,
    snapshot_date: str | None = None,
) -> dict[str, Any]:
    """Write vessel_gfw_events rows + set gfw_verified on daily archive (no source overwrite)."""
    from services.storage import DEFAULT_DB

    db = Path(db_path or os.environ.get("SENTINEL_DB_PATH") or DEFAULT_DB)
    day = snapshot_date or (_utc_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    imo_i = int(str(imo).strip())
    fetched = _utc_iso()
    conn = sqlite3.connect(str(db), timeout=30.0)
    try:
        ensure_gfw_schema(conn)
        n = 0
        for ev in events:
            raw = ev.get("raw_json") or "{}"
            if not isinstance(raw, str):
                raw = json.dumps(raw, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO vessel_gfw_events
                  (vessel_imo, event_id, event_type, start_utc, end_utc, lat, lon, fetched_at, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(vessel_imo, event_id) DO UPDATE SET
                  event_type=excluded.event_type,
                  start_utc=excluded.start_utc,
                  end_utc=excluded.end_utc,
                  lat=excluded.lat,
                  lon=excluded.lon,
                  fetched_at=excluded.fetched_at,
                  raw_json=excluded.raw_json
                """,
                (
                    imo_i,
                    ev.get("event_id") or f"anon-{n}-{fetched}",
                    str(ev.get("event_type") or "unknown"),
                    ev.get("start_utc"),
                    ev.get("end_utc"),
                    ev.get("lat"),
                    ev.get("lon"),
                    fetched,
                    raw,
                ),
            )
            n += 1
        # Flag only — never overwrite terrestrial_ais / vf_api source
        cur = conn.execute(
            """
            UPDATE vessel_daily_archive
               SET gfw_verified = 1,
                   gfw_events_n = ?
             WHERE snapshot_date = ? AND imo = ?
            """,
            (len(events), day, imo_i),
        )
        updated = cur.rowcount
        conn.commit()
        return {"ok": True, "inserted": n, "archive_rows_flagged": updated, "day": day}
    finally:
        conn.close()


def load_gap48_imos(*, db_path: Path | None = None, limit: int = BATCH_DAILY_MAX) -> list[str]:
    from services.storage import DEFAULT_DB

    db = Path(db_path or os.environ.get("SENTINEL_DB_PATH") or DEFAULT_DB)
    if not db.is_file():
        return []
    day = (_utc_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT imo FROM vessel_daily_archive
            WHERE snapshot_date = ?
              AND gap_hours IS NOT NULL AND gap_hours > 48
            ORDER BY gap_hours DESC
            LIMIT ?
            """,
            (day, int(limit)),
        ).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def run_daily_gfw_batch(
    *,
    limit: int = BATCH_DAILY_MAX,
    db_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Poll ≤limit gap_48h vessels. No-op when token missing."""
    token = resolve_token()
    imos = load_gap48_imos(db_path=db_path, limit=limit)
    result: dict[str, Any] = {
        "ok": True,
        "configured": bool(token),
        "planned": len(imos),
        "fetched": 0,
        "events_total": 0,
        "flagged": 0,
        "errors": [],
        "dry_run": dry_run,
        "imos": imos,
    }
    if not token:
        result["ok"] = False
        result["error"] = "not_configured"
        return result
    if dry_run:
        return result

    auth_failures = 0
    for imo in imos:
        try:
            payload = fetch_events_for_imo(imo, use_cache=False)
            if payload.get("auth_failed") or str(payload.get("error") or "").startswith("auth_http_"):
                auth_failures += 1
                result["errors"].append({"imo": imo, "error": payload.get("error")})
                # Stop early — dead key, don't burn budget
                if auth_failures >= 3:
                    result["ok"] = False
                    result["error"] = str(payload.get("error") or "auth_failed")
                    result["auth_failed"] = True
                    break
                continue
            if not payload.get("ok"):
                result["errors"].append({"imo": imo, "error": payload.get("error")})
                continue
            result["fetched"] += 1
            events = payload.get("events") or []
            result["events_total"] += len(events)
            # Only flag gfw_verified after a real successful events call (verify=True)
            if payload.get("verify") is True:
                pers = persist_events(imo, events, db_path=db_path)
                result["flagged"] += int(pers.get("archive_rows_flagged") or 0)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("GFW batch imo=%s failed: %s", imo, exc)
            result["errors"].append({"imo": imo, "error": str(exc)[:120]})
    result["budget"] = get_budget_status()
    result["status"] = gfw_status()
    if auth_failures and result.get("flagged", 0) == 0 and result.get("events_total", 0) == 0:
        result["ok"] = False
        result.setdefault("error", "auth_failed")
        result["auth_failed"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=BATCH_DAILY_MAX)
    ap.add_argument("--imo", default=None, help="Single IMO probe")
    args = ap.parse_args(argv)
    if args.imo:
        out = fetch_events_for_imo(args.imo)
        print(json.dumps({k: v for k, v in out.items() if k != "events"}, indent=2))
        print(json.dumps({"events_n": len(out.get("events") or [])}))
        return 0 if out.get("ok") or out.get("error") == "not_configured" else 1
    out = run_daily_gfw_batch(limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if out.get("error") == "not_configured":
        return 2
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
