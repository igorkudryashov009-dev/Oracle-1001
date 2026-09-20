"""Macro FX + Nasdaq commodity adapter — Contract 1.8.0-ops-gis-sot."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_keys import CONTRACT_VERSION, get_key

LOG = logging.getLogger("sentinel.market_data_service")
ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "output" / "cache" / "market_summary.json"

EXCHANGERATE_TMPL = "https://v6.exchangerate-api.com/v6/{key}/latest/USD"
NASDAQ_DATASET_TMPL = "https://data.nasdaq.com/api/v3/datasets/{code}.json"

# Best-effort public CHRIS codes (may 404 on free tier — then cache/empty).
NASDAQ_CODES = {
    "ttf_proxy": "CHRIS/ICE_TFM1",
    "brent": "CHRIS/ICE_B1",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_json(url: str, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Oracle-1001-Sentinel/1.8.0", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _load_cache() -> dict[str, Any] | None:
    if not CACHE_PATH.is_file():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _save_cache(payload: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("market cache write failed: %s", exc)


def _fetch_fx() -> dict[str, Any]:
    key = get_key("EXCHANGERATE_KEY")
    if not key:
        return {"configured": False, "rates": {}}
    data = _http_json(EXCHANGERATE_TMPL.format(key=key))
    rates = data.get("conversion_rates") or data.get("rates") or {}
    wanted = ("EUR", "GBP", "RUB", "CNY", "JPY")
    slim = {k: rates[k] for k in wanted if k in rates}
    return {
        "configured": True,
        "base": data.get("base_code") or data.get("base") or "USD",
        "rates": slim,
        "provider": "exchangerate-api",
        "time_last_update_utc": data.get("time_last_update_utc"),
    }


def _fetch_nasdaq_dataset(code: str) -> dict[str, Any]:
    key = get_key("NASDAQ_DATA_KEY")
    if not key:
        return {"configured": False, "code": code}
    q = urllib.parse.urlencode({"api_key": key, "rows": "1"})
    data = _http_json(f"{NASDAQ_DATASET_TMPL.format(code=code)}?{q}")
    ds = data.get("dataset") or {}
    rows = ds.get("data") or []
    cols = ds.get("column_names") or []
    latest = None
    if rows and cols:
        latest = {cols[i]: rows[0][i] for i in range(min(len(cols), len(rows[0])))}
    return {
        "configured": True,
        "code": code,
        "name": ds.get("name"),
        "latest": latest,
        "provider": "nasdaq_data_link",
    }


def fetch_market_summary(*, use_cache: bool = True) -> dict[str, Any]:
    """FX + Nasdaq with stale-while-revalidate cache."""

    def _fresh() -> dict[str, Any]:
        errors: list[str] = []
        fx: dict[str, Any] = {"configured": False, "rates": {}}
        commodities: dict[str, Any] = {}
        try:
            fx = _fetch_fx()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"fx:{exc}")
            LOG.warning("FX fetch failed: %s", exc)
        for label, code in NASDAQ_CODES.items():
            try:
                commodities[label] = _fetch_nasdaq_dataset(code)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
                errors.append(f"nasdaq:{label}:{exc}")
                commodities[label] = {
                    "configured": bool(get_key("NASDAQ_DATA_KEY")),
                    "code": code,
                    "error": str(exc)[:160],
                }
        has_data = bool(fx.get("rates")) or any(
            isinstance(v, dict) and v.get("latest") for v in commodities.values()
        )
        if not has_data:
            raise RuntimeError(";".join(errors) if errors else "empty_market")
        return {
            "ok": True,
            "contract_version": CONTRACT_VERSION,
            "fetched_at": _now_iso(),
            "fx": fx,
            "commodities": commodities,
            "is_cached": False,
            "errors": errors,
        }

    if not use_cache:
        try:
            return _fresh()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": True,
                "contract_version": CONTRACT_VERSION,
                "fetched_at": _now_iso(),
                "fx": {"configured": False, "rates": {}},
                "commodities": {},
                "is_cached": False,
                "errors": [str(exc)[:240]],
            }

    from services.cache_swr import swr_fetch

    try:
        return swr_fetch(cache_path=CACHE_PATH, fresh_fetch=_fresh, cache_key="market_summary")
    except Exception as exc:  # noqa: BLE001
        cached = _load_cache()
        if cached:
            cached = dict(cached)
            cached["ok"] = True
            cached["is_cached"] = True
            cached["cache_fallback"] = True
            cached["errors"] = list(cached.get("errors") or []) + [str(exc)[:160]]
            cached["contract_version"] = CONTRACT_VERSION
            return cached
        return {
            "ok": True,
            "contract_version": CONTRACT_VERSION,
            "fetched_at": _now_iso(),
            "fx": {"configured": False, "rates": {}},
            "commodities": {},
            "is_cached": False,
            "errors": [str(exc)[:240]],
            "note": "Configure EXCHANGERATE_KEY / NASDAQ_DATA_KEY in .env",
        }


__all__ = ("fetch_market_summary", "CACHE_PATH", "NASDAQ_CODES")
