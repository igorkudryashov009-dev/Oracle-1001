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
  .\\venv\\Scripts\\python.exe run_release.py --prod-rebuild --serve --no-open   # cron publish
  .\\venv\\Scripts\\python.exe run_release.py --prod-gate --no-open             # check-only
  .\\venv\\Scripts\\python.exe run_release.py --serve                           # HTTP only
  .\\venv\\Scripts\\python.exe run_release.py --skip-heavy --skip-remote        # dev rebuild
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
PERF_LOG = ROOT / "logs" / "release_perf.json"


def dashboard_port() -> int:
    """Canonical HTTP port — DASHBOARD_PORT env, default 8765; reject 8478 drift."""
    from services.utils.canonical_port import assert_canonical_dashboard_port, resolve_dashboard_port

    return assert_canonical_dashboard_port(resolve_dashboard_port(), context="run_release")


def _dash_url(path: str = "/output/sentinel_dashboard.html", *, query: str = "") -> str:
    q = f"?{query}" if query else ""
    return f"http://127.0.0.1:{dashboard_port()}{path}{q}"


HEALTH_URL = _dash_url()
TTF_URL = _dash_url(query="sheet=ttf")
TOP10_URL = _dash_url(query="sheet=top10")
ROUTE_URL = _dash_url(query="sheet=route")
BALANCE_URL = _dash_url(query="sheet=balance")


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
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env)
    dt = round(time.perf_counter() - t0, 3)
    rss1 = _rss_mb()
    print(f"EXIT {name}: {proc.returncode} | {dt}s | RSS {rss0}->{rss1} MB")
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
    has_top10 = (
        'data-sheet="top10"' in text
        or 'data-sheet="qflex"' in text
    ) and (
        "Q-Flex" in text or "Q-FLEX" in text or "ТОП 10 ФЛАГМАНОВ" in text
    ) and "sheet-top10" in text and (
        ROOT / "output" / "js" / "top10_sheet.js"
    ).exists() and (
        ROOT / "output" / "js" / "vessel_3d_reconstruction.js"
    ).exists() and (
        ROOT / "output" / "js" / "top10_vessels_manifest.js"
    ).exists()

    has_balance = (
        'data-sheet="balance"' in text
        and "БАЛАНС" in text
        and "sheet-balance" in text
        and "bal-kpi-row" in text
        and (ROOT / "output" / "js" / "balance_engine.js").exists()
    )
    has_route = (
        'data-sheet="route"' in text
        and "МАРШРУТ" in text
        and "sheet-route" in text
        and "route_analytics" in text
        and (ROOT / "output" / "js" / "route_sheet.js").exists()
        and (ROOT / "output" / "js" / "route_infographics.js").exists()
    )
    print(f"OK path={SENTINEL_HTML} size={size:,} (min={MIN_DASHBOARD_BYTES:,})")
    print(f"OK TTF sheet markers: {has_ttf}")
    print(f"OK Hedging panels G/H/I: {has_hedge}")
    print(f"OK tab routing sync helpers: {has_routing}")
    print(f"OK TOP10 flagships sheet: {has_top10}")
    print(f"OK ROUTE analytics sheet: {has_route}")
    print(f"OK BALANCE sheet: {has_balance}")
    if size < MIN_DASHBOARD_BYTES:
        print("FAIL: dashboard artifact below enterprise size gate")
        return 1
    if not has_ttf:
        print("FAIL: TTF forecast sheet not found in HTML")
        return 1
    if not has_hedge:
        print("FAIL: Portfolio hedging panels G/H/I missing")
        return 1
    if not has_top10:
        print("FAIL: TOP 10 Flagships sheet / JS assets missing")
        return 1
    if not has_route:
        print("FAIL: ROUTE analytics sheet / JS assets missing")
        return 1
    if not has_balance:
        print("FAIL: BALANCE sheet / balance_engine.js missing")
        return 1

    try:
        from services.ttf_forecast.integrity import (
            SREBuildError,
            assert_market_features_integrity,
            assert_ensemble_forecast_populated,
            assert_top10_flagships,
            assert_route_analytics,
            validate_html_artifact,
        )

        market = assert_market_features_integrity()
        forecast = assert_ensemble_forecast_populated()
        from services.ttf_forecast.integrity import assert_ais_daily_aggregates

        ais_agg = assert_ais_daily_aggregates(min_days=30)
        from services.ttf_forecast.integrity import assert_paper_ledger

        paper = assert_paper_ledger()
        html_gate = validate_html_artifact(SENTINEL_HTML, min_bytes=MIN_DASHBOARD_BYTES)
        top10_gate = assert_top10_flagships(SENTINEL_HTML)
        route_gate = assert_route_analytics(SENTINEL_HTML)
        print(f"OK integrity market rows={market['rows']} spot={market['spot_eur_mwh']}")
        print(f"OK integrity forecast model={forecast.get('model_id')}")
        print(
            f"OK integrity ais_daily_aggregates rows={ais_agg['rows']} "
            f"nonempty={ais_agg['nonempty']} span={ais_agg['date_first']}->{ais_agg['date_last']}"
        )
        print(f"OK integrity paper_ledger open_orders={paper['open_orders']}")
        print(f"OK integrity HTML payload_chars={html_gate['payload_chars']} spot={html_gate['spot']}")
        print(
            f"OK integrity TOP10 vessels={top10_gate['vessels']} "
            f"images={top10_gate['images']} assets={top10_gate['asset_dir']}"
        )
        print(
            f"OK integrity ROUTE vessels={route_gate['vessels']} "
            f"source={route_gate.get('source_mode')}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL TTF integrity gate: {exc}")
        return 1
    return 0

def port_open(host: str = "127.0.0.1", port: int | None = None) -> bool:
    listen = dashboard_port() if port is None else int(port)
    try:
        with socket.create_connection((host, listen), timeout=1.5):
            return True
    except OSError:
        return False


def _serve_script() -> Path:
    dedicated = ROOT / "scripts" / "serve_dashboard.py"
    if dedicated.exists():
        return dedicated
    return ROOT / "run_server.py"


def ensure_http_server() -> int:
    """Ensure non-blocking static HTTP daemon on DASHBOARD_PORT (default 8765)."""
    if assert_forbidden_port_unbound() != 0:
        return 1
    port = dashboard_port()
    print()
    print("=" * 72)
    print(f"STEP: confirm HTTP server :{port}")
    print("=" * 72)
    serve = _serve_script()
    if port_open(port=port):
        print(f"OK server already listening on :{port}")
    else:
        print(f"NOTE: starting {serve.name} in background (repo root -> :{port})")
        log_path = ROOT / "logs" / "dashboard_http.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 — kept open for daemon lifetime
        creation = 0
        if sys.platform == "win32":
            creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        cmd = [_py(), str(serve), "--port", str(port), "--host", "127.0.0.1"]
        if serve.name == "run_server.py":
            cmd.append("--force")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PORT"] = str(port)
        env["DASHBOARD_PORT"] = str(port)
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creation if sys.platform == "win32" else 0,
            start_new_session=(sys.platform != "win32"),
        )
        for _ in range(30):
            time.sleep(0.35)
            if port_open(port=port):
                break
        if not port_open(port=port):
            print(f"WARN: server did not bind :{port} within timeout (see {log_path})")
            return 1
        print(f"OK server started on :{port}")

    # Refresh URL constants for current port
    global HEALTH_URL, TTF_URL, TOP10_URL, ROUTE_URL, BALANCE_URL
    HEALTH_URL = _dash_url()
    TTF_URL = _dash_url(query="sheet=ttf")
    TOP10_URL = _dash_url(query="sheet=top10")
    ROUTE_URL = _dash_url(query="sheet=route")
    BALANCE_URL = _dash_url(query="sheet=balance")

    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            code = getattr(resp, "status", 200)
            body = resp.read(128)
            print(f"OK HTTP {code} bytes_prefix={len(body)} url={HEALTH_URL}")
            print(f"Sentinel Dashboard live at {TTF_URL}")
            print(f"TOP10 Flagships sheet at {TOP10_URL}")
            print(f"ROUTE Analytics sheet at {ROUTE_URL}")
            print(f"BALANCE sheet at {BALANCE_URL}")
        # Probe TOP10 / ROUTE assets
        for url in (
            TOP10_URL,
            ROUTE_URL,
            _dash_url("/assets/7000/1-1.jpg"),
            _dash_url("/assets/7000/10-3.jpg"),
            _dash_url("/output/assets/top10/1-1.jpg"),
            _dash_url("/output/js/vessel_3d_reconstruction.js"),
            _dash_url("/output/js/top10_vessels_manifest.js"),
            _dash_url("/output/js/route_sheet.js"),
            _dash_url("/output/js/route_infographics.js"),
            BALANCE_URL,
            _dash_url("/output/js/balance_engine.js"),
        ):
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    st = getattr(r, "status", 200)
                    print(f"OK HTTP {st} {url}")
                    if int(st) == 404:
                        return 1
            except urllib.error.HTTPError as he:
                print(f"FAIL HTTP {he.code} {url}")
                return 1
        return 0 if int(code) == 200 else 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"WARN: HTTP check failed: {exc}")
        return 1


def open_dashboard() -> None:
    print(f"OPEN ROUTE sheet: {ROUTE_URL}")
    try:
        webbrowser.open(ROUTE_URL)
    except OSError as exc:
        print(f"WARN: webbrowser open failed: {exc}")
        try:
            if sys.platform == "win32":
                os.startfile(str(SENTINEL_HTML.resolve()))  # type: ignore[attr-defined]
        except OSError as exc2:
            print(f"WARN: os.startfile failed: {exc2}")


def assert_prod_truth_contract() -> int:
    """Deprecated compat wrapper → diagnostic Deploy Gate checklist."""
    from services.release_gate import assert_deploy_gate

    code, _ = assert_deploy_gate(strict=False, label="PROD Truth Gate (compat)")
    return code


def _run_full_rebuild_pipeline(args: argparse.Namespace) -> list[int]:
    codes: list[int] = []
    if args.with_osint:
        codes.append(run_step("1 OSINT run_all (QC)", "run_all.py"))
    else:
        print("NOTE: OSINT run_all skipped (use --with-osint). Expect fleet_database.csv.")
    if (ROOT / "prepare_targets.py").exists():
        codes.append(run_step("1b prepare_targets", "prepare_targets.py"))
    if (ROOT / "services" / "migrate_legacy_to_sentinel.py").exists():
        codes.append(run_step("1c migrate_legacy_to_sentinel", "services/migrate_legacy_to_sentinel.py"))

    codes.append(validate_local_db())
    if not args.skip_remote:
        sync_script = ROOT / "deploy" / "sentinel" / "sync_ais_db_to_korolev.sh"
        if sync_script.exists() and sys.platform != "win32":
            print()
            print("=" * 72)
            print("STEP: 2b remote AIS DB sync (London->Korolev)")
            print("=" * 72)
            proc = subprocess.run(["bash", str(sync_script)], cwd=str(ROOT))
            codes.append(int(proc.returncode))
        else:
            print("NOTE: remote sync is London cron; Windows local release skips SSH push.")

    codes.append(run_step("3a ais_daily_aggregates roll-up", "services/ais_daily_aggregates.py"))
    codes.append(run_step("3b sqlite_retention", "services/sqlite_retention.py"))

    if not args.skip_ttf:
        codes.append(run_ttf_chain(skip_catboost=bool(args.skip_catboost)))
    else:
        print("NOTE: TTF pipeline skipped (--skip-ttf)")

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

    os.environ["SENTINEL_BUILD_MODE"] = "rebuild"
    # TOP-10 photogrammetric GLB (hash-stale / missing only) — non-AIS, non-gate
    if (ROOT / "scripts" / "build_top10_3d_models.py").exists():
        codes.append(
            run_step("4h build_top10_3d_models (.glb)", "scripts/build_top10_3d_models.py")
        )
    if (ROOT / "services" / "archive_snapshot_worker.py").exists():
        codes.append(
            run_step(
                "4i vessel_daily_archive snapshot",
                "services/archive_snapshot_worker.py",
            )
        )
    codes.append(run_step("5 build_sentinel_dashboard (+TTF sheet)", "build_sentinel_dashboard.py"))
    if (ROOT / "build_archive_dashboard.py").exists():
        codes.append(run_step("5e build_archive_dashboard", "build_archive_dashboard.py"))
    if (ROOT / "build_top500_analytics.py").exists():
        codes.append(run_step("5b build_top500_analytics", "build_top500_analytics.py"))
    if (ROOT / "build_mission_control.py").exists() and not args.skip_heavy:
        codes.append(run_step("5c build_mission_control", "build_mission_control.py"))
    if (ROOT / "scripts" / "detect_ais_anomalies.py").exists():
        codes.append(run_step("5d detect_ais_anomalies report", "scripts/detect_ais_anomalies.py"))

    codes.append(verify_dashboard_artifact())
    codes.append(sync_web_assets_gate(force=False))
    codes.append(verify_3d_and_health_gate(disk_only=True))
    return codes


def verify_3d_and_health_gate(*, require_http: bool = False, disk_only: bool = False) -> int:
    """Pre-deploy: GLB magic/size + dual-gate health + UI HTML (blocks on GLB/pipeline/UI)."""
    print()
    print("=" * 72)
    print("STEP: verify_3d_and_health (GLB + pipeline + UI)")
    print("=" * 72)
    script = ROOT / "scripts" / "verify_3d_and_health.py"
    if not script.exists():
        print(f"FAIL: missing {script}")
        return 1
    cmd = [str(PY if PY.exists() else sys.executable), str(script)]
    if disk_only:
        cmd.append("--disk-only")
    elif require_http:
        cmd.append("--require-http")
    proc = subprocess.run(cmd, cwd=str(ROOT))
    return int(proc.returncode)


def sync_web_assets_gate(*, force: bool = False) -> int:
    """Blocking: copy web/js|css → output/ with MD5 checksum validation."""
    print()
    print("=" * 72)
    print("STEP: sync web → output assets (MD5)")
    print("=" * 72)
    try:
        from services.web_assets_sync import sync_web_assets, verify_sha256_pairs

        results = sync_web_assets(ROOT, force=force, log=True)
        ok, rows = verify_sha256_pairs(ROOT)
        if not ok:
            bad = [r for r in rows if not r.get("ok")]
            print(f"FAIL: {len(bad)} asset pair(s) out of sync after copy", file=sys.stderr)
            for row in bad[:15]:
                print(
                    f"  - {row.get('src')} → {row.get('dst')} "
                    f"({row.get('error') or 'sha_mismatch'})",
                    file=sys.stderr,
                )
            return 1
        written = sum(1 for r in results if r.get("status") == "OK")
        skipped = sum(1 for r in results if r.get("status") == "SKIP")
        print(f"OK web→output sync verified ({written} written, {skipped} unchanged, {len(rows)} pairs)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: web assets sync: {exc}", file=sys.stderr)
        return 1


def assert_forbidden_port_unbound() -> int:
    """CRITICAL: refuse release if :8478 is listening."""
    if port_open(port=8478):
        print(
            "CRITICAL deployment drift: port 8478 is listening. "
            "Canonical DASHBOARD_PORT is 8765 only (AGENTS.md).",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Oracle-1001 / Sentinel + TTF release orchestrator.\n\n"
            "Release semantics (Gate != Publish):\n"
            "  --prod-gate     Check acceptance criteria on CURRENT disk artifacts (no rebuild).\n"
            "  --prod-rebuild  Full rebuild, then Deploy Gate on FRESH artifacts; rollback on fail.\n"
            "  --serve         Orthogonal HTTP serve on :8765 (does NOT rebuild alone).\n"
            "\n"
            "Cron:  python run_release.py --prod-rebuild --serve --no-open\n"
            "Check: python run_release.py --prod-gate --no-open"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--skip-heavy", action="store_true", help="Skip legacy heavy ML contour")
    parser.add_argument("--with-osint", action="store_true", help="Force OSINT run_all.py")
    parser.add_argument("--skip-remote", action="store_true", help="Skip SSH remote AIS sync")
    parser.add_argument("--skip-ttf", action="store_true", help="Skip TTF forecast pipeline")
    parser.add_argument("--skip-catboost", action="store_true", help="Force Spectral+Markov TTF fallback")
    parser.add_argument(
        "--serve",
        action="store_true",
        help=(
            "Ensure HTTP server on DASHBOARD_PORT (default 8765). "
            "Orthogonal: does NOT by itself trigger rebuild."
        ),
    )
    parser.add_argument(
        "--prod-gate",
        action="store_true",
        help=(
            "DIAGNOSTIC: evaluate Deploy criteria against on-disk health / live AIS. "
            "No rebuild, no artifact mutation."
        ),
    )
    parser.add_argument(
        "--prod-rebuild",
        action="store_true",
        help=(
            "PRODUCTION publish: snapshot → full rebuild → Deploy Gate on fresh health. "
            "On FAIL: restore previous publish artifacts. Blocking: "
            "pipeline_health_status==NOMINAL (lag/WAL/429/port). "
            "fleet_sample_status is informational (LIMITED terrestrial OK)."
        ),
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="DEPRECATED alias for --prod-rebuild. Prefer --prod-rebuild or --prod-gate.",
    )
    parser.add_argument("--open", action="store_true", default=True, help="Open browser (default on)")
    parser.add_argument("--no-open", action="store_true", help="Do not open browser")
    args = parser.parse_args()
    do_open = bool(args.open) and not bool(args.no_open)

    if args.prod and not args.prod_rebuild:
        print(
            "NOTE: --prod is deprecated; treating as --prod-rebuild "
            "(use --prod-gate for check-only).",
            file=sys.stderr,
        )
        args.prod_rebuild = True

    if args.prod_gate and args.prod_rebuild:
        print("FAIL: choose either --prod-gate OR --prod-rebuild, not both.", file=sys.stderr)
        return 2

    ensure_dirs()
    release_t0 = time.perf_counter()

    from services.release_gate import (
        assert_deploy_gate,
        load_health_disk,
        restore_publish_artifacts,
        snapshot_publish_artifacts,
    )

    # ── Mode A: --prod-gate (no rebuild) ─────────────────────────────────────
    if args.prod_gate:
        code, fails = assert_deploy_gate(strict=True, label="PROD-GATE (disk artifacts)")
        # GLB + UI + pipeline lag / dual-gate sample report (sample never blocks).
        if args.serve:
            ensure_http_server()
            v3d = verify_3d_and_health_gate(require_http=True)
        else:
            v3d = verify_3d_and_health_gate(disk_only=True)
        if v3d != 0:
            code = 1
            fails = list(fails) + ["verify_3d_and_health failed"]
        report = {
            "mode": "prod_gate",
            "build_mode": "gate_only_recheck",
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "exit_code": code,
            "failures": fails,
            "verify_3d_and_health": v3d,
            "health_published_at": (load_health_disk() or {}).get("published_at"),
        }
        gate_path = ROOT / "output" / "api" / "v1" / "gate_recheck.json"
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        gate_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if args.serve and do_open and SENTINEL_HTML.exists():
            open_dashboard()
        return int(code)

    # ── Mode B: --serve alone → HTTP only, no rebuild ────────────────────────
    argv_flags = {a for a in sys.argv[1:] if a.startswith("-")}
    serve_only_flags = {"--serve", "--open", "--no-open"}
    if args.serve and not args.prod_rebuild and argv_flags <= serve_only_flags:
        code = ensure_http_server()
        if do_open and code == 0 and SENTINEL_HTML.exists():
            open_dashboard()
        return int(code)

    # ── Mode C: full rebuild (± --prod-rebuild) ───────────────────────────────
    snap = None
    if args.prod_rebuild:
        print()
        print("=" * 72)
        print("STEP: snapshot publish artifacts (rollback target)")
        print("=" * 72)
        snap = snapshot_publish_artifacts()
        print(f"OK snapshot → {snap}")

    codes = _run_full_rebuild_pipeline(args)

    if args.prod_rebuild:
        gate_code, fails = assert_deploy_gate(strict=True, label="PROD-REBUILD Deploy Gate")
        sync_code = sync_web_assets_gate(force=False)
        if sync_code != 0:
            gate_code = 1
            fails = list(fails) + ["web_assets_md5_sync failed"]
        # Disk gate first (always); HTTP gate mandatory when --serve
        v3d = verify_3d_and_health_gate(disk_only=True)
        if v3d != 0:
            gate_code = 1
            fails = list(fails) + ["verify_3d_and_health (disk) failed"]
        if gate_code != 0:
            print()
            print("=" * 72, file=sys.stderr)
            print(
                "PROD-REBUILD BLOCKED — Deploy Gate FAILED. Rolling back publish.",
                file=sys.stderr,
            )
            for item in fails:
                print(f"  - {item}", file=sys.stderr)
            print("=" * 72, file=sys.stderr)
            if snap is not None:
                restore_publish_artifacts(snap)
            return 1
        print("[PROD-REBUILD] Deploy Gate PASSED — fresh artifacts published.")
        health = load_health_disk()
        if health.get("build_mode") != "rebuild" or not health.get("published_at"):
            health["build_mode"] = "rebuild"
            health["published_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                from services.ais_health import write_health_files

                write_health_files(health, root=ROOT)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: could not stamp published_at: {exc}", file=sys.stderr)

    # Serve policy:
    #  - non-prod full release: always ensure HTTP (legacy behavior)
    #  - prod-rebuild: only if --serve
    if (not args.prod_rebuild) or args.serve:
        codes.append(ensure_http_server())
        if args.prod_rebuild and args.serve:
            # Prompt-4: mandatory blocking HTTP E2E gate after bind :8765
            v3d_http = verify_3d_and_health_gate(require_http=True)
            codes.append(v3d_http)
            if v3d_http != 0:
                print(
                    "PROD-REBUILD BLOCKED — verify_3d_and_health --require-http FAILED.",
                    file=sys.stderr,
                )
                if snap is not None:
                    restore_publish_artifacts(snap)
                return 1
        if do_open and SENTINEL_HTML.exists():
            open_dashboard()

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_seconds": round(time.perf_counter() - release_t0, 3),
        "mode": "prod_rebuild" if args.prod_rebuild else "dev_release",
        "steps": _perf_records,
    }
    PERF_LOG.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    forecast_path = ROOT / "output" / "ttf_ensemble_forecast.json"
    pred_summary: dict = {}
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
    else:
        print("RELEASE OK — Sentinel + TTF dual-plane chain complete.")
    print(f"Sentinel: {HEALTH_URL}")
    print(f"TTF sheet:{TTF_URL}")
    print(f"TOP10:    {TOP10_URL}")
    print(f"ROUTE:    {ROUTE_URL}")
    print(f"BALANCE:  {BALANCE_URL}")
    print(f"Health:   {_dash_url('/output/api/v1/health')}")
    print(f"Perf log: {PERF_LOG}")
    if pred_summary:
        print("TTF predictions:")
        print(json.dumps(pred_summary, indent=2, ensure_ascii=False))
    print("=" * 72)

    if not SENTINEL_HTML.exists() or SENTINEL_HTML.stat().st_size < MIN_DASHBOARD_BYTES:
        return 1
    text = SENTINEL_HTML.read_text(encoding="utf-8", errors="ignore")
    if "tab-ttf-forecast" not in text:
        return 1
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
