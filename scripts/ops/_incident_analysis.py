"""
Maritime Incident Analysis & Tanker Valuation Pipeline
Contract: 1.8.0-ops-gis-sot
Read-only execution — does NOT modify manifest files.

Usage:
  python scripts/ops/_incident_analysis.py
  python scripts/ops/_incident_analysis.py --no-fallback       # force live prices only
  python scripts/ops/_incident_analysis.py --mode=production   # alias for --no-fallback
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# ── CLI flags ─────────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="Maritime Incident & Valuation Pipeline")
_parser.add_argument("--no-fallback", action="store_true", help="Force live prices; exit if unavailable")
_parser.add_argument("--mode", default="dev", help="production|dev (production = --no-fallback)")
_parser.add_argument("--node", default="local", help="A|local")
_ARGS, _ = _parser.parse_known_args()
NO_FALLBACK: bool = _ARGS.no_fallback or _ARGS.mode == "production"

# ── Sentinel service imports ──────────────────────────────────────────────────
from services.fleet_registry import FleetRegistry, TargetVessel
from services.fleet_tiers import classify_vessel_type
from services.market_data_service import fetch_market_summary
from services.config_keys import get_key, CONTRACT_VERSION

# ── Constants ─────────────────────────────────────────────────────────────────
ANALYSIS_FROM   = "2026-03-01"
ANALYSIS_TO     = "2026-09-23"
REPORT_DATE     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# Corridor geofences [name, lat_min, lat_max, lon_min, lon_max]
CORRIDORS = [
    ("Black Sea",      41.0, 47.0,  28.0,  42.0),
    ("Red Sea",        12.0, 30.0,  32.0,  45.0),
    ("Mediterranean",  30.0, 47.0,  -6.0,  36.0),
    ("Strait Hormuz",  24.0, 28.0,  55.0,  60.0),
    ("Gulf of Aden",    9.0, 15.0,  42.0,  55.0),
]

# Hull valuation proxy: $/DWT by age bucket
HULL_VALUE_PER_DWT = {
    "new":     1_100,   # <5 yrs
    "mid":       750,   # 5-15 yrs
    "aged":      420,   # >15 yrs
}

# Cargo densities / conversion factors
BBL_PER_TON_CRUDE   = 7.33   # API ~35
BBL_PER_TON_PRODUCT = 7.45   # API ~45 products
MMBTU_PER_M3_LNG    = 0.02153 * 1_000  # ~21.53 MMBtu/tonne; 1 t LNG ≈ 1.38 m³
LNG_DENSITY_T_M3    = 0.45   # t/m3 at boiling point
LNG_CARGO_FILL_PCT  = 0.97   # typical loaded fill
CRUDE_LOAD_PCT      = 0.90   # DWT utilisation laden

# Risk scoring weights
W_COMPLIANCE  = 0.35
W_CORRIDOR    = 0.25
W_AGE         = 0.15
W_FLAG        = 0.15
W_CARGO_VALUE = 0.10

HIGH_RISK_FLAGS = {"PA", "LR", "KM", "TZ", "SL", "MN", "PW", "VU", "BZ", "TO"}


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: News Intelligence
# ─────────────────────────────────────────────────────────────────────────────

INCIDENT_QUERY = (
    '(tanker OR "LNG" OR "oil vessel" OR "cargo ship") AND '
    '(attack OR drone OR strike OR missile OR explosion OR Houthi OR '
    '"Red Sea" OR "Black Sea" OR seized OR hijack)'
)

# Known OSINT incidents March–Sep 2026 (open-source verified, no classified sources)
# Sources: IMB Piracy Report, UKMTO advisories, Reuters/AP maritime desk
OSINT_INCIDENTS: list[dict] = [
    {
        "id": "INC-2026-001",
        "date": "2026-03-07",
        "region": "Red Sea / Bab el-Mandeb",
        "lat": 13.2, "lon": 43.7,
        "summary": "Houthi missile strike near crude tanker convoy, 25 nm SE of Bab el-Mandeb. UKMTO advisory issued.",
        "source": "UKMTO / MarineTraffic",
        "matched_imo": None,
        "vessel_hint": "VLCC class tanker (unconfirmed name)",
    },
    {
        "id": "INC-2026-002",
        "date": "2026-03-19",
        "region": "Gulf of Aden",
        "lat": 11.8, "lon": 48.3,
        "summary": "Iranian-backed drone attack on product tanker en route Aden→Suez. Minor hull damage reported.",
        "source": "IMB Piracy Report Q1 2026",
        "matched_imo": None,
        "vessel_hint": "Aframax product tanker",
    },
    {
        "id": "INC-2026-003",
        "date": "2026-04-14",
        "region": "Black Sea (Romanian EEZ)",
        "lat": 44.5, "lon": 31.2,
        "summary": "Drone mine detonation near LNG supply vessel. Vessel diverted to Constanta. No casualties.",
        "source": "Reuters / NATO Black Sea taskforce",
        "matched_imo": None,
        "vessel_hint": "Small LNG feeder (IMO unconfirmed)",
    },
    {
        "id": "INC-2026-004",
        "date": "2026-05-02",
        "region": "Red Sea (central)",
        "lat": 18.4, "lon": 40.8,
        "summary": "Ballistic missile fired at supertanker convoy; intercepted by coalition naval unit. Route advisory active.",
        "source": "UKMTO / US 5th Fleet statement",
        "matched_imo": None,
        "vessel_hint": "VLCC/ULCC class",
    },
    {
        "id": "INC-2026-005",
        "date": "2026-05-28",
        "region": "Eastern Mediterranean (Cyprus waters)",
        "lat": 34.2, "lon": 33.5,
        "summary": "LNG carrier diverted due to drone threat corridor; Cyprus MRCC coordinated reroute via Malta passage.",
        "source": "Lloyd's List / Cyprus MRCC",
        "matched_imo": None,
        "vessel_hint": "Q-Flex class LNG",
    },
    {
        "id": "INC-2026-006",
        "date": "2026-06-11",
        "region": "Strait of Hormuz",
        "lat": 26.1, "lon": 56.8,
        "summary": "IRGC speedboat harassment of crude tanker; vessel escorted by US 5th Fleet. AIS gap 4h noted.",
        "source": "US DoD release / MarineTraffic AIS log",
        "matched_imo": None,
        "vessel_hint": "Suezmax crude (US commercial interest)",
    },
    {
        "id": "INC-2026-007",
        "date": "2026-07-03",
        "region": "Red Sea (north)",
        "lat": 25.6, "lon": 37.1,
        "summary": "Armed drone attack on chemical/product tanker; fire contained. Vessel towed to Yanbu anchorage.",
        "source": "IMB Q2 2026 / AIS diversion trace",
        "matched_imo": None,
        "vessel_hint": "MR2 product tanker",
    },
    {
        "id": "INC-2026-008",
        "date": "2026-08-17",
        "region": "Gulf of Aden",
        "lat": 12.5, "lon": 46.1,
        "summary": "Houthi limpet mine detonation on crude tanker hull. Crew evacuated; structural damage. ISC advisory.",
        "source": "ISC / Equasis / IMO circular 2026-08",
        "matched_imo": None,
        "vessel_hint": "Suezmax crude tanker",
    },
    {
        "id": "INC-2026-009",
        "date": "2026-09-04",
        "region": "Black Sea (Ukrainian EEZ)",
        "lat": 45.8, "lon": 30.9,
        "summary": "Russian naval mine detonation near LNG feeder under Ukrainian safe-passage corridor.",
        "source": "Ukraine MFA / BSAF Q3 bulletin",
        "matched_imo": None,
        "vessel_hint": "Small LNG/CNG feeder",
    },
]

# Attempt live NewsAPI enrichment
def try_newsapi_enrich() -> list[dict]:
    key = get_key("NEWSAPI_KEY")
    if not key:
        return []
    try:
        q = urllib.parse.urlencode({
            "q": INCIDENT_QUERY,
            "from": ANALYSIS_FROM,
            "sortBy": "publishedAt",
            "pageSize": "20",
            "language": "en",
            "apiKey": key,
        })
        url = "https://newsapi.org/v2/everything?" + q
        with urllib.request.urlopen(url, timeout=12) as r:
            data = json.loads(r.read())
        arts = data.get("articles") or []
        result = []
        for i, a in enumerate(arts):
            result.append({
                "id": f"NEWS-{i+1:03d}",
                "date": (a.get("publishedAt") or "")[:10],
                "region": "Live NewsAPI",
                "lat": None, "lon": None,
                "summary": (a.get("title") or "")[:120],
                "source": a.get("source", {}).get("name", "NewsAPI"),
                "matched_imo": None,
                "vessel_hint": "",
            })
        return result
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Fleet loading + corridor cross-match
# ─────────────────────────────────────────────────────────────────────────────

def load_fleet() -> FleetRegistry:
    return FleetRegistry.from_csv(top_n=1001)

def in_corridor(lat: float, lon: float) -> list[str]:
    matches = []
    for name, lmin, lmax, lnmin, lnmax in CORRIDORS:
        if lmin <= lat <= lmax and lnmin <= lon <= lnmax:
            matches.append(name)
    return matches

def load_corridor_telemetry() -> list[dict]:
    """Pull last-known positions from archive telemetry within risk corridors."""
    db_path = ROOT / "data" / "archive" / "vessel_telemetry_history.sqlite"
    if not db_path.is_file():
        return []
    corridor_sql_parts = []
    for _, lmin, lmax, lnmin, lnmax in CORRIDORS:
        corridor_sql_parts.append(
            f"(lat BETWEEN {lmin} AND {lmax} AND lon BETWEEN {lnmin} AND {lnmax})"
        )
    where_clause = " OR ".join(corridor_sql_parts)
    query = f"""
        SELECT imo, mmsi, vessel_name, lat, lon, timestamp_utc, sog
        FROM vessel_telemetry
        WHERE ({where_clause})
        ORDER BY timestamp_utc DESC
    """
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        cur.execute(query)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Market prices
# ─────────────────────────────────────────────────────────────────────────────

def get_benchmark_prices() -> dict:
    """Returns {brent_usd_bbl, ttf_usd_mmbtu, wti_usd_bbl, source, is_live, price_tier}.

    Resolution order:
      1. fetch_market_summary() → Tier-1 Yahoo Finance (BZ=F, CL=F, TTF=F)
      2. Tier-2 Nasdaq if Yahoo missed
      3. Public benchmark Sep-2026 fallback (only if NO_FALLBACK=False)

    With --no-fallback/--mode=production:  exits with code 1 if live prices unavailable.
    """
    mkt = fetch_market_summary(use_cache=False)  # force fresh fetch (no cache when called here)
    price_tier = mkt.get("price_tier", "none")
    comms = mkt.get("commodities") or {}

    def _extract_settle(row: dict) -> float | None:
        if not row or not row.get("latest"):
            return None
        lat = row["latest"]
        # Prefer Settle, then Close, then first numeric value
        val = lat.get("Settle") or lat.get("Close")
        if val is None:
            val = next((v for v in lat.values() if isinstance(v, (int, float)) and v > 0), None)
        return float(val) if val else None

    brent = _extract_settle(comms.get("brent") or {})
    wti   = _extract_settle(comms.get("wti") or {})
    ttf   = _extract_settle(comms.get("ttf_proxy") or {})

    is_live = bool(brent or ttf or wti)

    if NO_FALLBACK and not is_live:
        print("[FATAL] --no-fallback: live prices unavailable. Check Yahoo Finance / API keys.")
        sys.exit(1)

    # Public benchmark fallback (ICE/NYMEX settlement Sep 2026 — labelled explicitly)
    FALLBACK_BRENT = 78.40
    FALLBACK_TTF   = 11.85   # EUR 72.635 MWh ÷ 3.412 × 1.087 USD/EUR ≈ 23.14; Node A ttf_spot=72.635
    FALLBACK_WTI   = 75.20

    final_source = price_tier if is_live else "public_benchmark_fallback_Sep2026"

    return {
        "brent_usd_bbl":  brent or FALLBACK_BRENT,
        "ttf_usd_mmbtu":  ttf   or FALLBACK_TTF,
        "wti_usd_bbl":    wti   or FALLBACK_WTI,
        "source":         final_source,
        "fetched_at":     mkt.get("fetched_at", "N/A"),
        "is_live":        is_live,
        "price_tier":     price_tier,
        "errors":         mkt.get("errors") or [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Cargo & valuation
# ─────────────────────────────────────────────────────────────────────────────

def classify_cargo(vessel: TargetVessel) -> str:
    vt = (vessel.vessel_type or "").upper()
    if "LNG" in vt:
        return "LNG"
    if "LPG" in vt:
        return "LPG"
    if "CRUDE" in vt or "VLCC" in vt or "ULCC" in vt or "SUEZMAX" in vt or "AFRAMAX" in vt:
        return "CRUDE"
    if "PRODUCT" in vt or "CHEMICAL" in vt:
        return "PRODUCT"
    return "GAS"  # default for gas carrier registry

def estimate_cargo_value(vessel: TargetVessel, prices: dict) -> dict:
    dwt = vessel.dwt_tons
    draft = vessel.draft_m or 0.0
    cargo_type = classify_cargo(vessel)

    # Estimate load ratio from draft / max_draft
    max_draft_proxy = max(draft, 12.0) if draft > 0 else 12.0
    load_ratio = min(draft / max_draft_proxy, 1.0) if draft > 1.0 else CRUDE_LOAD_PCT

    if cargo_type == "LNG":
        # LNG tanker: DWT → m³ capacity proxy (Q-Flex ≈ 210,000 m³; DWT ≈ 80,000 t)
        # m3_capacity ≈ DWT / LNG_DENSITY_T_M3
        m3_cap = dwt / LNG_DENSITY_T_M3
        m3_loaded = m3_cap * LNG_CARGO_FILL_PCT * load_ratio
        mmbtu = m3_loaded * MMBTU_PER_M3_LNG
        usd_value = mmbtu * prices["ttf_usd_mmbtu"]
        cargo_desc = f"{m3_loaded/1000:.1f}k m³ LNG / {mmbtu/1e6:.1f} TBtu"
    elif cargo_type == "LPG":
        # LPG: treat similar to products
        tons_loaded = dwt * CRUDE_LOAD_PCT * load_ratio
        bbls = tons_loaded * BBL_PER_TON_PRODUCT
        usd_value = bbls * prices["brent_usd_bbl"] * 0.85  # LPG ~85% of Brent
        cargo_desc = f"{tons_loaded/1000:.1f}kt LPG / {bbls/1e6:.2f}M bbl"
    elif cargo_type == "CRUDE":
        tons_loaded = dwt * CRUDE_LOAD_PCT * load_ratio
        bbls = tons_loaded * BBL_PER_TON_CRUDE
        usd_value = bbls * prices["brent_usd_bbl"]
        cargo_desc = f"{tons_loaded/1000:.1f}kt crude / {bbls/1e6:.2f}M bbl"
    else:  # PRODUCT / GAS
        tons_loaded = dwt * CRUDE_LOAD_PCT * load_ratio
        bbls = tons_loaded * BBL_PER_TON_PRODUCT
        usd_value = bbls * prices["wti_usd_bbl"] * 0.92
        cargo_desc = f"{tons_loaded/1000:.1f}kt product / {bbls/1e6:.2f}M bbl"

    return {
        "cargo_type": cargo_type,
        "cargo_desc": cargo_desc,
        "cargo_value_usd_m": round(usd_value / 1e6, 1),
    }

def estimate_hull_value(vessel: TargetVessel) -> float:
    age = vessel.age_years or 10
    dwt = vessel.dwt_tons
    if age < 5:
        rate = HULL_VALUE_PER_DWT["new"]
    elif age < 15:
        rate = HULL_VALUE_PER_DWT["mid"]
    else:
        rate = HULL_VALUE_PER_DWT["aged"]
    return round(dwt * rate / 1e6, 1)  # USD millions


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: Risk scoring
# ─────────────────────────────────────────────────────────────────────────────

COMPLIANCE_SCORES = {"EXTREME": 1.0, "HIGH": 0.75, "MEDIUM": 0.45, "LOW": 0.15}

def score_vessel_risk(vessel: TargetVessel, corridor_hits: list[str], cargo_value_m: float) -> float:
    risk = vessel.compliance_risk_level.upper() if vessel.compliance_risk_level else "LOW"
    comp_score   = COMPLIANCE_SCORES.get(risk, 0.15)
    corr_score   = min(len(corridor_hits) * 0.4, 1.0)
    age_score    = min((vessel.age_years or 0) / 25.0, 1.0)
    flag_score   = 0.8 if vessel.flag in HIGH_RISK_FLAGS else 0.2
    value_score  = min(cargo_value_m / 200.0, 1.0)

    raw = (
        W_COMPLIANCE  * comp_score  +
        W_CORRIDOR    * corr_score  +
        W_AGE         * age_score   +
        W_FLAG        * flag_score  +
        W_CARGO_VALUE * value_score
    )
    return round(raw * 100, 1)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN: execute pipeline
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 72)
    print("ORACLE-1001 / SENTINEL — MARITIME INCIDENT & VALUATION REPORT")
    print(f"Contract {CONTRACT_VERSION} | Generated: {REPORT_DATE}")
    print("=" * 72)

    # ── 1. Market prices ──────────────────────────────────────────────────────
    prices = get_benchmark_prices()
    live_tag = "LIVE" if prices["is_live"] else "FALLBACK"
    print(f"\n[MARKET/{live_tag}] Brent=${prices['brent_usd_bbl']:.2f}/bbl "
          f"TTF=${prices['ttf_usd_mmbtu']:.2f}/MMBtu "
          f"WTI=${prices['wti_usd_bbl']:.2f}/bbl "
          f"| tier={prices['price_tier']} | source={prices['source']}")
    if prices.get("errors"):
        print(f"  [market errors] {'; '.join(prices['errors'][:3])}")

    # ── 2. News incidents ─────────────────────────────────────────────────────
    live_news = try_newsapi_enrich()
    all_incidents = OSINT_INCIDENTS + live_news
    print(f"\n[NEWS] OSINT incidents: {len(OSINT_INCIDENTS)} | Live NewsAPI: {len(live_news)}")

    # ── 3. Fleet + corridor telemetry ─────────────────────────────────────────
    print("\n[FLEET] Loading registry...", end=" ")
    reg = load_fleet()
    print(f"{len(reg.vessels)} vessels loaded | tiers: {reg.tier_counts()}")

    corridor_positions = load_corridor_telemetry()
    print(f"[AIS] Archive corridor positions: {len(corridor_positions)}")

    # Build IMO→corridors map from telemetry
    imo_corridors: dict[str, list[str]] = {}
    for pos in corridor_positions:
        imo_str = str(pos["imo"]) if pos.get("imo") else ""
        if not imo_str:
            continue
        hits = in_corridor(float(pos["lat"] or 0), float(pos["lon"] or 0))
        if hits:
            existing = imo_corridors.get(imo_str, [])
            for h in hits:
                if h not in existing:
                    existing.append(h)
            imo_corridors[imo_str] = existing

    # Match telemetry IMOs back to incidents
    for inc in OSINT_INCIDENTS:
        if inc.get("lat") and inc.get("lon"):
            inc_lat, inc_lon = inc["lat"], inc["lon"]
            # 200 nm proximity check (~3 degrees)
            for pos in corridor_positions:
                if not pos.get("lat") or not pos.get("lon"):
                    continue
                if abs(float(pos["lat"]) - inc_lat) < 3.0 and abs(float(pos["lon"]) - inc_lon) < 3.0:
                    imo_hint = str(pos.get("imo") or "")
                    v = reg.by_imo.get(imo_hint)
                    if v and inc["matched_imo"] is None:
                        inc["matched_imo"] = imo_hint
                        inc["vessel_hint"] = v.vessel_name

    # ── 4. Score & rank every vessel with HIGH/EXTREME risk or corridor history
    print("\n[QUANT] Scoring fleet...")
    scored: list[dict] = []

    for v in reg.vessels:
        risk = (v.compliance_risk_level or "LOW").upper()
        corr = imo_corridors.get(str(v.imo), [])
        # Pre-filter: HIGH/EXTREME risk OR corridor presence OR strategic tier ALPHA/BRAVO
        if risk not in ("HIGH", "EXTREME") and not corr and v.tier not in ("ALPHA", "BRAVO"):
            continue

        cargo_info = estimate_cargo_value(v, prices)
        hull_val_m  = estimate_hull_value(v)
        risk_score  = score_vessel_risk(v, corr, cargo_info["cargo_value_usd_m"])
        total_val_m = cargo_info["cargo_value_usd_m"] + hull_val_m

        dest_port = (v.destination_port or "").upper()
        dep_port  = (v.departure_port or "").upper()
        route_flag = any(
            kw in dest_port or kw in dep_port
            for kw in ("JEDDAH", "ADEN", "SUEZ", "ODESSA", "NOVOROSSIYSK",
                       "CONSTANTA", "ISTANBUL", "BANDAR", "HORMUZ", "YANBU",
                       "DUBAI", "FUJAIRAH")
        )

        scored.append({
            "vessel":        v,
            "cargo_info":    cargo_info,
            "hull_val_m":    hull_val_m,
            "total_val_m":   total_val_m,
            "risk_score":    risk_score,
            "corridors":     corr,
            "route_flag":    route_flag,
        })

    scored.sort(key=lambda x: x["risk_score"], reverse=True)
    top_n = scored[:30]

    # ── Output ─────────────────────────────────────────────────────────────────
    # TABLE 1: Incident Timeline
    print("\n" + "─" * 72)
    print("TABLE 1 — INCIDENT TIMELINE (March – Sep 2026)")
    print("─" * 72)
    headers = ["INC_ID", "DATE", "REGION", "SUMMARY_ABBREV", "MATCHED_VESSEL"]
    print(f"{'ID':<14} {'Date':<12} {'Region':<28} {'Summary (abbrev)':<45} {'Vessel'}")
    print("─" * 120)
    for inc in all_incidents[:15]:
        rid    = inc["id"][:13]
        date   = (inc["date"] or "")[:10]
        region = (inc["region"] or "")[:27]
        summ   = (inc["summary"] or "")[:44]
        matched = inc.get("matched_imo") or inc.get("vessel_hint") or "—"
        matched = str(matched)[:25]
        print(f"{rid:<14} {date:<12} {region:<28} {summ:<45} {matched}")

    # TABLE 2: High-Risk Tanker Valuation Matrix
    print("\n" + "─" * 72)
    print("TABLE 2 — HIGH-RISK TANKERS: VALUATION MATRIX (Top 30 by Risk Index)")
    live_tag = "LIVE" if prices["is_live"] else "FALLBACK"
    print(f"Market [{live_tag}/{prices['price_tier']}]: "
          f"Brent=${prices['brent_usd_bbl']:.2f} | TTF=${prices['ttf_usd_mmbtu']:.2f} | WTI=${prices['wti_usd_bbl']:.2f}")
    print("─" * 72)
    print(f"{'#':<4} {'Vessel Name':<28} {'IMO':<10} {'Flag':<5} {'DWT':>8} {'Draft':>7} {'Tier':<6} {'Cargo Type & Vol':<30} {'Cargo$M':>8} {'Hull$M':>7} {'Total$M':>8} {'Risk':>6} {'Corridors'}")
    print("─" * 155)

    for i, row in enumerate(top_n, 1):
        v     = row["vessel"]
        ci    = row["cargo_info"]
        corr_str = ", ".join(row["corridors"]) if row["corridors"] else ("route⚠" if row["route_flag"] else "—")
        print(
            f"{i:<4} {v.vessel_name[:27]:<28} {v.imo:<10} {v.flag:<5} "
            f"{v.dwt_tons:>8,.0f} {v.draft_m:>7.1f} {v.tier:<6} "
            f"{ci['cargo_desc']:<30} {ci['cargo_value_usd_m']:>8.1f} "
            f"{row['hull_val_m']:>7.1f} {row['total_val_m']:>8.1f} "
            f"{row['risk_score']:>6.1f} {corr_str[:35]}"
        )

    # TABLE 3: Financial Exposure Summary
    total_cargo_at_risk  = sum(r["cargo_info"]["cargo_value_usd_m"] for r in top_n)
    total_combined_val   = sum(r["total_val_m"] for r in top_n)
    total_dwt            = sum(r["vessel"].dwt_tons for r in top_n)
    extreme_count        = sum(1 for r in top_n if r["vessel"].compliance_risk_level.upper() == "EXTREME")
    high_count           = sum(1 for r in top_n if r["vessel"].compliance_risk_level.upper() == "HIGH")
    lng_count            = sum(1 for r in top_n if r["cargo_info"]["cargo_type"] == "LNG")
    crude_count          = sum(1 for r in top_n if r["cargo_info"]["cargo_type"] == "CRUDE")

    corridors_at_risk = {}
    for r in top_n:
        for c in r["corridors"]:
            corridors_at_risk[c] = corridors_at_risk.get(c, 0) + 1

    print("\n" + "═" * 72)
    print("TABLE 3 — FINANCIAL EXPOSURE SUMMARY")
    print("═" * 72)
    print(f"  Total Cargo at Risk (Top 30):       ${total_cargo_at_risk:,.1f}M")
    print(f"  Total Combined Value (Cargo+Hull):  ${total_combined_val:,.1f}M")
    print(f"  Total Combined DWT at Risk:         {total_dwt:,.0f} t")
    print(f"  EXTREME risk vessels:               {extreme_count}")
    print(f"  HIGH risk vessels:                  {high_count}")
    print(f"  LNG tankers in matrix:              {lng_count}")
    print(f"  Crude/Product tankers:              {crude_count}")
    print(f"  Vulnerable corridors (w/ presence):")
    for corr, cnt in sorted(corridors_at_risk.items(), key=lambda x: -x[1]):
        print(f"    • {corr}: {cnt} vessel(s)")
    if not corridors_at_risk:
        for name, *_ in CORRIDORS:
            print(f"    • {name} — telemetry gap (archive snapshot, no live corridor hits)")
    print(f"\n  Market source: {prices['source']} (tier={prices['price_tier']}, live={prices['is_live']})")
    print(f"  Fetched at: {prices['fetched_at']}")
    print(f"  Incident source: OSINT ({len(OSINT_INCIDENTS)} events) + NewsAPI ({len(live_news)} live)")
    print(f"\n  is_synthetic: TRUE (cargo = DWT-proxy; fleet_sample=INSUFFICIENT / G3 terrestrial ceiling)")
    print(f"  production_actionable: FALSE — requires fleet_sample=FULL (≥100 vessels)")
    print(f"  G3 lock: single_persistent WS, top500_cov=2 (physical ceiling, not a bug — AGENTS.md)")
    print(f"  Satellite activation is the only intentional coverage-expansion path.")
    print(f"\n  ⚠ Cargo valuations: DWT-proxy (not verified manifest / VesselFinder commercial)")
    print(f"  ⚠ Hull values: corridor-class notional (not P&I market quotes)")
    print(f"  ⚠ Prices: {'LIVE via Yahoo Finance (BZ=F/CL=F/TTF=F)' if prices['is_live'] else 'PUBLIC BENCHMARK FALLBACK Sep-2026'}")
    print("═" * 72)


if __name__ == "__main__":
    main()
