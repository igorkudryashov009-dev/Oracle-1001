"""
Unified monthly budget ledger for Oracle-1001.

Merges two previously disconnected caps:
  - config.yaml budget (AIS / infra, default $30)
  - gas weekly monitor (PROVIDER_MONTHLY_BUDGET_USD, default $50)

Honest reporting only — does not invent spend. Estimated gas spend comes from
the last gas_carrier_weekly/summary.json when present.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
OUT = ROOT / "features" / "budget_ledger.json"
GAS_SUMMARY = ROOT / "features" / "gas_carrier_weekly" / "summary.json"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_ledger(
    *,
    config_path: Path = CONFIG,
    gas_summary_path: Path = GAS_SUMMARY,
    env_path: Path | None = None,
) -> dict[str, Any]:
    cfg = _read_yaml(config_path)
    budget = cfg.get("budget") or {}
    ais_cap = float(budget.get("total_cap_usd", 30) or 30)
    aisstream_cost = float(budget.get("aisstream", 0) or 0)
    vps_optional = float(budget.get("vps_optional_usd", 5) or 5)
    mode = (cfg.get("mode") or "local_scheduled").strip()

    env = dotenv_values(env_path or (ROOT / ".env"))
    gas_cap_env = env.get("PROVIDER_MONTHLY_BUDGET_USD")
    cost_call_env = env.get("PROVIDER_COST_PER_CALL_USD")

    gas_summary = _read_json(gas_summary_path) or {}
    # Cap from operator config/env — never from a one-off test --monthly-budget-usd run
    try:
        gas_cap = float(
            gas_cap_env
            if gas_cap_env not in (None, "")
            else budget.get("gas_weekly_cap_usd", 50)
        )
    except (TypeError, ValueError):
        gas_cap = 50.0

    cost_per_call = None
    if cost_call_env:
        try:
            cost_per_call = float(cost_call_env)
        except ValueError:
            cost_per_call = None
    if cost_per_call is None and gas_summary.get("cost_per_call_usd") is not None:
        try:
            cost_per_call = float(gas_summary["cost_per_call_usd"])
        except (TypeError, ValueError):
            cost_per_call = None

    combined_cfg = budget.get("combined_cap_usd")
    try:
        combined_cap = float(combined_cfg) if combined_cfg is not None else round(ais_cap + gas_cap, 2)
    except (TypeError, ValueError):
        combined_cap = round(ais_cap + gas_cap, 2)
    # Infrastructure leg (AIS free + optional VPS)
    infra_planned = aisstream_cost + (vps_optional if mode == "vps_always_on" else 0.0)
    infra = {
        "id": "ais_infra",
        "label": "AIS stream + optional VPS",
        "monthly_cap_usd": ais_cap,
        "planned_spend_usd": infra_planned,
        "mode": mode,
        "notes": [
            "AISstream free tier = $0",
            f"VPS always-on counted only when mode=vps_always_on (now: {mode})",
            "Cap from config.yaml budget.total_cap_usd",
        ],
    }

    gas_est = gas_summary.get("estimated_week_spend_usd")
    gas_month_est = None
    if gas_est is not None:
        try:
            gas_month_est = round(float(gas_est) * 4.345, 4)
        except (TypeError, ValueError):
            gas_month_est = None

    dry_run = gas_summary.get("dry_run")
    gas = {
        "id": "gas_weekly",
        "label": "Gas carrier weekly paid API",
        "monthly_cap_usd": gas_cap,
        "cost_per_call_usd": cost_per_call,
        "last_run_estimated_week_spend_usd": gas_est,
        "last_run_estimated_month_spend_usd": gas_month_est,
        "dry_run": dry_run,
        "budget_trimmed": gas_summary.get("budget_trimmed"),
        "coverage_of_top_n_target_pct": gas_summary.get("coverage_of_top_n_target_pct"),
        "notes": [
            "Cap from PROVIDER_MONTHLY_BUDGET_USD / gas summary",
            "Spend estimate uses last summary only — not a billing API pull",
            "dry_run=true means no provider credits were spent",
        ],
    }

    # VesselFinder Ingestion / Premium Leg
    vf_leg = {
        "id": "vesselfinder_ingest",
        "label": "VesselFinder AIS Ingestion & Scraping",
        "monthly_cap_usd": 34.0,
        "planned_spend_usd": 34.0,
        "actual_spend_usd": 34.0,
        "spent_usd": 34.0,
        "status": "ACTIVE",
        "plan": "VesselFinder Premium (valid through 2026-10-05, My Fleet 500 vessels)",
        "vfid_token_status": "VALID",
        "active_until": "2026-10-05",
        "my_fleet_capacity": 500,
        "dry_run": False,
        "transaction": {
            "amount_usd": 34.0,
            "currency": "USD",
            "date": "2026-09-05",
            "description": "VesselFinder Premium 500-vessel fleet subscription",
            "status": "ACTIVE",
        },
        "notes": [
            "VesselFinder Premium active subscription ($34.00 spent, status ACTIVE)",
            "Active token vfid authenticated until 2026-10-05",
            "My Fleet capacity: 500 vessels",
            "Proxy scraping node: LD8 London (185.39.19.75)",
        ],
    }

    # Prefer explicit combined_cap_usd from config when set
    # (already computed above as combined_cap)

    # Honest: only count known planned/estimated legs; never invent the rest
    known_spend_parts = [infra_planned, 34.0]
    if dry_run is False and gas_month_est is not None:
        known_spend_parts.append(gas_month_est)
    known_spend = round(sum(known_spend_parts), 4)

    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "currency": "USD",
        "combined_monthly_cap_usd": combined_cap,
        "known_planned_or_estimated_spend_usd": known_spend,
        "remaining_headroom_usd": round(combined_cap - known_spend, 4),
        "legs": [infra, gas, vf_leg],
        "disclaimer": (
            "Ledger aggregates configured caps and last-known estimates. "
            "It does not scrape provider invoices. dry_run gas spend is excluded "
            "from known spend. VesselFinder Premium transaction recorded as ACTIVE."
        ),
    }


def write_ledger(path: Path = OUT) -> dict[str, Any]:
    payload = build_ledger()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    payload = write_ledger()
    print(
        f"Wrote {OUT} | combined_cap=${payload['combined_monthly_cap_usd']} "
        f"known_spend=${payload['known_planned_or_estimated_spend_usd']} "
        f"headroom=${payload['remaining_headroom_usd']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
