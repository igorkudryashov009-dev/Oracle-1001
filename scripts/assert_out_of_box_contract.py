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

    if "1.0.0-prompt12" not in agents and "Contract-Version" in agents:
        warn("AGENTS.md Contract-Version present but expected 1.0.0-prompt12 not found")
    elif "1.0.0-prompt12" in agents:
        ok("AGENTS.md Contract-Version=1.0.0-prompt12")

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

    assert compute_fleet_sample_status(5)["fleet_sample_status"] == "LIMITED"
    assert compute_fleet_sample_status(4)["fleet_sample_status"] == "INSUFFICIENT"
    assert compute_fleet_sample_status(100)["fleet_sample_status"] == "FULL"
    ok("fleet_sample thresholds: 4->INSUFFICIENT, 5->LIMITED, 100->FULL")

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

    # --- envelope rule ---
    rule = (ROOT / ".cursor/rules/sentinel-envelope.mdc").read_text(encoding="utf-8")
    if "alwaysApply: true" not in rule:
        fail(".cursor/rules/sentinel-envelope.mdc must be alwaysApply: true")
    else:
        ok("cursor rule alwaysApply=true")

    print("\n=== Envelope summary (print for the next agent) ===")
    print(
        f"  LIMITED_MIN={FLEET_SAMPLE_LIMITED_MIN} FULL_MIN={FLEET_SAMPLE_FULL_MIN} "
        f"FLEET_WIDE_MIN_N={FLEET_WIDE_METRIC_MIN_N} LAG_SEC={PIPELINE_LIVE_LAG_SEC}"
    )
    print("  Publish blocks on pipeline_health only; fleet_sample is informational.")
    print("  Do not chase coverage>=100 on terrestrial AIS. Satellite=stub.")
    print(f"  demo fleet: LIMITED@5 -> {compute_fleet_sample_status(5)['fleet_sample_status']}")
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
