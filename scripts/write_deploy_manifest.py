#!/usr/bin/env python3
"""Write deploy/sentinel/deploy_manifest.json from auto-discovered MANIFEST + hashes.

Manifest entries are discovered via globs in verify_deploy_manifest.py — new
HUD/service files appear automatically (no hand-edited list).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.verify_deploy_manifest import (  # noqa: E402
    CRITICAL_BASENAMES,
    DEFAULT_BASE,
    DEFAULT_NODE_A,
    LEGACY_REQUIRED_BASENAMES,
    assert_legacy_coverage,
    discover_manifest_entries,
    discover_web_output_pairs,
    refresh_discovered,
    resolve_local,
    sha256_file,
)


OUT_PATHS = (
    ROOT / "deploy" / "sentinel" / "deploy_manifest.json",
    ROOT / "output" / "deploy_manifest.json",
)


def _sha(p: Path) -> str | None:
    if not p.is_file():
        return None
    return sha256_file(p)


def build_manifest_document(*, node_a: str, base_url: str) -> dict:
    refresh_discovered()
    manifest = discover_manifest_entries()
    pairs = discover_web_output_pairs()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assets = []
    for entry in manifest:
        local = resolve_local(entry)
        assets.append(
            {
                "name": entry.name,
                "kind": entry.kind,
                "url_path": entry.url_path,
                "local_paths": list(entry.local_paths),
                "resolved_path": (
                    str(local.relative_to(ROOT)).replace("\\", "/") if local else None
                ),
                "sha256": _sha(local) if local else None,
                "bytes": local.stat().st_size if local and local.is_file() else None,
                "critical": Path(entry.local_paths[0]).name in CRITICAL_BASENAMES
                or entry.name in CRITICAL_BASENAMES,
            }
        )

    sync_pairs = []
    for web_rel, out_rel in pairs:
        web = ROOT / web_rel
        out = ROOT / out_rel
        wsha = _sha(web)
        osha = _sha(out)
        sync_pairs.append(
            {
                "web": web_rel,
                "output": out_rel,
                "web_sha256": wsha,
                "output_sha256": osha,
                "match": bool(wsha and osha and wsha == osha),
            }
        )

    legacy_gap = assert_legacy_coverage(manifest)

    return {
        "manifest_version": "2.0.0-autodiscover",
        "discovery": "auto-glob",
        "generated_at": generated_at,
        "contract_version": "1.7.0-autodiscover-oracle-sot",
        "node_a": {
            "host": node_a,
            "base_url": base_url.rstrip("/"),
            "port": 8765,
        },
        "oracle_hud": {
            "oracle_engine.js": True,
            "oracle_sheet.js": True,
            "oracle_event_bus.js": True,
            "oracle_engine.py": True,
            "vessel_card_metrics.js": True,
            "sentinel_hud.css": True,
            "sheet": "oracle",
            "tab_label": "Oracle Engine",
            "thresholds_source": "health.thresholds ← services.dual_gate.export_dual_gate_thresholds",
        },
        "legacy_required_basenames": sorted(LEGACY_REQUIRED_BASENAMES),
        "legacy_coverage_ok": not legacy_gap,
        "legacy_missing": legacy_gap,
        "assets": assets,
        "web_output_pairs": sync_pairs,
        "verify": {
            "command": "python scripts/verify_deploy_manifest.py --live --strict",
            "list_command": "python scripts/verify_deploy_manifest.py --list-discovered",
            "sync_command": "node scripts/sync_web_output.js",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node-a", default=DEFAULT_NODE_A)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    args = ap.parse_args()

    doc = build_manifest_document(node_a=args.node_a, base_url=args.base_url)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    for path in OUT_PATHS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {path.relative_to(ROOT)}  assets={len(doc['assets'])}")

    if doc.get("legacy_missing"):
        print(f"FAIL legacy coverage gap: {doc['legacy_missing']}")
        return 1

    critical = [a for a in doc["assets"] if a.get("critical")]
    missing = [a["name"] for a in critical if not a.get("sha256")]
    if missing:
        print(f"FAIL missing critical assets: {missing}")
        return 1
    print(
        "Critical Oracle HUD:",
        ", ".join(
            f"{a['name']}={a['sha256'][:12]}…"
            for a in critical
            if "oracle" in a["name"] or a["name"] in {"sentinel_hud.css", "vessel_card_metrics.js"}
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
