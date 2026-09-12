"""CLI: verify SHA-256 parity between web/ and output/ JS|CSS assets.

Usage:
  python -m scripts.verify_assets_sync
  python -m scripts.verify_assets_sync --fix
  python -m scripts.verify_assets_sync --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.web_assets_sync import sync_web_assets, verify_sha256_pairs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify SHA-256 match between web/js|css and output/js|css"
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Run atomic sync first, then verify",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON report",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Project root (default: Oracle-1001/7000)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    if args.fix:
        print(f"[VERIFY] syncing assets under {root}", flush=True)
        sync_web_assets(root, force=False, log=True)

    ok, rows = verify_sha256_pairs(root)
    mismatches = [r for r in rows if not r.get("ok")]

    if args.json:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "checked": len(rows),
                    "mismatches": len(mismatches),
                    "rows": rows,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for r in rows:
            src = Path(r["src"])
            dst = Path(r["dst"])
            try:
                src_rel = src.resolve().relative_to(root).as_posix()
            except Exception:
                src_rel = src.as_posix()
            try:
                dst_rel = dst.resolve().relative_to(root).as_posix()
            except Exception:
                dst_rel = dst.as_posix()
            status = "OK" if r.get("ok") else "DRIFT"
            err = r.get("error")
            if err:
                print(f"[VERIFY] {status} {src_rel} -> {dst_rel} ({err})")
            else:
                print(
                    f"[VERIFY] {status} {src_rel} -> {dst_rel} "
                    f"sha256={r.get('src_sha256', '')[:12]} bytes={r.get('bytes', '?')}"
                )
        print(
            f"[VERIFY] {'PASS' if ok else 'FAIL'}: checked={len(rows)} mismatches={len(mismatches)}"
        )

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
