#!/usr/bin/env python3
"""Thin CLI wrapper — same job as scripts/sync_web_output.js via Python SoT.

Usage:
  python scripts/sync_web_output.py
  python scripts/sync_web_output.py --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.web_assets_sync import sync_web_assets, verify_sha256_pairs  # noqa: E402

HOT = {
    "top10_sheet.js",
    "arctic_sheet.js",
    "oracle_sheet.js",
    "sentinel_hud.css",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync web/ → output/ with SHA-256 report")
    parser.add_argument("--check", action="store_true", help="Verify only (no write)")
    args = parser.parse_args()

    print("=== sync_web_output.py · web/ → output/ ===")
    print(f"root: {ROOT}")
    print()

    if not args.check:
        results = sync_web_assets(ROOT, force=False, log=True)
        # Extra: web/oracle_sheet.js → output/oracle_sheet.js
        src = ROOT / "web" / "oracle_sheet.js"
        dst = ROOT / "output" / "oracle_sheet.js"
        if src.is_file():
            from services.web_assets_sync import sync_one, format_sync_line

            r = sync_one(src, dst)
            print(format_sync_line(r, ROOT), flush=True)
            results.append(r)

    ok, rows = verify_sha256_pairs(ROOT)
    # Also verify root oracle_sheet pair
    src = ROOT / "web" / "oracle_sheet.js"
    dst = ROOT / "output" / "oracle_sheet.js"
    if src.is_file():
        import hashlib

        def sha(p: Path) -> str:
            return hashlib.sha256(p.read_bytes()).hexdigest()

        if not dst.is_file() or sha(src) != sha(dst):
            ok = False
            print(f"  DRIFT  web/oracle_sheet.js → output/oracle_sheet.js")
        else:
            print(f"  OK     web/oracle_sheet.js → output/oracle_sheet.js  sha256={sha(src)[:12]}…")

    print()
    print("--- HOT ---")
    for r in rows:
        name = Path(r["src"]).name
        if name not in HOT:
            continue
        status = "OK  " if r.get("ok") else "DRIFT"
        print(f"  {status} {Path(r['src']).name} → {Path(r['dst']).name}")

    mismatches = [r for r in rows if not r.get("ok")]
    print()
    print(
        f"REPORT: checked={len(rows)} match={len(rows) - len(mismatches)} "
        f"drift={len(mismatches)}"
    )
    if ok and not mismatches:
        print("RESULT: web/ and output/ are 100% identical (managed pairs)")
        return 0
    print("RESULT: DRIFT remains")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
