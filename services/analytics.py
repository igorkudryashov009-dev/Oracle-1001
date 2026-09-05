"""
TOP-500 OSINT & kinematics engine for Sentinel dual-plane analytics.

Single source of truth for:
  - TOP-500 fleet selection (risk × Alpha/Bravo priority × DWT × P_osint)
  - Real-time kinematics (SOG spectrum, draught load %, course Δθ)
  - Live STS / Dark AIS anomaly cards (wired from detect_ais_anomalies)
"""

from __future__ import annotations

import importlib.util
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

SOG_BINS = (0, 2, 8, 12, 16, 20, 999)
SOG_LABELS = ("0-2", "2-8", "8-12", "12-16", "16-20", "20+")
COURSE_DEV_THRESHOLD_DEG = 35.0
STS_MIN_DURATION_MIN = 30.0
TOP_N = 500

RISK_SCORE = {"EXTREME": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}
TIER_PRIORITY = {"ALPHA": 4, "BRAVO": 3, "CHARLIE": 2, "DELTA": 1}


def _load_anomaly_mod():
    path = ROOT / "scripts" / "detect_ais_anomalies.py"
    spec = importlib.util.spec_from_file_location("detect_ais_anomalies", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load anomaly module: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["detect_ais_anomalies"] = mod
    spec.loader.exec_module(mod)
    return mod


def estimate_p_osint(row: pd.Series) -> float:
    """OSINT provenance mass in [0, 1]. Synth / imputed / mock reduce confidence."""
    conf = row.get("source_confidence")
    try:
        if conf is not None and str(conf).strip() and str(conf).lower() != "nan":
            v = float(conf)
            if v > 1.0:
                v = v / 100.0
            return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        pass
    synth = str(row.get("synthetic_fields") or "")
    imputed = str(row.get("imputed_fields") or "")
    mock = str(row.get("registry_mock_fields") or "")
    penalty = 0.0
    for blob, w in ((synth, 0.35), (imputed, 0.25), (mock, 0.20)):
        if blob and blob.lower() not in ("", "nan", "[]", "{}"):
            n = max(1, blob.count(",") + 1)
            penalty += min(w, 0.05 * n)
    return round(max(0.05, 1.0 - penalty), 3)


def select_top500_fleet(csv_path: Path | None = None, top_n: int = TOP_N) -> pd.DataFrame:
    """Rank fleet_database.csv → TOP-N by risk, tier priority, DWT, P_osint."""
    path = csv_path or (ROOT / "output" / "fleet_database.csv")
    df = pd.read_csv(path, low_memory=False)
    if "vessel_category" in df.columns:
        df = df[df["vessel_category"].astype(str).str.lower() == "vessel"].copy()
    df["dwt_tons"] = pd.to_numeric(df.get("dwt_tons"), errors="coerce").fillna(0.0)
    df["draft_m"] = pd.to_numeric(df.get("draft_m"), errors="coerce").fillna(0.0)
    df["risk_u"] = df.get("compliance_risk_level", "LOW").astype(str).str.upper()
    df["risk_score"] = df["risk_u"].map(lambda x: RISK_SCORE.get(x, 0))
    # Provisional DWT rank → NATO tier (same cutoffs as fleet_registry)
    df = df.sort_values("dwt_tons", ascending=False).reset_index(drop=True)
    df["dwt_rank"] = df.index + 1

    def _tier(rank: int, risk: str) -> str:
        from services.fleet_registry import assign_strategic_tier

        return assign_strategic_tier(int(rank), risk)

    df["tier"] = [_tier(int(r), str(risk)) for r, risk in zip(df["dwt_rank"], df["risk_u"])]
    df["tier_pri"] = df["tier"].map(lambda t: TIER_PRIORITY.get(str(t), 0))
    df["p_osint"] = df.apply(estimate_p_osint, axis=1)
    df = df.sort_values(
        by=["risk_score", "tier_pri", "dwt_tons", "p_osint"],
        ascending=[False, False, False, False],
    ).head(int(top_n)).reset_index(drop=True)
    df["top500_rank"] = df.index + 1
    return df


def sog_spectrum(vessels: list[dict[str, Any]]) -> dict[str, Any]:
    counts = [0] * len(SOG_LABELS)
    for v in vessels:
        s = float(v.get("sog") or 0)
        for i in range(len(SOG_LABELS)):
            if SOG_BINS[i] <= s < SOG_BINS[i + 1]:
                counts[i] += 1
                break
    return {"labels": list(SOG_LABELS), "counts": counts}


def draught_load_estimator(vessels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cargo load % ≈ draft / design_draft (fallback 0.85 × beam-scaled proxy)."""
    out = []
    for v in vessels:
        draft = float(v.get("draft_m") or 0)
        if draft <= 0:
            continue
        # Max design draft proxy: registry draft if larger, else 18 m VLCC-ish clamp
        design = float(v.get("design_draft_m") or 0) or max(draft / 0.85, draft + 2.0)
        design = min(max(design, draft), 25.0)
        load_pct = round(min(100.0, 100.0 * draft / design), 1)
        out.append({
            "imo": v.get("imo"),
            "name": v.get("vessel_name"),
            "draft_m": draft,
            "dwt": float(v.get("dwt_tons") or 0),
            "sog": float(v.get("sog") or 0),
            "tier": v.get("tier"),
            "load_index": load_pct,
            "design_draft_m": round(design, 2),
        })
    return out[:400]


def course_deviation_index(vessels: list[dict[str, Any]]) -> dict[str, Any]:
    """Δθ = |COG − heading|; anomaly when > 35° while underway (SOG ≥ 2)."""
    anomalies = []
    cog_vals = []
    for v in vessels:
        cog = v.get("cog")
        heading = v.get("heading")
        sog = float(v.get("sog") or 0)
        if cog is None:
            continue
        try:
            c = float(cog)
        except (TypeError, ValueError):
            continue
        cog_vals.append(c)
        if heading is None or sog < 2.0:
            continue
        try:
            h = float(heading)
        except (TypeError, ValueError):
            continue
        # AIS often uses 511 / 360 as "not available"
        if h >= 360 or c >= 360:
            continue
        dtheta = abs(c - h)
        dtheta = min(dtheta, 360.0 - dtheta)
        if dtheta > COURSE_DEV_THRESHOLD_DEG:
            anomalies.append({
                "imo": v.get("imo"),
                "name": v.get("vessel_name"),
                "tier": v.get("tier"),
                "delta_theta_deg": round(dtheta, 1),
                "cog": round(c, 1),
                "heading": round(h, 1),
                "sog": sog,
                "nav_status": v.get("nav_status"),
            })
    anomalies.sort(key=lambda x: -x["delta_theta_deg"])
    if cog_vals:
        mean_cog = sum(cog_vals) / len(cog_vals)
        var = sum((c - mean_cog) ** 2 for c in cog_vals) / len(cog_vals)
        score = round(min(100.0, math.sqrt(var) / 1.8), 1)
    else:
        mean_cog, score = 0.0, 0.0
    return {
        "score": score,
        "mean_cog": round(mean_cog, 1),
        "threshold_deg": COURSE_DEV_THRESHOLD_DEG,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies[:40],
    }


def flag_sanctions_matrix(vessels: list[dict[str, Any]]) -> dict[str, Any]:
    flag_dwt: Counter[str] = Counter()
    sanctions_by_flag: dict[str, Counter[str]] = defaultdict(Counter)
    for v in vessels:
        flag = str(v.get("flag") or "Unknown")[:40]
        flag_dwt[flag] += float(v.get("dwt_tons") or 0)
        tags = str(v.get("sanctions_tags") or "NONE").upper() or "NONE"
        primary = tags.split(",")[0].strip() or "NONE"
        sanctions_by_flag[flag][primary] += 1
    tree = [{"name": k, "value": round(val, 0)} for k, val in flag_dwt.most_common(16)]
    exposure = [
        {
            "flag": fl,
            "tags": dict(sanctions_by_flag[fl]),
            "dwt": round(flag_dwt[fl], 0),
        }
        for fl, _ in flag_dwt.most_common(12)
    ]
    return {"flag_tree": tree, "sanctions_exposure": exposure}


def build_live_anomaly_cards(
    *,
    lookback_hours: int = 24,
    sts_min_duration_min: float = STS_MIN_DURATION_MIN,
) -> dict[str, Any]:
    """
    Embed detect_ais_anomalies into dashboard cards.
    Card STS: pairs <0.5 nm, SOG <1.0, duration >30 min.
    Card Dark: Alpha/Bravo gaps >4h reappearing in critical zones.
    """
    mod = _load_anomaly_mod()
    try:
        db = mod.resolve_db()
    except FileNotFoundError:
        return {
            "sts_clusters": [],
            "dark_timeline": [],
            "engine": "detect_ais_anomalies",
            "status": "no_db",
        }

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        mx = con.execute("SELECT MAX(timestamp_utc) FROM ais_positions").fetchone()[0]
        if not mx:
            return {"sts_clusters": [], "dark_timeline": [], "engine": "detect_ais_anomalies", "status": "empty"}
        window_end = mod.parse_ts(mx)
        window_start = window_end - timedelta(hours=lookback_hours)
        meta = mod.load_registry_meta()
        rows = mod.fetch_registry_positions(con, window_start, ("ALPHA", "BRAVO", "CHARLIE", "DELTA"))
        rows = [mod.enrich(r, meta) for r in rows]
    except sqlite3.DatabaseError as exc:
        return {
            "sts_clusters": [],
            "dark_timeline": [],
            "engine": "detect_ais_anomalies",
            "status": f"db_error:{exc}",
        }
    finally:
        con.close()

    sts_raw = mod.detect_sts(rows)
    sts_clusters = []
    for c in sts_raw:
        dur_min = (c["last_contact_utc"] - c["first_contact_utc"]).total_seconds() / 60.0
        # Accept sustained contacts OR multi-sample proxies when window < 30 min total
        if dur_min < sts_min_duration_min and c.get("samples", 0) < 3:
            continue
        a, b = c["vessel_a"], c["vessel_b"]
        sts_clusters.append({
            "n": 2,
            "lat": c["lat"],
            "lon": c["lon"],
            "min_dist_nm": c["min_dist_nm"],
            "duration_min": round(dur_min, 1),
            "samples": c["samples"],
            "zone": c.get("zone"),
            "vessels": [
                a.get("vessel_name") or a.get("imo"),
                b.get("vessel_name") or b.get("imo"),
            ],
            "imos": [a.get("imo"), b.get("imo")],
            "tiers": [a.get("tier"), b.get("tier")],
            "mmsis": [a.get("mmsi"), b.get("mmsi")],
            "pair": {
                "a": {
                    "imo": a.get("imo"),
                    "name": a.get("vessel_name"),
                    "tier": a.get("tier"),
                    "sog": a.get("sog"),
                },
                "b": {
                    "imo": b.get("imo"),
                    "name": b.get("vessel_name"),
                    "tier": b.get("tier"),
                    "sog": b.get("sog"),
                },
            },
            "window_utc": {
                "first": c["first_contact_utc"].isoformat().replace("+00:00", "Z"),
                "last": c["last_contact_utc"].isoformat().replace("+00:00", "Z"),
            },
        })

    dark_raw = mod.detect_dark(rows)
    dark_timeline = [
        {
            "imo": e.get("imo"),
            "name": e.get("vessel_name"),
            "tier": e.get("tier"),
            "gap_hours": e.get("gap_hours"),
            "lat": e.get("reacq_lat"),
            "lon": e.get("reacq_lon"),
            "loss_lat": e.get("loss_lat"),
            "loss_lon": e.get("loss_lon"),
            "last_contact_utc": e.get("last_contact_utc"),
            "reacq_utc": e.get("reacq_utc"),
            "zone": e.get("reacq_zone"),
        }
        for e in dark_raw
    ]

    return {
        "sts_clusters": sts_clusters,
        "dark_timeline": dark_timeline,
        "engine": "detect_ais_anomalies",
        "status": "ok",
        "rows_scanned": len(rows),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def filter_vessels_to_top500(
    vessels: list[dict[str, Any]], top_df: Optional[pd.DataFrame] = None
) -> list[dict[str, Any]]:
    """Keep only live positions whose IMO/MMSI is in the TOP-500 set."""
    df = top_df if top_df is not None else select_top500_fleet()
    imos = {str(x) for x in df["imo"].astype(str).tolist() if x and x != "nan"}
    mmsis = {str(x) for x in df["mmsi"].astype(str).tolist() if x and x != "nan"}
    meta_by_mmsi: dict[str, Any] = {}
    meta_by_imo: dict[str, Any] = {}
    for _, row in df.iterrows():
        m = str(row.get("mmsi") or "")
        i = str(row.get("imo") or "")
        if m and m != "nan":
            meta_by_mmsi[m] = row
        if i and i != "nan":
            meta_by_imo[i] = row
    out = []
    for v in vessels:
        mmsi = str(v.get("mmsi") or "")
        imo = str(v.get("imo") or "")
        if mmsi not in mmsis and imo not in imos:
            continue
        row = meta_by_mmsi.get(mmsi)
        if row is None:
            row = meta_by_imo.get(imo)
        if row is not None:
            v = dict(v)
            v["tier"] = v.get("tier") or str(row.get("tier"))
            v["dwt_tons"] = v.get("dwt_tons") or float(row.get("dwt_tons") or 0)
            v["flag"] = v.get("flag") or str(row.get("flag") or "")
            v["sanctions_tags"] = v.get("sanctions_tags") or str(row.get("sanctions_tags") or "")
            v["p_osint"] = float(row.get("p_osint") or 0)
            v["top500_rank"] = int(row.get("top500_rank") or 0)
            v["risk"] = str(row.get("risk_u") or row.get("compliance_risk_level") or "")
            if not v.get("vessel_name"):
                v["vessel_name"] = str(row.get("vessel_name") or "")
        out.append(v)
    return out
