"""
AIS archive → laden/ballast features + DWT-flow timeseries (Kpler/Vortexa-style proxy).

Inputs (offline only):
  история1/by_vessel/{imo}.csv   — daily vessel snapshots (lat/lon/timestamp[/draft])
  output/fleet_database.csv      — static dwt_tons, vessel_type, draft_m
  targets.json                   — tracked MMSI universe (~2711)

Outputs:
  features/dwt_flow_timeseries.parquet
  features/vessel_state_transitions.csv

CRITICAL: every DWT-flow index row carries coverage metadata. An index without
sample-coverage context is misleading and must not be emitted bare.

Honest limits:
  - Current daily_snapshot rows do NOT yet include time-varying AIS draught;
    draft may come from fleet_database.draft_m (static OSINT) → low_confidence.
  - If by_vessel is empty or most vessels have < MIN_DAYS_FOR_STABLE history,
    the pipeline prints an explicit insufficiency warning and flags outputs.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from distance_calc import haversine_km

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "история1"
BY_VESSEL_DIR = HISTORY / "by_vessel"
FLEET_PATH = ROOT / "output" / "fleet_database.csv"
TARGETS_PATH = ROOT / "targets.json"
FEATURES_DIR = ROOT / "features"
OUT_TIMESERIES = FEATURES_DIR / "dwt_flow_timeseries.parquet"
OUT_TRANSITIONS = FEATURES_DIR / "vessel_state_transitions.csv"

# Classification thresholds (draft_ratio = current / max_known)
LADEN_THRESHOLD = 0.85
BALLAST_THRESHOLD = 0.60
STATE_LADEN = "laden"
STATE_BALLAST = "ballast"
STATE_UNCERTAIN = "transitional/uncertain"

MIN_DAYS_FOR_STABLE = 7
MIN_DRAFT_OBS_FOR_ARCHIVE_MAX = 7
MIN_DRAFT_RANGE_FRAC = 0.05  # need some variation to trust archive max
NEAREST_PORT_MAX_KM = 250.0

INSUFFICIENT_DATA_MSG = (
    "недостаточно данных для устойчивой feature engineering"
)

# Offline port gazetteer (oil/LNG hubs). No network.
PORT_COORDS: dict[str, tuple[float, float]] = {
    "singapore": (1.264, 103.820),
    "rotterdam": (51.950, 4.140),
    "houston": (29.760, -95.370),
    "fujairah": (25.120, 56.340),
    "ras_tanura": (26.640, 50.160),
    "basrah": (29.930, 48.480),
    "ningbo": (29.870, 121.950),
    "qingdao": (36.070, 120.320),
    "ulsan": (35.430, 129.390),
    "chiba": (35.580, 140.100),
    "jamnagar": (22.480, 69.850),
    "suez": (29.960, 32.550),
    "gibraltar": (36.140, -5.350),
    "malacca": (2.190, 102.250),
    "sabine_pass": (29.730, -93.870),
    "gladstone": (-23.840, 151.270),
    "qatar_ras_laffan": (25.910, 51.580),
    "yamal_sabetta": (71.270, 72.070),
    "bahamas_sts": (26.540, -78.780),
    "ceuta": (35.890, -5.320),
}


# ---------------------------------------------------------------------------
# Cargo class / DWT helpers
# ---------------------------------------------------------------------------

def classify_cargo_class(vessel_type: Optional[str]) -> str:
    """Map free-text vessel_type → crude_oil | lng_lpg | products_chemical | other."""
    if vessel_type is None or (isinstance(vessel_type, float) and np.isnan(vessel_type)):
        return "other"
    t = str(vessel_type).lower()
    if any(k in t for k in ("lng", "lpg", "газовоз", "hydrogen", "водоро")):
        return "lng_lpg"
    if "газо" in t and ("танкер" in t or "carrier" in t or "tanker" in t):
        return "lng_lpg"
    if any(
        k in t
        for k in (
            "crude",
            "vlcc",
            "ulcc",
            "suezmax",
            "aframax",
            "сырой",
            "нефть сыр",
            "very large crude",
        )
    ):
        return "crude_oil"
    if any(
        k in t
        for k in (
            "product",
            "chemical",
            "oil/chemical",
            "нефтепродукт",
            "химическ",
            "products tanker",
            "oil products",
        )
    ):
        return "products_chemical"
    if "oil" in t or "танкер" in t or "tanker" in t:
        # generic tanker without crude/gas/product markers → crude_oil proxy bucket
        # (conservative for oil-balance modelling; still separated from LNG)
        if any(k in t for k in ("lng", "lpg", "gas")):
            return "lng_lpg"
        return "crude_oil"
    return "other"


def empirical_max_draft_m(dwt_tons: float, cargo_class: str) -> Optional[float]:
    """Scantling / summer-draft ceiling from DWT (offline empirical curve).

    Piecewise calibration to typical tanker summer drafts (order-of-magnitude):
      Handy ~40k → ~11.5 m, Aframax ~110k → ~15 m, Suezmax ~160k → ~16.5 m,
      VLCC ~300k → ~21.5 m. Gas carriers use a slightly shallower curve.
    Always treated as estimate → caller must set draft_confidence=low_confidence
    when this is the sole ceiling source.
    """
    if dwt_tons is None or (isinstance(dwt_tons, float) and np.isnan(dwt_tons)):
        return None
    dwt = float(dwt_tons)
    if dwt <= 0:
        return None

    if dwt < 50_000:
        draft = 8.5 + dwt / 12_000.0
    elif dwt < 120_000:
        draft = 11.0 + (dwt - 50_000) / 20_000.0
    elif dwt < 200_000:
        draft = 14.5 + (dwt - 120_000) / 40_000.0
    else:
        draft = 17.0 + (dwt - 200_000) / 40_000.0

    if cargo_class == "lng_lpg":
        # Gas carriers: lower DWT for a given draft / different form factor
        draft *= 0.92

    return round(float(draft), 2)


def classify_laden_ballast(
    draft_ratio: Optional[float],
    *,
    force_uncertain: bool = False,
) -> str:
    """Three-way state. Never force binary when uncertainty is real."""
    if force_uncertain or draft_ratio is None or (isinstance(draft_ratio, float) and np.isnan(draft_ratio)):
        return STATE_UNCERTAIN
    if draft_ratio > LADEN_THRESHOLD:
        return STATE_LADEN
    if draft_ratio < BALLAST_THRESHOLD:
        return STATE_BALLAST
    return STATE_UNCERTAIN


# ---------------------------------------------------------------------------
# Draft ceiling resolution
# ---------------------------------------------------------------------------

@dataclass
class DraftCeiling:
    max_draft_m: Optional[float]
    source: str  # archive_max | dwt_empirical | unavailable
    confidence: str  # high | medium | low_confidence | unavailable


def resolve_draft_ceiling(
    draft_series: pd.Series,
    dwt_tons: Optional[float],
    cargo_class: str,
) -> DraftCeiling:
    """Choose max draft for draft_ratio denominator.

    Prefer historical archive max when enough observations AND meaningful range.
    Otherwise fall back to DWT empirical formula (explicitly low_confidence).
    Never use a single static fleet draft as both numerator and denominator
    (that would collapse ratio→1 and fake 'laden').
    """
    vals = pd.to_numeric(draft_series, errors="coerce").dropna()
    vals = vals[(vals > 0) & (vals < 40)]  # physical sanity for tankers

    if len(vals) >= MIN_DRAFT_OBS_FOR_ARCHIVE_MAX:
        dmax = float(vals.max())
        dmin = float(vals.min())
        span = (dmax - dmin) / dmax if dmax > 0 else 0.0
        if span >= MIN_DRAFT_RANGE_FRAC:
            return DraftCeiling(dmax, "archive_max", "high")
        # flat series — archive max exists but may be a repeated static value
        emp = empirical_max_draft_m(dwt_tons, cargo_class) if dwt_tons else None
        if emp and emp > dmax:
            return DraftCeiling(emp, "dwt_empirical", "low_confidence")
        return DraftCeiling(dmax, "archive_max", "medium")

    emp = empirical_max_draft_m(dwt_tons, cargo_class) if dwt_tons else None
    if emp is not None:
        return DraftCeiling(emp, "dwt_empirical", "low_confidence")
    if len(vals) > 0:
        return DraftCeiling(float(vals.max()), "archive_max", "low_confidence")
    return DraftCeiling(None, "unavailable", "unavailable")


def nearest_port(lat: float, lon: float) -> tuple[Optional[str], Optional[float]]:
    if lat is None or lon is None or np.isnan(lat) or np.isnan(lon):
        return None, None
    best_name = None
    best_km = None
    for name, (plat, plon) in PORT_COORDS.items():
        km = haversine_km(lat, lon, plat, plon)
        if best_km is None or km < best_km:
            best_km = km
            best_name = name
    if best_km is None or best_km > NEAREST_PORT_MAX_KM:
        return None, best_km
    return best_name, best_km


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_tracked_universe(targets_path: Path = TARGETS_PATH) -> dict:
    if not targets_path.exists():
        return {
            "n_tracked_mmsi": 0,
            "n_tracked_imo": 0,
            "imo_set": set(),
        }
    data = json.loads(targets_path.read_text(encoding="utf-8"))
    targets = data.get("targets", [])
    imos = {str(t["imo"]) for t in targets}
    mmsis = {str(t["mmsi"]) for t in targets}
    stats = data.get("stats", {})
    return {
        "n_tracked_mmsi": int(stats.get("unique_mmsi_for_subscription", len(mmsis))),
        "n_tracked_imo": int(stats.get("unique_imo_with_mmsi", len(imos))),
        "imo_set": imos,
        "empirical_max_fleet_dwt": None,  # filled after fleet join
    }


def load_fleet_static(fleet_path: Path = FLEET_PATH) -> pd.DataFrame:
    if not fleet_path.exists():
        raise FileNotFoundError(f"fleet_database not found: {fleet_path}")
    usecols = ["imo", "vessel_name", "vessel_type", "dwt_tons", "draft_m", "vessel_category"]
    df = pd.read_csv(fleet_path, usecols=lambda c: c in usecols, low_memory=False)
    if "vessel_category" in df.columns:
        df = df[df["vessel_category"] == "vessel"].copy()
    df["imo"] = df["imo"].astype(str).str.replace(r"\.0$", "", regex=True)
    df["dwt_tons"] = pd.to_numeric(df.get("dwt_tons"), errors="coerce")
    df["draft_m"] = pd.to_numeric(df.get("draft_m"), errors="coerce")
    df["cargo_class"] = df["vessel_type"].map(classify_cargo_class)
    # one row per IMO: prefer row with DWT, then with draft
    df["_score"] = df["dwt_tons"].notna().astype(int) * 2 + df["draft_m"].notna().astype(int)
    df = df.sort_values(["imo", "_score"], ascending=[True, False]).drop_duplicates("imo", keep="first")
    return df.drop(columns=["_score"], errors="ignore")


def _normalize_by_vessel_columns(df: pd.DataFrame, imo: str) -> pd.DataFrame:
    """Accept both current snapshot schema and richer future schema."""
    out = df.copy()
    out["imo"] = imo
    rename = {}
    if "speed" in out.columns and "speed_knots" not in out.columns:
        rename["speed"] = "speed_knots"
    if "timestamp" in out.columns and "timestamp_utc" not in out.columns:
        rename["timestamp"] = "timestamp_utc"
    out = out.rename(columns=rename)

    if "snapshot_date" in out.columns:
        out["date"] = pd.to_datetime(out["snapshot_date"], errors="coerce").dt.normalize()
    elif "timestamp_utc" in out.columns:
        out["date"] = pd.to_datetime(out["timestamp_utc"], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
    else:
        out["date"] = pd.NaT

    if "draft_m" not in out.columns:
        out["draft_m"] = np.nan
    else:
        out["draft_m"] = pd.to_numeric(out["draft_m"], errors="coerce")

    for col in ("lat", "lon", "speed_knots"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        else:
            out[col] = np.nan

    if "timestamp_utc" not in out.columns:
        out["timestamp_utc"] = out["date"].astype(str)

    return out


def load_by_vessel_archive(
    by_vessel_dir: Path = BY_VESSEL_DIR,
    imo_filter: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    if not by_vessel_dir.exists():
        return pd.DataFrame()
    files = sorted(by_vessel_dir.glob("*.csv"))
    if not files:
        return pd.DataFrame()

    allow = set(str(x) for x in imo_filter) if imo_filter is not None else None
    frames: list[pd.DataFrame] = []
    for path in files:
        imo = path.stem
        if allow is not None and imo not in allow:
            continue
        try:
            raw = pd.read_csv(path, encoding="utf-8-sig")
        except Exception as exc:  # noqa: BLE001 — skip corrupt file, keep going
            warnings.warn(f"skip {path.name}: {exc}")
            continue
        if raw.empty:
            continue
        frames.append(_normalize_by_vessel_columns(raw, imo))

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Core feature construction
# ---------------------------------------------------------------------------

def assess_data_sufficiency(panel: pd.DataFrame, n_tracked: int) -> dict:
    """Return sufficiency diagnostics. Majority of vessels need ≥7 days."""
    if panel.empty:
        return {
            "sufficient": False,
            "warning": INSUFFICIENT_DATA_MSG,
            "n_vessel_files": 0,
            "n_vessels_with_ge7_days": 0,
            "share_with_ge7_days": 0.0,
            "median_days_per_vessel": 0.0,
            "n_tracked_mmsi": n_tracked,
            "detail": "by_vessel archive empty or missing",
        }

    days = panel.dropna(subset=["date"]).groupby("imo")["date"].nunique()
    n_ves = int(len(days))
    n_ge7 = int((days >= MIN_DAYS_FOR_STABLE).sum())
    share = float(n_ge7 / n_ves) if n_ves else 0.0
    sufficient = n_ves > 0 and share >= 0.50 and n_ge7 >= 1
    # Also require absolute calendar span hint
    calendar_days = int(panel["date"].nunique()) if panel["date"].notna().any() else 0
    if calendar_days < MIN_DAYS_FOR_STABLE:
        sufficient = False

    warning = None if sufficient else INSUFFICIENT_DATA_MSG
    return {
        "sufficient": sufficient,
        "warning": warning,
        "n_vessel_files": n_ves,
        "n_vessels_with_ge7_days": n_ge7,
        "share_with_ge7_days": round(share, 4),
        "median_days_per_vessel": float(days.median()) if n_ves else 0.0,
        "calendar_days_in_archive": calendar_days,
        "n_tracked_mmsi": n_tracked,
        "detail": (
            "ok"
            if sufficient
            else (
                f"calendar_days={calendar_days}, "
                f"vessels_with_>={MIN_DAYS_FOR_STABLE}d={n_ge7}/{n_ves} "
                f"(need majority ≥{MIN_DAYS_FOR_STABLE} days)"
            )
        ),
    }


def enrich_panel_with_states(
    panel: pd.DataFrame,
    fleet: pd.DataFrame,
) -> pd.DataFrame:
    """Attach DWT/type, resolve draft ceiling per vessel, classify state per row."""
    if panel.empty:
        return panel

    fleet_cols = fleet[["imo", "vessel_name", "vessel_type", "dwt_tons", "draft_m", "cargo_class"]].rename(
        columns={"draft_m": "fleet_draft_m", "vessel_name": "fleet_vessel_name"}
    )
    df = panel.merge(fleet_cols, on="imo", how="left")
    df["cargo_class"] = df["cargo_class"].fillna("other")

    # Numerators: prefer archive draft; else static fleet draft (marked)
    df["draft_m_archive"] = pd.to_numeric(df["draft_m"], errors="coerce")
    df["draft_source"] = np.where(df["draft_m_archive"].notna(), "archive", "none")
    use_fleet = df["draft_m_archive"].isna() & df["fleet_draft_m"].notna()
    df.loc[use_fleet, "draft_m"] = df.loc[use_fleet, "fleet_draft_m"]
    df.loc[use_fleet, "draft_source"] = "fleet_static"
    df.loc[df["draft_m"].isna(), "draft_source"] = "unavailable"
    df["draft_m"] = pd.to_numeric(df["draft_m"], errors="coerce")

    ceilings: dict[str, DraftCeiling] = {}
    for imo, g in df.groupby("imo"):
        dwt = g["dwt_tons"].dropna()
        dwt_val = float(dwt.iloc[0]) if len(dwt) else None
        cc = str(g["cargo_class"].iloc[0]) if len(g) else "other"
        # Ceiling from archive drafts only (not fleet-filled), to avoid tautology
        ceilings[str(imo)] = resolve_draft_ceiling(g["draft_m_archive"], dwt_val, cc)

    df["max_draft_m"] = df["imo"].map(lambda i: ceilings[str(i)].max_draft_m)
    df["max_draft_source"] = df["imo"].map(lambda i: ceilings[str(i)].source)
    df["draft_confidence"] = df["imo"].map(lambda i: ceilings[str(i)].confidence)

    df["draft_ratio"] = np.where(
        (df["max_draft_m"].notna()) & (df["max_draft_m"] > 0) & df["draft_m"].notna(),
        df["draft_m"] / df["max_draft_m"],
        np.nan,
    )
    # Cap absurd ratios (bad OSINT draft vs empirical ceiling)
    df["draft_ratio"] = df["draft_ratio"].clip(upper=1.5)

    force_unc = df["draft_confidence"].isin(["low_confidence", "unavailable"]) & (
        df["max_draft_source"] == "unavailable"
    )
    # If draft itself unavailable → uncertain
    no_draft = df["draft_m"].isna() | df["draft_ratio"].isna()
    df["laden_state"] = [
        classify_laden_ballast(
            r,
            force_uncertain=bool(fu or nd),
        )
        for r, fu, nd in zip(df["draft_ratio"], force_unc, no_draft)
    ]
    # low_confidence empirical ceiling still allows classification, but we keep
    # the confidence flag on the row for downstream modelling filters.
    return df


def detect_state_transitions(panel: pd.DataFrame) -> pd.DataFrame:
    """Laden↔ballast flips as load/discharge proxy. Uncertain does not trigger."""
    if panel.empty:
        return pd.DataFrame(
            columns=[
                "imo",
                "timestamp_utc",
                "date",
                "lat",
                "lon",
                "nearest_port",
                "nearest_port_km",
                "from_state",
                "to_state",
                "draft_m",
                "draft_ratio",
                "draft_confidence",
                "max_draft_source",
                "dwt_tons",
                "cargo_class",
                "event_proxy",
            ]
        )

    rows = []
    ranked = panel.sort_values(["imo", "date", "timestamp_utc"])
    for imo, g in ranked.groupby("imo"):
        prev_state = None
        prev_row = None
        for _, row in g.iterrows():
            state = row["laden_state"]
            if state == STATE_UNCERTAIN:
                # do not update prev_state through uncertain haze
                continue
            if prev_state is not None and state != prev_state:
                lat, lon = row.get("lat"), row.get("lon")
                port, pkm = nearest_port(
                    float(lat) if pd.notna(lat) else np.nan,
                    float(lon) if pd.notna(lon) else np.nan,
                )
                event = "discharge_proxy" if (prev_state == STATE_LADEN and state == STATE_BALLAST) else "load_proxy"
                rows.append(
                    {
                        "imo": imo,
                        "timestamp_utc": row.get("timestamp_utc"),
                        "date": row["date"],
                        "lat": lat,
                        "lon": lon,
                        "nearest_port": port,
                        "nearest_port_km": None if pkm is None else round(float(pkm), 1),
                        "from_state": prev_state,
                        "to_state": state,
                        "draft_m": row.get("draft_m"),
                        "draft_ratio": None if pd.isna(row.get("draft_ratio")) else round(float(row["draft_ratio"]), 4),
                        "draft_confidence": row.get("draft_confidence"),
                        "max_draft_source": row.get("max_draft_source"),
                        "dwt_tons": row.get("dwt_tons"),
                        "cargo_class": row.get("cargo_class"),
                        "event_proxy": event,
                    }
                )
            prev_state = state
            prev_row = row
        _ = prev_row  # silence lint

    return pd.DataFrame(rows)


def build_dwt_flow_timeseries(
    panel: pd.DataFrame,
    n_tracked_mmsi: int,
    empirical_max_fleet_dwt: float,
    sufficiency: dict,
) -> pd.DataFrame:
    """Daily DWT-flow by cargo_class + coverage metadata on EVERY row."""
    meta_cols = [
        "date",
        "cargo_class",
        "laden_dwt_sum",
        "ballast_dwt_sum",
        "uncertain_dwt_sum",
        "n_laden",
        "n_ballast",
        "n_uncertain",
        "n_vessels_observed",
        "n_vessels_tracked",
        "coverage_pct_vessels",
        "fleet_dwt_empirical_max",
        "laden_dwt_vs_fleet_max_pct",
        "observed_dwt_sum",
        "observed_dwt_vs_fleet_max_pct",
        "n_low_confidence_draft",
        "insufficient_data_flag",
        "insufficient_data_warning",
        "sufficiency_detail",
    ]
    if panel.empty:
        return pd.DataFrame(columns=meta_cols)

    df = panel.dropna(subset=["date"]).copy()
    df["dwt_tons"] = pd.to_numeric(df["dwt_tons"], errors="coerce").fillna(0.0)

    # Per date: how many tracked vessels have any row that day
    daily_obs = df.groupby("date")["imo"].nunique().rename("n_vessels_observed_day")

    records = []
    for (date, cargo_class), g in df.groupby(["date", "cargo_class"]):
        laden = g[g["laden_state"] == STATE_LADEN]
        ballast = g[g["laden_state"] == STATE_BALLAST]
        unc = g[g["laden_state"] == STATE_UNCERTAIN]
        laden_dwt = float(laden["dwt_tons"].sum())
        ballast_dwt = float(ballast["dwt_tons"].sum())
        unc_dwt = float(unc["dwt_tons"].sum())
        observed_dwt = float(g["dwt_tons"].sum())
        n_obs_day = int(daily_obs.loc[date]) if date in daily_obs.index else int(g["imo"].nunique())
        cov_v = (100.0 * n_obs_day / n_tracked_mmsi) if n_tracked_mmsi else 0.0
        laden_vs_max = (
            100.0 * laden_dwt / empirical_max_fleet_dwt if empirical_max_fleet_dwt > 0 else np.nan
        )
        obs_vs_max = (
            100.0 * observed_dwt / empirical_max_fleet_dwt if empirical_max_fleet_dwt > 0 else np.nan
        )
        records.append(
            {
                "date": date,
                "cargo_class": cargo_class,
                "laden_dwt_sum": round(laden_dwt, 1),
                "ballast_dwt_sum": round(ballast_dwt, 1),
                "uncertain_dwt_sum": round(unc_dwt, 1),
                "n_laden": int(laden["imo"].nunique()),
                "n_ballast": int(ballast["imo"].nunique()),
                "n_uncertain": int(unc["imo"].nunique()),
                "n_vessels_observed": n_obs_day,
                "n_vessels_tracked": int(n_tracked_mmsi),
                "coverage_pct_vessels": round(cov_v, 2),
                "fleet_dwt_empirical_max": round(float(empirical_max_fleet_dwt), 1),
                "laden_dwt_vs_fleet_max_pct": None if np.isnan(laden_vs_max) else round(laden_vs_max, 2),
                "observed_dwt_sum": round(observed_dwt, 1),
                "observed_dwt_vs_fleet_max_pct": None if np.isnan(obs_vs_max) else round(obs_vs_max, 2),
                "n_low_confidence_draft": int((g["draft_confidence"] == "low_confidence").sum()),
                "insufficient_data_flag": not bool(sufficiency.get("sufficient")),
                "insufficient_data_warning": sufficiency.get("warning"),
                "sufficiency_detail": sufficiency.get("detail"),
            }
        )

    out = pd.DataFrame.from_records(records)
    if out.empty:
        return pd.DataFrame(columns=meta_cols)
    return out.sort_values(["date", "cargo_class"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_feature_engineering(
    *,
    by_vessel_dir: Path = BY_VESSEL_DIR,
    fleet_path: Path = FLEET_PATH,
    targets_path: Path = TARGETS_PATH,
    out_timeseries: Path = OUT_TIMESERIES,
    out_transitions: Path = OUT_TRANSITIONS,
    imo_filter: Optional[Iterable[str]] = None,
) -> dict:
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)

    universe = load_tracked_universe(targets_path)
    n_tracked = universe["n_tracked_mmsi"] or 2711
    fleet = load_fleet_static(fleet_path)

    # Empirical max DWT of tracked fleet (sum of unique IMO DWT where known)
    tracked_imos = universe["imo_set"] or set(fleet["imo"])
    fleet_tracked = fleet[fleet["imo"].isin(tracked_imos)] if tracked_imos else fleet
    empirical_max_fleet_dwt = float(fleet_tracked["dwt_tons"].fillna(0).sum())
    if empirical_max_fleet_dwt <= 0:
        empirical_max_fleet_dwt = float(fleet["dwt_tons"].fillna(0).sum())

    panel = load_by_vessel_archive(by_vessel_dir, imo_filter=imo_filter)
    sufficiency = assess_data_sufficiency(panel, n_tracked)

    if sufficiency["warning"]:
        print("=" * 72)
        print(f"WARNING: {sufficiency['warning']}")
        print(f"  detail: {sufficiency['detail']}")
        print(
            f"  vessels={sufficiency['n_vessel_files']}, "
            f">={MIN_DAYS_FOR_STABLE}d={sufficiency['n_vessels_with_ge7_days']}, "
            f"median_days={sufficiency['median_days_per_vessel']:.1f}, "
            f"tracked_mmsi={n_tracked}"
        )
        print("  Outputs will be written but must NOT be treated as reliable features.")
        print("=" * 72)

    panel = enrich_panel_with_states(panel, fleet)
    transitions = detect_state_transitions(panel)
    timeseries = build_dwt_flow_timeseries(
        panel,
        n_tracked_mmsi=n_tracked,
        empirical_max_fleet_dwt=empirical_max_fleet_dwt,
        sufficiency=sufficiency,
    )

    # Always persist schema even if empty (downstream contracts)
    if timeseries.empty:
        timeseries = build_dwt_flow_timeseries(
            pd.DataFrame(),
            n_tracked_mmsi=n_tracked,
            empirical_max_fleet_dwt=empirical_max_fleet_dwt,
            sufficiency=sufficiency,
        )
        # empty frame with columns — add a coverage stub row? Prefer empty schema.
        pass

    out_timeseries.parent.mkdir(parents=True, exist_ok=True)
    timeseries.to_parquet(out_timeseries, index=False)
    transitions.to_csv(out_transitions, index=False, encoding="utf-8-sig")

    summary = {
        "n_panel_rows": int(len(panel)),
        "n_transitions": int(len(transitions)),
        "n_timeseries_rows": int(len(timeseries)),
        "n_tracked_mmsi": n_tracked,
        "empirical_max_fleet_dwt": empirical_max_fleet_dwt,
        "sufficiency": sufficiency,
        "out_timeseries": str(out_timeseries),
        "out_transitions": str(out_transitions),
    }
    print(
        f"Wrote {out_timeseries} ({summary['n_timeseries_rows']} rows); "
        f"{out_transitions} ({summary['n_transitions']} events)"
    )
    return summary


def main() -> int:
    # Windows consoles often default to a legacy code page; keep Cyrillic warnings readable.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="AIS laden/ballast + DWT-flow features")
    parser.add_argument("--by-vessel-dir", type=Path, default=BY_VESSEL_DIR)
    parser.add_argument("--fleet", type=Path, default=FLEET_PATH)
    parser.add_argument("--targets", type=Path, default=TARGETS_PATH)
    parser.add_argument("--out-timeseries", type=Path, default=OUT_TIMESERIES)
    parser.add_argument("--out-transitions", type=Path, default=OUT_TRANSITIONS)
    args = parser.parse_args()
    try:
        run_feature_engineering(
            by_vessel_dir=args.by_vessel_dir,
            fleet_path=args.fleet,
            targets_path=args.targets,
            out_timeseries=args.out_timeseries,
            out_transitions=args.out_transitions,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
