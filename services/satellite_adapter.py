"""Plug-and-play satellite AIS. Parked until SAT_PROVIDER and SATELLITE_API_KEY exist.

No vendor URL is hardcoded. A pull calls the injected transport only when the
operator has selected spire, unseenlabs, or iceye and installed a key.
Without that pair the module returns immediately and writes nothing.
"""

from __future__ import annotations

import math
import os
from typing import Any, Callable

VENDORS = ("spire", "unseenlabs", "iceye")
ARCHIVE_SOURCE = "satellite_ais"
DEFAULT_DAILY_CAP = 50

Transport = Callable[[str, dict[str, str]], Any]


def _provider() -> str:
    return (os.getenv("SAT_PROVIDER") or "").strip().lower()


def _key() -> str:
    return (os.getenv("SATELLITE_API_KEY") or "").strip()


def mask_key(value: str) -> str:
    text = value or ""
    if len(text) <= 4:
        return "****"
    return "****" + text[-4:]


def armed() -> bool:
    return _provider() in VENDORS and bool(_key())


def daily_cap() -> int:
    raw = (os.getenv("SAT_DAILY_CAP") or "").strip()
    try:
        cap = int(raw) if raw else DEFAULT_DAILY_CAP
    except ValueError:
        cap = DEFAULT_DAILY_CAP
    return max(0, cap)


def public_status(*, verified_24h: int = 0) -> dict[str, Any]:
    """Health view. The raw key never leaves this module."""
    provider = _provider()
    key_on = bool(_key())
    if not armed():
        reason = "provider_unset" if key_on and provider not in VENDORS else "not_activated"
        return {
            "status": "parked",
            "provider": provider or None,
            "verified_24h": 0,
            "error_streak": 0,
            "key_mask": mask_key(_key()) if key_on else None,
            "note": reason,
            "side_effects": False,
        }
    return {
        "status": "armed",
        "provider": provider,
        "verified_24h": max(0, int(verified_24h)),
        "daily_cap": daily_cap(),
        "key_mask": mask_key(_key()),
        "note": "key present — pull only via explicit transport",
        "side_effects": False,
    }


def parse_vendor_payload(provider: str, payload: Any) -> list[dict[str, Any]]:
    """Normalize a vendor document that the caller already fetched. No network."""
    name = (provider or "").strip().lower()
    if name not in VENDORS or not isinstance(payload, dict):
        return []
    rows: list[dict[str, Any]] = []
    if name == "spire":
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            lat, lon = item.get("latitude"), item.get("longitude")
            imo = item.get("imo")
            if lat is None or lon is None or imo is None:
                continue
            rows.append(
                _with_provenance(
                    name,
                    {"imo": int(imo), "lat": float(lat), "lon": float(lon), "mmsi": item.get("mmsi")},
                    item,
                )
            )
    elif name == "unseenlabs":
        for item in payload.get("vessels") or []:
            if not isinstance(item, dict):
                continue
            if item.get("lat") is None or item.get("lon") is None or item.get("imo") is None:
                continue
            rows.append(
                _with_provenance(
                    name,
                    {
                        "imo": int(item["imo"]),
                        "lat": float(item["lat"]),
                        "lon": float(item["lon"]),
                        "mmsi": item.get("mmsi"),
                    },
                    item,
                )
            )
    else:
        for feat in payload.get("features") or []:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") or {}
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            if props.get("imo") is None or len(coords) < 2:
                continue
            rows.append(
                _with_provenance(
                    name,
                    {
                        "imo": int(props["imo"]),
                        "lon": float(coords[0]),
                        "lat": float(coords[1]),
                        "mmsi": props.get("mmsi"),
                    },
                    props,
                )
            )
    return rows


def _with_provenance(provider: str, row: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    observed = str(item.get("timestamp") or item.get("observed_at") or item.get("dt") or "").strip()
    if observed:
        row["provenance"] = {"provider": provider, "observed_at": observed}
    return row


def _provenance_ok(row: dict[str, Any]) -> bool:
    prov = row.get("provenance")
    if not isinstance(prov, dict):
        return False
    provider = str(prov.get("provider") or "").strip().lower()
    observed = str(prov.get("observed_at") or "").strip()
    return provider in VENDORS and bool(observed)


def _finite_point(row: dict[str, Any]) -> bool:
    try:
        lat = float(row["lat"])
        lon = float(row["lon"])
        imo = int(row["imo"])
    except (KeyError, TypeError, ValueError):
        return False
    return imo > 0 and math.isfinite(lat) and math.isfinite(lon) and _provenance_ok(row)


def allocate_budget(
    gap_imos: list[int],
    *,
    used_today: int = 0,
    cap: int | None = None,
    top500_imos: list[int] | None = None,
) -> list[int]:
    """Contract daily quota. TOP-500 gap vessels are taken first; order inside each group is kept."""
    limit = daily_cap() if cap is None else max(0, int(cap))
    room = max(0, limit - max(0, int(used_today)))
    preferred: set[int] = set()
    for raw in top500_imos or []:
        try:
            preferred.add(int(raw))
        except (TypeError, ValueError):
            continue
    ordered = list(gap_imos)
    if preferred:
        first = [imo for imo in gap_imos if _imo_or_none(imo) in preferred]
        rest = [imo for imo in gap_imos if _imo_or_none(imo) not in preferred]
        ordered = first + rest
    chosen: list[int] = []
    seen: set[int] = set()
    for imo in ordered:
        if room <= 0:
            break
        key = int(imo)
        if key in seen:
            continue
        seen.add(key)
        chosen.append(key)
        room -= 1
    return chosen


def _imo_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _default_transport(url: str, headers: dict[str, str]) -> Any:
    """One GET to the operator-supplied SAT_BASE_URL. No vendor host is hardcoded."""
    import json
    from urllib.request import Request, urlopen

    req = Request(
        url,
        headers={**headers, "Accept": "application/json", "User-Agent": "Oracle-1001-Sentinel-Satellite/1.0"},
        method="GET",
    )
    with urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pull(*, transport: Transport | None = None, path: str = "/positions") -> dict[str, Any]:
    """Fetch only when armed and SAT_BASE_URL is set. Otherwise zero network calls."""
    if not armed():
        return {"status": "parked", "rows": [], "requests": 0}
    base = (os.getenv("SAT_BASE_URL") or "").strip()
    if not base:
        return {"status": "parked", "rows": [], "requests": 0, "reason": "base_url_unset"}
    caller = transport if transport is not None else _default_transport
    try:
        payload = caller(base.rstrip("/") + path, {"Authorization": "Bearer " + _key()})
    except Exception:
        return {"status": "parked", "rows": [], "requests": 1, "reason": "transport_error"}
    rows = parse_vendor_payload(_provider(), payload)
    return {"status": "ok", "rows": rows, "requests": 1}


def probe(*, transport: Transport | None = None) -> dict[str, Any]:
    """active only after a real point. Otherwise parked, and the transport is not called.

    apply_runtime_env runs first so a key written by install_key.sh wins over the image env.
    """
    from services.runtime_env import apply_runtime_env

    apply_runtime_env()
    if not armed():
        return {"status": "parked", "requests": 0, "reason": "not_activated"}
    base = (os.getenv("SAT_BASE_URL") or "").strip()
    if not base:
        return {"status": "parked", "requests": 0, "reason": "no_endpoint"}
    out = pull(transport=transport)
    real = [row for row in (out.get("rows") or []) if _finite_point(row)]
    if out.get("status") == "ok" and int(out.get("requests") or 0) == 1 and real:
        return {"status": "active", "requests": 1, "rows": len(real)}
    return {
        "status": "parked",
        "requests": int(out.get("requests") or 0),
        "reason": "no_point" if out.get("status") == "ok" else str(out.get("status") or "parked"),
    }


def store_satellite_rows(conn: Any, rows: list[dict[str, Any]], *, snapshot_date: str) -> int:
    """Write source=satellite_ais only for positions the provider actually returned.

    A terrestrial or VF row is left untouched. A missing position is not invented.
    """
    written = 0
    for row in rows:
        imo = row.get("imo")
        lat = row.get("lat")
        lon = row.get("lon")
        if imo is None or lat is None or lon is None:
            continue
        if not _finite_point(row):
            continue
        current = conn.execute(
            "SELECT source FROM vessel_daily_archive WHERE snapshot_date=? AND imo=?",
            (snapshot_date, int(imo)),
        ).fetchone()
        if current is not None and str(current[0]) in {"terrestrial_ais", "vf_api"}:
            continue
        if current is None:
            conn.execute(
                """
                INSERT INTO vessel_daily_archive (snapshot_date, imo, lat, lon, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (snapshot_date, int(imo), float(lat), float(lon), ARCHIVE_SOURCE),
            )
        else:
            conn.execute(
                """
                UPDATE vessel_daily_archive
                   SET lat=?, lon=?, source=?
                 WHERE snapshot_date=? AND imo=? AND source IN ('none', 'satellite_ais')
                """,
                (float(lat), float(lon), ARCHIVE_SOURCE, snapshot_date, int(imo)),
            )
        written += 1
    return written
