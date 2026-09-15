#!/usr/bin/env python3
"""Q-Flex VesselFinder draft poller — budget-aware, cached, never on page-view.

Cadence (default): every 24h per Q-Flex vessel (AIS current draught + master when
DWT/max-draft missing from cache).

Budget math (MONTHLY_BUDGET=500, reserve≈200):
  10 vessels × 1 call/day × 30 days = 300 calls/mo → ~200 left for tests/adhoc.
  Draft changes slowly; sub-daily polling wastes Premium credits.

Does NOT write into ais_positions / fleet_sample_status (G3 Dual Gate untouched).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.top10_vessels import TOP10_VESSELS  # noqa: E402
from services.vesselfinder_budget import get_budget_status  # noqa: E402
from services.vesselfinder_client import fetch_vessel  # noqa: E402

LOG = logging.getLogger("sentinel.qflex_vf_poller")

CACHE_DB = ROOT / "data" / "archive" / "qflex_vesselfinder_cache.sqlite"
OUT_JSON = ROOT / "output" / "qflex_fleet_cargo.json"

# 24h — see module docstring budget math.
DEFAULT_INTERVAL_HOURS = float(os.getenv("QFLEX_VF_POLL_INTERVAL_HOURS", "24"))
STALE_HOURS = float(os.getenv("QFLEX_VF_STALE_HOURS", "36"))
BALLAST_FRAC = float(os.getenv("QFLEX_DRAFT_BALLAST_FRAC", "0.55"))

CACHE_DDL = """
CREATE TABLE IF NOT EXISTS qflex_vf_cache (
    imo TEXT PRIMARY KEY,
    name TEXT,
    fetched_at TEXT NOT NULL,
    ais_timestamp_utc TEXT,
    current_draft_m REAL,
    max_draft_m REAL,
    dwt REAL,
    nav_status TEXT,
    destination TEXT,
    sog REAL,
    lat REAL,
    lon REAL,
    raw_json TEXT,
    laden_fraction REAL,
    current_cargo_tonnes REAL,
    data_source TEXT,
    approximation_note TEXT
)
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    d = dt or _utc_now()
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_cache() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(CACHE_DDL)
    conn.commit()
    return conn


def catalog_by_imo() -> dict[str, dict[str, Any]]:
    return {str(v["imo"]): v for v in TOP10_VESSELS}


def estimate_laden(
    *,
    current_draft: Optional[float],
    max_draft: Optional[float],
    catalog_draft: Optional[float],
    dwt: Optional[float],
) -> dict[str, Any]:
    """Linear draft↔load approximation (honest, not BoL)."""
    note = (
        "Cargo load estimated from VesselFinder current draft telemetry "
        f"(linear approximation vs design/ballast draft, ballast={BALLAST_FRAC:.2f}×design) "
        "where available; notional full-capacity assumed otherwise. "
        "Not equivalent to verified bill-of-lading cargo weight."
    )
    if current_draft is None or dwt is None or dwt <= 0:
        return {
            "laden_fraction_estimate": None,
            "current_cargo_estimate_tonnes": None,
            "data_source": "notional_full_capacity_fallback",
            "design_draft_m": max_draft or catalog_draft,
            "ballast_draft_m": None,
            "approximation_note": note,
            "notional_cargo_tonnes": float(dwt) if dwt else None,
        }

    design = None
    if max_draft is not None and max_draft > 0:
        design = float(max_draft)
    elif catalog_draft is not None and catalog_draft > 0:
        design = max(float(catalog_draft), float(current_draft))
    else:
        design = max(float(current_draft), 12.0)  # Q-Max-ish floor

    ballast = float(design) * BALLAST_FRAC
    if design <= ballast:
        frac = 1.0 if current_draft >= design else 0.0
    else:
        frac = (float(current_draft) - ballast) / (design - ballast)
    frac = max(0.0, min(1.0, frac))
    cargo = float(dwt) * frac
    return {
        "laden_fraction_estimate": round(frac, 4),
        "current_cargo_estimate_tonnes": round(cargo, 1),
        "data_source": "vesselfinder_live_draft",
        "design_draft_m": round(design, 3),
        "ballast_draft_m": round(ballast, 3),
        "approximation_note": note,
        "notional_cargo_tonnes": round(float(dwt), 1),
    }


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        return None


def cache_age_hours(fetched_at: Optional[str]) -> Optional[float]:
    dt = _parse_ts(fetched_at)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (_utc_now() - dt.astimezone(timezone.utc)).total_seconds() / 3600.0


def read_cache_row(conn: sqlite3.Connection, imo: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM qflex_vf_cache WHERE imo=?", (imo,)).fetchone()
    if not row:
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM qflex_vf_cache LIMIT 0").description]
    return dict(zip(cols, row))


def upsert_cache(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO qflex_vf_cache (
          imo, name, fetched_at, ais_timestamp_utc, current_draft_m, max_draft_m, dwt,
          nav_status, destination, sog, lat, lon, raw_json,
          laden_fraction, current_cargo_tonnes, data_source, approximation_note
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(imo) DO UPDATE SET
          name=excluded.name,
          fetched_at=excluded.fetched_at,
          ais_timestamp_utc=excluded.ais_timestamp_utc,
          current_draft_m=excluded.current_draft_m,
          max_draft_m=COALESCE(excluded.max_draft_m, qflex_vf_cache.max_draft_m),
          dwt=COALESCE(excluded.dwt, qflex_vf_cache.dwt),
          nav_status=excluded.nav_status,
          destination=excluded.destination,
          sog=excluded.sog,
          lat=excluded.lat,
          lon=excluded.lon,
          raw_json=excluded.raw_json,
          laden_fraction=excluded.laden_fraction,
          current_cargo_tonnes=excluded.current_cargo_tonnes,
          data_source=excluded.data_source,
          approximation_note=excluded.approximation_note
        """,
        (
            payload["imo"],
            payload.get("name"),
            payload["fetched_at"],
            payload.get("ais_timestamp_utc"),
            payload.get("current_draft_m"),
            payload.get("max_draft_m"),
            payload.get("dwt"),
            str(payload.get("nav_status")) if payload.get("nav_status") is not None else None,
            payload.get("destination"),
            payload.get("sog"),
            payload.get("lat"),
            payload.get("lon"),
            payload.get("raw_json"),
            payload.get("laden_fraction"),
            payload.get("current_cargo_tonnes"),
            payload.get("data_source"),
            payload.get("approximation_note"),
        ),
    )
    conn.commit()


def needs_refresh(row: Optional[dict[str, Any]], *, interval_hours: float) -> bool:
    if not row:
        return True
    age = cache_age_hours(row.get("fetched_at"))
    if age is None:
        return True
    return age >= interval_hours


def poll_one(
    imo: str,
    *,
    force: bool = False,
    interval_hours: float = DEFAULT_INTERVAL_HOURS,
) -> dict[str, Any]:
    cat = catalog_by_imo().get(str(imo), {})
    conn = open_cache()
    try:
        cached = read_cache_row(conn, str(imo))
        if not force and not needs_refresh(cached, interval_hours=interval_hours):
            return build_vessel_view(cat, cached, fetched=False)

        # Master only when DWT / max draft unknown — saves credits on steady-state polls.
        need_master = True
        if cached and cached.get("dwt") and (cached.get("max_draft_m") or cat.get("draft_m")):
            need_master = False
        extradata = "master" if need_master else ""

        result = fetch_vessel(str(imo), extradata=extradata)
        norm = result["normalized"]
        dwt = norm.get("dwt") or (cached or {}).get("dwt") or cat.get("dwt_tons")
        max_draft = norm.get("max_draft_m") or (cached or {}).get("max_draft_m")
        est = estimate_laden(
            current_draft=norm.get("current_draft_m"),
            max_draft=max_draft,
            catalog_draft=float(cat["draft_m"]) if cat.get("draft_m") is not None else None,
            dwt=float(dwt) if dwt is not None else None,
        )
        payload = {
            "imo": str(imo),
            "name": norm.get("name") or cat.get("name"),
            "fetched_at": _utc_iso(),
            "ais_timestamp_utc": norm.get("timestamp_utc"),
            "current_draft_m": norm.get("current_draft_m"),
            "max_draft_m": max_draft,
            "dwt": float(dwt) if dwt is not None else None,
            "nav_status": norm.get("nav_status"),
            "destination": norm.get("destination"),
            "sog": norm.get("sog"),
            "lat": norm.get("lat"),
            "lon": norm.get("lon"),
            "raw_json": json.dumps(result.get("raw") or {}, ensure_ascii=False),
            "laden_fraction": est.get("laden_fraction_estimate"),
            "current_cargo_tonnes": est.get("current_cargo_estimate_tonnes"),
            "data_source": est.get("data_source"),
            "approximation_note": est.get("approximation_note"),
        }
        upsert_cache(conn, payload)
        cached2 = read_cache_row(conn, str(imo))
        view = build_vessel_view(cat, cached2, fetched=True)
        view["estimated_credits"] = result.get("estimated_credits")
        view["extradata"] = extradata or "ais_only"
        return view
    finally:
        conn.close()


def build_vessel_view(
    cat: dict[str, Any],
    cached: Optional[dict[str, Any]],
    *,
    fetched: bool,
) -> dict[str, Any]:
    imo = str(cat.get("imo") or (cached or {}).get("imo") or "")
    dwt = None
    if cached and cached.get("dwt") is not None:
        dwt = float(cached["dwt"])
    elif cat.get("dwt_tons") is not None:
        dwt = float(cat["dwt_tons"])

    age_h = cache_age_hours((cached or {}).get("fetched_at")) if cached else None
    stale = age_h is None or age_h > STALE_HOURS

    if cached and cached.get("current_draft_m") is not None and not stale:
        est = estimate_laden(
            current_draft=float(cached["current_draft_m"]),
            max_draft=float(cached["max_draft_m"]) if cached.get("max_draft_m") is not None else None,
            catalog_draft=float(cat["draft_m"]) if cat.get("draft_m") is not None else None,
            dwt=dwt,
        )
        source = "vesselfinder_live_draft"
        cargo = est.get("current_cargo_estimate_tonnes")
        laden = est.get("laden_fraction_estimate")
    else:
        est = estimate_laden(
            current_draft=None,
            max_draft=None,
            catalog_draft=float(cat["draft_m"]) if cat.get("draft_m") is not None else None,
            dwt=dwt,
        )
        source = "notional_full_capacity_fallback"
        cargo = float(dwt) if dwt is not None else None
        laden = 1.0 if dwt is not None else None

    return {
        "rank": cat.get("rank"),
        "imo": imo,
        "name": (cached or {}).get("name") or cat.get("name"),
        "fetched": fetched,
        "as_of": (cached or {}).get("fetched_at") or (cached or {}).get("ais_timestamp_utc"),
        "ais_timestamp_utc": (cached or {}).get("ais_timestamp_utc"),
        "cache_age_hours": round(age_h, 2) if age_h is not None else None,
        "stale": bool(stale if cached else True),
        "current_draft_m": (cached or {}).get("current_draft_m"),
        "max_draft_m": (cached or {}).get("max_draft_m"),
        "dwt": dwt,
        "nav_status": (cached or {}).get("nav_status"),
        "destination": (cached or {}).get("destination"),
        "laden_fraction_estimate": laden if source == "vesselfinder_live_draft" else laden,
        "current_cargo_estimate_tonnes": cargo,
        "data_source": source,
        "design_draft_m": est.get("design_draft_m"),
        "ballast_draft_m": est.get("ballast_draft_m"),
        "approximation_note": est.get("approximation_note"),
    }


def build_fleet_cargo_document() -> dict[str, Any]:
    cat = catalog_by_imo()
    conn = open_cache()
    vessels = []
    try:
        for v in TOP10_VESSELS:
            imo = str(v["imo"])
            cached = read_cache_row(conn, imo)
            vessels.append(build_vessel_view(v, cached, fetched=False))
    finally:
        conn.close()

    live_n = sum(1 for x in vessels if x.get("data_source") == "vesselfinder_live_draft")
    fallback_n = len(vessels) - live_n
    cargo_live = sum(
        float(x["current_cargo_estimate_tonnes"])
        for x in vessels
        if x.get("data_source") == "vesselfinder_live_draft"
        and x.get("current_cargo_estimate_tonnes") is not None
    )
    cargo_notional = sum(
        float(x["current_cargo_estimate_tonnes"] or x.get("dwt") or 0)
        for x in vessels
        if x.get("data_source") == "notional_full_capacity_fallback"
    )
    total = cargo_live + cargo_notional

    ttf_spot = _read_ttf_spot()
    # Honest: DWT-tonnes × TTF €/MWh is NOT a thermodynamically correct LNG valuation.
    fleet_value_note = (
        "fleet_value_eur_proxy uses sum(cargo_tonnes)×TTF_spot as a coarse notional "
        "proxy only — not a market mark-to-model of LNG cargo."
    )
    value_proxy = None
    if ttf_spot is not None and total > 0:
        value_proxy = round(total * float(ttf_spot), 2)

    return {
        "generated_at": _utc_iso(),
        "poll_interval_hours": DEFAULT_INTERVAL_HOURS,
        "stale_after_hours": STALE_HOURS,
        "ballast_frac": BALLAST_FRAC,
        "caveat": (
            "Cargo load estimated from VesselFinder current draft telemetry "
            "(linear approximation vs design/ballast draft) where available; "
            "notional full-capacity assumed otherwise. Not equivalent to verified "
            "bill-of-lading cargo weight."
        ),
        "vesselfinder_budget": get_budget_status(),
        "fleet": {
            "vessel_count": len(vessels),
            "live_draft_count": live_n,
            "notional_fallback_count": fallback_n,
            "current_cargo_estimate_tonnes_total": round(total, 1),
            "current_cargo_from_live_draft_tonnes": round(cargo_live, 1),
            "current_cargo_from_notional_tonnes": round(cargo_notional, 1),
            "ttf_spot": ttf_spot,
            "fleet_value_eur_proxy": value_proxy,
            "fleet_value_note": fleet_value_note,
        },
        "vessels": vessels,
    }


def _read_ttf_spot() -> Optional[float]:
    for path in (
        ROOT / "output" / "api" / "v1" / "health.json",
        ROOT / "output" / "ttf_hedging_portfolio.json",
    ):
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict):
            if doc.get("ttf_spot") is not None:
                try:
                    return float(doc["ttf_spot"])
                except (TypeError, ValueError):
                    pass
            spot = doc.get("spot") or doc.get("ttf")
            if isinstance(spot, dict) and spot.get("last") is not None:
                try:
                    return float(spot["last"])
                except (TypeError, ValueError):
                    pass
    return None


def write_fleet_cargo_json(doc: Optional[dict[str, Any]] = None) -> Path:
    payload = doc or build_fleet_cargo_document()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return OUT_JSON


def run_poll(
    *,
    force: bool = False,
    only_imo: Optional[str] = None,
    interval_hours: float = DEFAULT_INTERVAL_HOURS,
) -> dict[str, Any]:
    targets = [str(v["imo"]) for v in TOP10_VESSELS]
    if only_imo:
        targets = [str(only_imo)]
    results = []
    errors = []
    for imo in targets:
        try:
            results.append(
                poll_one(imo, force=force, interval_hours=interval_hours)
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("poll failed imo=%s", imo)
            errors.append({"imo": imo, "error": str(exc)})
    doc = build_fleet_cargo_document()
    write_fleet_cargo_json(doc)
    return {
        "ok": len(errors) == 0,
        "polled": results,
        "errors": errors,
        "budget": get_budget_status(),
        "output": str(OUT_JSON),
        "fleet": doc.get("fleet"),
    }


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Q-Flex VesselFinder draft poller (budgeted)")
    p.add_argument("--force", action="store_true", help="Ignore cache interval")
    p.add_argument("--imo", default=None, help="Single IMO (pilot)")
    p.add_argument("--interval-hours", type=float, default=DEFAULT_INTERVAL_HOURS)
    p.add_argument("--status", action="store_true", help="Write cargo JSON from cache only")
    args = p.parse_args(argv)
    if args.status:
        path = write_fleet_cargo_json()
        print(json.dumps({"ok": True, "output": str(path), "budget": get_budget_status()}, indent=2))
        return 0
    out = run_poll(force=args.force, only_imo=args.imo, interval_hours=args.interval_hours)
    # Redact raw if any
    print(json.dumps({k: v for k, v in out.items() if k != "polled"} | {
        "polled": [
            {kk: vv for kk, vv in r.items() if kk != "raw_json"}
            for r in out.get("polled") or []
        ]
    }, indent=2, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
