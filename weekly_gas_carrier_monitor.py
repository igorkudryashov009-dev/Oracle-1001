"""
Weekly gas-carrier (LNG/LPG) monitor — paid provider poll under a hard $50/mo budget.

Honest degradation: if the weekly call budget cannot cover the full priority list,
the list is truncated and the fact is logged in summary (coverage_of_top_n_target_pct).
Never fabricates vessel positions or provider payloads.

Default provider adapter: VesselFinder credit API
  GET https://api.vesselfinder.com/vessels?userkey=…&imo=…&format=json&errormode=409

Override via env if you already pay for another compatible HTTPS IMO lookup.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
FEATURES = ROOT / "features"
DEFAULT_FLEET_CSV = OUTPUT / "fleet_database.csv"
DEFAULT_OUT_DIR = FEATURES / "gas_carrier_weekly"
DEFAULT_TOP_N_TARGET = 500

GAS_TYPE_RE = re.compile(
    r"(LNG|LPG|СПГ|СУГ|VLGC|Gas\s*Carrier|газовоз|газовозн|"
    r"Membrane\s*Type|"
    r"перевозк\w*\s+сжиженн)",
    re.IGNORECASE,
)

# compliance_risk_level → priority (lower = first)
TIER_ORDER = {
    "TIER_0": 0,
    "TIER_1": 1,
    "TIER_2": 2,
    "TIER_3": 3,
}

RISK_ALIASES = {
    "critical": "TIER_0",
    "критическ": "TIER_0",
    "высокий": "TIER_0",
    "high": "TIER_0",
    "повышен": "TIER_1",
    "elevated": "TIER_1",
    "medium": "TIER_1",
    "средн": "TIER_1",
    "умерен": "TIER_2",
    "moderate": "TIER_2",
    "низк": "TIER_3",
    "low": "TIER_3",
    "minimal": "TIER_3",
}

WEEKS_PER_MONTH = 4.345  # average Gregorian weeks/month for budget split
logger = logging.getLogger("weekly_gas_carrier_monitor")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class MonitorConfig(BaseModel):
    """Validated at startup — refuse to run with missing/invalid secrets or cost."""

    provider_api_key: str = Field(..., min_length=1)
    provider_cost_per_call_usd: float = Field(..., gt=0)
    monthly_budget_usd: float = Field(default=50.0, gt=0)
    provider_name: str = Field(default="vesselfinder")
    provider_base_url: str = Field(
        default="https://api.vesselfinder.com/vessels"
    )
    request_timeout_sec: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=4, ge=0)
    backoff_base_sec: float = Field(default=1.0, gt=0)
    fleet_csv: Path = Field(default=DEFAULT_FLEET_CSV)
    out_dir: Path = Field(default=DEFAULT_OUT_DIR)
    top_n_target: int = Field(default=DEFAULT_TOP_N_TARGET, ge=0)
    user_agent: str = Field(default="Oracle-1001-gas-monitor/1.0")

    @field_validator("provider_api_key")
    @classmethod
    def _key_not_placeholder(cls, v: str) -> str:
        key = v.strip()
        if not key:
            raise ValueError("PROVIDER_API_KEY is empty")
        placeholders = {
            "YOUR_PROVIDER_API_KEY_HERE",
            "changeme",
            "xxx",
            "TODO",
        }
        if key == "DRY_RUN_NO_NETWORK_KEY":
            return key
        if key.upper() in {p.upper() for p in placeholders} or key.startswith("YOUR_"):
            raise ValueError(
                "PROVIDER_API_KEY looks like a placeholder — set a real key in .env"
            )
        return key

    @property
    def weekly_call_budget(self) -> int:
        """Max provider calls this week under monthly budget (honest floor)."""
        weekly_usd = self.monthly_budget_usd / WEEKS_PER_MONTH
        n = math.floor(weekly_usd / self.provider_cost_per_call_usd)
        return max(0, int(n))


class ConfigError(RuntimeError):
    """Raised when required env/config is missing or invalid."""


def _read_top_n_target() -> int:
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        return DEFAULT_TOP_N_TARGET
    with cfg_path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    gw = cfg.get("gas_weekly") or {}
    try:
        return int(gw.get("top_n_target", DEFAULT_TOP_N_TARGET))
    except (TypeError, ValueError):
        return DEFAULT_TOP_N_TARGET


def load_config(
    *,
    env_file: Optional[Path] = None,
    monthly_budget_usd: Optional[float] = None,
    cost_per_call_usd: Optional[float] = None,
    fleet_csv: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    require_api_key: bool = True,
) -> MonitorConfig:
    load_dotenv(env_file or (ROOT / ".env"), override=False)

    raw_key = os.getenv("PROVIDER_API_KEY", "").strip()
    raw_cost = os.getenv("PROVIDER_COST_PER_CALL_USD", "").strip()
    raw_budget = os.getenv("PROVIDER_MONTHLY_BUDGET_USD", "50").strip()
    raw_name = os.getenv("PROVIDER_NAME", "vesselfinder").strip() or "vesselfinder"
    raw_url = (
        os.getenv("PROVIDER_BASE_URL", "https://api.vesselfinder.com/vessels").strip()
        or "https://api.vesselfinder.com/vessels"
    )

    if not require_api_key and (
        not raw_key
        or raw_key.startswith("YOUR_")
        or raw_key.upper() in {"YOUR_PROVIDER_API_KEY_HERE", "CHANGEME", "XXX", "TODO"}
    ):
        # dry-run: never send a placeholder to the provider; use a non-secret sentinel
        raw_key = "DRY_RUN_NO_NETWORK_KEY"

    if not raw_key:
        raise ConfigError(
            "PROVIDER_API_KEY is not set. Copy .env.example → .env and set the key."
        )

    if cost_per_call_usd is not None:
        cost = float(cost_per_call_usd)
    elif raw_cost:
        try:
            cost = float(raw_cost)
        except ValueError as e:
            raise ConfigError(
                f"PROVIDER_COST_PER_CALL_USD must be a positive number, got {raw_cost!r}"
            ) from e
    elif not require_api_key:
        # dry-run without .env: explicit demo tariff so budget gate still works
        cost = 0.05
        logger.warning(
            "PROVIDER_COST_PER_CALL_USD unset — using dry-run default 0.05 USD/call"
        )
    else:
        raise ConfigError(
            "PROVIDER_COST_PER_CALL_USD is not set (USD cost of one provider call)."
        )

    try:
        budget = float(monthly_budget_usd if monthly_budget_usd is not None else raw_budget)
    except ValueError as e:
        raise ConfigError(
            f"monthly budget must be a positive number, got {raw_budget!r}"
        ) from e

    try:
        return MonitorConfig(
            provider_api_key=raw_key,
            provider_cost_per_call_usd=cost,
            monthly_budget_usd=budget,
            provider_name=raw_name,
            provider_base_url=raw_url,
            fleet_csv=fleet_csv or Path(os.getenv("FLEET_CSV", str(DEFAULT_FLEET_CSV))),
            out_dir=out_dir or Path(os.getenv("GAS_MONITOR_OUT_DIR", str(DEFAULT_OUT_DIR))),
            top_n_target=_read_top_n_target(),
        )
    except ValidationError as e:
        raise ConfigError(f"Invalid monitor configuration:\n{e}") from e


# ---------------------------------------------------------------------------
# Fleet load / prioritize
# ---------------------------------------------------------------------------


def map_risk_tier(compliance_risk_level: Any) -> str:
    if compliance_risk_level is None or (isinstance(compliance_risk_level, float) and math.isnan(compliance_risk_level)):
        return "TIER_3"
    text = str(compliance_risk_level).strip().lower()
    if text in {k.lower() for k in TIER_ORDER}:
        return text.upper()
    for needle, tier in RISK_ALIASES.items():
        if needle in text:
            return tier
    return "TIER_3"


def is_gas_carrier(vessel_type: Any) -> bool:
    if vessel_type is None or (isinstance(vessel_type, float) and math.isnan(vessel_type)):
        return False
    return bool(GAS_TYPE_RE.search(str(vessel_type)))


def load_gas_carriers(fleet_csv: Path) -> pd.DataFrame:
    if not fleet_csv.exists():
        raise FileNotFoundError(f"Fleet DB not found: {fleet_csv}")
    df = pd.read_csv(fleet_csv, low_memory=False)
    if "vessel_type" not in df.columns:
        raise ValueError(f"{fleet_csv} has no vessel_type column")
    gas = df[df["vessel_type"].map(is_gas_carrier)].copy()
    if "vessel_category" in gas.columns:
        gas = gas[gas["vessel_category"].fillna("vessel").astype(str).str.lower() == "vessel"]
    gas["risk_tier"] = gas.get(
        "compliance_risk_level", pd.Series([None] * len(gas))
    ).map(map_risk_tier)
    gas["tier_rank"] = gas["risk_tier"].map(lambda t: TIER_ORDER.get(t, 3))
    if "dwt_tons" not in gas.columns:
        gas["dwt_tons"] = float("nan")
    gas["dwt_sort"] = pd.to_numeric(gas["dwt_tons"], errors="coerce").fillna(-1.0)
    gas = gas.sort_values(
        by=["tier_rank", "dwt_sort"], ascending=[True, False], kind="mergesort"
    )
    return gas.reset_index(drop=True)


def apply_budget_gate(
    fleet: pd.DataFrame, weekly_call_budget: int
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Honest truncation: keep top-N by priority; never invent rows."""
    total = int(len(fleet))
    if weekly_call_budget <= 0:
        selected = fleet.iloc[0:0].copy()
        trimmed = True
    elif total > weekly_call_budget:
        selected = fleet.iloc[:weekly_call_budget].copy()
        trimmed = True
    else:
        selected = fleet.copy()
        trimmed = False

    coverage_pct = (
        100.0 if total == 0 else round(100.0 * len(selected) / total, 2)
    )
    meta = {
        "gas_carriers_total": total,
        "weekly_call_budget": weekly_call_budget,
        "selected_for_poll": int(len(selected)),
        "budget_trimmed": trimmed,
        "coverage_of_top_n_target_pct": coverage_pct,
        "omitted_due_to_budget": max(0, total - int(len(selected))),
    }
    if trimmed:
        logger.warning(
            "Budget gate: polling %s of %s gas carriers (coverage=%.2f%%). "
            "List truncated — not padded with fake data.",
            meta["selected_for_poll"],
            total,
            coverage_pct,
        )
    return selected, meta


# ---------------------------------------------------------------------------
# Provider adapter (VesselFinder-compatible)
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class AuthError(ProviderError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message, status=status, retryable=False)


class RateLimitError(ProviderError):
    def __init__(self, message: str, status: Optional[int] = 429):
        super().__init__(message, status=status, retryable=True)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def call_provider_api(
    imo: Any,
    cfg: MonitorConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], None] = _sleep,
) -> dict[str, Any]:
    """
    One paid lookup for a vessel IMO.

    VesselFinder: /vessels?userkey=&imo=&format=json&errormode=409
    Retries with exponential backoff on network / 429 / 5xx.
    Auth failures (401/403) are not retried.
    """
    imo_str = str(int(float(imo))) if imo is not None and str(imo).strip() else ""
    if not imo_str:
        raise ProviderError("IMO missing — refusing empty provider call", retryable=False)

    params = {
        "userkey": cfg.provider_api_key,
        "imo": imo_str,
        "format": "json",
        "errormode": "409",
    }
    url = f"{cfg.provider_base_url.rstrip('?')}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "application/json",
    }

    last_err: Optional[Exception] = None
    attempts = cfg.max_retries + 1
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener(req, timeout=cfg.request_timeout_sec) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read().decode("utf-8", errors="replace")
                if status == 429:
                    raise RateLimitError("Provider rate limit (429)", status=429)
                if status in (401, 403):
                    raise AuthError(f"Provider authentication failed (HTTP {status})", status=status)
                if status >= 500:
                    raise ProviderError(f"Provider server error HTTP {status}", status=status, retryable=True)
                if status != 200:
                    raise ProviderError(f"Provider HTTP {status}: {body[:300]}", status=status, retryable=False)

                try:
                    payload = json.loads(body) if body.strip() else {}
                except json.JSONDecodeError as e:
                    raise ProviderError(f"Non-JSON provider response: {body[:200]}", retryable=False) from e

                if isinstance(payload, dict) and payload.get("error"):
                    err_txt = str(payload["error"])
                    low = err_txt.lower()
                    if "key" in low or "auth" in low or "userkey" in low:
                        raise AuthError(err_txt)
                    if "credit" in low or "balance" in low or "limit" in low:
                        raise RateLimitError(err_txt)
                    raise ProviderError(err_txt, retryable=False)

                return {
                    "imo": imo_str,
                    "provider": cfg.provider_name,
                    "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "raw": payload,
                }
        except AuthError:
            raise
        except RateLimitError as e:
            last_err = e
        except ProviderError as e:
            last_err = e
            if not e.retryable:
                raise
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            if e.code in (401, 403):
                raise AuthError(f"HTTP {e.code}: {body[:300]}", status=e.code) from e
            if e.code == 429:
                last_err = RateLimitError(f"HTTP 429: {body[:300]}", status=429)
            elif e.code >= 500:
                last_err = ProviderError(f"HTTP {e.code}: {body[:300]}", status=e.code, retryable=True)
            else:
                raise ProviderError(f"HTTP {e.code}: {body[:300]}", status=e.code, retryable=False) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = ProviderError(f"Network error: {e}", retryable=True)

        if attempt + 1 >= attempts:
            break
        delay = cfg.backoff_base_sec * (2**attempt)
        logger.warning(
            "Retry %s/%s for IMO %s after %.1fs (%s)",
            attempt + 1,
            cfg.max_retries,
            imo_str,
            delay,
            last_err,
        )
        sleeper(delay)

    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------------------
# Run + Mission Control artifact contract
# ---------------------------------------------------------------------------


def _vessel_row_public(row: pd.Series) -> dict[str, Any]:
    def _v(key: str) -> Any:
        if key not in row.index:
            return None
        val = row[key]
        if pd.isna(val):
            return None
        if hasattr(val, "item"):
            try:
                return val.item()
            except Exception:
                return val
        return val

    return {
        "imo": _v("imo"),
        "vessel_name": _v("vessel_name"),
        "vessel_type": _v("vessel_type"),
        "flag": _v("flag"),
        "dwt_tons": _v("dwt_tons"),
        "risk_tier": _v("risk_tier"),
        "compliance_risk_level": _v("compliance_risk_level"),
        "mmsi": _v("mmsi"),
    }


def build_mission_control_module(summary: dict[str, Any], results: list[dict], errors: list[dict]) -> dict[str, Any]:
    """
    Same shape as other Mission Control modules in build_mission_control.py:
    id/name/status/status_label/tone/banner/live/metrics/tables/links/...
    """
    trimmed = bool(summary.get("budget_trimmed"))
    coverage = float(summary.get("coverage_of_top_n_target_pct") or 0)
    ok = int(summary.get("success_count") or 0)
    fail = int(summary.get("error_count") or 0)
    selected = int(summary.get("selected_for_poll") or 0)

    if summary.get("status") == "refused":
        return {
            "id": "gas_weekly",
            "name": "GAS CARRIER WEEKLY · PAID AIS",
            "status": "AWAITING",
            "status_label": "AWAITING CONFIG",
            "tone": "amber",
            "banner": summary.get("message") or "Monitor refused — check PROVIDER_* env",
            "live": False,
            "metrics": summary,
            "tables": {"errors": errors[:50]},
            "links": {},
            "data_state": "insufficient_data",
        }

    if selected == 0:
        status, label, tone, live = "STANDBY", "STANDBY", "amber", False
        banner = "Weekly call budget is 0 or no gas carriers matched — nothing polled."
    elif trimmed and coverage < 100:
        status, label, tone, live = "BUDGET_TRIMMED", "BUDGET TRIMMED", "amber", ok > 0
        banner = (
            f"Бюджет ${summary.get('monthly_budget_usd')}/мес: опрос "
            f"{selected}/{summary.get('gas_carriers_total')} газовозов "
            f"(coverage {coverage}%). Список обрезан честно, без фиктивных данных."
        )
    elif fail and ok == 0:
        status, label, tone, live = "STANDBY", "PROVIDER ERRORS", "amber", False
        banner = f"Все {fail} вызовов завершились ошибкой — см. errors.json"
    elif fail:
        status, label, tone, live = "PARTIAL", "PARTIAL", "amber", True
        banner = f"Успешно {ok}, ошибок {fail}. Coverage {coverage}%."
    else:
        status, label, tone, live = "LIVE", "LIVE", "live", True
        banner = f"Опрошено {ok} газовозов. Coverage {coverage}% целевого списка."

    sample_rows = []
    for item in results[:25]:
        sample_rows.append(
            {
                "imo": item.get("imo"),
                "vessel_name": item.get("vessel_name"),
                "risk_tier": item.get("risk_tier"),
                "dwt_tons": item.get("dwt_tons"),
                "status": item.get("status"),
                "provider": item.get("provider"),
            }
        )

    return {
        "id": "gas_weekly",
        "name": "GAS CARRIER WEEKLY · PAID AIS",
        "status": status,
        "status_label": label,
        "tone": tone,
        "banner": banner,
        "live": live,
        "metrics": {
            "gas_carriers_total": summary.get("gas_carriers_total"),
            "selected_for_poll": selected,
            "success_count": ok,
            "error_count": fail,
            "weekly_call_budget": summary.get("weekly_call_budget"),
            "monthly_budget_usd": summary.get("monthly_budget_usd"),
            "cost_per_call_usd": summary.get("cost_per_call_usd"),
            "estimated_week_spend_usd": summary.get("estimated_week_spend_usd"),
            "coverage_of_top_n_target_pct": coverage,
            "budget_trimmed": trimmed,
            "provider": summary.get("provider"),
        },
        "tables": {
            "poll_sample": sample_rows,
            "errors": [
                {"imo": e.get("imo"), "error": e.get("error"), "status": e.get("status")}
                for e in errors[:50]
            ],
        },
        "links": {
            "summary": "features/gas_carrier_weekly/summary.json",
            "results": "features/gas_carrier_weekly/results.json",
        },
        "data_state": "ready" if live else "insufficient_data",
        "coverage_of_top_n_target_pct": coverage,
        "budget_trimmed": trimmed,
    }


def run_monitor(
    cfg: MonitorConfig,
    *,
    limit: Optional[int] = None,
    dry_run: bool = False,
    call_api: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    fleet = load_gas_carriers(cfg.fleet_csv)
    matched_in_fleet = int(len(fleet))
    if limit is not None:
        fleet = fleet.iloc[: max(0, limit)].copy()
    if cfg.top_n_target > 0:
        fleet = fleet.iloc[: cfg.top_n_target].copy()

    selected, gate = apply_budget_gate(fleet, cfg.weekly_call_budget)
    gate["top_n_target"] = cfg.top_n_target
    gate["gas_carriers_matched_in_fleet"] = matched_in_fleet
    gate["gas_carriers_in_universe"] = int(len(fleet))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    api = call_api or call_provider_api

    for _, row in selected.iterrows():
        public = _vessel_row_public(row)
        imo = public.get("imo")
        try:
            if dry_run:
                payload = {
                    "imo": str(imo),
                    "provider": f"{cfg.provider_name}-dry-run",
                    "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "raw": {"dry_run": True, "note": "no network call"},
                }
            else:
                payload = api(imo, cfg)
            results.append({**public, "status": "ok", **payload})
        except AuthError as e:
            err = {
                **public,
                "status": "auth_error",
                "error": str(e),
                "http_status": e.status,
            }
            errors.append(err)
            logger.error("Auth error — aborting remaining calls: %s", e)
            break
        except ProviderError as e:
            errors.append(
                {
                    **public,
                    "status": "error",
                    "error": str(e),
                    "http_status": e.status,
                }
            )
        except Exception as e:  # noqa: BLE001 — per-vessel isolation
            errors.append({**public, "status": "error", "error": str(e)})

    success = len(results)
    estimated_spend = round(success * cfg.provider_cost_per_call_usd, 4)
    summary: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "ok",
        "provider": cfg.provider_name,
        "provider_base_url": cfg.provider_base_url,
        "fleet_csv": cfg.fleet_csv.name,
        "monthly_budget_usd": cfg.monthly_budget_usd,
        "cost_per_call_usd": cfg.provider_cost_per_call_usd,
        "weeks_per_month": WEEKS_PER_MONTH,
        "dry_run": dry_run,
        "success_count": success,
        "error_count": len(errors),
        "estimated_week_spend_usd": estimated_spend,
        **gate,
    }
    mc_module = build_mission_control_module(summary, results, errors)
    summary["mission_control_module"] = {
        "id": mc_module["id"],
        "status": mc_module["status"],
        "status_label": mc_module["status_label"],
        "live": mc_module["live"],
    }

    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    (cfg.out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (cfg.out_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (cfg.out_dir / "errors.json").write_text(
        json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Contract consumed by build_mission_control.collect_module5_gas_weekly
    (cfg.out_dir / "mission_control_module.json").write_text(
        json.dumps(mc_module, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "Wrote %s (success=%s errors=%s coverage=%.2f%% trimmed=%s)",
        cfg.out_dir,
        success,
        len(errors),
        gate["coverage_of_top_n_target_pct"],
        gate["budget_trimmed"],
    )
    return {"summary": summary, "results": results, "errors": errors, "mission_control_module": mc_module}


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Weekly LNG/LPG carrier paid-provider monitor")
    p.add_argument("--fleet-csv", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--limit", type=int, default=None, help="Cap gas carriers before budget gate (test)")
    p.add_argument("--monthly-budget-usd", type=float, default=None)
    p.add_argument(
        "--cost-per-call-usd",
        type=float,
        default=None,
        help="Override PROVIDER_COST_PER_CALL_USD",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="No network: write artifacts with dry_run payloads (still enforces budget gate)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(
            monthly_budget_usd=args.monthly_budget_usd,
            cost_per_call_usd=args.cost_per_call_usd,
            fleet_csv=args.fleet_csv,
            out_dir=args.out_dir,
            require_api_key=not args.dry_run,
        )
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    if cfg.weekly_call_budget <= 0 and not args.dry_run:
        print(
            "CONFIG ERROR: weekly call budget is 0 — increase monthly budget or lower cost/call",
            file=sys.stderr,
        )
        return 2

    try:
        run_monitor(cfg, limit=args.limit, dry_run=args.dry_run)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
