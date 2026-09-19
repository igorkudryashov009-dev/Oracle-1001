#!/usr/bin/env python3
"""Unit / smoke script: OracleEngine ↔ OOB contract stays green under INSUFFICIENT.

Exit codes:
  0 — all assertions passed (WARN_NOMINAL warnings allowed)
  1 — hard failure

Usage:
  .\\venv\\Scripts\\python.exe scripts\\assert_oracle_oob.py
  .\\venv\\Scripts\\python.exe scripts\\assert_oracle_oob.py --full-oob
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _die(msg: str) -> None:
    print(f"FAIL  {msg}")
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _warn(msg: str) -> None:
    print(f" WARN {msg}")


def test_oracle_warn_nominal() -> dict:
    from services.oracle_engine import (
        CONTRACT_MODE_WARN_NOMINAL,
        OracleEngine,
        apply_oracle_state_to_health_doc,
        resolve_oob_contract_mode,
        sync_oracle_state_health_files,
    )

    engine = OracleEngine()

    # 1) Bad JSON must not raise / must fail-closed.
    bad = engine.evaluate_thresholds("{broken")
    assert bad.get("ok") is False, bad
    assert isinstance(bad.get("oracle_state"), dict), bad
    _ok("bad JSON fail-closed")

    # 2) Korolev-like INSUFFICIENT → WARN_NOMINAL (contract green semantics).
    ev = engine.evaluate_thresholds(
        {
            "top500_live_coverage": 2,
            "live_ok": True,
            "stale": False,
            "ais_lag_sec": 5,
            "integrity_ok": True,
            "port_ok": True,
            "disk_free_pct": 36.0,
            "active_node": "korolev",
            "quant_pipeline": {"model_cv_accuracy_pct": 53.6},
        }
    )
    assert ev["fleet_sample_status"] == "INSUFFICIENT", ev
    assert ev["contract_mode"] == CONTRACT_MODE_WARN_NOMINAL, ev
    ost = ev["oracle_state"]
    for key in ("status", "confidence", "coverage", "last_eval"):
        assert key in ost, ost
    assert ost["status"] == "INSUFFICIENT"
    assert ost["coverage"] == 2
    assert resolve_oob_contract_mode(
        fleet_sample_status="INSUFFICIENT",
        pipeline_health_status="NOMINAL",
    ) == CONTRACT_MODE_WARN_NOMINAL
    _warn(
        f"WARN_NOMINAL logged: status={ost['status']} coverage={ost['coverage']} "
        f"confidence={ost['confidence']}"
    )
    _ok("INSUFFICIENT -> WARN_NOMINAL (does not fail)")

    # 3) health.json structure auto-update.
    doc = {
        "service": "sentinel_dashboard",
        "pipeline_health_status": "NOMINAL",
        "fleet_sample_status": "INSUFFICIENT",
        "top500_live_coverage": 2,
        "live_ok": True,
        "stale": False,
        "ais_lag_sec": 5,
        "disk_free_pct": 36.0,
        "quant_pipeline": {"model_cv_accuracy_pct": 53.6},
    }
    apply_oracle_state_to_health_doc(doc)
    assert "oracle_state" in doc
    assert set(doc["oracle_state"]).issuperset(
        {"status", "confidence", "coverage", "last_eval"}
    )
    health_dir = ROOT / "output" / "api" / "v1"
    health_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    (health_dir / "health.json").write_text(text, encoding="utf-8")
    (health_dir / "health").write_text(text, encoding="utf-8")
    synced = sync_oracle_state_health_files(ROOT)
    health = json.loads(
        (ROOT / "output" / "api" / "v1" / "health.json").read_text(encoding="utf-8")
    )
    assert isinstance(health.get("oracle_state"), dict), health.keys()
    ost2 = health["oracle_state"]
    for key in ("status", "confidence", "coverage", "last_eval"):
        assert key in ost2, ost2
    _ok(
        f"health.json oracle_state ok status={ost2.get('status')} "
        f"coverage={ost2.get('coverage')} last_eval={ost2.get('last_eval')}"
    )

    # 4) JS ↔ Python twin files exist + local sync.
    web = ROOT / "web" / "js" / "oracle_engine.js"
    out = ROOT / "output" / "js" / "oracle_engine.js"
    assert web.is_file() and out.is_file()
    assert web.read_bytes() == out.read_bytes()
    _ok("web/js/oracle_engine.js <-> output/js sync")

    return {"oracle_state": ost2, "synced": synced, "contract_mode": CONTRACT_MODE_WARN_NOMINAL}


def run_full_oob() -> int:
    """Invoke the main OOB contract with live Node A check optional."""
    if os.environ.get("SENTINEL_VERIFY_LIVE", "").strip() not in {"1", "true", "yes"}:
        os.environ.setdefault("SENTINEL_VERIFY_SKIP_LIVE", "1")

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "assert_out_of_box_contract",
        ROOT / "scripts" / "assert_out_of_box_contract.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = int(mod.main())
    if rc != 0:
        _die(f"assert_out_of_box_contract exited {rc}")
    _ok("assert_out_of_box_contract exited 0 (green)")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full-oob",
        action="store_true",
        help="Also run scripts/assert_out_of_box_contract.py (expect exit 0)",
    )
    args = parser.parse_args()

    print("=== OracleEngine OOB unit check ===\n")
    try:
        test_oracle_warn_nominal()
    except AssertionError as exc:
        _die(f"assertion failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        _die(f"unexpected error: {exc}")

    if args.full_oob:
        print("\n=== Full OOB contract ===\n")
        run_full_oob()

    print("\nORACLE_OOB PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
