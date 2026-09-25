#!/usr/bin/env python3
"""Daily VF allocator live runner — ≤16 billed calls, skip when key unhealthy."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("sentinel.vf_allocator_runner")

MAX_SUCCESS = 16
MAX_ATTEMPTS = 24


def run_vf_allocator_live(*, dry_run: bool = False) -> dict[str, Any]:
    """Plan + fetch. Skips when paused / last_ok false / no key."""
    from services.key_activation import is_provider_paused, provider_state
    from services.storage import DEFAULT_DB
    from services.vf_budget_allocator import (
        commit_allocation,
        plan_daily_allocation,
    )
    from services.vesselfinder_budget import get_budget_status
    from services.vesselfinder_client import fetch_vessel, resolve_userkey

    if is_provider_paused("vesselfinder"):
        return {"ok": True, "skipped": True, "reason": "provider_paused"}
    key = resolve_userkey()
    if not key:
        return {"ok": True, "skipped": True, "reason": "no_key"}
    act = provider_state("vesselfinder")
    budget = get_budget_status()
    # Require activation probe success OR historical last_ok True on budget
    if act.get("last_ok") is not True and budget.get("last_ok") is not True:
        return {"ok": True, "skipped": True, "reason": "awaiting_key_activation"}

    plan = plan_daily_allocation(simulate=False)
    imos = list(plan.all_imos)[:MAX_SUCCESS]
    p1_n = sum(1 for i in imos if (plan.reasons or {}).get(i) == "P1_gap")
    result: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "planned": len(imos),
        "p1_gap_n": p1_n,
        "reasons": {i: (plan.reasons or {}).get(i) for i in imos},
        "success": [],
        "failed": [],
        "billed": 0,
        "dry_run": dry_run,
    }
    if dry_run or not imos:
        return result

    billed: list[str] = []
    attempts = 0
    # Ensure vf_position_cache exists for archive overlay
    db = Path(DEFAULT_DB)
    conn = sqlite3.connect(str(db), timeout=30.0)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vf_position_cache (
                imo TEXT PRIMARY KEY,
                lat REAL, lon REAL, sog REAL, cog REAL,
                nav_status TEXT, draught REAL, fetched_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    for imo in imos:
        if len(billed) >= MAX_SUCCESS or attempts >= MAX_ATTEMPTS:
            break
        attempts += 1
        try:
            # Cheapest: AIS only (no master) — 1 credit on success
            out = fetch_vessel(str(imo), extradata="")
            if not out.get("ok"):
                result["failed"].append({"imo": imo, "error": "not_ok"})
                continue
            norm = out.get("normalized") or {}
            lat, lon = norm.get("lat"), norm.get("lon")
            if lat is None or lon is None:
                result["failed"].append({"imo": imo, "error": "no_coords"})
                continue
            fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn = sqlite3.connect(str(db), timeout=30.0)
            try:
                conn.execute(
                    """
                    INSERT INTO vf_position_cache
                      (imo, lat, lon, sog, cog, nav_status, draught, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(imo) DO UPDATE SET
                      lat=excluded.lat, lon=excluded.lon, sog=excluded.sog,
                      cog=excluded.cog, nav_status=excluded.nav_status,
                      draught=excluded.draught, fetched_at=excluded.fetched_at
                    """,
                    (
                        str(imo),
                        lat,
                        lon,
                        norm.get("sog"),
                        norm.get("cog"),
                        norm.get("nav_status"),
                        norm.get("current_draft_m") or norm.get("draft_m") or norm.get("draught"),
                        fetched,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            billed.append(str(imo))
            result["success"].append(imo)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("VF fetch imo=%s failed: %s", imo, exc)
            result["failed"].append({"imo": imo, "error": str(exc)[:120]})

    if billed:
        commit_allocation(plan, billed_imos=billed)
    result["billed"] = len(billed)
    result["attempts"] = attempts
    result["budget"] = get_budget_status()
    return result


__all__ = ("run_vf_allocator_live",)
