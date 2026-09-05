"""Generate payload for the 18-chart Sentinel analytics dashboard."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.analytics import (
    build_live_anomaly_cards,
    course_deviation_index,
    draught_load_estimator,
    filter_vessels_to_top500,
    flag_sanctions_matrix,
    select_top500_fleet,
    sog_spectrum,
)
from services.chokepoints import CHOKEPOINTS, in_sanctioned_zone
from services.fleet_registry import FleetRegistry, TIER_COLORS
from services.storage import AISStorage

ROOT = Path(__file__).resolve().parents[1]
TOP500_N = 500

# Major destination ports for chart 5
TOP_PORTS = (
    "Pattaya", "Si Chang", "Sichang", "Singapore", "Fujairah",
    "Novorossiysk", "Ras Laffan", "Yokohama", "Rotterdam", "Houston",
    "Ningbo", "Zhoushan", "Suez", "Ceyhan",
)


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3440.065  # Earth radius in nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _seed_positions_from_fleet(registry: FleetRegistry) -> list[dict[str, Any]]:
    """Deterministic synthetic live positions when AIS DB is empty (demo / offline)."""
    rng = random.Random(1001)
    corridors = [
        (1.3, 103.8),   # Singapore
        (25.3, 55.3),   # Fujairah
        (44.7, 37.8),   # Novorossiysk
        (29.9, 32.5),   # Suez
        (12.7, 43.3),   # Bab-el-Mandeb
        (41.1, 29.0),   # Bosphorus
        (35.0, 129.0),  # Korea Strait
        (51.9, 4.3),    # Rotterdam
        (-5.0, 105.0),  # Java Sea
        (22.3, 114.2),  # HK / Pearl River
    ]
    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, v in enumerate(registry.vessels):
        base = corridors[i % len(corridors)]
        lat = base[0] + rng.uniform(-2.5, 2.5)
        lon = base[1] + rng.uniform(-3.0, 3.0)
        sog = round(rng.uniform(0.0, 18.5), 1)
        if v.tier == "ALPHA" and i % 7 == 0:
            sog = round(rng.uniform(0.0, 1.2), 1)  # STS / floating storage candidates
        draft = max(4.0, float(v.draft_m or 10.0) + rng.uniform(-1.5, 1.5))
        rows.append({
            "imo": v.imo,
            "mmsi": v.mmsi,
            "vessel_name": v.vessel_name,
            "tier": v.tier,
            "timestamp_utc": now,
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "sog": sog,
            "cog": round(rng.uniform(0, 359), 1),
            "heading": round(rng.uniform(0, 359), 1),
            "nav_status": "At anchor" if sog < 0.5 else "Under way using engine",
            "draft_m": round(draft, 2),
            "destination": v.destination_port or TOP_PORTS[i % len(TOP_PORTS)],
            "matched": 1,
            "message_type": "PositionReport",
            "received_at": now,
            "flag": v.flag,
            "dwt_tons": v.dwt_tons,
            "risk": v.compliance_risk_level,
            "sanctions_tags": v.sanctions_tags,
            "vessel_type": v.vessel_type,
            "_synthetic": True,
        })
    return rows


def _enrich(rows: list[dict[str, Any]], registry: FleetRegistry) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        v = registry.match(mmsi=str(r.get("mmsi") or ""), imo=str(r.get("imo") or ""))
        if v:
            r = dict(r)
            r.setdefault("tier", v.tier)
            r.setdefault("vessel_name", v.vessel_name)
            r["flag"] = v.flag
            r["dwt_tons"] = v.dwt_tons
            r["risk"] = v.compliance_risk_level
            r["sanctions_tags"] = v.sanctions_tags
            r["vessel_type"] = v.vessel_type
            r["matched"] = 1
        out.append(r)
    return out


def build_sentinel_payload(
    registry: FleetRegistry | None = None,
    storage: AISStorage | None = None,
    *,
    use_synthetic_if_empty: bool = True,
    top_n: int = TOP500_N,
) -> dict[str, Any]:
    """Build complete JSON payload powering all 18 Sentinel infographics (TOP-500)."""
    # Full registry still used for enrichment / synthetic seed; analytics filters to TOP-N
    reg = registry or FleetRegistry.from_csv(ROOT / "output" / "fleet_database.csv", top_n=1001)
    top_df = select_top500_fleet(ROOT / "output" / "fleet_database.csv", top_n=top_n)
    store = storage or AISStorage(sqlite_path=ROOT / "история1" / "sentinel_ais.db")
    try:
        store.open()
        live = store.fetch_recent(limit=8000, matched_only=False)
        telemetry = store.fetch_telemetry(limit=120)
    except Exception:  # noqa: BLE001
        live, telemetry = [], []

    matched_live = [r for r in live if int(r.get("matched") or 0) == 1]
    if not matched_live and use_synthetic_if_empty:
        # Seed from TOP-500 slice of registry for OOTB demos
        top_mmsis = {str(x) for x in top_df["mmsi"].astype(str).tolist()}
        seeded = _seed_positions_from_fleet(reg)
        matched_live = [r for r in seeded if str(r.get("mmsi")) in top_mmsis] or seeded[:top_n]
        source_mode = "synthetic_seed"
    else:
        matched_live = _enrich(matched_live, reg)
        source_mode = "live_ais" if matched_live else "empty"

    # Deduplicate to latest position per MMSI
    latest: dict[str, dict[str, Any]] = {}
    for r in matched_live:
        mmsi = str(r.get("mmsi") or "")
        if not mmsi:
            continue
        prev = latest.get(mmsi)
        if not prev or str(r.get("timestamp_utc") or "") >= str(prev.get("timestamp_utc") or ""):
            latest[mmsi] = r
    vessels = filter_vessels_to_top500(list(latest.values()), top_df)

    # Live STS / Dark from anomaly engine (replaces synthetic dark timeline)
    anomalies = build_live_anomaly_cards()
    sts_clusters = anomalies.get("sts_clusters") or []
    dark_events = anomalies.get("dark_timeline") or []

    # ── Chart 1: Heatmap points ──────────────────────────────────────────────
    heatmap = [
        {
            "lat": float(v["lat"]),
            "lon": float(v["lon"]),
            "tier": v.get("tier") or "DELTA",
            "name": v.get("vessel_name"),
            "imo": v.get("imo"),
            "sog": v.get("sog"),
            "color": TIER_COLORS.get(v.get("tier") or "DELTA", "#10b981"),
        }
        for v in vessels
        if v.get("lat") is not None and v.get("lon") is not None
    ]

    # ── Chart 2: Chokepoint density ──────────────────────────────────────────
    cp_counts: Counter[str] = Counter()
    for v in vessels:
        for cp in CHOKEPOINTS:
            if cp.contains(float(v["lat"]), float(v["lon"])):
                cp_counts[cp.name] += 1
                break
    chokepoints = [{"name": c.name, "count": int(cp_counts.get(c.name, 0)), "id": c.id} for c in CHOKEPOINTS]

    # ── Chart 4: Alpha route corridors ───────────────────────────────────────
    alpha_tracks = []
    for v in vessels:
        if v.get("tier") != "ALPHA":
            continue
        alpha_tracks.append({
            "imo": v.get("imo"),
            "name": v.get("vessel_name"),
            "lat": v.get("lat"),
            "lon": v.get("lon"),
            "cog": v.get("cog"),
            "sog": v.get("sog"),
            "destination": v.get("destination"),
        })

    # ── Chart 5: Port arrival / berth velocity matrix ────────────────────────
    port_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"anchor": 0.0, "berth": 0.0, "n": 0})
    for v in vessels:
        dest = str(v.get("destination") or "")
        port = next((p for p in TOP_PORTS if p.lower() in dest.lower()), None)
        if not port:
            continue
        sog = float(v.get("sog") or 0)
        nav = str(v.get("nav_status") or "").lower()
        if "moor" in nav or sog < 0.3:
            port_stats[port]["berth"] += 1
        elif "anchor" in nav or sog < 1.0:
            port_stats[port]["anchor"] += 1
        port_stats[port]["n"] += 1
    port_matrix = [
        {"port": p, "anchor_hours_proxy": round(s["anchor"] * 4.5, 1), "berth_hours_proxy": round(s["berth"] * 3.2, 1), "n": int(s["n"])}
        for p, s in sorted(port_stats.items(), key=lambda x: -x[1]["n"])[:10]
    ]

    # ── Chart 7–9 / 12: TOP-500 kinematics engine ────────────────────────────
    draft_scatter = draught_load_estimator(vessels)
    speed_spectrum = sog_spectrum(vessels)
    course_deviation = course_deviation_index(vessels)
    flag_pack = flag_sanctions_matrix(vessels)

    # ── Chart 10: Spoofing / GNSS radar ──────────────────────────────────────
    spoof_flags = {"coord_jump": 0, "velocity_spike": 0, "heading_incoherent": 0, "clean": 0}
    for v in vessels:
        sog = float(v.get("sog") or 0)
        heading = v.get("heading")
        cog = v.get("cog")
        flagged = False
        if sog > 25:
            spoof_flags["velocity_spike"] += 1
            flagged = True
        if heading is not None and cog is not None:
            try:
                dh = abs(float(heading) - float(cog))
                if min(dh, 360 - dh) > 90 and sog > 3:
                    spoof_flags["heading_incoherent"] += 1
                    flagged = True
            except (TypeError, ValueError):
                pass
        if abs(float(v.get("lat") or 0)) < 0.01 and abs(float(v.get("lon") or 0)) < 0.01:
            spoof_flags["coord_jump"] += 1
            flagged = True
        if not flagged:
            spoof_flags["clean"] += 1

    # ── Chart 11: Tanker tonnage in transit ──────────────────────────────────
    floating = sum(float(v.get("dwt_tons") or 0) for v in vessels if float(v.get("sog") or 0) < 1.0)
    steaming = sum(float(v.get("dwt_tons") or 0) for v in vessels if float(v.get("sog") or 0) >= 1.0)
    tonnage_transit = {
        "floating_storage_dwt": round(floating, 0),
        "steaming_dwt": round(steaming, 0),
        "utilization_pct": round(100.0 * floating / max(floating + steaming, 1), 1),
        "series": [
            {"t": f"T-{i}", "floating": round(floating * (0.85 + 0.03 * i), 0), "steaming": round(steaming * (0.9 + 0.02 * i), 0)}
            for i in range(12)
        ],
    }

    flag_tree = flag_pack["flag_tree"]

    # ── Chart 13: Risk / tier compliance radar ───────────────────────────────
    risk_radar = {
        "labels": ["Sanctions", "Age", "FOC Flag", "Dark AIS", "STS Prox", "Zone Risk"],
        "ALPHA": [78, 42, 55, 61, 70, 66],
        "BRAVO": [55, 48, 50, 40, 45, 52],
        "CHARLIE": [35, 40, 38, 28, 30, 34],
        "DELTA": [22, 30, 25, 18, 20, 24],
    }
    # Adjust from live data
    extreme = sum(1 for v in vessels if str(v.get("risk") or "").upper() == "EXTREME")
    risk_radar["ALPHA"][0] = min(100, 40 + extreme)

    # ── Chart 14: Geopolitical exposure ──────────────────────────────────────
    zone_tiers: dict[str, Counter[str]] = defaultdict(Counter)
    for v in vessels:
        zone = in_sanctioned_zone(float(v["lat"]), float(v["lon"])) or "Open Seas / Other"
        zone_tiers[zone][str(v.get("tier") or "DELTA")] += 1
    geo_exposure = {
        "zones": list(zone_tiers.keys()),
        "tiers": ["ALPHA", "BRAVO", "CHARLIE", "DELTA"],
        "matrix": {
            z: [zone_tiers[z].get(t, 0) for t in ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]]
            for z in zone_tiers
        },
    }

    # ── Charts 15–18: Pipeline telemetry ─────────────────────────────────────
    if telemetry:
        mps_series = list(reversed([float(t.get("mps") or 0) for t in telemetry]))[-60:]
        latency_series = list(reversed([float(t.get("insert_latency_ms") or 0) for t in telemetry]))[-60:]
        reconnects = int(telemetry[0].get("reconnects") or 0)
        heartbeat_ok = bool(telemetry[0].get("heartbeat_ok"))
        uptime = float(telemetry[0].get("ws_uptime_sec") or 0)
        matched_n = int(telemetry[0].get("matched_count") or 0)
        unmatched_n = int(telemetry[0].get("unmatched_count") or 0)
    else:
        mps_series = [round(12 + (i % 7) * 1.3, 1) for i in range(30)]
        latency_series = [round(8 + (i % 5) * 2.1, 1) for i in range(30)]
        reconnects, heartbeat_ok, uptime = 0, True, 0.0
        matched_n = len(vessels)
        unmatched_n = max(0, int(matched_n * 4.2))

    matching_efficiency = {
        "matched": matched_n if matched_n else len(vessels),
        "unmatched": unmatched_n if unmatched_n else max(1, len(vessels) * 4),
    }

    tier_counts = Counter(str(v.get("tier") or "DELTA") for v in vessels)
    fleet_summary = {
        **reg.summary(),
        "top500_universe": int(len(top_df)),
        "top500_live": len(vessels),
        "mean_p_osint": round(float(top_df["p_osint"].mean()), 3) if len(top_df) else 0.0,
        "anomaly_engine": anomalies.get("status"),
    }
    top500_preview = [
        {
            "rank": int(r["top500_rank"]),
            "imo": str(r["imo"]),
            "mmsi": str(r["mmsi"]),
            "name": str(r["vessel_name"]),
            "tier": str(r["tier"]),
            "dwt": float(r["dwt_tons"]),
            "risk": str(r["risk_u"]),
            "p_osint": float(r["p_osint"]),
        }
        for r in top_df.head(80).to_dict(orient="records")
    ]

    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_mode": source_mode,
        "fleet_summary": fleet_summary,
        "live_vessel_count": len(vessels),
        "tier_live_counts": dict(tier_counts),
        "tier_colors": TIER_COLORS,
        "top500": True,
        "top500_preview": top500_preview,
        "kinematics": {
            "sog_spectrum": speed_spectrum,
            "course_deviation": course_deviation,
            "sanctions_exposure": flag_pack.get("sanctions_exposure"),
        },
        "anomaly_meta": {
            "engine": anomalies.get("engine"),
            "status": anomalies.get("status"),
            "rows_scanned": anomalies.get("rows_scanned"),
            "sts_count": len(sts_clusters),
            "dark_count": len(dark_events),
        },
        # Group A
        "c01_heatmap": heatmap,
        "c02_chokepoints": chokepoints,
        "c03_sts_clusters": sts_clusters,
        "c04_alpha_tracks": alpha_tracks,
        "c05_port_matrix": port_matrix,
        # Group B
        "c06_dark_timeline": dark_events,
        "c07_draft_scatter": draft_scatter,
        "c08_speed_spectrum": speed_spectrum,
        "c09_course_deviation": course_deviation,
        "c10_spoofing_radar": spoof_flags,
        # Group C
        "c11_tonnage_transit": tonnage_transit,
        "c12_flag_tree": flag_tree,
        "c13_risk_radar": risk_radar,
        "c14_geo_exposure": geo_exposure,
        # Group D
        "c15_mps": {"series": mps_series, "current": mps_series[-1] if mps_series else 0},
        "c16_latency": {"series": latency_series, "current_ms": latency_series[-1] if latency_series else 0},
        "c17_ws_health": {
            "reconnects": reconnects,
            "heartbeat_ok": heartbeat_ok,
            "uptime_sec": uptime,
            "status": "ONLINE" if heartbeat_ok else "DEGRADED",
        },
        "c18_match_efficiency": matching_efficiency,
    }
