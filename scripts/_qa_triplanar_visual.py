#!/usr/bin/env python3
"""Visual QA: ortho side vs triplanar baked side-chart."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "_qa_triplanar"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    pairs = [
        ("9388833", 1, "BU SAMRA"),
        ("9337755", 5, "MOZAH"),
        ("9397327", 4, "AL KHARAITIYAT"),
        ("9397315", 3, "AL MAFYAR"),
    ]
    report = []
    for imo, rank, name in pairs:
        glb = ROOT / "web" / "assets" / "3d_models" / f"vessel_{imo}.glb"
        meta = json.loads(glb.with_suffix(".meta.json").read_text(encoding="utf-8"))
        mesh = trimesh.load(str(glb), force="mesh")
        img = None
        try:
            img = mesh.visual.material.image
        except Exception:
            pass
        if img is None:
            try:
                img = mesh.visual.material.baseColorTexture
            except Exception:
                pass
        if hasattr(img, "convert"):
            atlas = img.convert("RGB")
        elif isinstance(img, np.ndarray):
            atlas = Image.fromarray(img)
        else:
            atlas = Image.new("RGB", (256, 256), (80, 80, 80))
        atlas.save(OUT / f"atlas_{rank}_{imo}.jpg", quality=90)

        ortho = Image.open(ROOT / "assets" / "7000" / f"{rank}-1.jpg").convert("RGB")
        ortho = ortho.resize((900, 360), Image.Resampling.LANCZOS)
        aw, ah = atlas.size
        side_chart = atlas.crop((0, 0, aw // 2, ah)).resize((450, 360), Image.Resampling.LANCZOS)
        strip = Image.new("RGB", (900 + 450 + 16, 400), (12, 18, 28))
        strip.paste(ortho, (0, 20))
        strip.paste(side_chart, (916, 20))
        d = ImageDraw.Draw(strip)
        d.text((8, 2), f"{name} · IMO {imo} · ORTHO SIDE", fill=(200, 220, 230))
        d.text((916, 2), "TRIPLANAR BAKE (side chart)", fill=(200, 220, 230))
        strip.save(OUT / f"compare_{rank}_{imo}.jpg", quality=92)

        sc = np.asarray(side_chart, dtype=np.float32)
        oc = np.asarray(ortho.resize(side_chart.size), dtype=np.float32)
        row = {
            "imo": imo,
            "rank": rank,
            "name": name,
            "bytes": meta["bytes"],
            "alg": meta.get("alg"),
            "texturing": meta.get("texturing"),
            "atlas_px": meta.get("atlas_px"),
            "jpeg_quality": meta.get("jpeg_quality"),
            "mean_rgb_atlas_side": sc.mean(axis=(0, 1)).round(1).tolist(),
            "mean_rgb_ortho": oc.mean(axis=(0, 1)).round(1).tolist(),
        }
        report.append(row)
        print(
            f"IMO {imo}: bytes={meta['bytes']} alg={meta.get('alg')} "
            f"atlas={meta.get('atlas_px')} q={meta.get('jpeg_quality')}"
        )
        print(f"  mean RGB atlas-side={row['mean_rgb_atlas_side']} ortho={row['mean_rgb_ortho']}")

    (OUT / "qa_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"WROTE {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
