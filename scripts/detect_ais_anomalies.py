#!/usr/bin/env python3
"""Operational anomaly report: STS proximity + Dark AIS gaps from sentinel_ais.db.

Hardened for empty telemetry windows: never crashes on NaN / zero-division /
missing slices — always emits a valid fallback report (exit 0).
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.utils.path_sanitizer import (  # noqa: E402
    sanitize_path_string,
    sanitize_structure,
)

DB_CANDIDATES = (
    ROOT / "история1" / "sentinel_ais.db",
    ROOT / "sentinel_ais.db",
)
OUT_MD = ROOT / "output" / "anomalies_report.md"
OUT_JSON = ROOT / "output" / "anomalies_report.json"
TARGETS_JSON = ROOT / "output" / "sentinel_targets.json"

STS_DIST_NM = 0.5
STS_SOG_MAX = 1.0
STS_BUCKET_SEC = 10 * 60
DARK_GAP_HOURS = 4.0
LOOKBACK_HOURS = 24


@dataclass(frozen=True)
class Zone:
    id: str
    name: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max


CRITICAL_ZONES: tuple[Zone, ...] = (
    Zone("malacca", "Strait of Malacca", 1.0, 8.0, 98.0, 105.0),
    Zone("suez", "Suez Canal", 29.0, 31.8, 32.0, 33.5),
    Zone("bab_el_mandeb", "Bab-el-Mandeb", 11.5, 14.5, 42.0, 44.5),
    Zone("bosphorus", "Bosphorus", 40.8, 41.4, 28.7, 29.4),
    Zone("danish", "Danish Straits", 54.3, 56.8, 10.0, 13.5),
    Zone("hormuz", "Strait of Hormuz", 25.0, 27.5, 55.5, 57.5),
    Zone("gibraltar", "Strait of Gibraltar", 35.7, 36.3, -6.0, -5.0),
    Zone("fujairah_sts", "Fujairah STS Hub", 24.8, 25.8, 56.0, 56.9),
    Zone("laconian_sts", "Laconian Gulf STS", 36.2, 36.9, 22.2, 23.2),
    Zone("ceuta_sts", "Ceuta / Alboran STS", 35.6, 36.2, -5.6, -4.8),
    Zone("goa_sts", "Gulf of Guinea STS", 0.0, 6.0, -2.0, 8.0),
    Zone("singapore_sts", "Singapore STS Approaches", 1.0, 1.5, 103.5, 104.2),
)


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3440.065
    try:
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlmb = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
        a = min(1.0, max(0.0, a))
        return 2 * r * math.asin(math.sqrt(a))
    except (ValueError, OverflowError, ZeroDivisionError):
        return float("inf")


def parse_ts(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    try:
        t = str(s).replace("Z", "+00:00").strip()
        if not t or t.lower() in {"nan", "nat", "none", "null"}:
            return None
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def detect_zone(lat: float, lon: float) -> Optional[Zone]:
    for z in CRITICAL_ZONES:
        if z.contains(lat, lon):
            return z
    return None


def resolve_db() -> Optional[Path]:
    for p in DB_CANDIDATES:
        if p.is_file():
            return p
    return None


def load_registry_meta() -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}
    if not TARGETS_JSON.is_file():
        return meta
    try:
        data = json.loads(TARGETS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return meta
    for t in data.get("targets") or []:
        row = {
            "imo": str(t.get("imo") or ""),
            "mmsi": str(t.get("mmsi") or ""),
            "name": str(t.get("name") or ""),
            "tier": str(t.get("tier") or ""),
            "rank": t.get("rank"),
            "risk": t.get("risk"),
            "dwt_tons": t.get("dwt_tons"),
        }
        if row["mmsi"]:
            meta[f"mmsi:{row['mmsi']}"] = row
        if row["imo"]:
            meta[f"imo:{row['imo']}"] = row
    return meta


def _clean_name(s: Any) -> str:
    text = str(s or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    cleaned = "".join(
        ch if (ord(ch) >= 32 and ord(ch) < 127) or ch in "-/'." else " " for ch in text
    )
    return " ".join(cleaned.split())


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        # pandas / numpy NaN guard without hard dependency
        if isinstance(value, float) and math.isnan(value):
            return None
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def enrich(row: dict[str, Any], meta: dict[str, dict[str, Any]]) -> dict[str, Any]:
    hit = meta.get(f"mmsi:{row.get('mmsi')}") or meta.get(f"imo:{row.get('imo')}")
    ais_name = _clean_name(row.get("vessel_name"))
    if hit:
        reg_name = _clean_name(hit.get("name"))
        row["vessel_name"] = reg_name or ais_name
        row["tier"] = row.get("tier") or hit.get("tier")
        row["imo"] = row.get("imo") or hit.get("imo")
        row["registry_rank"] = hit.get("rank")
        row["registry_risk"] = hit.get("risk")
    else:
        row["vessel_name"] = ais_name or row.get("vessel_name")
    return row


def fetch_registry_positions(
    con: sqlite3.Connection, since: datetime, tiers: tuple[str, ...]
) -> list[dict[str, Any]]:
    if not tiers:
        return []
    placeholders = ",".join("?" * len(tiers))
    sql = f"""
        SELECT imo, mmsi, vessel_name, tier, timestamp_utc, lat, lon, sog, cog,
               nav_status, destination, matched
        FROM ais_positions
        WHERE timestamp_utc >= ?
          AND tier IN ({placeholders})
        ORDER BY timestamp_utc ASC
    """
    since_s = since.isoformat()
    try:
        cur = con.execute(sql, (since_s, *tiers))
    except sqlite3.Error:
        return []
    cols = [d[0] for d in cur.description]
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        row = dict(zip(cols, r))
        lat = _safe_float(row.get("lat"))
        lon = _safe_float(row.get("lon"))
        if lat is None or lon is None:
            continue
        if parse_ts(row.get("timestamp_utc")) is None:
            continue
        row["lat"], row["lon"] = lat, lon
        row["sog"] = _safe_float(row.get("sog"))
        out.append(row)
    return out


def detect_sts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        sog = _safe_float(r.get("sog"))
        if sog is None or sog >= STS_SOG_MAX:
            continue
        ts = parse_ts(r.get("timestamp_utc"))
        if ts is None:
            continue
        b = int(ts.timestamp()) // STS_BUCKET_SEC
        buckets[b].append(r)

    contacts: dict[tuple[str, str], dict[str, Any]] = {}
    for _b, items in buckets.items():
        by_mmsi: dict[str, dict[str, Any]] = {}
        for it in items:
            by_mmsi[str(it["mmsi"])] = it
        vessels = list(by_mmsi.values())
        for i in range(len(vessels)):
            a = vessels[i]
            for j in range(i + 1, len(vessels)):
                bves = vessels[j]
                dist = haversine_nm(
                    float(a["lat"]), float(a["lon"]), float(bves["lat"]), float(bves["lon"])
                )
                if not math.isfinite(dist) or dist >= STS_DIST_NM:
                    continue
                key = tuple(sorted((str(a["mmsi"]), str(bves["mmsi"]))))
                ts_a = parse_ts(a["timestamp_utc"])
                ts_b = parse_ts(bves["timestamp_utc"])
                if ts_a is None or ts_b is None:
                    continue
                t_lo, t_hi = min(ts_a, ts_b), max(ts_a, ts_b)
                mid_lat = (float(a["lat"]) + float(bves["lat"])) / 2.0
                mid_lon = (float(a["lon"]) + float(bves["lon"])) / 2.0
                zone = detect_zone(mid_lat, mid_lon)
                if key not in contacts:
                    contacts[key] = {
                        "vessel_a": a,
                        "vessel_b": bves,
                        "min_dist_nm": round(dist, 3),
                        "first_contact_utc": t_lo,
                        "last_contact_utc": t_hi,
                        "samples": 1,
                        "lat": round(mid_lat, 5),
                        "lon": round(mid_lon, 5),
                        "zone": zone.name if zone else "Open water / other",
                    }
                else:
                    c = contacts[key]
                    c["min_dist_nm"] = min(c["min_dist_nm"], round(dist, 3))
                    c["first_contact_utc"] = min(c["first_contact_utc"], t_lo)
                    c["last_contact_utc"] = max(c["last_contact_utc"], t_hi)
                    c["samples"] += 1
                    if dist <= c["min_dist_nm"]:
                        c["lat"], c["lon"] = round(mid_lat, 5), round(mid_lon, 5)
                        c["zone"] = zone.name if zone else c["zone"]
    return sorted(contacts.values(), key=lambda x: (-x["samples"], x["min_dist_nm"]))


def detect_dark(
    rows: list[dict[str, Any]], gap_hours: float = DARK_GAP_HOURS
) -> list[dict[str, Any]]:
    if not rows or gap_hours <= 0:
        return []
    by_mmsi: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if str(r.get("tier") or "").upper() not in ("ALPHA", "BRAVO"):
            continue
        by_mmsi[str(r["mmsi"])].append(r)

    events: list[dict[str, Any]] = []
    gap_td = timedelta(hours=gap_hours)
    for mmsi, track in by_mmsi.items():
        track = [t for t in track if parse_ts(t.get("timestamp_utc")) is not None]
        track.sort(key=lambda x: parse_ts(x["timestamp_utc"]) or datetime.min.replace(tzinfo=timezone.utc))
        for i in range(len(track) - 1):
            a, b = track[i], track[i + 1]
            t0, t1 = parse_ts(a["timestamp_utc"]), parse_ts(b["timestamp_utc"])
            if t0 is None or t1 is None:
                continue
            gap = t1 - t0
            if gap < gap_td:
                continue
            zone = detect_zone(float(b["lat"]), float(b["lon"]))
            if zone is None:
                continue
            sec = gap.total_seconds()
            gap_h = round(sec / 3600.0, 2) if sec > 0 else 0.0
            events.append({
                "imo": b.get("imo") or a.get("imo"),
                "mmsi": mmsi,
                "vessel_name": b.get("vessel_name") or a.get("vessel_name"),
                "tier": b.get("tier") or a.get("tier"),
                "gap_hours": gap_h,
                "last_contact_utc": t0.isoformat().replace("+00:00", "Z"),
                "loss_lat": round(float(a["lat"]), 5),
                "loss_lon": round(float(a["lon"]), 5),
                "reacq_utc": t1.isoformat().replace("+00:00", "Z"),
                "reacq_lat": round(float(b["lat"]), 5),
                "reacq_lon": round(float(b["lon"]), 5),
                "reacq_zone": zone.name,
                "registry_risk": b.get("registry_risk") or a.get("registry_risk"),
            })
    events.sort(key=lambda x: -x["gap_hours"])
    return events


def fmt_vessel(v: dict[str, Any]) -> str:
    name = v.get("vessel_name") or "UNKNOWN"
    imo = v.get("imo") or "—"
    tier = v.get("tier") or "—"
    risk = v.get("registry_risk") or "—"
    sog = _safe_float(v.get("sog"))
    sog_s = f"{sog:.1f}" if sog is not None else "—"
    return f"{name} (IMO {imo}, {tier}, risk={risk}, SOG={sog_s} kn)"


def empty_fallback_payload(*, reason: str, db: Optional[Path] = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return sanitize_structure({
        "status": "ok",
        "mode": "fallback_empty_telemetry",
        "reason": reason,
        "generated_at": now,
        "db": sanitize_path_string(str(db)) if db else None,
        "summary": {"sts_pairs": 0, "dark_ais": 0, "registry_positions": 0},
        "sts": [],
        "dark": [],
    })


def render_report(
    *,
    db: Path,
    window_start: datetime,
    window_end: datetime,
    db_min: Optional[datetime],
    db_max: Optional[datetime],
    n_pos: int,
    n_registry_pos: int,
    sts: list[dict[str, Any]],
    dark: list[dict[str, Any]],
    caveat: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    obs_h = None
    if db_min and db_max:
        denom = (db_max - db_min).total_seconds()
        obs_h = round(denom / 3600.0, 2) if denom > 0 else 0.0

    db_disp = sanitize_path_string(str(db))
    lines = [
        "# Sentinel Operational Anomalies Report",
        "",
        f"- Generated (UTC): `{now.isoformat().replace('+00:00', 'Z')}`",
        f"- Source DB: `{db_disp}` ({db.stat().st_size:,} bytes)",
        f"- Requested lookback: last **{LOOKBACK_HOURS}h** "
        f"(`{window_start.isoformat().replace('+00:00', 'Z')}` → "
        f"`{window_end.isoformat().replace('+00:00', 'Z')}`)",
        f"- Observed AIS span in DB: "
        f"`{(db_min.isoformat().replace('+00:00', 'Z') if db_min else 'n/a')}` → "
        f"`{(db_max.isoformat().replace('+00:00', 'Z') if db_max else 'n/a')}`"
        + (f" (**{obs_h}h** available)" if obs_h is not None else ""),
        f"- Positions total / registry-tiered: **{n_pos:,}** / **{n_registry_pos:,}**",
        "",
        "## Summary",
        "",
        "| Category | Count |",
        "|----------|------:|",
        f"| STS operations (< {STS_DIST_NM} nm, SOG < {STS_SOG_MAX} kn) | **{len(sts)}** |",
        f"| Dark AIS (gap > {DARK_GAP_HOURS:g}h → critical zone) | **{len(dark)}** |",
        "",
    ]
    if caveat:
        lines += [f"> **Note:** {caveat}", ""]
    if obs_h is not None and obs_h < DARK_GAP_HOURS:
        lines += [
            f"> **Data caveat:** continuous ingest window is only **{obs_h}h**, "
            f"which is shorter than the Dark AIS threshold ({DARK_GAP_HOURS:g}h). "
            "Zero Dark events in this run are expected until retention exceeds the gap threshold.",
            "",
        ]

    lines += [
        "## 1. STS Operations (Ship-to-Ship)",
        "",
        f"Criteria: both vessels in Alpha–Delta registry, contemporaneous "
        f"(±{STS_BUCKET_SEC // 60} min bucket), distance **< {STS_DIST_NM} nm**, "
        f"SOG **< {STS_SOG_MAX} kn**.",
        "",
    ]
    if not sts:
        lines += ["_No STS proximity pairs detected in the available window._", ""]
    else:
        lines += [
            "| # | Vessel A | Vessel B | Min dist (nm) | Contact window (UTC) | Lat / Lon | Zone / hub | Samples |",
            "|--:|----------|----------|--------------:|---------------------:|-----------|------------|--------:|",
        ]
        for i, c in enumerate(sts, 1):
            a, b = c["vessel_a"], c["vessel_b"]
            t0 = c["first_contact_utc"].isoformat().replace("+00:00", "Z")
            t1 = c["last_contact_utc"].isoformat().replace("+00:00", "Z")
            lines.append(
                f"| {i} | {fmt_vessel(a)} | {fmt_vessel(b)} | {c['min_dist_nm']:.3f} | "
                f"{t0} → {t1} | {c['lat']}, {c['lon']} | {c['zone']} | {c['samples']} |"
            )
        lines.append("")

    lines += [
        "## 2. Dark AIS Activity",
        "",
        f"Criteria: Alpha/Bravo only; consecutive-track gap **> {DARK_GAP_HOURS:g} hours**; "
        "re-acquisition inside chokepoint or STS hub.",
        "",
    ]
    if not dark:
        lines += ["_No Dark AIS gap→critical-zone reacquisition events detected._", ""]
    else:
        lines += [
            "| # | Vessel | Tier | Gap (h) | Last contact (UTC) | Loss lat/lon | Reacq (UTC) | Reacq lat/lon | Zone |",
            "|--:|--------|------|--------:|-------------------:|-------------:|------------:|--------------:|------|",
        ]
        for i, e in enumerate(dark, 1):
            lines.append(
                f"| {i} | {e.get('vessel_name') or 'UNKNOWN'} (IMO {e.get('imo') or '—'}) | "
                f"{e.get('tier')} | {e['gap_hours']:.2f} | `{e['last_contact_utc']}` | "
                f"{e['loss_lat']}, {e['loss_lon']} | `{e['reacq_utc']}` | "
                f"{e['reacq_lat']}, {e['reacq_lon']} | {e['reacq_zone']} |"
            )
        lines.append("")

    lines += [
        "## Method notes",
        "",
        "- STS pairing uses time-bucketed co-location (not full O(n²) continuous tracks).",
        "- Dark AIS requires both sides of the gap inside the local SQLite retention window.",
        "- Critical zones = Sentinel chokepoints + Fujairah / Laconian / Ceuta / GoG / Singapore STS hubs.",
        "- Empty telemetry windows emit this report with zero counts (non-fatal).",
        "",
    ]
    return "\n".join(lines)


def _write_fallback(reason: str, db: Optional[Path] = None) -> int:
    payload = empty_fallback_payload(reason=reason, db=db)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    now = datetime.now(timezone.utc)
    md = [
        "# Sentinel Operational Anomalies Report",
        "",
        f"- Generated (UTC): `{now.strftime('%Y-%m-%dT%H:%M:%SZ')}`",
        f"- Mode: `fallback_empty_telemetry`",
        f"- Reason: {reason}",
        "",
        "## Summary",
        "",
        "| Category | Count |",
        "|----------|------:|",
        "| STS operations | **0** |",
        "| Dark AIS | **0** |",
        "",
        "_No telemetry available for anomaly detection in this window._",
        "",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(f"ANOMALIES FALLBACK: {reason}")
    print(f"Wrote: {sanitize_path_string(str(OUT_MD))}")
    return 0


def main() -> int:
    try:
        db = resolve_db()
        if db is None:
            return _write_fallback("sentinel_ais.db not found locally")

        meta = load_registry_meta()
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            return _write_fallback(f"db open failed: {exc}", db=db)

        try:
            try:
                mx = con.execute("SELECT MAX(timestamp_utc) FROM ais_positions").fetchone()[0]
                mn = con.execute("SELECT MIN(timestamp_utc) FROM ais_positions").fetchone()[0]
                n_pos = int(con.execute("SELECT COUNT(*) FROM ais_positions").fetchone()[0] or 0)
            except sqlite3.Error as exc:
                con.close()
                return _write_fallback(f"ais_positions query failed: {exc}", db=db)

            if n_pos == 0 or mx is None:
                con.close()
                return _write_fallback("ais_positions empty — zero telemetry", db=db)

            db_max = parse_ts(mx) or datetime.now(timezone.utc)
            db_min = parse_ts(mn)
            window_end = db_max
            window_start = window_end - timedelta(hours=LOOKBACK_HOURS)
            tiers = ("ALPHA", "BRAVO", "CHARLIE", "DELTA")
            rows = fetch_registry_positions(con, window_start, tiers)
            rows = [enrich(r, meta) for r in rows]
            con.close()
        except Exception as exc:  # noqa: BLE001
            try:
                con.close()
            except Exception:
                pass
            return _write_fallback(f"telemetry slice failed: {exc}", db=db)

        caveat = None
        if not rows:
            caveat = "Registry-tiered slice empty in lookback window — STS/Dark counts are zero."

        try:
            sts = detect_sts(rows)
            dark = detect_dark(rows)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            sts, dark = [], []
            caveat = f"detector exception swallowed: {exc}"

        md = render_report(
            db=db,
            window_start=window_start,
            window_end=window_end,
            db_min=db_min,
            db_max=db_max,
            n_pos=int(n_pos),
            n_registry_pos=len(rows),
            sts=sts,
            dark=dark,
            caveat=caveat,
        )
        OUT_MD.parent.mkdir(parents=True, exist_ok=True)
        OUT_MD.write_text(md, encoding="utf-8")

        # JSON twin (container-safe paths)
        def _ser_sts(c: dict[str, Any]) -> dict[str, Any]:
            return {
                "min_dist_nm": c["min_dist_nm"],
                "samples": c["samples"],
                "lat": c["lat"],
                "lon": c["lon"],
                "zone": c["zone"],
                "first_contact_utc": c["first_contact_utc"].isoformat().replace("+00:00", "Z"),
                "last_contact_utc": c["last_contact_utc"].isoformat().replace("+00:00", "Z"),
                "vessel_a": {
                    "mmsi": c["vessel_a"].get("mmsi"),
                    "imo": c["vessel_a"].get("imo"),
                    "name": c["vessel_a"].get("vessel_name"),
                    "tier": c["vessel_a"].get("tier"),
                },
                "vessel_b": {
                    "mmsi": c["vessel_b"].get("mmsi"),
                    "imo": c["vessel_b"].get("imo"),
                    "name": c["vessel_b"].get("vessel_name"),
                    "tier": c["vessel_b"].get("tier"),
                },
            }

        payload = sanitize_structure({
            "status": "ok",
            "mode": "live" if rows else "fallback_empty_telemetry",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "db": str(db),
            "summary": {
                "sts_pairs": len(sts),
                "dark_ais": len(dark),
                "registry_positions": len(rows),
                "positions_total": int(n_pos),
            },
            "sts": [_ser_sts(c) for c in sts],
            "dark": dark,
            "caveat": caveat,
        })
        OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        print("=" * 72)
        print("ANOMALIES REPORT")
        print("=" * 72)
        print(f"DB: {sanitize_path_string(str(db))} ({db.stat().st_size:,} bytes)")
        if db_min and db_max:
            span_h = max(0.0, (db_max - db_min).total_seconds() / 3600.0)
            print(f"AIS span: {db_min} -> {db_max} ({span_h:.2f}h)")
        print(f"Registry-tiered fixes (24h window): {len(rows):,}")
        print(f"STS pairs:  {len(sts)}")
        print(f"Dark AIS:   {len(dark)}")
        print(f"Wrote:      {sanitize_path_string(str(OUT_MD))}")
        print(f"Wrote JSON: {sanitize_path_string(str(OUT_JSON))}")
        print("=" * 72)
        return 0
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return _write_fallback(f"uncaught: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
