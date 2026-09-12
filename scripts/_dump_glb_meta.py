"""Dump GLB meta for fidelity v3.1 QA."""
from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    metas = sorted(Path("output/assets/3d_models").glob("vessel_*.meta.json"))
    print("=== meta alg / dilate / taubin / bbox ===")
    for m in metas:
        d = json.loads(m.read_text(encoding="utf-8"))
        bs = d.get("bbox_smoothing") or d.get("bbox_drift") or {}
        print(
            f"{d.get('imo')}: alg={d.get('alg')} dilate={d.get('mask_dilate_px')} "
            f"taubin={d.get('taubin_iters')} faces={d.get('faces')} bbox={bs}"
        )


if __name__ == "__main__":
    main()
