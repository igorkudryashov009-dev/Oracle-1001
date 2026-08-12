"""
Spectral analysis of the DWT-flow timeseries (Prompt 1 output).

1) Fourier / periodogram → top frequencies by power
2) Hard length gates: detecting period T needs ≥ 2–3×T observations
   (Nyquist + noise margin). Monthly/quarterly claims are refused on short archives.
3) Increment normality: Shapiro–Wilk + Jarque–Bera + QQ plot.
   Explicit "приращения НЕ нормальны" when rejected; propose Student-t / empirical
   quantiles for CIs — never silently assume Gaussian.
4) Elliott wave is OPTIONAL and lives in elliott_wave_heuristic.py with
   method_type = discretionary_heuristic_not_statistically_validated.
   Never blended into spectral scores.

Outputs (offline):
  features/spectral_report.json
  features/plots/periodogram_{series}.png
  features/plots/increments_hist_qq_{series}.png

No data dredging: significance thresholds and length gates are fixed a priori.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats
from scipy.fft import rfft, rfftfreq

ROOT = Path(__file__).resolve().parent
FEATURES_DIR = ROOT / "features"
TIMESERIES_PATH = FEATURES_DIR / "dwt_flow_timeseries.parquet"
OUT_REPORT = FEATURES_DIR / "spectral_report.json"
PLOTS_DIR = FEATURES_DIR / "plots"

# ---- Fixed a priori gates (do NOT tune to make spectra "pretty") ----
MIN_POINTS_FOR_ANY_SPECTRUM = 14          # ~2 weeks daily
MIN_CYCLES_FOR_CLAIM = 2.5               # need ≥2.5 full periods of T
WEEKLY_PERIOD_DAYS = 7.0
MONTHLY_PERIOD_DAYS = 30.0
QUARTERLY_PERIOD_DAYS = 90.0
TOP_K_FREQUENCIES = 5
NORMALITY_ALPHA = 0.05                   # fixed; not optimized
SHAPIRO_MAX_N = 5000                     # scipy Shapiro limit practice

CYCLE_CLAIMS = (
    ("weekly", WEEKLY_PERIOD_DAYS),
    ("monthly", MONTHLY_PERIOD_DAYS),
    ("quarterly", QUARTERLY_PERIOD_DAYS),
)

VALUE_COL_DEFAULT = "laden_dwt_sum"


# ---------------------------------------------------------------------------
# Series preparation
# ---------------------------------------------------------------------------

def load_dwt_flow_series(
    path: Path = TIMESERIES_PATH,
    *,
    value_col: str = VALUE_COL_DEFAULT,
    cargo_class: Optional[str] = None,
    aggregate: str = "sum",
) -> pd.DataFrame:
    """Return daily series with columns: date, value, coverage_pct_vessels (mean)."""
    if not path.exists():
        return pd.DataFrame(columns=["date", "value", "coverage_pct_vessels", "n_rows"])

    df = pd.read_parquet(path)
    if df.empty:
        return pd.DataFrame(columns=["date", "value", "coverage_pct_vessels", "n_rows"])

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"])
    if cargo_class is not None:
        df = df[df["cargo_class"] == cargo_class]
    if df.empty:
        return pd.DataFrame(columns=["date", "value", "coverage_pct_vessels", "n_rows"])

    if value_col not in df.columns:
        raise KeyError(f"column '{value_col}' not in timeseries")

    grouped = (
        df.groupby("date", as_index=False)
        .agg(
            value=(value_col, aggregate),
            coverage_pct_vessels=("coverage_pct_vessels", "mean"),
            n_rows=("cargo_class", "count"),
        )
        .sort_values("date")
    )
    # Fill calendar gaps with NaN (FFT will use linear interpolate only for transform,
    # but length / reliability checks use observed count separately)
    full_idx = pd.date_range(grouped["date"].min(), grouped["date"].max(), freq="D")
    grouped = grouped.set_index("date").reindex(full_idx).rename_axis("date").reset_index()
    return grouped


def series_length_assessment(n_obs: int, n_calendar: int) -> dict[str, Any]:
    """Nyquist + multi-cycle gate for weekly/monthly/quarterly claims."""
    claims = {}
    for name, period in CYCLE_CLAIMS:
        need = int(np.ceil(MIN_CYCLES_FOR_CLAIM * period))
        # Also Nyquist: sampling daily → detectable periods > 2 days; period itself
        # must satisfy n_obs >= 2*period for even one cycle at Nyquist margin,
        # we require MIN_CYCLES_FOR_CLAIM * T.
        ok = n_obs >= need
        claims[name] = {
            "period_days": period,
            "min_observations_required": need,
            "n_observations": n_obs,
            "detectable_reliably": bool(ok),
            "reason": (
                "ok"
                if ok
                else (
                    f"need ≥{need} observations (~{MIN_CYCLES_FOR_CLAIM}×{period:.0f}d) "
                    f"for a reliable {name} cycle claim; have {n_obs}"
                ),
            ),
        }

    short_archive = n_calendar < 60 or n_obs < 60
    return {
        "n_observations_non_nan": n_obs,
        "n_calendar_days_span": n_calendar,
        "short_archive_flag": short_archive,
        "short_archive_message": (
            None
            if not short_archive
            else (
                "архив короче ~60–90 дней: месячные/квартальные циклы физически "
                "не могут быть надёжно обнаружены; не интерпретируйте спектр "
                "как подтверждение таких периодов"
            )
        ),
        "cycle_detectability": claims,
    }


# ---------------------------------------------------------------------------
# Fourier / periodogram
# ---------------------------------------------------------------------------

def compute_periodogram(
    values: np.ndarray,
    *,
    sample_spacing_days: float = 1.0,
) -> dict[str, Any]:
    """Real FFT power spectrum. values must be finite, demeaned."""
    x = np.asarray(values, dtype=float)
    n = len(x)
    if n < MIN_POINTS_FOR_ANY_SPECTRUM:
        return {
            "ok": False,
            "reason": (
                f"n={n} < {MIN_POINTS_FOR_ANY_SPECTRUM}: spectrum not computed "
                "(insufficient length even for weekly-scale exploration)"
            ),
            "frequencies_per_day": [],
            "periods_days": [],
            "power": [],
            "top_frequencies": [],
        }

    x = x - np.nanmean(x)
    # replace any residual nan (shouldn't) with 0 after demean
    x = np.nan_to_num(x, nan=0.0)

    yf = rfft(x)
    power = (np.abs(yf) ** 2) / n
    freqs = rfftfreq(n, d=sample_spacing_days)

    # Drop DC (freq ~ 0)
    mask = freqs > 0
    freqs, power = freqs[mask], power[mask]
    periods = np.where(freqs > 0, 1.0 / freqs, np.nan)

    order = np.argsort(power)[::-1]
    top = []
    for idx in order[:TOP_K_FREQUENCIES]:
        top.append(
            {
                "rank": len(top) + 1,
                "frequency_per_day": round(float(freqs[idx]), 6),
                "period_days": round(float(periods[idx]), 3),
                "power": round(float(power[idx]), 6),
                "power_share": round(float(power[idx] / power.sum()), 4) if power.sum() > 0 else 0.0,
            }
        )

    return {
        "ok": True,
        "n_fft": n,
        "frequencies_per_day": freqs.tolist(),
        "periods_days": periods.tolist(),
        "power": power.tolist(),
        "top_frequencies": top,
        "note": (
            "Top frequencies ranked by raw periodogram power only — "
            "no stepwise frequency fishing / Bonferroni tuning."
        ),
    }


def annotate_top_with_reliability(
    top: list[dict],
    length_assess: dict[str, Any],
) -> list[dict]:
    """Flag each top peak if claiming its period is unsupported by sample length."""
    n_obs = length_assess["n_observations_non_nan"]
    out = []
    for item in top:
        t = float(item["period_days"])
        need = int(np.ceil(MIN_CYCLES_FOR_CLAIM * t))
        reliable = n_obs >= need and t >= 2.0  # Nyquist: period ≥ 2 samples
        tagged = dict(item)
        tagged["claim_reliable"] = bool(reliable)
        tagged["min_obs_for_this_period"] = need
        if not reliable:
            tagged["reliability_warning"] = (
                f"period≈{t:.1f}d would need ≥{need} obs for a reliable claim; n={n_obs}"
            )
        out.append(tagged)
    return out


# ---------------------------------------------------------------------------
# Normality of increments
# ---------------------------------------------------------------------------

def normality_of_increments(values: np.ndarray, alpha: float = NORMALITY_ALPHA) -> dict[str, Any]:
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return {
            "n_increments": int(max(0, len(x) - 1)) if len(x) else 0,
            "verdict": "insufficient_data",
            "verdict_ru": "недостаточно данных для теста нормальности приращений",
            "tests": {},
            "ci_recommendation": {
                "prefer": "empirical_quantiles",
                "reason": "too few increments",
            },
        }

    inc = np.diff(x)
    inc = inc[np.isfinite(inc)]
    n = len(inc)
    if n < 8:
        return {
            "n_increments": n,
            "verdict": "insufficient_data",
            "verdict_ru": "недостаточно данных для теста нормальности приращений",
            "tests": {},
            "ci_recommendation": {
                "prefer": "empirical_quantiles",
                "reason": "n_increments < 8",
            },
        }

    # Shapiro–Wilk (subsample if huge — but we won't dredge; fixed cap)
    shapiro_x = inc if n <= SHAPIRO_MAX_N else inc[:SHAPIRO_MAX_N]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sw_stat, sw_p = stats.shapiro(shapiro_x)
    jb_stat, jb_p = stats.jarque_bera(inc)

    sw_reject = bool(sw_p < alpha)
    jb_reject = bool(jb_p < alpha)
    not_normal = sw_reject or jb_reject

    # Student-t df via MLE (scipy); may fail on tiny samples
    t_df = None
    t_fit_ok = False
    try:
        df_hat, loc_hat, scale_hat = stats.t.fit(inc)
        t_df = float(df_hat)
        t_fit_ok = True
    except Exception as exc:  # noqa: BLE001
        t_fit_ok = False
        t_fit_error = str(exc)

    empir_q = {
        "q05": round(float(np.quantile(inc, 0.05)), 6),
        "q25": round(float(np.quantile(inc, 0.25)), 6),
        "q50": round(float(np.quantile(inc, 0.50)), 6),
        "q75": round(float(np.quantile(inc, 0.75)), 6),
        "q95": round(float(np.quantile(inc, 0.95)), 6),
    }

    if not_normal:
        verdict = "increments_NOT_normal"
        verdict_ru = "приращения НЕ нормальны"
        if t_fit_ok and t_df is not None and t_df < 30:
            prefer = "student_t"
            reason = (
                f"Shapiro/JB reject normality at α={alpha}; "
                f"use Student-t (df̂={t_df:.2f}) or empirical quantiles for CIs — "
                "do NOT use Gaussian intervals"
            )
        else:
            prefer = "empirical_quantiles"
            reason = (
                f"Shapiro/JB reject normality at α={alpha}; "
                "prefer empirical quantiles (or heavy-tailed parametric) for CIs — "
                "do NOT use Gaussian intervals"
            )
    else:
        verdict = "increments_consistent_with_normal"
        verdict_ru = "приращения совместимы с нормальностью (не доказательство нормальности)"
        prefer = "gaussian_or_empirical"
        reason = (
            f"tests did not reject at α={alpha}; still prefer reporting empirical "
            "quantiles alongside any Gaussian CI for robustness"
        )

    out: dict[str, Any] = {
        "n_increments": n,
        "alpha": alpha,
        "verdict": verdict,
        "verdict_ru": verdict_ru,
        "tests": {
            "shapiro_wilk": {
                "statistic": round(float(sw_stat), 6),
                "p_value": float(sw_p),
                "reject_normal_at_alpha": sw_reject,
                "n_used": int(len(shapiro_x)),
            },
            "jarque_bera": {
                "statistic": round(float(jb_stat), 6),
                "p_value": float(jb_p),
                "reject_normal_at_alpha": jb_reject,
            },
        },
        "skewness": round(float(stats.skew(inc)), 6),
        "excess_kurtosis": round(float(stats.kurtosis(inc)), 6),
        "empirical_quantiles": empir_q,
        "student_t_fit": {
            "ok": t_fit_ok,
            "df": None if t_df is None else round(t_df, 4),
            "loc": None if not t_fit_ok else round(float(loc_hat), 6),
            "scale": None if not t_fit_ok else round(float(scale_hat), 6),
        },
        "ci_recommendation": {
            "prefer": prefer,
            "reason": reason,
            "do_not_use": "gaussian_ci_alone" if not_normal else None,
        },
    }
    if not t_fit_ok:
        out["student_t_fit"]["error"] = locals().get("t_fit_error")
    return out


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def _setup_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def plot_periodogram(
    spectrum: dict[str, Any],
    length_assess: dict[str, Any],
    out_path: Path,
    title: str,
) -> Optional[Path]:
    if not spectrum.get("ok"):
        return None
    plt = _setup_matplotlib()
    periods = np.asarray(spectrum["periods_days"], dtype=float)
    power = np.asarray(spectrum["power"], dtype=float)
    # Focus display on periods between 2 and min(n/2, 180)
    n = spectrum["n_fft"]
    max_p = min(n / 2.0, 180.0)
    mask = (periods >= 2.0) & (periods <= max_p)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(periods[mask], power[mask], color="#1f4e79", lw=1.2)
    ax.set_xlabel("Period (days)")
    ax.set_ylabel("Power")
    ax.set_title(title)
    ax.axvline(7, color="#c45911", ls="--", lw=0.9, label="7d weekly ref")
    ax.axvline(30, color="#548235", ls="--", lw=0.9, label="30d monthly ref")
    ax.axvline(90, color="#833c0c", ls="--", lw=0.9, label="90d quarterly ref")
    if length_assess.get("short_archive_flag"):
        ax.text(
            0.02,
            0.95,
            "SHORT ARCHIVE: monthly/quarterly claims not reliable",
            transform=ax.transAxes,
            va="top",
            fontsize=9,
            color="#c00000",
            bbox=dict(boxstyle="round", facecolor="#fff2cc", edgecolor="#c00000", alpha=0.9),
        )
    ax.legend(loc="upper right", fontsize=8)
    ax.set_xlim(2, max_p)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_increments_hist_qq(
    values: np.ndarray,
    normality: dict[str, Any],
    out_path: Path,
    title: str,
) -> Optional[Path]:
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return None
    inc = np.diff(x)
    inc = inc[np.isfinite(inc)]
    if len(inc) < 3:
        return None

    plt = _setup_matplotlib()
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))

    # Histogram + overlays
    ax = axes[0]
    ax.hist(inc, bins=min(30, max(8, len(inc) // 3)), density=True, color="#9dc3e6", edgecolor="#1f4e79")
    xs = np.linspace(inc.min(), inc.max(), 200)
    # Gaussian fit (for contrast — even if rejected)
    mu, sigma = float(np.mean(inc)), float(np.std(inc, ddof=1) or 1.0)
    ax.plot(xs, stats.norm.pdf(xs, mu, sigma), "r-", lw=1.5, label="Gaussian fit")
    st = normality.get("student_t_fit") or {}
    if st.get("ok") and st.get("df") is not None:
        ax.plot(
            xs,
            stats.t.pdf(xs, st["df"], loc=st["loc"], scale=st["scale"]),
            color="#548235",
            lw=1.5,
            label=f"Student-t df={st['df']:.1f}",
        )
    ax.set_title("Increments density")
    ax.set_xlabel("Δ index")
    ax.legend(fontsize=8)
    ax.text(
        0.02,
        0.98,
        normality.get("verdict_ru", ""),
        transform=ax.transAxes,
        va="top",
        fontsize=9,
        color="#c00000" if "НЕ" in str(normality.get("verdict_ru", "")) else "#375623",
    )

    # QQ vs normal
    ax2 = axes[1]
    stats.probplot(inc, dist="norm", plot=ax2)
    ax2.set_title("QQ plot vs Normal")
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def analyze_series(
    series: pd.DataFrame,
    *,
    label: str,
    plots_dir: Path = PLOTS_DIR,
) -> dict[str, Any]:
    vals_obs = series["value"].to_numpy(dtype=float)
    n_obs = int(np.isfinite(vals_obs).sum())
    n_calendar = int(len(series))
    length_assess = series_length_assessment(n_obs, n_calendar)

    # Interpolate only for FFT continuity; reliability uses n_obs
    if n_obs >= 2:
        interp = pd.Series(vals_obs).interpolate(limit_direction="both").to_numpy(dtype=float)
    else:
        interp = vals_obs

    spectrum = compute_periodogram(interp if n_obs >= MIN_POINTS_FOR_ANY_SPECTRUM else vals_obs[:0])
    if spectrum.get("ok"):
        spectrum["top_frequencies"] = annotate_top_with_reliability(
            spectrum["top_frequencies"], length_assess
        )

    # Normality on observed (non-interpolated) increments where possible
    obs_only = vals_obs[np.isfinite(vals_obs)]
    normality = normality_of_increments(obs_only)

    plots = {}
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    if spectrum.get("ok"):
        p1 = plots_dir / f"periodogram_{safe_label}.png"
        got = plot_periodogram(
            spectrum,
            length_assess,
            p1,
            title=f"Periodogram — {label}",
        )
        if got:
            plots["periodogram"] = str(got)
    p2 = plots_dir / f"increments_hist_qq_{safe_label}.png"
    got2 = plot_increments_hist_qq(obs_only, normality, p2, title=f"Increments — {label}")
    if got2:
        plots["increments_hist_qq"] = str(got2)

    # Honest summary of what may be claimed
    detectable = {
        k: v["detectable_reliably"] for k, v in length_assess["cycle_detectability"].items()
    }
    reliable_tops = [
        t for t in spectrum.get("top_frequencies", []) if t.get("claim_reliable")
    ]

    return {
        "label": label,
        "n_observations_non_nan": n_obs,
        "n_calendar_days_span": n_calendar,
        "date_start": str(series["date"].iloc[0].date()) if n_calendar else None,
        "date_end": str(series["date"].iloc[-1].date()) if n_calendar else None,
        "length_assessment": length_assess,
        "detectable_cycle_classes": detectable,
        "spectrum": {
            "ok": spectrum.get("ok", False),
            "reason": spectrum.get("reason"),
            "n_fft": spectrum.get("n_fft"),
            "top_frequencies": spectrum.get("top_frequencies", []),
            "n_reliable_top_peaks": len(reliable_tops),
            "note": spectrum.get("note"),
        },
        "normality_increments": normality,
        "plots": plots,
        "elliott_wave": {
            "included_here": False,
            "note": (
                "Elliott wave output is produced only by elliott_wave_heuristic.py "
                "with method_type=discretionary_heuristic_not_statistically_validated "
                "and must not be mixed into spectral scores."
            ),
        },
    }


def run_spectral_analysis(
    *,
    timeseries_path: Path = TIMESERIES_PATH,
    out_report: Path = OUT_REPORT,
    plots_dir: Path = PLOTS_DIR,
    value_col: str = VALUE_COL_DEFAULT,
    run_elliott: bool = False,
) -> dict[str, Any]:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    base = load_dwt_flow_series(timeseries_path, value_col=value_col, cargo_class=None)
    analyses = []

    # Aggregate total laden DWT across cargo classes
    analyses.append(analyze_series(base, label=f"all__{value_col}", plots_dir=plots_dir))

    # Per cargo class if data present
    if timeseries_path.exists():
        raw = pd.read_parquet(timeseries_path)
        if not raw.empty and "cargo_class" in raw.columns:
            for cc in sorted(raw["cargo_class"].dropna().unique()):
                s = load_dwt_flow_series(
                    timeseries_path, value_col=value_col, cargo_class=str(cc)
                )
                if s["value"].notna().sum() >= 3:
                    analyses.append(
                        analyze_series(s, label=f"{cc}__{value_col}", plots_dir=plots_dir)
                    )

    primary = analyses[0]
    print("=" * 72)
    print("SPECTRAL ANALYSIS — sample length disclosure")
    print(f"  N_obs (non-NaN)   = {primary['n_observations_non_nan']}")
    print(f"  calendar span     = {primary['n_calendar_days_span']}")
    print(f"  detectable        = {primary['detectable_cycle_classes']}")
    if primary["length_assessment"].get("short_archive_message"):
        print(f"  WARNING: {primary['length_assessment']['short_archive_message']}")
    print(f"  normality verdict = {primary['normality_increments'].get('verdict_ru')}")
    print("=" * 72)

    elliott_block: dict[str, Any] = {
        "run": False,
        "method_type": "discretionary_heuristic_not_statistically_validated",
        "separated_from_spectral_scores": True,
    }
    if run_elliott:
        try:
            from elliott_wave_heuristic import run_elliott_heuristic

            elliott_block = run_elliott_heuristic(base)
            elliott_block["separated_from_spectral_scores"] = True
        except Exception as exc:  # noqa: BLE001
            elliott_block = {
                "run": True,
                "error": str(exc),
                "method_type": "discretionary_heuristic_not_statistically_validated",
                "separated_from_spectral_scores": True,
            }

    report = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": str(timeseries_path),
        "value_col": value_col,
        "methodology_notes": {
            "no_data_dredging": True,
            "fixed_alpha_normality": NORMALITY_ALPHA,
            "min_cycles_for_period_claim": MIN_CYCLES_FOR_CLAIM,
            "nyquist_note": (
                "Daily sampling: theoretical minimum period is 2 days. "
                "Reliable claim for period T requires ≥ "
                f"{MIN_CYCLES_FOR_CLAIM}×T observations."
            ),
        },
        "analyses": analyses,
        "elliott_wave_heuristic": elliott_block,
        "dashboard_separation_rule": (
            "Spectral/normality results (analyses[]) and Elliott heuristic "
            "must be shown as separate panels/sources — never summed into one score."
        ),
    }

    # Compact JSON: drop bulky full frequency arrays if present (already not stored)
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_report}")
    for a in analyses:
        for k, p in a.get("plots", {}).items():
            print(f"  plot[{a['label']}/{k}]: {p}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Spectral analysis of DWT-flow index")
    parser.add_argument("--timeseries", type=Path, default=TIMESERIES_PATH)
    parser.add_argument("--out", type=Path, default=OUT_REPORT)
    parser.add_argument("--plots-dir", type=Path, default=PLOTS_DIR)
    parser.add_argument("--value-col", default=VALUE_COL_DEFAULT)
    parser.add_argument(
        "--elliott",
        action="store_true",
        help="Also run discretionary Elliott heuristic (separate block, not scored with FFT)",
    )
    args = parser.parse_args()
    run_spectral_analysis(
        timeseries_path=args.timeseries,
        out_report=args.out,
        plots_dir=args.plots_dir,
        value_col=args.value_col,
        run_elliott=args.elliott,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
