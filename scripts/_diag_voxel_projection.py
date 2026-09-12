#!/usr/bin/env python3
"""Prompt 3: landmark UV→pixel check on BU SAMRA Side photo."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from services.top10_3d_mesh import ORTHO_TEX_SIZE, load_ortho_rgb, resolve_ortho_paths
    from services.top10_vessels import REF_BASE, TOP10_VESSELS
    from services.top10_voxel_cubes import diagnose_voxel_projection, uv_to_pixel

    vessel = next(v for v in TOP10_VESSELS if str(v["imo"]) == "9388833")
    report = diagnose_voxel_projection(vessel)
    paths = resolve_ortho_paths(vessel, REF_BASE)
    rgb = load_ortho_rgb(paths["side"], size=ORTHO_TEX_SIZE)
    im = Image.fromarray((rgb * 255).clip(0, 255).astype("uint8"), "RGB")
    draw = ImageDraw.Draw(im)

    def _dots(zone: dict, color: tuple[int, int, int]) -> None:
        for col, row in zone.get("pixels") or []:
            x, y = int(col), int(row)
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), outline=color, width=2)

    _dots(report.get("waterline") or {}, (220, 40, 40))
    _dots(report.get("superstructure") or {}, (240, 240, 240))
    out = ROOT / "logs" / "voxel_projection_landmarks_9388833.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    report["overlay"] = str(out)
    (ROOT / "logs" / "voxel_projection_diag_9388833.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    return 0 if not report.get("calibration_patch_needed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
