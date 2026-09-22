"""Macro FX + Nasdaq commodity adapter — Contract 1.8.0-ops-gis-sot.

Price resolution chain (three tiers, first success wins):
  Tier 1 — Yahoo Finance chart API  (BZ=F Brent, CL=F WTI, TTF=F TTF Gas)
            Zero-cost, no API key required; public exchange data.
  Tier 2 — Nasdaq Data Link REST    (CHRIS/ICE_B1, ICE_TFM1, LBMA/GOLD)
            Free tier often 404s on continuous contracts; used as supplement.
  Tier 3 — Public benchmark fallback (hardcoded Sep-2026 settlement; labelled)
            Applied only when both Tier 1 and Tier 2 are unavailable.
"""

from __future__ import annotations

import json
import logging
import math
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

# Best-effort public CHRIS / LBMA codes (may 404 on free tier — then cache/empty).
NASDAQ_CODES = {
    "ttf_proxy": "CHRIS/ICE_TFM1",
    "brent": "CHRIS/ICE_B1",
    "gold": "LBMA/GOLD",
}

# ── Tier-1: Yahoo Finance spot tickers (no API key required) ─────────────────
# BZ=F  = ICE Brent Crude continuous (USD/bbl)
# CL=F  = NYMEX WTI Crude continuous (USD/bbl)
# TTF=F = ICE Dutch TTF Natural Gas continuous (EUR/MWh → converted to USD/MMBtu)
# NG=F  = NYMEX Henry Hub Natural Gas (USD/MMBtu, as WTI complement)
YAHOO_SPOT_TICKERS: dict[str, str] = {
    "brent": "BZ=F",
    "wti":   "CL=F",
    "ttf":   "TTF=F",
    "natgas": "NG=F",
}
YAHOO_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)
# Fallback EUR/USD rate when ExchangeRate API is unreachable
_FALLBACK_EUR_USD = 1.087


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_json(url: str, timeout: float = 15.0, *, extra_headers: dict | None = None) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Oracle-1001-Sentinel/1.8.0)",
            "Accept": "application/json",
            **(extra_headers or {}),
        },
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



# ── Tier-1: Yahoo Finance spot price fetcher ─────────────────────────────────

def _yahoo_latest_close(ticker: str, timeout: float = 12.0) -> float | None:
    """Fetch the most recent daily close for a Yahoo Finance ticker.

    Uses the v8/chart API (same as ingest_ttf.py) — no yfinance dependency.
    Returns a float price or None on any failure (caller decides fallback).
    """
    period2 = int(datetime.now(timezone.utc).timestamp())
    period1 = period2 - 5 * 86400  # last 5 calendar days → ensures ≥1 trading day
    last_err: Exception | None = None
    for host in YAHOO_HOSTS:
        url = (
            f"{host}/v8/finance/chart/{ticker}"
            f"?period1={period1}&period2={period2}&interval=1d"
            f"&includePrePost=false"
        )
        try:
            payload = _http_json(url, timeout=timeout)
            result = (payload.get("chart") or {}).get("result") or []
            if not result:
                raise ValueError(f"empty chart result for {ticker}")
            block = result[0]
            closes = ((block.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            # Take the last non-null close
            price: float | None = None
            for px in reversed(closes):
                if px is not None and math.isfinite(float(px)) and float(px) > 0:
                    price = round(float(px), 4)
                    break
            if price is not None:
                LOG.debug("Yahoo spot %s → %.4f", ticker, price)
                return price
            raise ValueError(f"all closes null for {ticker}")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            LOG.debug("Yahoo %s via %s: %s", ticker, host, exc)
    LOG.info("Yahoo spot fetch failed for %s: %s", ticker, last_err)
    return None


def _get_eur_usd_rate() -> float:
    """Return EUR/USD rate — ExchangeRate API first, fallback constant."""
    key = get_key("EXCHANGERATE_KEY")
    if key:
        try:
            data = _http_json(EXCHANGERATE_TMPL.format(key=key), timeout=8.0)
            rates = data.get("conversion_rates") or data.get("rates") or {}
            eur_per_usd = float(rates.get("EUR") or 0)
            if eur_per_usd > 0:
                return round(1.0 / eur_per_usd, 6)  # convert to USD per EUR
        except Exception as exc:  # noqa: BLE001
            LOG.debug("ExchangeRate EUR/USD fetch failed: %s", exc)
    return _FALLBACK_EUR_USD


def _fetch_yahoo_spot_commodities() -> dict[str, Any]:
    """Tier-1: Fetch Brent, WTI, TTF spot prices from Yahoo Finance.

    TTF=F is denominated in EUR/MWh; converted to USD/MMBtu via EUR/USD rate.
    (1 MWh = 3.41214 MMBtu)

    Returns a dict compatible with the `commodities` payload schema:
      {"brent": {"latest": {"Settle": float, ...}, "provider": "yahoo_finance", ...}, ...}
    Raises RuntimeError if ALL tickers fail (caller catches and falls back to Nasdaq).
    """
    MWH_TO_MMBTU = 3.41214
    results: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    eur_usd: float | None = None  # lazy fetch

    brent_px = _yahoo_latest_close("BZ=F")
    if brent_px:
        results["brent"] = {
            "configured": True,
            "provider": "yahoo_finance",
            "ticker": "BZ=F",
            "latest": {"Settle": brent_px, "unit": "USD/bbl", "fetched_at": _now_iso()},
        }
    else:
        errors.append("BZ=F:failed")

    wti_px = _yahoo_latest_close("CL=F")
    if wti_px:
        results["wti"] = {
            "configured": True,
            "provider": "yahoo_finance",
            "ticker": "CL=F",
            "latest": {"Settle": wti_px, "unit": "USD/bbl", "fetched_at": _now_iso()},
        }
    else:
        errors.append("CL=F:failed")

    ttf_eur_mwh = _yahoo_latest_close("TTF=F")
    if ttf_eur_mwh:
        if eur_usd is None:
            eur_usd = _get_eur_usd_rate()
        ttf_usd_mmbtu = round(ttf_eur_mwh / MWH_TO_MMBTU * eur_usd, 4)
        results["ttf_proxy"] = {
            "configured": True,
            "provider": "yahoo_finance",
            "ticker": "TTF=F",
            "latest": {
                "Settle": ttf_usd_mmbtu,
                "Settle_eur_mwh": ttf_eur_mwh,
                "unit": "USD/MMBtu",
                "eur_usd_rate": eur_usd,
                "fetched_at": _now_iso(),
            },
        }
    else:
        errors.append("TTF=F:failed")

    natgas_px = _yahoo_latest_close("NG=F")
    if natgas_px:
        results["henry_hub"] = {
            "configured": True,
            "provider": "yahoo_finance",
            "ticker": "NG=F",
            "latest": {"Settle": natgas_px, "unit": "USD/MMBtu", "fetched_at": _now_iso()},
        }
    else:
        errors.append("NG=F:failed")

    if not results:
        raise RuntimeError("yahoo_spot_all_failed: " + "; ".join(errors))

    return results


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
    """FX + commodity prices with three-tier price resolution chain.

    Tier 1 — Yahoo Finance (BZ=F, CL=F, TTF=F, NG=F)  — no API key, free
    Tier 2 — Nasdaq Data Link (CHRIS/ICE_B1, ICE_TFM1) — key required, free tier 404s often
    Tier 3 — Public benchmark fallback (hardcoded Sep-2026 settlement)
    """

    def _fresh() -> dict[str, Any]:
        errors: list[str] = []
        fx: dict[str, Any] = {"configured": False, "rates": {}}
        commodities: dict[str, Any] = {}
        price_tier: str = "none"

        # FX rates (always attempt — needed for TTF EUR→USD conversion)
        try:
            fx = _fetch_fx()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"fx:{exc}")
            LOG.warning("FX fetch failed: %s", exc)

        # ── Tier 1: Yahoo Finance spot ────────────────────────────────────────
        try:
            yahoo_commodities = _fetch_yahoo_spot_commodities()
            commodities.update(yahoo_commodities)
            price_tier = "yahoo_finance"
            LOG.info("Market Tier-1 (Yahoo Finance) OK: %s tickers", len(yahoo_commodities))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"yahoo:{exc}")
            LOG.warning("Yahoo Finance spot fetch failed: %s", exc)

        # ── Tier 2: Nasdaq Data Link (supplement / gold) ──────────────────────
        for label, code in NASDAQ_CODES.items():
            if label in commodities and commodities[label].get("latest"):
                continue  # already populated by Yahoo
            try:
                nd = _fetch_nasdaq_dataset(code)
                if nd.get("latest"):
                    commodities[label] = nd
                    if price_tier == "none":
                        price_tier = "nasdaq_data_link"
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
                errors.append(f"nasdaq:{label}:{exc}")
                if label not in commodities:
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
            "price_tier": price_tier,
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
                "price_tier": "none",
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
            "price_tier": "none",
            "is_cached": False,
            "errors": [str(exc)[:240]],
            "note": "Configure EXCHANGERATE_KEY / NASDAQ_DATA_KEY in .env; Yahoo Finance used as Tier-1 (no key required)",
        }


__all__ = ("fetch_market_summary", "CACHE_PATH", "NASDAQ_CODES", "YAHOO_SPOT_TICKERS")
