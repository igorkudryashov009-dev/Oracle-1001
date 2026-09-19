#!/usr/bin/env python3
"""Prove Oracle UI thresholds follow dual_gate SoT (no JS twin).

Temporarily mutates FLEET_SAMPLE_LIMITED_MIN, rebuilds health thresholds blob,
asserts export reflects the mutation, then restores module constants.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    import services.dual_gate as dg
    from services.oracle_engine import OracleEngine, apply_oracle_state_to_health_doc

    original = dg.FLEET_SAMPLE_LIMITED_MIN
    try:
        # Temporary mutation (test env only).
        dg.FLEET_SAMPLE_LIMITED_MIN = 7
        thr = dg.export_dual_gate_thresholds()
        assert thr["FLEET_SAMPLE_LIMITED_MIN"] == 7, thr
        print("  OK  export_dual_gate_thresholds reflects mutated LIMITED_MIN=7")

        doc = {
            "top500_live_coverage": 2,
            "live_ok": True,
            "stale": False,
            "ais_lag_sec": 5,
            "integrity_ok": True,
            "port_ok": True,
            "disk_free_pct": 36.0,
            "active_node": "korolev",
        }
        apply_oracle_state_to_health_doc(doc)
        assert doc["thresholds"]["FLEET_SAMPLE_LIMITED_MIN"] == 7, doc["thresholds"]
        print("  OK  health.thresholds LIMITED_MIN=7 without touching any JS file")

        # Client contract: JS source must not contain the literal cutoff.
        js = (ROOT / "web" / "js" / "oracle_engine.js").read_text(encoding="utf-8")
        assert "FLEET_SAMPLE_LIMITED_MIN: 5" not in js
        assert "FLEET_SAMPLE_LIMITED_MIN: 7" not in js
        assert "oracle_applyThresholdsFromHealth" in js
        print("  OK  oracle_engine.js has no hardcoded LIMITED_MIN — reads health SoT")

        # Engine evaluate embeds live export.
        ev = OracleEngine().evaluate_thresholds(doc)
        assert ev["thresholds"]["FLEET_SAMPLE_LIMITED_MIN"] == 7, ev["thresholds"]
        print("  OK  OracleEngine.evaluate_thresholds thresholds follow dual_gate")
    finally:
        dg.FLEET_SAMPLE_LIMITED_MIN = original

    restored = dg.export_dual_gate_thresholds()
    assert restored["FLEET_SAMPLE_LIMITED_MIN"] == original, restored
    print(f"  OK  restored FLEET_SAMPLE_LIMITED_MIN={original}")
    print("ORACLE_THRESHOLDS_SOT_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
