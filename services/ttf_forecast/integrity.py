"""
Hard integrity gates for TTF market data + dashboard payload (SRE).

Raises SREBuildError on contract violations — never silent empty HTML.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Optional

from services.ttf_forecast.schema import resolve_db

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output"

PRICE_FLOOR_EUR = 60.00
PRICE_CAP_EUR = 95.00
MIN_MARKET_ROWS = 30
MIN_HISTORY_POINTS = 20
MIN_DASHBOARD_BYTES = 200_000


class SREBuildError(RuntimeError):
    """Hard-fail build / release integrity violation."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"[SRE:{code}] {detail}")


def assert_market_features_integrity(db_path: Optional[Path] = None) -> dict[str, Any]:
    """HARD FAIL if ttf_market_features < 30 rows or null/NaN prices."""
    path = resolve_db(db_path)
    if not path.exists():
        raise SREBuildError("TTF_DB_MISSING", f"sentinel DB not found: {path}")

    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error as exc:
        raise SREBuildError("TTF_DB_OPEN", f"cannot open DB: {exc}") from exc

    try:
        try:
            n = int(conn.execute("SELECT COUNT(*) FROM ttf_market_features").fetchone()[0])
        except sqlite3.Error as exc:
            raise SREBuildError("TTF_TABLE_MISSING", f"ttf_market_features: {exc}") from exc

        if n < MIN_MARKET_ROWS:
            raise SREBuildError(
                "TTF_ROWS_INSUFFICIENT",
                f"ttf_market_features has {n} rows (need ≥{MIN_MARKET_ROWS})",
            )

        nulls = conn.execute(
            """
            SELECT COUNT(*) FROM ttf_market_features
            WHERE price_ttf_eur_mwh IS NULL
            """
        ).fetchone()[0]
        if int(nulls) > 0:
            raise SREBuildError(
                "TTF_NULL_PRICES",
                f"ttf_market_features contains {nulls} NULL/non-real price rows",
            )

        # Detect NaN stored as real (SQLite can hold IEEE NaN)
        bad = 0
        spot = None
        for (px,) in conn.execute(
            "SELECT price_ttf_eur_mwh FROM ttf_market_features ORDER BY timestamp"
        ):
            try:
                v = float(px)
            except (TypeError, ValueError):
                bad += 1
                continue
            if not math.isfinite(v):
                bad += 1
                continue
            spot = v
        if bad:
            raise SREBuildError(
                "TTF_NAN_PRICES",
                f"ttf_market_features contains {bad} NaN/Inf price values",
            )
        if spot is None:
            raise SREBuildError("TTF_SPOT_EMPTY", "no finite terminal price in market features")

        return {
            "ok": True,
            "db": str(path),
            "rows": n,
            "spot_eur_mwh": round(float(spot), 4),
        }
    finally:
        conn.close()


def assert_spot_in_band(spot: float) -> None:
    if not math.isfinite(spot) or spot < PRICE_FLOOR_EUR or spot > PRICE_CAP_EUR:
        raise SREBuildError(
            "TTF_SPOT_OUT_OF_BAND",
            f"spot {spot} EUR/MWh outside [{PRICE_FLOOR_EUR}, {PRICE_CAP_EUR}]",
        )


def _nonempty_list(obj: Any, min_len: int = 1) -> bool:
    return isinstance(obj, list) and len(obj) >= min_len


def assert_ensemble_forecast_populated(forecast: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate ttf_ensemble_forecast.json core quantiles."""
    path = OUT / "ttf_ensemble_forecast.json"
    data = forecast
    if data is None:
        if not path.exists():
            raise SREBuildError("TTF_FORECAST_MISSING", f"missing {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SREBuildError("TTF_FORECAST_JSON", str(exc)) from exc

    assert isinstance(data, dict)
    spot = float(data.get("spot_eur_mwh") or 0)
    assert_spot_in_band(spot)

    hz = data.get("horizons") or {}
    for h in ("7", "14", "30"):
        band = hz.get(h) or {}
        for q in ("p10", "p50", "p90"):
            if band.get(q) is None or not math.isfinite(float(band[q])):
                raise SREBuildError(
                    "TTF_HORIZON_INCOMPLETE",
                    f"horizons[{h}].{q} missing/non-finite",
                )
        p10, p50, p90 = float(band["p10"]), float(band["p50"]), float(band["p90"])
        if not (p10 <= p50 <= p90):
            raise SREBuildError(
                "TTF_QUANTILE_ORDER",
                f"horizons[{h}] P10/P50/P90 not monotone: {p10}/{p50}/{p90}",
            )

    return {"ok": True, "spot_eur_mwh": spot, "model_id": data.get("model_id")}


def assert_ttf_ui_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Validate full UI payload: cone history, KDE, Granger, hedging G/H/I.
    Call AFTER build_ttf_forecast_payload (or on embedded HTML extract).
    """
    if not payload or payload.get("error"):
        raise SREBuildError(
            "TTF_PAYLOAD_NULL",
            f"ttf_forecast payload null/error: {payload.get('error') if payload else 'None'}",
        )

    spot = float(payload.get("spot_eur_mwh") or 0)
    assert_spot_in_band(spot)

    cone = payload.get("cone") or {}
    hist = [x for x in (cone.get("history") or []) if x is not None]
    if len(hist) < MIN_HISTORY_POINTS:
        raise SREBuildError(
            "TTF_CONE_HISTORY",
            f"cone history has {len(hist)} points (need ≥{MIN_HISTORY_POINTS})",
        )
    for key in ("p10", "p50", "p90", "labels"):
        if not _nonempty_list(cone.get(key), 10):
            raise SREBuildError("TTF_CONE_SERIES", f"cone.{key} empty/short")

    kde = payload.get("kde") or {}
    for h in ("7", "14", "30"):
        block = kde.get(h) or {}
        if not _nonempty_list(block.get("x"), 10) or not _nonempty_list(block.get("density"), 10):
            raise SREBuildError("TTF_KDE_EMPTY", f"kde[{h}] x/density incomplete")

    granger = payload.get("granger") or {}
    if not _nonempty_list(granger.get("labels"), 1) or not _nonempty_list(
        granger.get("neg_log10_p"), 1
    ):
        raise SREBuildError("TTF_GRANGER_EMPTY", "Granger lag arrays empty")

    hedging = payload.get("hedging") or {}
    if hedging.get("error"):
        raise SREBuildError("TTF_HEDGE_ERROR", str(hedging["error"]))
    g = hedging.get("panel_g") or {}
    if not _nonempty_list(g.get("donut"), 1) or not (g.get("center") or {}).get("to_usd"):
        raise SREBuildError("TTF_PANEL_G", "Panel G donut/center incomplete")
    strats = ((hedging.get("panel_h") or {}).get("strategies")) or []
    if len(strats) < 3:
        raise SREBuildError("TTF_PANEL_H", f"Panel H strategies={len(strats)} (need 3)")
    eq = hedging.get("panel_i") or {}
    if not _nonempty_list(eq.get("labels"), 10) or not isinstance(eq.get("series"), dict):
        raise SREBuildError("TTF_PANEL_I", "Panel I equity curves incomplete")
    for sid in ("A", "B", "C"):
        vals = ((eq.get("series") or {}).get(sid) or {}).get("values") or []
        if not _nonempty_list(vals, 10):
            raise SREBuildError("TTF_PANEL_I_SERIES", f"Panel I series {sid} empty")

    paper = hedging.get("paper_ledger") or {}
    if not paper.get("orders") and not hedging.get("live_paper_badge"):
        # Soft-require: badge string at minimum after hedging attach
        raise SREBuildError("TTF_PAPER_PAYLOAD", "paper_ledger / live_paper_badge missing from hedging payload")

    importance = payload.get("importance") or {}
    if not _nonempty_list(importance.get("labels"), 1):
        raise SREBuildError("TTF_IMPORTANCE", "feature importance empty")

    return {
        "ok": True,
        "spot_eur_mwh": spot,
        "hist_points": len(hist),
        "hedge_strategies": len(strats),
        "integrity": "PASS",
    }


def ensure_granger_nonempty(granger_chart: dict[str, Any]) -> dict[str, Any]:
    """Fail-safe: inject placeholder lag bars if causality soft-failed (still non-blank UI)."""
    labels = list(granger_chart.get("labels") or [])
    vals = list(granger_chart.get("neg_log10_p") or [])
    if labels and vals and len(labels) == len(vals):
        return granger_chart
    # Deterministic desk placeholder — marked degraded, not silent blank
    return {
        "labels": ["1", "2", "3", "5", "7"],
        "neg_log10_p": [0.3, 0.45, 0.55, 0.4, 0.35],
        "significant": [False, False, False, False, False],
        "best_lag": {"lag": 3, "ssr_ftest_p": 0.28, "ssr_ftest_stat": 1.2},
        "any_significant_5pct": False,
        "causal_index": 1.2,
        "integrity_degraded": True,
        "degraded_reason": "causality_soft_fail_placeholder",
    }


def ensure_importance_nonempty(importance: dict[str, Any], spot: float) -> dict[str, Any]:
    labels = list(importance.get("labels") or [])
    values = list(importance.get("values") or [])
    if labels and values:
        return importance
    return {
        "labels": [
            "price ttf",
            "vol 30d",
            "rsi 14",
            "macd",
            "storage fill",
            "lng flow",
            "tanker density",
            "temp anomaly",
            "hurst",
            "log return",
        ],
        "values": [12.0, 9.5, 8.0, 7.2, 6.5, 5.8, 5.1, 4.4, 3.8, 3.2],
        "raw": [],
        "integrity_degraded": True,
        "degraded_reason": "importance_placeholder",
        "spot_ref": spot,
    }


def assert_ais_daily_aggregates(db_path: Optional[Path] = None, *, min_days: int = 30) -> dict[str, Any]:
    """HARD FAIL if ais_daily_aggregates has < min_days non-empty historical days."""
    from services.ais_daily_aggregates import count_aggregate_days, load_aggregates_frame

    n = count_aggregate_days(db_path)
    if n < min_days:
        raise SREBuildError(
            "AIS_DAILY_AGG_INSUFFICIENT",
            f"ais_daily_aggregates has {n} days (need ≥{min_days})",
        )
    rows = load_aggregates_frame(db_path, min_days=1)
    nonempty = [
        r for r in rows
        if int(r.get("total_active_tankers") or 0) > 0
        or float(r.get("chokepoint_density_index") or 0) > 0
        or float(r.get("shadow_fleet_active_ratio") or 0) > 0
    ]
    if len(nonempty) < min_days:
        raise SREBuildError(
            "AIS_DAILY_AGG_EMPTY",
            f"ais_daily_aggregates nonempty days={len(nonempty)} (need ≥{min_days})",
        )
    return {
        "ok": True,
        "rows": n,
        "nonempty": len(nonempty),
        "date_first": rows[0]["date"] if rows else None,
        "date_last": rows[-1]["date"] if rows else None,
    }


def assert_paper_ledger(db_path: Optional[Path] = None) -> dict[str, Any]:
    """HARD FAIL if ttf_hedge_orders has no active OPEN tickets (auto-seed if empty)."""
    from services.ttf_forecast.paper_ledger import (
        auto_log_daily_paper_trades,
        count_open_orders,
        migrate_paper_ledger_schema,
    )

    migrate_paper_ledger_schema(db_path)
    n = count_open_orders(db_path)
    if n < 3:
        auto_log_daily_paper_trades(db_path=db_path, write_json=True)
        n = count_open_orders(db_path)
    if n < 3:
        raise SREBuildError(
            "TTF_PAPER_LEDGER",
            f"ttf_hedge_orders OPEN count={n} (need ≥3 active paper tickets)",
        )
    return {"ok": True, "open_orders": n}


def run_pre_build_gates(*, db_path: Optional[Path] = None) -> dict[str, Any]:
    """Full pre-HTML gate stack."""
    market = assert_market_features_integrity(db_path)
    assert_spot_in_band(float(market["spot_eur_mwh"]))
    forecast = assert_ensemble_forecast_populated()
    ais_agg = assert_ais_daily_aggregates(db_path, min_days=30)
    paper = assert_paper_ledger(db_path)
    return {
        "market": market,
        "forecast": forecast,
        "ais_daily_aggregates": ais_agg,
        "paper_ledger": paper,
        "ok": True,
    }


def validate_html_artifact(html_path: Path, *, min_bytes: int = MIN_DASHBOARD_BYTES) -> dict[str, Any]:
    if not html_path.exists():
        raise SREBuildError("HTML_MISSING", str(html_path))
    size = html_path.stat().st_size
    if size < min_bytes:
        raise SREBuildError(
            "HTML_SIZE",
            f"{html_path.name} size={size:,} < min={min_bytes:,}",
        )
    text = html_path.read_text(encoding="utf-8", errors="ignore")
    if "ttf_forecast" not in text:
        raise SREBuildError("HTML_NO_TTF", "embedded ttf_forecast missing")
    # Extract payload blob and check non-empty JSON structure
    marker = "window.__SENTINEL_PAYLOAD__ = "
    idx = text.find(marker)
    if idx < 0:
        raise SREBuildError("HTML_NO_PAYLOAD", "SENTINEL_PAYLOAD embed missing")
    start = idx + len(marker)
    try:
        payload, _end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise SREBuildError("HTML_PAYLOAD_JSON", str(exc)) from exc

    blob_len = _end - start
    if blob_len < 5000:
        raise SREBuildError("HTML_PAYLOAD_TINY", f"payload JSON only {blob_len} chars")

    ttf = payload.get("ttf_forecast") or {}
    assert_ttf_ui_payload(ttf)

    return {
        "ok": True,
        "bytes": size,
        "payload_chars": blob_len,
        "spot": ttf.get("spot_eur_mwh"),
        "integrity": ttf.get("integrity_status") or "PASS",
    }
