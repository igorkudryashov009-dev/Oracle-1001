#!/usr/bin/env python3
"""Build TOP-10 LNG flagship .glb meshes from orthographic triplets.

Usage:
  python scripts/build_top10_3d_models.py
  python scripts/build_top10_3d_models.py --force
  python scripts/build_top10_3d_models.py --imo 9388833
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="TOP-10 photogrammetric GLB builder")
    parser.add_argument("--force", "--force-rebuild", action="store_true", dest="force", help="Rebuild even if hash fresh")
    parser.add_argument("--imo", type=str, default="", help="Build single IMO only")
    parser.add_argument(
        "--voxel-res",
        type=int,
        default=0,
        help="Voxel grid resolution (0 = services.top10_3d_mesh.DEFAULT_VOXEL)",
    )
    args = parser.parse_args()

    from services.top10_3d_mesh import DEFAULT_VOXEL, build_all_top10, build_vessel_glb
    from services.top10_vessels import REF_BASE, TOP10_VESSELS, write_js_manifest

    voxel_res = int(args.voxel_res) if int(args.voxel_res) > 0 else int(DEFAULT_VOXEL)

    if args.imo:
        vessel = next((v for v in TOP10_VESSELS if str(v["imo"]) == str(args.imo)), None)
        if not vessel:
            print(f"FAIL: IMO {args.imo} not in TOP10 catalog", file=sys.stderr)
            return 1
        report = {
            "ok": True,
            "results": [
                build_vessel_glb(
                    vessel,
                    ref_base=REF_BASE,
                    voxel_res=voxel_res,
                    force=bool(args.force),
                )
            ],
        }
        report["ok"] = report["results"][0].get("status") in ("BUILT", "SKIP_FRESH")
    else:
        report = build_all_top10(force=bool(args.force), voxel_res=voxel_res)

    # Refresh manifest with glb urls
    write_js_manifest()

    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    for r in report.get("results") or []:
        extra = ""
        if r.get("lb_ratio") is not None:
            extra = f" L/B={r['lb_ratio']:.2f} B/D={r['bd_ratio']:.2f}"
        print(
            f"  IMO={r.get('imo')} status={r.get('status')} "
            f"bytes={r.get('bytes')} under={r.get('under_budget')}{extra}"
        )
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
