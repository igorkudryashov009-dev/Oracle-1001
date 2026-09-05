"""
Release orchestrator for Oracle-1001 / Sentinel dual-plane + TTF Forecast.

Idempotent enterprise chain:
  1) OSINT Fleet Registry QC (optional)
  2) DB sync validation / remote replication hook
  3) sqlite_retention
  4) TTF: ingest → spectral/HMM → CatBoost ensemble (Spectral+Markov fallback)
  5) Sentinel / TOP-500 dashboards (inject TTF sheet)
  6) Verify artifact size + HTTP :8765 + open TTF sheet

Usage:
  .\\venv\\Scripts\\python.exe run_release.py --skip-remote
  .\\venv\\Scripts\\python.exe run_release.py --skip-heavy --skip-remote
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / "venv" / "Scripts" / "python.exe"
SENTINEL_HTML = ROOT / "output" / "sentinel_dashboard.html"
MIN_DASHBOARD_BYTES = 220_000
HEALTH_URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html"
TTF_URL = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=ttf"
PERF_LOG = ROOT / "logs" / "release_perf.json"


def _py() -> str:
    return str(PY if PY.exists() else sys.executable)


def _rss_mb() -> float | None:
    try:
        import psutil  # type: ignore

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
    except Exception:
        return None


def run_step(name: str, script: str, args: list[str] | None = None) -> int:
    cmd = [_py(), str(ROOT / script)] + (args or [])
    print()
    print("=" * 72)
    print(f"STEP: {name}")
    print("CMD :", " ".join(cmd))
    print("=" * 72)
    t0 = time.perf_counter()
    rss0 = _rss_mb()
    proc = subprocess.run(cmd, cwd=str(ROOT))
    dt = round(time.perf_counter() - t0, 3)
    rss1 = _rss_mb()
    print(f"EXIT {name}: {proc.returncode} · {dt}s · RSS {rss0}→{rss1} MB")
    _perf_records.append(
        {"step": name, "exit": int(proc.returncode), "seconds": dt, "rss_mb_before": rss0, "rss_mb_after": rss1}
    )
    return int(proc.returncode)


_perf_records: list[dict] = []


def ensure_dirs() -> None:
    (ROOT / "output").mkdir(parents=True, exist_ok=True)
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    (ROOT / "output" / "models").mkdir(parents=True, exist_ok=True)


def validate_local_db() -> int:
    candidates = [
        ROOT / "история1" / "sentinel_ais.db",
        ROOT / "sentinel_ais.db",
    ]
    db = next((p for p in candidates if p.exists()), None)
    print()
    print("=" * 72)
    print("STEP: 2 database fetch/sync validation")
    print("=" * 72)
    if db is None:
        print("WARN: sentinel_ais.db missing locally — dashboard may use synthetic_seed")
        return 0
    size = db.stat().st_size
    age_h = (time.time() - db.stat().st_mtime) / 3600.0
    print(f"OK DB={db} size={size:,} age_h={age_h:.2f}")
    if size < 100_000:
        print("WARN: DB unusually small — consider SCP from London/Korolev")
    return 0


def run_ttf_chain(*, skip_catboost: bool = False) -> int:
    print()
    print("=" * 72)
    print("STEP: TTF Forecasting Engine")
    print("=" * 72)
    t0 = time.perf_counter()
    rss0 = _rss_mb()
    try:
        from services.ttf_forecast.run_ttf_pipeline import run_ttf_release_pipeline

        summary = run_ttf_release_pipeline(skip_catboost=skip_catboost)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL TTF pipeline import/run: {exc}")
        _perf_records.append(
            {
                "step": "TTF pipeline",
                "exit": 1,
                "seconds": round(time.perf_counter() - t0, 3),
                "rss_mb_before": rss0,
                "rss_mb_after": _rss_mb(),
                "error": str(exc),
            }
        )
        return 1

    dt = round(time.perf_counter() - t0, 3)
    rss1 = _rss_mb()
    code = 0 if summary.get("ok") else 1
    print(json.dumps({
        "ok": summary.get("ok"),
        "mode": summary.get("mode"),
        "spot": summary.get("spot"),
        "horizons": summary.get("horizons"),
        "optimal_range": summary.get("optimal_range"),
        "total_seconds": summary.get("total_seconds"),
    }, indent=2, ensure_ascii=False))
    _perf_records.append(
        {
            "step": "TTF pipeline",
            "exit": code,
            "seconds": dt,
            "rss_mb_before": rss0,
            "rss_mb_after": rss1,
            "mode": summary.get("mode"),
            "module_perf": summary.get("perf"),
        }
    )
    # Soft-fail: fallback success still OK; hard-fail only if nothing produced
    if code != 0 and not (ROOT / "output" / "ttf_ensemble_forecast.json").exists():
        return 1
    return 0


def verify_dashboard_artifact() -> int:
    print()
    print("=" * 72)
    print("STEP: verify sentinel_dashboard.html (+ TTF integrity gates)")
    print("=" * 72)
    if not SENTINEL_HTML.exists():
        print(f"FAIL: missing {SENTINEL_HTML}")
        return 1
    size = SENTINEL_HTML.stat().st_size
    text = SENTINEL_HTML.read_text(encoding="utf-8", errors="ignore")
    has_ttf = ("tab-ttf-forecast" in text) and (
        "ПРОГНОЗ TTF" in text or "MARKET FORECAST" in text or "ttf_forecast" in text
    )
    has_hedge = (
        "ttfAllocDonut" in text
        and "ttfHedgeMatrix" in text
        and "ttfEquityCurves" in text
        and "G · Allocation" in text
    )
    has_routing = (
        "data-sheet" in text
        and ("switchTab" in (ROOT / "output" / "js" / "sentinel_engine.js").read_text(encoding="utf-8", errors="ignore")
             if (ROOT / "output" / "js" / "sentinel_engine.js").exists() else False)
    )
    print(f"OK path={SENTINEL_HTML} size={size:,} (min={MIN_DASHBOARD_BYTES:,})")
    print(f"OK TTF sheet markers: {has_ttf}")
    print(f"OK Hedging panels G/H/I: {has_hedge}")
    print(f"OK tab routing sync helpers: {has_routing}")
    if size < MIN_DASHBOARD_BYTES:
        print("FAIL: dashboard artifact below enterprise size gate")
        return 1
    if not has_ttf:
        print("FAIL: TTF forecast sheet not found in HTML")
        return 1
    if not has_hedge:
        print("FAIL: Portfolio hedging panels G/H/I missing")
        return 1

    try:
        from services.ttf_forecast.integrity import (
            SREBuildError,
            assert_market_features_integrity,
            assert_ensemble_forecast_populated,
            validate_html_artifact,
        )

        market = assert_market_features_integrity()
        forecast = assert_ensemble_forecast_populated()
        from services.ttf_forecast.integrity import assert_ais_daily_aggregates

        ais_agg = assert_ais_daily_aggregates(min_days=30)
        from services.ttf_forecast.integrity import assert_paper_ledger

        paper = assert_paper_ledger()
        html_gate = validate_html_artifact(SENTINEL_HTML, min_bytes=MIN_DASHBOARD_BYTES)
        print(f"OK integrity market rows={market['rows']} spot={market['spot_eur_mwh']}")
        print(f"OK integrity forecast model={forecast.get('model_id')}")
        print(
            f"OK integrity ais_daily_aggregates rows={ais_agg['rows']} "
            f"nonempty={ais_agg['nonempty']} span={ais_agg['date_first']}→{ais_agg['date_last']}"
        )
        print(f"OK integrity paper_ledger open_orders={paper['open_orders']}")
        print(f"OK integrity HTML payload_chars={html_gate['payload_chars']} spot={html_gate['spot']}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL TTF integrity gate: {exc}")
        return 1
    return 0

def port_open(host: str = "127.0.0.1", port: int = 8765) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def ensure_http_server() -> int:
    print()
    print("=" * 72)
    print("STEP: confirm HTTP server :8765")
    print("=" * 72)
    if port_open():
        print("OK server already listening on :8765")
    else:
        print("NOTE: starting run_server.py --force in background")
        creation = 0
        if sys.platform == "win32":
            creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        subprocess.Popen(
            [_py(), str(ROOT / "run_server.py"), "--force"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation if sys.platform == "win32" else 0,
            start_new_session=(sys.platform != "win32"),
        )
        for _ in range(25):
            time.sleep(0.4)
            if port_open():
                break
        if not port_open():
            print("WARN: server did not bind :8765 within timeout")
            return 1
        print("OK server started on :8765")

    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            code = getattr(resp, "status", 200)
            body = resp.read(128)
            print(f"OK HTTP {code} bytes_prefix={len(body)} url={HEALTH_URL}")
            return 0 if int(code) == 200 else 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"WARN: HTTP check failed: {exc}")
        return 1


def open_dashboard() -> None:
    print(f"OPEN TTF sheet: {TTF_URL}")
    try:
        webbrowser.open(TTF_URL)
    except OSError as exc:
        print(f"WARN: webbrowser open failed: {exc}")
        try:
            if sys.platform == "win32":
                os.startfile(str(SENTINEL_HTML.resolve()))  # type: ignore[attr-defined]
        except OSError as exc2:
            print(f"WARN: os.startfile failed: {exc2}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle-1001 / Sentinel + TTF unified release")
    parser.add_argument("--skip-heavy", action="store_true", help="Skip legacy heavy ML contour")
    parser.add_argument("--with-osint", action="store_true", help="Force OSINT run_all.py")
    parser.add_argument("--skip-remote", action="store_true", help="Skip SSH remote AIS sync")
    parser.add_argument("--skip-ttf", action="store_true", help="Skip TTF forecast pipeline")
    parser.add_argument("--skip-catboost", action="store_true", help="Force Spectral+Markov TTF fallback")
    parser.add_argument("--open", action="store_true", default=True, help="Open browser (default on)")
    parser.add_argument("--no-open", action="store_true", help="Do not open browser")
    args = parser.parse_args()
    do_open = bool(args.open) and not bool(args.no_open)

    ensure_dirs()
    codes: list[int] = []
    release_t0 = time.perf_counter()

    # ── Step 1: OSINT QC ─────────────────────────────────────────────────────
    if args.with_osint:
        codes.append(run_step("1 OSINT run_all (QC)", "run_all.py"))
    else:
        print("NOTE: OSINT run_all skipped (use --with-osint). Expect fleet_database.csv.")
    if (ROOT / "prepare_targets.py").exists():
        codes.append(run_step("1b prepare_targets", "prepare_targets.py"))
    if (ROOT / "services" / "migrate_legacy_to_sentinel.py").exists():
        codes.append(run_step("1c migrate_legacy_to_sentinel", "services/migrate_legacy_to_sentinel.py"))

    # ── Step 2: DB validation ────────────────────────────────────────────────
    codes.append(validate_local_db())
    if not args.skip_remote:
        sync_script = ROOT / "deploy" / "sentinel" / "sync_ais_db_to_korolev.sh"
        if sync_script.exists() and sys.platform != "win32":
            print()
            print("=" * 72)
            print("STEP: 2b remote AIS DB sync (London→Korolev)")
            print("=" * 72)
            proc = subprocess.run(["bash", str(sync_script)], cwd=str(ROOT))
            codes.append(int(proc.returncode))
        else:
            print("NOTE: remote sync is London cron; Windows local release skips SSH push.")

    # ── Step 3: AIS daily aggregates THEN Retention ──────────────────────────
    # Aggregate raw telemetry into durable history BEFORE 7-day purge
    codes.append(run_step("3a ais_daily_aggregates roll-up", "services/ais_daily_aggregates.py"))
    codes.append(run_step("3b sqlite_retention", "services/sqlite_retention.py"))

    # ── Step 4: TTF Forecasting Engine ───────────────────────────────────────
    if not args.skip_ttf:
        codes.append(run_ttf_chain(skip_catboost=bool(args.skip_catboost)))
    else:
        print("NOTE: TTF pipeline skipped (--skip-ttf)")

    # ── Optional legacy heavy contour ────────────────────────────────────────
    if not args.skip_heavy:
        for name, script in (
            ("4 legacy feature_engineering", "feature_engineering.py"),
            ("4b legacy markov_model", "markov_model.py"),
            ("4c legacy spectral_analysis", "spectral_analysis.py"),
            ("4d legacy causal_analysis", "causal_analysis.py"),
            ("4e legacy forecast_ensemble", "forecast_ensemble.py"),
            ("4f build_history_dashboard", "build_history_dashboard.py"),
            ("4g build_fleet_dashboard", "build_fleet_dashboard.py"),
        ):
            if (ROOT / script).exists():
                codes.append(run_step(name, script))

    # ── Step 5: Dashboards (inject TTF) ──────────────────────────────────────
    codes.append(run_step("5 build_sentinel_dashboard (+TTF sheet)", "build_sentinel_dashboard.py"))
    if (ROOT / "build_top500_analytics.py").exists():
        codes.append(run_step("5b build_top500_analytics", "build_top500_analytics.py"))
    if (ROOT / "build_mission_control.py").exists() and not args.skip_heavy:
        codes.append(run_step("5c build_mission_control", "build_mission_control.py"))
    if (ROOT / "scripts" / "detect_ais_anomalies.py").exists():
        codes.append(run_step("5d detect_ais_anomalies report", "scripts/detect_ais_anomalies.py"))

    codes.append(verify_dashboard_artifact())

    # ── Step 6: HTTP + open TTF sheet ────────────────────────────────────────
    codes.append(ensure_http_server())
    if do_open and SENTINEL_HTML.exists():
        open_dashboard()

    # Persist perf ledger
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_seconds": round(time.perf_counter() - release_t0, 3),
        "steps": _perf_records,
    }
    PERF_LOG.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Prediction summary
    forecast_path = ROOT / "output" / "ttf_ensemble_forecast.json"
    pred_summary = {}
    if forecast_path.exists():
        try:
            fc = json.loads(forecast_path.read_text(encoding="utf-8"))
            pred_summary = {
                "spot": fc.get("spot_eur_mwh"),
                "mode": fc.get("model_id"),
                "horizons": {
                    h: {k: round(float(v.get(k, 0)), 2) for k in ("p10", "p50", "p90")}
                    for h, v in (fc.get("horizons") or {}).items()
                    if isinstance(v, dict)
                },
                "optimal_range": fc.get("optimal_range"),
            }
        except json.JSONDecodeError:
            pred_summary = {"error": "forecast json parse failed"}

    failed = [c for c in codes if c != 0]
    print()
    print("=" * 72)
    if failed:
        print(f"RELEASE DONE WITH {len(failed)} non-zero step exit(s).")
        print("Non-zero may be honest refusals (missing prices / empty archive / soft ML fails).")
    else:
        print("RELEASE OK — Sentinel + TTF dual-plane chain complete.")
    print(f"Sentinel: {HEALTH_URL}")
    print(f"TTF sheet:{TTF_URL}")
    print("Health:   http://127.0.0.1:8765/output/api/v1/health")
    print(f"Perf log: {PERF_LOG}")
    if pred_summary:
        print("TTF predictions:")
        print(json.dumps(pred_summary, indent=2, ensure_ascii=False))
    hedge_path = ROOT / "output" / "ttf_hedging_portfolio.json"
    if hedge_path.exists():
        try:
            hg = json.loads(hedge_path.read_text(encoding="utf-8"))
            print("Portfolio hedging ($1,000):")
            print(json.dumps({
                "recommended": hg.get("recommended_strategy_id"),
                "center": (hg.get("panel_g") or {}).get("center"),
                "capital_eur": hg.get("capital_eur"),
                "usd_eur": hg.get("usd_eur_rate"),
                "strategies": [
                    {
                        "id": s.get("id"),
                        "yield": s.get("expected_yield_pct"),
                        "var95": s.get("var_95_pct"),
                        "sharpe": s.get("expected_sharpe"),
                    }
                    for s in ((hg.get("panel_h") or {}).get("strategies") or [])
                ],
            }, indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            print("WARN: hedging portfolio JSON parse failed")
    print("=" * 72)

    if not SENTINEL_HTML.exists() or SENTINEL_HTML.stat().st_size < MIN_DASHBOARD_BYTES:
        return 1
    text = SENTINEL_HTML.read_text(encoding="utf-8", errors="ignore")
    if "tab-ttf-forecast" not in text:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
