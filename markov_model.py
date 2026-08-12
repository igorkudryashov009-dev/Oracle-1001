"""
First-order Markov model of tanker operational states → DWT-laden forecasts.

States (daily, discrete):
  ballast → transitional → laden → loading (in port) → laden →
  transitional → ballast → discharging (in port) → ballast
  + no_signal (coverage gap; sticky — see ASSUMPTION_MARKOV / no_signal note)

Cohorts: cargo_class × DWT bucket (VLCC ≠ chemical handy; matrices never pooled).

Outputs (offline):
  features/markov_transition_matrix.json

CRITICAL:
  - Every run logs training sample size N; a forecast without N is invalid.
  - Out-of-sample backtest requires ≥60 calendar days of archive; otherwise
    status = "insufficient history for backtesting" (no fake accuracy).
  - Markov memory-1 is an explicit modelling simplification, not reality.

Depends on feature_engineering.py for draft laden/ballast labels (Prompt 1).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

import feature_engineering as fe
from feature_engineering import (
    BY_VESSEL_DIR,
    FLEET_PATH,
    NEAREST_PORT_MAX_KM,
    TARGETS_PATH,
    nearest_port,
)

ROOT = Path(__file__).resolve().parent
FEATURES_DIR = ROOT / "features"
OUT_JSON = FEATURES_DIR / "markov_transition_matrix.json"

# ---------------------------------------------------------------------------
# Model documentation (emitted into every JSON artefact)
# ---------------------------------------------------------------------------

ASSUMPTION_MARKOV = (
    "MARKOV ASSUMPTION (memory = 1 day): P(state_{t+1} | state_t, state_{t-1}, …) "
    "= P(state_{t+1} | state_t). This is a deliberate simplification. Real tanker "
    "voyages have longer memory (fixtures/contracts booked weeks ahead, canal "
    "slots, STS schedules). One-step Markov dynamics therefore understate "
    "path-dependence. Treat forecasts as short-horizon statistical proxies, "
    "not as fixtures-market truth."
)

NOTE_NO_SIGNAL = (
    "no_signal is the AIS coverage-gap state. In the business narrative it acts "
    "as a sink for unobserved vessels; empirically we still estimate escape "
    "probabilities P(no_signal → ·) when vessels reappear, otherwise Monte Carlo "
    "fleet forecasts would falsely absorb 100% of DWT into silence within weeks. "
    "If no escape is observed in-sample, the row is left absorbing (P_ii = 1)."
)

STATES: list[str] = [
    "ballast",
    "transitional",
    "laden",
    "loading",
    "discharging",
    "no_signal",
]

ROLLING_WINDOWS = (30, 90)
MIN_HISTORY_BACKTEST_DAYS = 60
MIN_TRANSITIONS_PER_COHORT = 30
PORT_SPEED_MAX_KNOTS = 3.0
MC_DEFAULT_TRAJECTORIES = 1000
FORECAST_HORIZONS = (7, 30)
CI_LO, CI_HI = 0.05, 0.95
RNG_SEED = 42

INSUFFICIENT_BACKTEST_MSG = "insufficient history for backtesting"


# ---------------------------------------------------------------------------
# Cohort definition
# ---------------------------------------------------------------------------

def dwt_bucket(dwt: Optional[float]) -> str:
    if dwt is None or (isinstance(dwt, float) and np.isnan(dwt)) or dwt <= 0:
        return "unknown_dwt"
    d = float(dwt)
    if d >= 200_000:
        return "vlcc"  # VLCC / ULCC
    if d >= 120_000:
        return "suezmax"
    if d >= 80_000:
        return "aframax_lr2"
    if d >= 25_000:
        return "handy_mr"
    return "small"


def cohort_key(cargo_class: str, dwt: Optional[float]) -> str:
    return f"{cargo_class}|{dwt_bucket(dwt)}"


# ---------------------------------------------------------------------------
# Operational state labelling
# ---------------------------------------------------------------------------

def _base_draft_state(laden_state: str) -> str:
    if laden_state == fe.STATE_LADEN:
        return "laden"
    if laden_state == fe.STATE_BALLAST:
        return "ballast"
    return "transitional"


def assign_operational_state(row: pd.Series) -> str:
    """Map one enriched daily row → discrete ops state."""
    src = str(row.get("data_source") or "")
    if src == "no_signal_24h":
        return "no_signal"
    n_pings = row.get("num_pings_24h")
    if pd.notna(n_pings) and float(n_pings) <= 0 and src not in ("aisstream_live", ""):
        return "no_signal"

    base = _base_draft_state(str(row.get("laden_state") or fe.STATE_UNCERTAIN))
    lat, lon = row.get("lat"), row.get("lon")
    speed = row.get("speed_knots")
    in_port = False
    if pd.notna(lat) and pd.notna(lon):
        port, km = nearest_port(float(lat), float(lon))
        if port is not None and km is not None and km <= NEAREST_PORT_MAX_KM:
            in_port = True
    slow = pd.notna(speed) and float(speed) <= PORT_SPEED_MAX_KNOTS

    # In-port cargo ops overlay (proxy — not berth confirmation)
    if in_port and slow:
        if base == "ballast":
            return "loading"
        if base == "laden":
            return "discharging"
        # transitional in port: infer from draft_ratio side of midpoint
        ratio = row.get("draft_ratio")
        if pd.notna(ratio):
            return "loading" if float(ratio) < 0.725 else "discharging"
    return base


def build_ops_panel(
    *,
    by_vessel_dir: Path = BY_VESSEL_DIR,
    fleet_path: Path = FLEET_PATH,
    targets_path: Path = TARGETS_PATH,
) -> tuple[pd.DataFrame, dict]:
    """Load archive → feature states → operational Markov states."""
    universe = fe.load_tracked_universe(targets_path)
    fleet = fe.load_fleet_static(fleet_path)
    panel = fe.load_by_vessel_archive(by_vessel_dir)
    if panel.empty:
        return panel, {
            "n_vessel_days": 0,
            "n_vessels": 0,
            "calendar_days": 0,
            "date_start": None,
            "date_end": None,
            "n_tracked_mmsi": universe["n_tracked_mmsi"] or 2711,
        }

    panel = fe.enrich_panel_with_states(panel, fleet)
    if "data_source" not in panel.columns:
        panel["data_source"] = "aisstream_live"
    if "num_pings_24h" not in panel.columns:
        panel["num_pings_24h"] = np.nan
    if "speed_knots" not in panel.columns:
        panel["speed_knots"] = np.nan

    panel = panel.dropna(subset=["date"]).sort_values(["imo", "date"]).copy()
    panel["ops_state"] = panel.apply(assign_operational_state, axis=1)
    panel["dwt_bucket"] = panel["dwt_tons"].map(dwt_bucket)
    panel["cohort"] = [
        cohort_key(str(cc), dwt)
        for cc, dwt in zip(panel["cargo_class"], panel["dwt_tons"])
    ]

    dates = panel["date"].dropna()
    sample = {
        "n_vessel_days": int(len(panel)),
        "n_vessels": int(panel["imo"].nunique()),
        "calendar_days": int(dates.dt.normalize().nunique()),
        "date_start": str(dates.min().date()) if len(dates) else None,
        "date_end": str(dates.max().date()) if len(dates) else None,
        "n_tracked_mmsi": universe["n_tracked_mmsi"] or 2711,
        "n_ops_state_counts": panel["ops_state"].value_counts().to_dict(),
    }
    return panel, sample


# ---------------------------------------------------------------------------
# Transition matrix estimation
# ---------------------------------------------------------------------------

def _empty_count_matrix() -> np.ndarray:
    n = len(STATES)
    return np.zeros((n, n), dtype=float)


def _index(state: str) -> int:
    return STATES.index(state)


def accumulate_transitions(seq: list[str]) -> np.ndarray:
    """Count first-order transitions along one vessel's daily state sequence."""
    C = _empty_count_matrix()
    for a, b in zip(seq[:-1], seq[1:]):
        if a not in STATES or b not in STATES:
            continue
        C[_index(a), _index(b)] += 1.0
    return C


def counts_to_matrix(C: np.ndarray, *, alpha: float = 1e-6) -> np.ndarray:
    """Row-stochastic matrix with tiny Laplace smoother for empty rows."""
    P = C.astype(float).copy()
    for i in range(len(STATES)):
        row_sum = P[i].sum()
        if row_sum <= 0:
            # No observations leaving i: identity (absorbing / unknown)
            P[i] = 0.0
            P[i, i] = 1.0
        else:
            P[i] = (P[i] + alpha) / (row_sum + alpha * len(STATES))
            P[i] = P[i] / P[i].sum()
    return P


def expected_sojourn_days(P: np.ndarray) -> dict[str, float]:
    """E[sojourn in i] = 1 / (1 - P_ii) for non-absorbing; inf if P_ii≈1."""
    out: dict[str, float] = {}
    for i, s in enumerate(STATES):
        p_ii = float(P[i, i])
        if p_ii >= 1.0 - 1e-12:
            out[s] = float("inf")
        else:
            out[s] = round(1.0 / (1.0 - p_ii), 3)
    return out


def expected_round_trip_proxy(sojourn: dict[str, float]) -> Optional[float]:
    """Rough laden↔ballast cycle length (days), ignoring inf components."""
    keys = ("ballast", "transitional", "laden", "loading", "discharging")
    vals = []
    for k in keys:
        v = sojourn.get(k)
        if v is None or not np.isfinite(v):
            continue
        vals.append(float(v))
    if len(vals) < 2:
        return None
    return round(sum(vals), 2)


def matrix_to_dict(P: np.ndarray) -> dict[str, dict[str, float]]:
    return {
        STATES[i]: {STATES[j]: round(float(P[i, j]), 6) for j in range(len(STATES))}
        for i in range(len(STATES))
    }


def counts_to_dict(C: np.ndarray) -> dict[str, dict[str, int]]:
    return {
        STATES[i]: {STATES[j]: int(C[i, j]) for j in range(len(STATES))}
        for i in range(len(STATES))
    }


def sequences_by_cohort(
    panel: pd.DataFrame,
    *,
    date_from: Optional[pd.Timestamp] = None,
    date_to: Optional[pd.Timestamp] = None,
) -> dict[str, list[list[str]]]:
    df = panel
    if date_from is not None:
        df = df[df["date"] >= date_from]
    if date_to is not None:
        df = df[df["date"] <= date_to]
    out: dict[str, list[list[str]]] = {}
    for (cohort, imo), g in df.sort_values("date").groupby(["cohort", "imo"]):
        seq = [str(s) for s in g["ops_state"].tolist()]
        if len(seq) >= 2:
            out.setdefault(str(cohort), []).append(seq)
    return out


def estimate_cohort_models(
    panel: pd.DataFrame,
    *,
    date_from: Optional[pd.Timestamp] = None,
    date_to: Optional[pd.Timestamp] = None,
    min_transitions: int = MIN_TRANSITIONS_PER_COHORT,
) -> dict[str, dict[str, Any]]:
    seqs = sequences_by_cohort(panel, date_from=date_from, date_to=date_to)
    models: dict[str, dict[str, Any]] = {}
    for cohort, sequences in sorted(seqs.items()):
        C = _empty_count_matrix()
        for seq in sequences:
            C += accumulate_transitions(seq)
        n_trans = int(C.sum())
        n_seq = len(sequences)
        P = counts_to_matrix(C)
        sojourn = expected_sojourn_days(P)
        models[cohort] = {
            "cohort": cohort,
            "cargo_class": cohort.split("|")[0],
            "dwt_bucket": cohort.split("|")[1] if "|" in cohort else "unknown",
            "n_sequences": n_seq,
            "n_transitions": n_trans,
            "sufficient_sample": n_trans >= min_transitions,
            "counts": counts_to_dict(C),
            "matrix": matrix_to_dict(P),
            "expected_sojourn_days": {
                k: (None if not np.isfinite(v) else v) for k, v in sojourn.items()
            },
            "expected_round_trip_days_proxy": expected_round_trip_proxy(sojourn),
            "_P": P,  # numpy, stripped before JSON
            "_C": C,
        }
    return models


def estimate_rolling(
    panel: pd.DataFrame,
    windows: tuple[int, ...] = ROLLING_WINDOWS,
) -> dict[str, dict[str, Any]]:
    """Sliding end-anchored windows: last W calendar days → cohort matrices."""
    if panel.empty:
        return {}
    end = panel["date"].max()
    rolling: dict[str, dict[str, Any]] = {}
    for w in windows:
        start = end - pd.Timedelta(days=w - 1)
        models = estimate_cohort_models(panel, date_from=start, date_to=end)
        rolling[f"{w}d"] = {
            "window_days": w,
            "date_from": str(start.date()),
            "date_to": str(end.date()),
            "n_vessel_days": int(
                ((panel["date"] >= start) & (panel["date"] <= end)).sum()
            ),
            "cohorts": {
                k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                for k, v in models.items()
            },
        }
    return rolling


# ---------------------------------------------------------------------------
# Monte Carlo forecast
# ---------------------------------------------------------------------------

def _current_fleet_state(panel: pd.DataFrame) -> pd.DataFrame:
    """Last observation per vessel on the latest archive date available per imo."""
    last = panel.sort_values("date").groupby("imo", as_index=False).tail(1)
    return last[["imo", "cohort", "ops_state", "dwt_tons"]].copy()


def monte_carlo_laden_share(
    panel: pd.DataFrame,
    cohort_models: dict[str, dict[str, Any]],
    *,
    horizons: tuple[int, ...] = FORECAST_HORIZONS,
    n_trajectories: int = MC_DEFAULT_TRAJECTORIES,
    seed: int = RNG_SEED,
) -> dict[str, Any]:
    """Simulate N trajectories; return % of observed DWT in state 'laden'."""
    fleet = _current_fleet_state(panel)
    fleet["dwt_tons"] = pd.to_numeric(fleet["dwt_tons"], errors="coerce").fillna(0.0)
    total_dwt = float(fleet["dwt_tons"].sum())
    if total_dwt <= 0 or fleet.empty:
        return {
            "n_monte_carlo": n_trajectories,
            "n_vessels_simulated": 0,
            "total_dwt_simulated": 0.0,
            "horizons": {},
            "note": "no fleet state to simulate",
        }

    # Fallback matrix: pool all cohorts with insufficient sample
    pooled_C = _empty_count_matrix()
    for m in cohort_models.values():
        pooled_C += m["_C"]
    pooled_P = counts_to_matrix(pooled_C)

    matrices = {
        c: (m["_P"] if m["n_transitions"] > 0 else pooled_P)
        for c, m in cohort_models.items()
    }

    imos = fleet["imo"].tolist()
    cohorts = fleet["cohort"].tolist()
    states0 = [_index(s) if s in STATES else _index("transitional") for s in fleet["ops_state"]]
    dwts = fleet["dwt_tons"].to_numpy(dtype=float)
    n_ves = len(imos)
    rng = np.random.default_rng(seed)

    results: dict[str, Any] = {
        "n_monte_carlo": int(n_trajectories),
        "n_vessels_simulated": int(n_ves),
        "total_dwt_simulated": round(total_dwt, 1),
        "horizons": {},
    }

    for h in horizons:
        laden_pct = np.zeros(n_trajectories, dtype=float)
        for t in range(n_trajectories):
            st = np.array(states0, dtype=int)
            for _step in range(h):
                for i in range(n_ves):
                    P = matrices.get(cohorts[i], pooled_P)
                    st[i] = rng.choice(len(STATES), p=P[st[i]])
            laden_dwt = dwts[st == _index("laden")].sum()
            laden_pct[t] = 100.0 * laden_dwt / total_dwt

        results["horizons"][f"T+{h}"] = {
            "laden_dwt_pct_mean": round(float(laden_pct.mean()), 3),
            "laden_dwt_pct_median": round(float(np.median(laden_pct)), 3),
            "laden_dwt_pct_ci_low": round(float(np.quantile(laden_pct, CI_LO)), 3),
            "laden_dwt_pct_ci_high": round(float(np.quantile(laden_pct, CI_HI)), 3),
            "ci_level": f"{int((CI_HI - CI_LO) * 100)}% equal-tailed",
            "state_definition": "strict 'laden' only (excludes loading/discharging/transitional)",
        }
    return results


# ---------------------------------------------------------------------------
# Out-of-sample validation
# ---------------------------------------------------------------------------

def validate_out_of_sample(
    panel: pd.DataFrame,
    *,
    horizon: int = 7,
    min_history_days: int = MIN_HISTORY_BACKTEST_DAYS,
) -> dict[str, Any]:
    """Train on (−∞, X], score state-distribution vs fact on X+horizon.

    Refuses cleanly when calendar span < min_history_days.
    """
    if panel.empty:
        return {
            "status": "skipped",
            "message": INSUFFICIENT_BACKTEST_MSG,
            "calendar_days": 0,
            "metrics": None,
        }

    dates = sorted(panel["date"].dropna().dt.normalize().unique())
    calendar_days = len(dates)
    if calendar_days < min_history_days:
        return {
            "status": "skipped",
            "message": INSUFFICIENT_BACKTEST_MSG,
            "calendar_days": calendar_days,
            "min_required_days": min_history_days,
            "metrics": None,
        }

    # X = last date that still leaves `horizon` days of fact ahead
    x_idx = calendar_days - 1 - horizon
    if x_idx < min_history_days // 2:
        return {
            "status": "skipped",
            "message": INSUFFICIENT_BACKTEST_MSG,
            "calendar_days": calendar_days,
            "detail": "not enough headroom after train cut for fact window",
            "metrics": None,
        }

    x_date = pd.Timestamp(dates[x_idx])
    fact_date = pd.Timestamp(dates[x_idx + horizon]) if (x_idx + horizon) < calendar_days else None
    # Prefer exact +horizon calendar day if present; else nearest available
    target_day = x_date + pd.Timedelta(days=horizon)
    fact_panel = panel[panel["date"] == target_day]
    if fact_panel.empty:
        # nearest date at or after target within +2 days
        later = panel[panel["date"] >= target_day]
        if later.empty:
            return {
                "status": "skipped",
                "message": INSUFFICIENT_BACKTEST_MSG,
                "calendar_days": calendar_days,
                "detail": f"no fact rows on/after {target_day.date()}",
                "metrics": None,
            }
        fact_date = later["date"].min()
        fact_panel = panel[panel["date"] == fact_date]
    else:
        fact_date = target_day

    train = panel[panel["date"] <= x_date]
    models = estimate_cohort_models(train)
    n_train_trans = int(sum(m["n_transitions"] for m in models.values()))
    n_train_vdays = int(len(train))

    # Predict: evolve empirical DWT-weighted distribution with mean matrix path
    # (deterministic π_{t} P^h) — compared to fact; MC used for live forecast only
    start_fleet = train.sort_values("date").groupby("imo", as_index=False).tail(1)
    start_fleet["dwt_tons"] = pd.to_numeric(start_fleet["dwt_tons"], errors="coerce").fillna(0.0)
    total = float(start_fleet["dwt_tons"].sum()) or 1.0

    pooled_C = _empty_count_matrix()
    for m in models.values():
        pooled_C += m["_C"]
    pooled_P = counts_to_matrix(pooled_C)

    # One-step mean evolution per vessel using its cohort matrix
    pred_mass = {s: 0.0 for s in STATES}
    for _, row in start_fleet.iterrows():
        s0 = str(row["ops_state"])
        if s0 not in STATES:
            s0 = "transitional"
        P = models.get(str(row["cohort"]), {}).get("_P", pooled_P)
        dist = np.zeros(len(STATES))
        dist[_index(s0)] = 1.0
        for _ in range(horizon):
            dist = dist @ P
        for i, s in enumerate(STATES):
            pred_mass[s] += float(dist[i]) * float(row["dwt_tons"])

    fact_mass = {s: 0.0 for s in STATES}
    fp = fact_panel.copy()
    fp["dwt_tons"] = pd.to_numeric(fp["dwt_tons"], errors="coerce").fillna(0.0)
    # align vessels present at train end ∩ fact
    common = set(start_fleet["imo"]) & set(fp["imo"])
    fp = fp[fp["imo"].isin(common)]
    sf = start_fleet[start_fleet["imo"].isin(common)]
    total_common = float(sf["dwt_tons"].sum()) or 1.0
    for _, row in fp.iterrows():
        st = str(row["ops_state"])
        if st in fact_mass:
            fact_mass[st] += float(row["dwt_tons"])

    pred_pct = {s: 100.0 * pred_mass[s] / total for s in STATES}
    # renormalize pred on common DWT scale for comparison
    pred_pct_common = {s: 100.0 * pred_mass[s] * (total_common / total) / total_common for s in STATES}
    fact_pct = {s: 100.0 * fact_mass[s] / total_common for s in STATES}

    laden_ae = abs(pred_pct_common["laden"] - fact_pct["laden"])
    # Total variation distance between distributions
    tv = 0.5 * sum(abs(pred_pct_common[s] - fact_pct[s]) for s in STATES) / 100.0

    return {
        "status": "ok",
        "message": "out-of-sample distribution check completed",
        "calendar_days": calendar_days,
        "train_end": str(x_date.date()),
        "fact_date": str(pd.Timestamp(fact_date).date()),
        "horizon_days": horizon,
        "n_training_vessel_days": n_train_vdays,
        "n_training_transitions": n_train_trans,
        "n_vessels_scored": int(len(common)),
        "predicted_laden_dwt_pct": round(pred_pct_common["laden"], 3),
        "actual_laden_dwt_pct": round(fact_pct["laden"], 3),
        "laden_abs_error_pp": round(laden_ae, 3),
        "total_variation_distance": round(float(tv), 4),
        "predicted_state_dwt_pct": {k: round(v, 3) for k, v in pred_pct_common.items()},
        "actual_state_dwt_pct": {k: round(v, 3) for k, v in fact_pct.items()},
        "metrics": {
            "laden_abs_error_pp": round(laden_ae, 3),
            "total_variation_distance": round(float(tv), 4),
        },
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _strip_private(models: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in models.items()}


def run_markov_model(
    *,
    by_vessel_dir: Path = BY_VESSEL_DIR,
    fleet_path: Path = FLEET_PATH,
    targets_path: Path = TARGETS_PATH,
    out_json: Path = OUT_JSON,
    n_monte_carlo: int = MC_DEFAULT_TRAJECTORIES,
    panel: Optional[pd.DataFrame] = None,
    sample_meta: Optional[dict] = None,
) -> dict[str, Any]:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if panel is None:
        panel, sample_meta = build_ops_panel(
            by_vessel_dir=by_vessel_dir,
            fleet_path=fleet_path,
            targets_path=targets_path,
        )
    assert sample_meta is not None

    n_obs = int(sample_meta.get("n_vessel_days", 0))
    n_trans_est = 0
    print("=" * 72)
    print("MARKOV MODEL — training sample size (mandatory disclosure)")
    print(f"  N_vessel_days     = {n_obs}")
    print(f"  N_vessels         = {sample_meta.get('n_vessels')}")
    print(f"  calendar_days     = {sample_meta.get('calendar_days')}")
    print(f"  date_range        = {sample_meta.get('date_start')} → {sample_meta.get('date_end')}")
    print(f"  n_tracked_mmsi    = {sample_meta.get('n_tracked_mmsi')}")
    print("=" * 72)

    if n_obs == 0 or panel.empty:
        payload = {
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model_assumption": ASSUMPTION_MARKOV,
            "no_signal_note": NOTE_NO_SIGNAL,
            "states": STATES,
            "training_sample": {
                **sample_meta,
                "n_transitions": 0,
                "forecast_valid": False,
                "reason": "empty panel — no transitions to estimate",
            },
            "cohorts": {},
            "rolling": {},
            "forecast": {
                "valid": False,
                "reason": "forecast without N>0 observations is invalid",
                "n_training_obs": 0,
                "horizons": {},
            },
            "validation": {
                "status": "skipped",
                "message": INSUFFICIENT_BACKTEST_MSG,
                "calendar_days": 0,
                "metrics": None,
            },
        }
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {out_json} (empty model — insufficient data)")
        return payload

    full_models = estimate_cohort_models(panel)
    n_trans_est = int(sum(m["n_transitions"] for m in full_models.values()))
    print(f"  N_transitions     = {n_trans_est}")
    for c, m in full_models.items():
        flag = "OK" if m["sufficient_sample"] else "LOW_N"
        print(f"  cohort[{c}]: n_trans={m['n_transitions']} n_seq={m['n_sequences']} [{flag}]")

    rolling = estimate_rolling(panel)
    validation = validate_out_of_sample(panel)
    if validation["status"] == "skipped":
        print(f"VALIDATION: {validation['message']} (calendar_days={validation.get('calendar_days')})")
    else:
        print(
            f"VALIDATION OK: laden AE={validation['laden_abs_error_pp']} pp, "
            f"TV={validation['total_variation_distance']}, "
            f"N_train_trans={validation['n_training_transitions']}"
        )

    forecast_block: dict[str, Any]
    if n_trans_est <= 0:
        forecast_block = {
            "valid": False,
            "reason": "forecast without N>0 transitions is invalid",
            "n_training_obs": n_obs,
            "n_training_transitions": 0,
            "horizons": {},
        }
    else:
        mc = monte_carlo_laden_share(panel, full_models, n_trajectories=n_monte_carlo)
        forecast_block = {
            "valid": True,
            "as_of": sample_meta.get("date_end"),
            "n_training_obs": n_obs,
            "n_training_transitions": n_trans_est,
            "n_monte_carlo": n_monte_carlo,
            **mc,
        }
        print(
            f"FORECAST: T+7 laden% mean="
            f"{mc['horizons'].get('T+7', {}).get('laden_dwt_pct_mean')} "
            f"CI=[{mc['horizons'].get('T+7', {}).get('laden_dwt_pct_ci_low')}, "
            f"{mc['horizons'].get('T+7', {}).get('laden_dwt_pct_ci_high')}] "
            f"(N_obs={n_obs}, N_trans={n_trans_est}, MC={n_monte_carlo})"
        )

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_assumption": ASSUMPTION_MARKOV,
        "no_signal_note": NOTE_NO_SIGNAL,
        "states": STATES,
        "training_sample": {
            **sample_meta,
            "n_transitions": n_trans_est,
            "forecast_valid": bool(forecast_block.get("valid")),
            "min_transitions_per_cohort": MIN_TRANSITIONS_PER_COHORT,
        },
        "cohorts": _strip_private(full_models),
        "rolling": rolling,
        "forecast": forecast_block,
        "validation": validation,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_json}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Markov ops-state model for tanker DWT flow")
    parser.add_argument("--by-vessel-dir", type=Path, default=BY_VESSEL_DIR)
    parser.add_argument("--fleet", type=Path, default=FLEET_PATH)
    parser.add_argument("--targets", type=Path, default=TARGETS_PATH)
    parser.add_argument("--out", type=Path, default=OUT_JSON)
    parser.add_argument("--mc", type=int, default=MC_DEFAULT_TRAJECTORIES)
    args = parser.parse_args()
    try:
        run_markov_model(
            by_vessel_dir=args.by_vessel_dir,
            fleet_path=args.fleet,
            targets_path=args.targets,
            out_json=args.out,
            n_monte_carlo=args.mc,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
