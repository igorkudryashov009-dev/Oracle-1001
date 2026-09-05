#!/usr/bin/env python3
"""Operational anomaly report: STS proximity + Dark AIS gaps from sentinel_ais.db."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
DB_CANDIDATES = (
    ROOT / "история1" / "sentinel_ais.db",
    ROOT / "sentinel_ais.db",
)
OUT_MD = ROOT / "output" / "anomalies_report.md"
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


# Chokepoints + known STS hubs (critical re-acquisition zones).
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
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def parse_ts(s: str) -> datetime:
    t = str(s).replace("Z", "+00:00")
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def detect_zone(lat: float, lon: float) -> Optional[Zone]:
    for z in CRITICAL_ZONES:
        if z.contains(lat, lon):
            return z
    return None


def resolve_db() -> Path:
    for p in DB_CANDIDATES:
        if p.is_file():
            return p
    raise FileNotFoundError("sentinel_ais.db not found locally")


def load_registry_meta() -> dict[str, dict[str, Any]]:
    """Index by MMSI and IMO from sentinel_targets.json."""
    meta: dict[str, dict[str, Any]] = {}
    if not TARGETS_JSON.is_file():
        return meta
    import json

    data = json.loads(TARGETS_JSON.read_text(encoding="utf-8"))
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
    if not text:
        return ""
    # Drop control chars / mojibake tails; keep ASCII maritime name tokens.
    cleaned = "".join(ch if (ord(ch) >= 32 and ord(ch) < 127) or ch in "-/'." else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned


def enrich(row: dict[str, Any], meta: dict[str, dict[str, Any]]) -> dict[str, Any]:
    hit = meta.get(f"mmsi:{row.get('mmsi')}") or meta.get(f"imo:{row.get('imo')}")
    ais_name = _clean_name(row.get("vessel_name"))
    if hit:
        reg_name = _clean_name(hit.get("name"))
        # Prefer fleet-registry display name for Alpha–Delta operational reports.
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
    cur = con.execute(sql, (since_s, *tiers))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def detect_sts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pairwise STS: same time bucket, dist < 0.5 nm, both SOG < 1.0."""
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        sog = r.get("sog")
        if sog is None or float(sog) >= STS_SOG_MAX:
            continue
        ts = parse_ts(r["timestamp_utc"])
        b = int(ts.timestamp()) // STS_BUCKET_SEC
        buckets[b].append(r)

    # Merge contacts by unordered vessel pair
    contacts: dict[tuple[str, str], dict[str, Any]] = {}
    for b, items in buckets.items():
        # Deduplicate to latest fix per MMSI in bucket
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
                if dist >= STS_DIST_NM:
                    continue
                key = tuple(sorted((str(a["mmsi"]), str(bves["mmsi"]))))
                ts_a = parse_ts(a["timestamp_utc"])
                ts_b = parse_ts(bves["timestamp_utc"])
                t_lo, t_hi = min(ts_a, ts_b), max(ts_a, ts_b)
                mid_lat = (float(a["lat"]) + float(bves["lat"])) / 2
                mid_lon = (float(a["lon"]) + float(bves["lon"])) / 2
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
    out = sorted(contacts.values(), key=lambda x: (-x["samples"], x["min_dist_nm"]))
    return out


def detect_dark(
    rows: list[dict[str, Any]], gap_hours: float = DARK_GAP_HOURS
) -> list[dict[str, Any]]:
    """Alpha/Bravo gaps > gap_hours with reappearance in a critical zone."""
    by_mmsi: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if str(r.get("tier") or "").upper() not in ("ALPHA", "BRAVO"):
            continue
        by_mmsi[str(r["mmsi"])].append(r)

    events: list[dict[str, Any]] = []
    gap_td = timedelta(hours=gap_hours)
    for mmsi, track in by_mmsi.items():
        track.sort(key=lambda x: parse_ts(x["timestamp_utc"]))
        for i in range(len(track) - 1):
            a, b = track[i], track[i + 1]
            t0, t1 = parse_ts(a["timestamp_utc"]), parse_ts(b["timestamp_utc"])
            gap = t1 - t0
            if gap < gap_td:
                continue
            zone = detect_zone(float(b["lat"]), float(b["lon"]))
            if zone is None:
                continue
            events.append({
                "imo": b.get("imo") or a.get("imo"),
                "mmsi": mmsi,
                "vessel_name": b.get("vessel_name") or a.get("vessel_name"),
                "tier": b.get("tier") or a.get("tier"),
                "gap_hours": round(gap.total_seconds() / 3600.0, 2),
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
    sog = v.get("sog")
    sog_s = f"{float(sog):.1f}" if sog is not None else "—"
    return f"{name} (IMO {imo}, {tier}, risk={risk}, SOG={sog_s} kn)"


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
) -> str:
    now = datetime.now(timezone.utc)
    obs_h = None
    if db_min and db_max:
        obs_h = round((db_max - db_min).total_seconds() / 3600.0, 2)

    lines = [
        "# Sentinel Operational Anomalies Report",
        "",
        f"- Generated (UTC): `{now.isoformat().replace('+00:00', 'Z')}`",
        f"- Source DB: `{db.as_posix()}` ({db.stat().st_size:,} bytes)",
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
        f"| Category | Count |",
        f"|----------|------:|",
        f"| STS operations (< {STS_DIST_NM} nm, SOG < {STS_SOG_MAX} kn) | **{len(sts)}** |",
        f"| Dark AIS (gap > {DARK_GAP_HOURS:g}h → critical zone) | **{len(dark)}** |",
        "",
    ]

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
        lines += ["### STS detail cards", ""]
        for i, c in enumerate(sts[:25], 1):
            a, b = c["vessel_a"], c["vessel_b"]
            lines += [
                f"#### STS-{i:02d}",
                f"- **A:** {fmt_vessel(a)} · MMSI `{a.get('mmsi')}`",
                f"- **B:** {fmt_vessel(b)} · MMSI `{b.get('mmsi')}`",
                f"- **Min distance:** {c['min_dist_nm']:.3f} nm",
                f"- **Window:** `{c['first_contact_utc'].isoformat().replace('+00:00', 'Z')}` → "
                f"`{c['last_contact_utc'].isoformat().replace('+00:00', 'Z')}`",
                f"- **Position:** {c['lat']}, {c['lon']} · **Area:** {c['zone']}",
                f"- **Registry status:** A={a.get('tier')}/{a.get('registry_risk') or 'n/a'}; "
                f"B={b.get('tier')}/{b.get('registry_risk') or 'n/a'}",
                "",
            ]

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
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    db = resolve_db()
    meta = load_registry_meta()
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    mx = con.execute("SELECT MAX(timestamp_utc) FROM ais_positions").fetchone()[0]
    mn = con.execute("SELECT MIN(timestamp_utc) FROM ais_positions").fetchone()[0]
    n_pos = con.execute("SELECT COUNT(*) FROM ais_positions").fetchone()[0]
    db_max = parse_ts(mx) if mx else datetime.now(timezone.utc)
    db_min = parse_ts(mn) if mn else None
    window_end = db_max
    window_start = window_end - timedelta(hours=LOOKBACK_HOURS)

    tiers = ("ALPHA", "BRAVO", "CHARLIE", "DELTA")
    rows = fetch_registry_positions(con, window_start, tiers)
    rows = [enrich(r, meta) for r in rows]
    con.close()

    sts = detect_sts(rows)
    dark = detect_dark(rows)

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
    )
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(md, encoding="utf-8")

    print("=" * 72)
    print("ANOMALIES REPORT")
    print("=" * 72)
    print(f"DB: {db} ({db.stat().st_size:,} bytes)")
    if db_min and db_max:
        span_h = (db_max - db_min).total_seconds() / 3600.0
        print(f"AIS span: {db_min} -> {db_max} ({span_h:.2f}h)")
    print(f"Registry-tiered fixes (24h window): {len(rows):,}")
    print(f"STS pairs:  {len(sts)}")
    print(f"Dark AIS:   {len(dark)}")
    print(f"Wrote:      {OUT_MD}")
    print("-" * 72)
    for i, c in enumerate(sts[:10], 1):
        a, b = c["vessel_a"], c["vessel_b"]
        print(
            f"STS#{i} {_clean_name(a.get('vessel_name'))}[{a.get('tier')}] <-> "
            f"{_clean_name(b.get('vessel_name'))}[{b.get('tier')}] "
            f"dist={c['min_dist_nm']:.3f}nm samples={c['samples']} @ {c['zone']}"
        )
    if not sts:
        print("STS: none")
    for i, e in enumerate(dark[:10], 1):
        print(
            f"DARK#{i} {_clean_name(e.get('vessel_name'))}[{e.get('tier')}] "
            f"gap={e['gap_hours']}h -> {e['reacq_zone']}"
        )
    if not dark:
        print("DARK: none (see data caveat if span < 4h)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
