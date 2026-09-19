#!/usr/bin/env python3
"""Pre-bake proof: auto-discovery finds Oracle assets as stale/missing on Node A."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.verify_deploy_manifest import DEFAULT_BASE, check_live, discover_manifest_entries, refresh_discovered


PROOF_NAMES = {
    "oracle_sheet.js",
    "oracle_engine.js",
    "oracle_event_bus.js",
    "oracle_engine.py",
    "vessel_card_metrics.js",
}


def main() -> int:
    refresh_discovered()
    entries = discover_manifest_entries()
    discovered = [e for e in entries if e.name in PROOF_NAMES or Path(e.local_paths[0]).name in PROOF_NAMES]
    print(f"AUTO_DISCOVERED_ORACLE={len(discovered)} (no hardcode required)")
    for e in discovered:
        print(f"  FOUND {e.name:40s} kind={e.kind} url={e.url_path or '-'} local={e.local_paths[0]}")

    if len(discovered) < 5:
        print("FAIL expected >=5 Oracle-related discoveries")
        return 1

    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    print(f"\nLive probe (pre-bake expected FAIL/MISMATCH): {base}")
    rows = check_live(base, discovered)
    bad = 0
    for r in rows:
        mark = "STALE" if r.status != "MATCH" else "OK   "
        print(f"  {mark} {r.name:40s} {r.status}  {r.detail or ''}")
        if r.status != "MATCH":
            bad += 1

    proof = {
        "discovered_without_hardcode": [e.name for e in discovered],
        "stale_or_missing_on_node_a": bad,
        "rows": [
            {"name": r.name, "status": r.status, "detail": r.detail} for r in rows
        ],
    }
    out = ROOT / "output" / "oracle_autodiscover_prebake_proof.json"
    out.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out.relative_to(ROOT)}")
    if bad == 0:
        print(
            "WARN all Oracle assets MATCH Node A already — "
            "systemic hole may already be closed on this host"
        )
        return 0
    print(
        f"PROOF_OK auto-discovery caught {bad}/{len(rows)} Oracle assets "
        "as MISSING/MISMATCH on Node A without hand-editing the watch list"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
