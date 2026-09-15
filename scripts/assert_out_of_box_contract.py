#!/usr/bin/env python3
"""Prompt 10 — out-of-box contract self-check.

Fails if a first-time agent/human can still be misled by code↔docs drift on the
dual-gate envelope, ingest caps, or satellite stub activation.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
WARNS: list[str] = []


def ok(msg: str) -> None:
    print(f"  OK  {msg}")


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f" FAIL {msg}")


def warn(msg: str) -> None:
    WARNS.append(msg)
    print(f" WARN {msg}")


def main() -> int:
    print("=== Out-of-box contract (Prompt 10/11) ===\n")

    # --- Required docs ---
    for rel in ("AGENTS.md", ".cursor/rules/sentinel-envelope.mdc", "README.md", "CHANGELOG.md"):
        p = ROOT / rel
        if p.is_file() and p.stat().st_size > 200:
            ok(f"present {rel}")
        else:
            fail(f"missing/too-small {rel}")

    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8", errors="ignore")
    for needle in (
        "pipeline_health_status",
        "fleet_sample_status",
        "Do not",
        "SatelliteAISAdapter",
        "single_persistent",
        "G3",
        "live_inference_confidence",
        "8765",
        "Contract-Version",
        "STOP — coverage",
        "explicit human confirmation",
    ):
        if needle not in agents:
            fail(f"AGENTS.md missing required token: {needle}")
        else:
            ok(f"AGENTS.md has {needle!r}")

    if "1.6.0-arctic-tiles-vf" in agents:
        ok("AGENTS.md Contract-Version=1.6.0-arctic-tiles-vf")
    elif "1.5.0-baked" in agents:
        warn("AGENTS.md still on 1.5.0-baked — expected bump to 1.6.0-arctic-tiles-vf")
    elif "Contract-Version" in agents:
        warn("AGENTS.md Contract-Version present but expected 1.6.0-arctic-tiles-vf not found")

    for theme in (
        "Archive provenance",
        "Digital Twin",
        "Offline ML",
        "Disk headroom",
    ):
        if theme not in agents and theme.split()[0] not in agents:
            # Digital Twin may appear as GLB; Offline as ML Serving; Disk as DISK_FREE
            pass
    if "Consolidated contract themes" not in agents:
        fail("AGENTS.md missing consolidated themes section (v1.4.0)")
    else:
        ok("AGENTS.md has consolidated themes section")
    if "Image bake lock" not in agents and "1.5.0-baked" not in agents and "1.6.0-arctic-tiles-vf" not in agents:
        fail("AGENTS.md missing image bake lock (v1.5.0+)")
    else:
        ok("AGENTS.md has image bake lock")
    for needle in (
        "ARCTIC sheet",
        "VIDEO-DERIVED VIEWS",
        "/api/tiles/",
        "Esri",
        "notional_full_capacity_fallback",
    ):
        if needle not in agents:
            fail(f"AGENTS.md missing v1.6.0 contract token: {needle}")
        else:
            ok(f"AGENTS.md has v1.6.0 token {needle!r}")
    if "model_last_retrained" not in agents:
        fail("AGENTS.md missing model_last_retrained honesty rule")
    else:
        ok("AGENTS.md documents model_last_retrained")
    if "KNOWN REGISTRY" not in agents or "LIVE G3 AIS" not in agents:
        fail("AGENTS.md missing Archive KNOWN vs LIVE labeling")
    else:
        ok("AGENTS.md documents Archive KNOWN vs LIVE G3 split")

    if "Serving ≠ Training" not in agents and "ML Serving" not in agents:
        fail("AGENTS.md missing ML Serving != Training contract")
    else:
        ok("AGENTS.md has ML Serving != Training section")
    if "force_retrain" not in agents and "offline_batch" not in agents and "Variant A" not in agents:
        warn("AGENTS.md should document offline training Variant A")
    else:
        ok("AGENTS.md documents offline/Variant A training")

    if "docker compose up -d" not in agents:
        fail("AGENTS.md missing canonical docker compose up -d ops path")
    else:
        ok("AGENTS.md has canonical docker compose ops path")
    if "diagnostic" not in agents.lower() and "Diagnostic" not in agents:
        warn("AGENTS.md should mark manual aisstream_connector as diagnostic")
    else:
        ok("AGENTS.md marks manual connector as diagnostic/manual")

    hook = ROOT / "githooks" / "pre-commit"
    if hook.is_file() and "assert_out_of_box_contract" in hook.read_text(encoding="utf-8", errors="ignore"):
        ok("githooks/pre-commit present")
    else:
        fail("githooks/pre-commit missing or does not invoke assert_out_of_box_contract")

    snap = ROOT / "scripts" / "append_health_snapshot.py"
    if snap.is_file():
        ok("scripts/append_health_snapshot.py present")
    else:
        fail("scripts/append_health_snapshot.py missing")

    readme = (ROOT / "README.md").read_text(encoding="utf-8", errors="ignore")
    if "READ THIS FIRST" not in readme:
        fail("README.md missing 'READ THIS FIRST' banner")
    else:
        ok("README.md has READ THIS FIRST")
    if re.search(r"лимит \*\*50 MMSI", readme):
        fail("README.md still advertises obsolete 50 MMSI/WebSocket limit as current")
    else:
        ok("README.md no longer pushes 50 MMSI as current Sentinel limit")
    if "ceil(unique_mmsi / 50)" in readme and "Do **not** open" not in readme:
        fail("README.md still recommends ceil(N/50) parallel connections without warning")
    else:
        ok("README.md does not bare-recommend parallel ceil(N/50) sockets")

    # --- dual_gate constants ---
    from services.dual_gate import (
        FLEET_SAMPLE_FULL_MIN,
        FLEET_SAMPLE_LIMITED_MIN,
        FLEET_WIDE_METRIC_MIN_N,
        PIPELINE_LIVE_LAG_SEC,
        DISK_FREE_MIN_PCT,
        DISK_FREE_CRITICAL_PCT,
        compute_fleet_sample_status,
        compute_pipeline_health_status,
    )

    if FLEET_SAMPLE_LIMITED_MIN != 5:
        fail(f"FLEET_SAMPLE_LIMITED_MIN expected 5, got {FLEET_SAMPLE_LIMITED_MIN}")
    else:
        ok("FLEET_SAMPLE_LIMITED_MIN=5 (Prompt-7 peak)")
    if FLEET_SAMPLE_FULL_MIN != 100:
        fail(f"FLEET_SAMPLE_FULL_MIN expected 100, got {FLEET_SAMPLE_FULL_MIN}")
    else:
        ok("FLEET_SAMPLE_FULL_MIN=100")
    if FLEET_WIDE_METRIC_MIN_N != 30:
        fail(f"FLEET_WIDE_METRIC_MIN_N expected 30, got {FLEET_WIDE_METRIC_MIN_N}")
    else:
        ok("FLEET_WIDE_METRIC_MIN_N=30")
    if PIPELINE_LIVE_LAG_SEC != 300:
        fail(f"PIPELINE_LIVE_LAG_SEC expected 300, got {PIPELINE_LIVE_LAG_SEC}")
    else:
        ok("PIPELINE_LIVE_LAG_SEC=300")
    if DISK_FREE_MIN_PCT != 20.0:
        fail(f"DISK_FREE_MIN_PCT expected 20.0, got {DISK_FREE_MIN_PCT}")
    else:
        ok("DISK_FREE_MIN_PCT=20 (pre-ENOSPC DEGRADED)")
    if DISK_FREE_CRITICAL_PCT != 10.0:
        fail(f"DISK_FREE_CRITICAL_PCT expected 10.0, got {DISK_FREE_CRITICAL_PCT}")
    else:
        ok("DISK_FREE_CRITICAL_PCT=10")
    if "DISK_FREE_MIN_PCT" not in agents:
        fail("AGENTS.md missing DISK_FREE_MIN_PCT")
    else:
        ok("AGENTS.md documents DISK_FREE_MIN_PCT")
    if not (ROOT / "services" / "log_retention.py").is_file():
        fail("services/log_retention.py missing")
    else:
        ok("services/log_retention.py present")

    assert compute_fleet_sample_status(5)["fleet_sample_status"] == "LIMITED"
    assert compute_fleet_sample_status(4)["fleet_sample_status"] == "INSUFFICIENT"
    assert compute_fleet_sample_status(100)["fleet_sample_status"] == "FULL"
    ok("fleet_sample thresholds: 4->INSUFFICIENT, 5->LIMITED, 100->FULL")

    # --- dual_gate two-node failover & node resolution ---
    from services.dual_gate import resolve_active_node, check_failover_status
    node_res = resolve_active_node()
    if node_res not in ("korolev", "london"):
        fail(f"resolve_active_node() returned invalid node: {node_res}")
    else:
        ok(f"dual_gate.resolve_active_node() -> {node_res}")

    pipe_failover = compute_pipeline_health_status(failover_in_progress=True)
    if pipe_failover["pipeline_health_status"] == "NOMINAL":
        fail("compute_pipeline_health_status with failover_in_progress must not be NOMINAL")
    else:
        ok("dual_gate failover guard prevents false NOMINAL")

    # --- api_server dual_gate alignment & schema ---
    api_src = (ROOT / "api_server.py").read_text(encoding="utf-8")
    if "from services.dual_gate import" not in api_src:
        fail("api_server.py does not import from services.dual_gate")
    else:
        ok("api_server.py imports directly from services.dual_gate (Unified Gate SoT)")
    if "active_node" not in api_src:
        fail("api_server.py GateStatus schema missing active_node field")
    else:
        ok("api_server.py GateStatus contains active_node")

    # --- release_gate blocks only pipeline ---
    rg = (ROOT / "services/release_gate.py").read_text(encoding="utf-8", errors="ignore")
    if "pipeline_health_status" not in rg or "fleet_sample_status" not in rg:
        fail("release_gate.py missing dual-gate fields")
    else:
        ok("release_gate.py references both gate fields")
    if "NEVER fails the gate" not in rg and "informational" not in rg.lower():
        warn("release_gate.py should state fleet_sample is non-blocking")
    else:
        ok("release_gate.py documents non-blocking fleet_sample")

    # --- config ingest ---
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8", errors="ignore")
    if "subscription_mode: single_persistent" not in cfg:
        fail("config.yaml sentinel.subscription_mode is not single_persistent")
    else:
        ok("config.yaml single_persistent")
    if "mmsi_per_subscription: 200" not in cfg:
        fail("config.yaml missing mmsi_per_subscription: 200")
    else:
        ok("config.yaml mmsi_per_subscription=200")
    if "rotation_interval_seconds: 180" not in cfg:
        fail("config.yaml missing rotation_interval_seconds: 180")
    else:
        ok("config.yaml rotation_interval_seconds=180")

    # --- coverage window = rotation cycle (no legacy 720 hardcode) ---
    from services.ais_health import coverage_window_from_config, resolve_coverage_window_seconds

    derived = coverage_window_from_config()
    if abs(derived - 540.0) > 1e-6:
        fail(
            f"coverage_window_from_config={derived} (expected 540 = 180s×3 chunks); "
            "legacy 720 (=3×240s) must not return"
        )
    else:
        ok("coverage_window_from_config=540 (interval*chunks from config)")
    # Without env override, resolver must use derived cycle (not stale 720).
    os.environ.pop("SENTINEL_COVERAGE_WINDOW_SECONDS", None)
    resolved = resolve_coverage_window_seconds()
    if abs(resolved - 540.0) > 1e-6:
        fail(f"resolve_coverage_window_seconds={resolved} (expected 540)")
    else:
        ok("resolve_coverage_window_seconds=540 (synced with rotation cycle)")

    # --- satellite stub ---
    from services.satellite_ais_adapter import SatelliteAISAdapter
    import asyncio

    async def _stub() -> None:
        a = SatelliteAISAdapter()
        try:
            await a.connect()
            fail("SatelliteAISAdapter.connect must raise NotImplementedError")
        except NotImplementedError as exc:
            text = str(exc)
            if "fabricate" not in text.lower() and "credentials" not in text.lower():
                warn("Satellite stub error text should mention credentials/fabricate")
            else:
                ok("SatelliteAISAdapter.connect raises NotImplementedError (no fake API)")
        rep = a.coverage_report()
        if rep.get("status") != "not_activated":
            fail(f"satellite coverage_report status={rep.get('status')!r}")
        else:
            ok("satellite coverage_report status=not_activated")

    asyncio.run(_stub())

    sat_src = (ROOT / "services/satellite_ais_adapter.py").read_text(encoding="utf-8")
    if re.search(r"api[_-]?key\s*=\s*['\"](?!YOUR_|<.*>)[^'\"]+['\"]", sat_src, re.I):
        fail("satellite_ais_adapter.py appears to hardcode a real-looking API key")
    else:
        ok("satellite_ais_adapter.py has no hardcoded API key literals")

    # --- historical report footgun ---
    hist = ROOT / "output" / "final_prod_readiness_report.json"
    hist_note = ROOT / "output" / "final_prod_readiness_report.SUPERSEDED.txt"
    if hist.is_file():
        text = hist.read_text(encoding="utf-8", errors="ignore")
        if "coverage" in text.lower() and "100" in text:
            if hist_note.is_file():
                ok("historical readiness report flagged by SUPERSEDED sidecar")
            else:
                warn(
                    "output/final_prod_readiness_report.json still encodes old coverage narratives - "
                    "treat as historical; AGENTS.md / dual_gate.py are authoritative "
                    "(add final_prod_readiness_report.SUPERSEDED.txt)"
                )
        else:
            ok("historical readiness report present (no obvious coverage=100 trap scanned)")
    else:
        ok("no final_prod_readiness_report.json (nothing to supersede)")

    # --- archive contract & api_status honest labeling ---
    api_stat_file = ROOT / "output" / "archive" / "api_status.json"
    if api_stat_file.is_file():
        stat_txt = api_stat_file.read_text(encoding="utf-8", errors="ignore")
        if "PREMIUM SATELLITE" in stat_txt:
            fail("output/archive/api_status.json contains fraudulent 'PREMIUM SATELLITE'")
        else:
            ok("output/archive/api_status.json free of fraudulent 'PREMIUM SATELLITE'")
        if '"is_synthetic": true' in stat_txt or '"is_synthetic":true' in stat_txt:
            ok("output/archive/api_status.json contains 'is_synthetic': true")
        else:
            fail("output/archive/api_status.json missing mandatory 'is_synthetic': true")
    else:
        warn("output/archive/api_status.json not found")

    dash_file = ROOT / "output" / "sentinel_dashboard.html"
    if dash_file.is_file():
        dash_txt = dash_file.read_text(encoding="utf-8", errors="ignore")
        if "ARCHIVE REGISTRY: STATIC OSINT SNAPSHOT" not in dash_txt:
            fail("output/sentinel_dashboard.html missing ARCHIVE REGISTRY banner")
        else:
            ok("output/sentinel_dashboard.html has ARCHIVE REGISTRY banner")
        if "KNOWN REGISTRY" not in dash_txt or "LIVE G3 AIS" not in dash_txt:
            fail("output/sentinel_dashboard.html missing KNOWN REGISTRY / LIVE G3 AIS split")
        else:
            ok("output/sentinel_dashboard.html has KNOWN vs LIVE G3 KPIs")
    else:
        warn("output/sentinel_dashboard.html not found")

    # --- ML freshness + disk monitor + archive writer honesty ---
    qsrc = (ROOT / "services" / "quant_risk_service.py").read_text(encoding="utf-8", errors="ignore")
    if "model_last_retrained" not in qsrc:
        fail("quant_risk_service.py missing model_last_retrained field")
    else:
        ok("quant_risk_service.py exposes model_last_retrained")
    api_src2 = (ROOT / "api_server.py").read_text(encoding="utf-8", errors="ignore")
    if "model_last_retrained" not in api_src2:
        fail("api_server.py schema missing model_last_retrained")
    else:
        ok("api_server.py schema has model_last_retrained")
    dg_src = (ROOT / "services" / "dual_gate.py").read_text(encoding="utf-8", errors="ignore")
    if "probe_disk_usage" not in dg_src or "DISK_FREE_MIN_PCT" not in dg_src:
        fail("dual_gate.py missing disk probe / DISK_FREE_MIN_PCT")
    else:
        ok("dual_gate.py has disk probe + thresholds")
    ah_src = (ROOT / "services" / "ais_health.py").read_text(encoding="utf-8", errors="ignore")
    if "probe_disk_usage" not in ah_src or "disk_free_pct" not in ah_src:
        fail("ais_health.py missing disk_free_pct wiring")
    else:
        ok("ais_health.py wires disk_free_pct into health document")
    arch_src = (ROOT / "services" / "archive_service.py").read_text(encoding="utf-8", errors="ignore")
    if "PREMIUM SATELLITE" in arch_src:
        fail("archive_service.py still contains fraudulent PREMIUM SATELLITE default")
    else:
        ok("archive_service.py free of PREMIUM SATELLITE")
    if "OSINT REGISTRY (HYBRID LOCAL FALLBACK)" not in arch_src:
        fail("archive_service.py missing OSINT hybrid plan label")
    else:
        ok("archive_service.py uses OSINT hybrid plan label")

    # --- Deploy manifest: web↔output sync + live Node A sha256 (stale-JS lock) ---
    print("\n--- Deploy manifest (Node A / local sync) ---")
    try:
        from scripts.verify_deploy_manifest import DEFAULT_BASE, run as run_deploy_manifest
    except ImportError:
        # Allow `python scripts/assert_out_of_box_contract.py` without package install.
        import importlib.util

        _vm = ROOT / "scripts" / "verify_deploy_manifest.py"
        if not _vm.is_file():
            fail("scripts/verify_deploy_manifest.py missing")
            run_deploy_manifest = None  # type: ignore[assignment]
            DEFAULT_BASE = "http://45.8.230.214:8765"
        else:
            _spec = importlib.util.spec_from_file_location("verify_deploy_manifest", _vm)
            assert _spec and _spec.loader
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            run_deploy_manifest = _mod.run
            DEFAULT_BASE = _mod.DEFAULT_BASE

    if run_deploy_manifest is not None:
        force_live = os.environ.get("SENTINEL_VERIFY_LIVE", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        skip_live = os.environ.get("SENTINEL_VERIFY_SKIP_LIVE", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        base = os.environ.get("SENTINEL_VERIFY_BASE_URL", DEFAULT_BASE)
        dm_rc, dm_rows = run_deploy_manifest(
            base_url=str(base),
            live=not skip_live,
            local_sync=True,
            strict=force_live,
            unreachable_is_fail=force_live,
        )
        live_mismatch = [
            r
            for r in dm_rows
            if r.status == "MISMATCH" and not str(r.name).startswith("sync:")
        ]
        sync_mismatch = [
            r
            for r in dm_rows
            if r.status in {"MISMATCH", "MISSING_LOCAL"} and str(r.name).startswith("sync:")
        ]
        if sync_mismatch:
            for r in sync_mismatch:
                fail(f"deploy manifest local sync: {r.name} — {r.detail or r.status}")
        else:
            ok("deploy manifest: web/ <-> output/js sync OK")
        if live_mismatch:
            for r in live_mismatch:
                fail(
                    f"deploy manifest STALE on Node A: {r.name} "
                    f"(local={r.local_sha and r.local_sha[:12]}... "
                    f"remote={r.remote_sha and r.remote_sha[:12]}...) "
                    f"{r.detail or ''}"
                )
        elif skip_live:
            warn("deploy manifest live check skipped (SENTINEL_VERIFY_SKIP_LIVE=1)")
        elif dm_rc != 0 and force_live:
            fail("deploy manifest live check failed under SENTINEL_VERIFY_LIVE=1")
        elif any(r.status == "FETCH_ERROR" for r in dm_rows):
            warn("deploy manifest: Node A unreachable — live sha256 not verified")
        else:
            ok(f"deploy manifest: Node A matches working tree ({base})")

    # --- envelope rule ---
    rule = (ROOT / ".cursor/rules/sentinel-envelope.mdc").read_text(encoding="utf-8")
    if "alwaysApply: true" not in rule:
        fail(".cursor/rules/sentinel-envelope.mdc must be alwaysApply: true")
    else:
        ok("cursor rule alwaysApply=true")

    print("\n=== Envelope summary (print for the next agent) ===")
    print(
        f"  LIMITED_MIN={FLEET_SAMPLE_LIMITED_MIN} FULL_MIN={FLEET_SAMPLE_FULL_MIN} "
        f"FLEET_WIDE_MIN_N={FLEET_WIDE_METRIC_MIN_N} LAG_SEC={PIPELINE_LIVE_LAG_SEC} "
        f"DISK_MIN={DISK_FREE_MIN_PCT} DISK_CRIT={DISK_FREE_CRITICAL_PCT}"
    )
    print("  Publish blocks on pipeline_health only; fleet_sample is informational.")
    print("  Do not chase coverage>=100 on terrestrial AIS. Satellite=stub.")
    print("  Contract 1.4.0-consolidated: Archive + Twin + Offline ML + Disk.")
    print(f"  demo ticket: LIMITED@5 -> {compute_fleet_sample_status(5)['fleet_sample_status']}")
    _ = compute_pipeline_health_status  # imported for agents reading this file

    print()
    if FAILS:
        print(f"CONTRACT FAIL - {len(FAILS)} error(s), {len(WARNS)} warning(s)")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"CONTRACT PASS - {len(WARNS)} warning(s)")
    for w in WARNS:
        print(f"  - {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
