#!/usr/bin/env python3
"""VesselFinder credit REST client — /vessels (not ListManager fleet subscription).

Historical ``Invalid Userkey!`` on ``api.vesselfinder.com/listmanager`` often means
the key is a *credit* AIS API key (Vessels / MasterData), not a Fleet Positions
subscription key. This client targets:

  GET https://api.vesselfinder.com/vessels?userkey=…&imo=…&extradata=master

Every call is gated by ``services.vesselfinder_budget``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

from services.key_manager import mask_key, resolve_commercial_key
from services.vesselfinder_budget import (
    BudgetExhausted,
    record_spend,
    reserve_or_raise,
)

LOG = logging.getLogger("sentinel.vesselfinder_client")

VESSELS_URL = "https://api.vesselfinder.com/vessels"
DEFAULT_TIMEOUT = float(os.getenv("VESSELFINDER_HTTP_TIMEOUT_SEC", "30"))


def resolve_userkey() -> str:
    resolved = resolve_commercial_key(validate=False, auto_persist=False)
    if resolved and resolved.key:
        return resolved.key
    return (os.getenv("VESSELFINDER_API_KEY") or os.getenv("VESSEL_FINDER_USERKEY") or "").strip()


def estimate_credits(*, extradata: str = "", sat: bool = False) -> float:
    """Rough VF credit cost (docs): AIS 1 (or 10 sat) + voyage 1 + master 2."""
    parts = {p.strip().lower() for p in (extradata or "").split(",") if p.strip()}
    ais = 10.0 if sat else 1.0
    voyage = 1.0 if "voyage" in parts else 0.0
    master = 2.0 if "master" in parts else 0.0
    return ais + voyage + master


def fetch_vessel(
    imo: str,
    *,
    extradata: str = "master",
    userkey: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    sat: bool = False,
) -> dict[str, Any]:
    """One budgeted request for a single IMO. Returns normalized payload + raw."""
    imo_s = str(imo).strip()
    if not imo_s.isdigit():
        raise ValueError(f"invalid IMO: {imo!r}")

    key = (userkey or resolve_userkey()).strip()
    if not key:
        raise RuntimeError("VesselFinder userkey missing — set VESSELFINDER_API_KEY in .env")

    endpoint = "/vessels"
    reserve_or_raise(1, endpoint=endpoint, imo=imo_s)
    est = estimate_credits(extradata=extradata, sat=sat)
    params: dict[str, Any] = {
        "userkey": key,
        "imo": imo_s,
        "format": "json",
    }
    if extradata:
        params["extradata"] = extradata
    if sat:
        params["sat"] = 1

    ok = False
    detail: Optional[str] = None
    data: Any = None
    status_code: Optional[int] = None
    try:
        resp = requests.get(
            VESSELS_URL,
            params=params,
            timeout=timeout,
            headers={"User-Agent": "Oracle-1001-Sentinel-VFClient/1.0"},
        )
        status_code = resp.status_code
        text_head = (resp.text or "")[:400]
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": text_head}

        if isinstance(data, dict) and data.get("error"):
            detail = str(data.get("error"))
            ok = False
        elif status_code >= 400:
            detail = f"http_{status_code}:{text_head[:120]}"
            ok = False
        else:
            ok = True
            detail = None
    except requests.RequestException as exc:
        detail = f"network:{exc}"
        ok = False
        data = None
    finally:
        # Count against monthly envelope even on Invalid Userkey — VF saw the call.
        record_spend(
            1,
            endpoint=endpoint,
            imo=imo_s,
            ok=ok,
            detail=detail,
            estimated_credits=est if ok else 0.0,
        )

    if not ok:
        err = detail or "unknown_error"
        LOG.error("VesselFinder /vessels imo=%s key=%s failed: %s", imo_s, mask_key(key), err)
        if "Invalid Userkey" in str(err) or "invalid" in str(err).lower():
            raise RuntimeError(
                f"VesselFinder Invalid Userkey on /vessels (key={mask_key(key)}). "
                "This is the credit AIS endpoint — if ListManager also fails, the key "
                "is wrong/expired; if only ListManager failed historically, the key may "
                "be credit-plan-only (expected)."
            ) from None
        if isinstance(detail, str) and detail.startswith("network:"):
            raise ConnectionError(detail)
        raise RuntimeError(f"VesselFinder /vessels failed: {err}")

    row = _pick_vessel_row(data, imo_s)
    normalized = normalize_vessel_row(row)
    return {
        "ok": True,
        "imo": imo_s,
        "userkey_masked": mask_key(key),
        "endpoint": endpoint,
        "extradata": extradata,
        "estimated_credits": est,
        "status_code": status_code,
        "normalized": normalized,
        "raw": row,
    }


def _pick_vessel_row(data: Any, imo: str) -> dict[str, Any]:
    if isinstance(data, list) and data:
        for item in data:
            if not isinstance(item, dict):
                continue
            ais = item.get("AIS") if isinstance(item.get("AIS"), dict) else {}
            master = item.get("MASTERDATA") if isinstance(item.get("MASTERDATA"), dict) else {}
            if str(ais.get("IMO") or master.get("IMO") or "") == imo or len(data) == 1:
                return item
        return data[0] if isinstance(data[0], dict) else {"AIS": {}}
    if isinstance(data, dict):
        if "AIS" in data or "MASTERDATA" in data:
            return data
        # error already handled
        return data
    return {}


def normalize_vessel_row(row: dict[str, Any]) -> dict[str, Any]:
    ais = row.get("AIS") if isinstance(row.get("AIS"), dict) else {}
    master = row.get("MASTERDATA") if isinstance(row.get("MASTERDATA"), dict) else {}
    voyage = row.get("VOYAGE") if isinstance(row.get("VOYAGE"), dict) else {}

    draft = _f(ais.get("DRAUGHT"))
    if draft is not None and draft > 40:
        # AIS sometimes reports dm
        draft = draft / 10.0

    max_draft = _f(master.get("MAXDRAUGHT"))
    dwt = _f(master.get("DWT"))

    return {
        "imo": str(ais.get("IMO") or master.get("IMO") or ""),
        "mmsi": str(ais.get("MMSI") or "") or None,
        "name": ais.get("NAME") or master.get("NAME"),
        "timestamp_utc": _norm_ts(ais.get("TIMESTAMP")),
        "lat": _f(ais.get("LATITUDE")),
        "lon": _f(ais.get("LONGITUDE")),
        "sog": _f(ais.get("SPEED")),
        "cog": _f(ais.get("COURSE")),
        "heading": _f(ais.get("HEADING")),
        "nav_status": ais.get("NAVSTAT"),
        "current_draft_m": draft,
        "destination": ais.get("DESTINATION"),
        "eta": ais.get("ETA"),
        "src": ais.get("SRC"),
        "dwt": dwt,
        "max_draft_m": max_draft if max_draft and max_draft > 0 else None,
        "loa_m": _f(master.get("LENGTH")),
        "beam_m": _f(master.get("BEAM")),
        "flag": master.get("FLAG"),
        "vessel_type": master.get("TYPE"),
        "gas_m3": _f(master.get("GAS")),
        "voyage_last_port": voyage.get("LASTPORT"),
        "voyage_departure": voyage.get("DEPARTURE"),
    }


def _f(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_ts(v: Any) -> Optional[str]:
    if not v:
        return None
    s = str(v).strip().replace(" UTC", "").strip()
    if not s:
        return None
    # VF: "2017-08-11 11:15:15" → ISO-ish
    if "T" not in s and " " in s:
        s = s.replace(" ", "T") + "Z"
    elif not s.endswith("Z") and "+" not in s:
        s = s + "Z"
    return s


__all__ = [
    "BudgetExhausted",
    "VESSELS_URL",
    "estimate_credits",
    "fetch_vessel",
    "normalize_vessel_row",
    "resolve_userkey",
]
