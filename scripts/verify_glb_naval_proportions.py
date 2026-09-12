"""Verify Top-10 GLB naval proportions after anisotropic pitch rebuild."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import trimesh

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.top10_3d_mesh import (  # noqa: E402
    ALG_VERSION,
    OUT_GLB_DIR,
    _to_unit,
    naval_proportion_report,
)


def main() -> int:
    print("ALG", ALG_VERSION)
    hx, hy, hz = _to_unit(345.28, 53.83, 9.4)
    print(
        "design Q-Max half=",
        (hx, hy, hz),
        "L/B=",
        round((2 * hx) / (2 * hy), 3),
        "B/D=",
        round((2 * hy) / (2 * hz), 3),
    )
    overall = True
    for path in sorted(OUT_GLB_DIR.glob("vessel_*.glb")):
        mesh = trimesh.load(path, force="mesh")
        props = naval_proportion_report(mesh.extents)
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        lb = props["lb_ratio"]
        bd = props["bd_ratio"]
        band_ok = 5.5 <= lb <= 8.5 and 2.6 <= bd <= 4.5
        alg_ok = meta.get("alg") == ALG_VERSION
        pitch = meta.get("anisotropic_pitch")
        ok = band_ok and alg_ok and abs(mesh.volume) > 0.01
        overall = overall and ok
        flag = "OK" if ok else "FAIL"
        print(
            f"{path.name}: faces={len(mesh.faces)} L/B={lb:.2f} B/D={bd:.2f} "
            f"vol={abs(mesh.volume):.4f} pitch={pitch} {flag}"
        )
    print("OVERALL", "PASS" if overall else "FAIL")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
