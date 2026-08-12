"""
Release orchestrator for Oracle-1001 / 7000.

Runs analytical contours in order and rebuilds Mission Control as the final step.
Individual steps may honestly no-op / refuse when upstream data is missing —
that is expected, not a silent failure.

Usage:
  .\\venv\\Scripts\\python.exe run_release.py
  .\\venv\\Scripts\\python.exe run_release.py --skip-heavy
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / "venv" / "Scripts" / "python.exe"


def _py() -> str:
    return str(PY if PY.exists() else sys.executable)


def run_step(name: str, script: str, args: list[str] | None = None) -> int:
    cmd = [_py(), str(ROOT / script)] + (args or [])
    print()
    print("=" * 72)
    print(f"STEP: {name}")
    print("CMD :", " ".join(cmd))
    print("=" * 72)
    proc = subprocess.run(cmd, cwd=str(ROOT))
    print(f"EXIT {name}: {proc.returncode}")
    return int(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle-1001 release orchestrator")
    parser.add_argument(
        "--skip-heavy",
        action="store_true",
        help="Skip OSINT run_all / collector-related heavy steps; rebuild analytics + portal",
    )
    args = parser.parse_args()

    codes: list[int] = []

    if not args.skip_heavy:
        # Step 0 optional — fleet OSINT already produced; re-run only if needed by operator
        print("NOTE: run_all.py (OSINT) is manual/heavy — not auto-forced every release.")

    # STEP 1 — targets from fleet
    codes.append(run_step("1 prepare_targets", "prepare_targets.py"))

    # STEP 2 — feature engineering (honest empty OK)
    codes.append(run_step("2 feature_engineering", "feature_engineering.py"))

    # STEP 3 — markov
    codes.append(run_step("3 markov_model", "markov_model.py"))

    # STEP 4 — spectral
    codes.append(run_step("4 spectral_analysis", "spectral_analysis.py"))

    # STEP 5 — causal (refuses without PRICE_SOURCE_PATH — expected)
    codes.append(run_step("5 causal_analysis", "causal_analysis.py"))

    # STEP 6 — forecast ensemble
    codes.append(run_step("6 forecast_ensemble", "forecast_ensemble.py"))

    # STEP 6b — UI rebuilds
    codes.append(run_step("6b build_history_dashboard", "build_history_dashboard.py"))
    codes.append(run_step("6c build_fleet_dashboard", "build_fleet_dashboard.py"))

    # STEP 7 — MISSION CONTROL (final)
    codes.append(run_step("7 build_mission_control", "build_mission_control.py"))

    failed = [c for c in codes if c != 0]
    print()
    print("=" * 72)
    if failed:
        print(f"RELEASE DONE WITH {len(failed)} non-zero step exit(s).")
        print("Non-zero may be honest refusals (missing prices / empty archive).")
    else:
        print("RELEASE OK — all steps exited 0.")
    print("Open: http://127.0.0.1:8765/output/mission_control.html")
    print("=" * 72)
    # Portal build failing is hard-fail; other refusals soft
    return 0 if codes[-1] == 0 else codes[-1]


if __name__ == "__main__":
    raise SystemExit(main())
