#!/usr/bin/env python3
"""Build TOP-10 voxel-cube JSON assets (v3: ~100k + k≈50 palette)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from services.top10_vessels import REF_BASE, TOP10_VESSELS, write_js_manifest
    from services.top10_voxel_cubes import (
        ALG_VOXEL_CUBES,
        SCALE_FACTOR_V3,
        V3_METRIC_PITCH,
        calibrate_pitch_bu_samra,
        write_voxel_cube_asset,
    )

    ap = argparse.ArgumentParser()
    ap.add_argument("--imo", type=str, default="", help="Single IMO (default: all 10)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--sampling",
        choices=("mean", "multisample"),
        default="multisample",
        help="Face color extraction (do not change unless diagnosing)",
    )
    args = ap.parse_args()

    pilot = next(v for v in TOP10_VESSELS if str(v["imo"]) == "9388833")
    cal = calibrate_pitch_bu_samra(pilot, REF_BASE)
    print(
        json.dumps(
            {
                "calibration": cal,
                "alg": ALG_VOXEL_CUBES,
                "v3_pitch": V3_METRIC_PITCH,
                "scale_factor_v3": SCALE_FACTOR_V3,
            },
            indent=2,
        )
    )

    vessels = TOP10_VESSELS
    if args.imo:
        vessels = [v for v in TOP10_VESSELS if str(v["imo"]) == str(args.imo)]
        if not vessels:
            print(json.dumps({"ok": False, "error": f"imo {args.imo} not found"}))
            return 1

    rows = []
    for v in vessels:
        path = write_voxel_cube_asset(v, pitch=V3_METRIC_PITCH, sampling=args.sampling)
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        rows.append(meta)
        print(json.dumps({"built": meta}, ensure_ascii=False))

    write_js_manifest()
    report = {
        "ok": True,
        "alg": ALG_VOXEL_CUBES,
        "calibration": cal,
        "v3_pitch": V3_METRIC_PITCH,
        "scale_factor_v3": SCALE_FACTOR_V3,
        "vessels": rows,
        "max_bytes": max(r["bytes"] for r in rows) if rows else 0,
        "sampling": args.sampling,
        "sum_bytes": sum(r["bytes"] for r in rows) if rows else 0,
    }
    (ROOT / "logs" / "voxel_cubes_v3_build_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
