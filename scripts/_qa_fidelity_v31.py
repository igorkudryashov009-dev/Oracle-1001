#!/usr/bin/env python3
"""Visual QA v3.1: sky-bleed + atlas side chart for 5 vessels."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "_qa_fidelity_v31"


def blue_frac(arr: np.ndarray) -> float:
    return float(
        (
            (arr[:, :, 2] > arr[:, :, 0] + 25)
            & (arr[:, :, 2] > arr[:, :, 1] + 15)
            & (arr[:, :, 2] > 140)
        ).mean()
    )


def dihedral_stats(mesh: trimesh.Trimesh) -> dict:
    try:
        # face adjacency angles in radians
        angles = np.asarray(mesh.face_adjacency_angles, dtype=np.float64)
        deg = np.degrees(angles)
        return {
            "mean_deg": float(np.mean(deg)),
            "p50_deg": float(np.median(deg)),
            "p90_deg": float(np.percentile(deg, 90)),
            "n_adj": int(len(deg)),
        }
    except Exception as exc:
        return {"error": str(exc)}


def load_atlas(mesh: trimesh.Trimesh) -> Image.Image:
    img = None
    try:
        img = mesh.visual.material.baseColorTexture
    except Exception:
        pass
    if img is None:
        try:
            img = mesh.visual.material.image
        except Exception:
            pass
    if hasattr(img, "convert"):
        return img.convert("RGB")
    if isinstance(img, np.ndarray):
        return Image.fromarray(img.astype(np.uint8))
    return Image.new("RGB", (256, 256), (80, 80, 80))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    pairs = [
        ("9388833", 1, "BU SAMRA"),
        ("9397303", 2, "AL MAYEDA"),
        ("9397315", 3, "AL MAFYAR"),
        ("9397327", 4, "AL KHARAITIYAT"),
        ("9337755", 5, "MOZAH"),
    ]
    report = []
    for imo, rank, name in pairs:
        glb = ROOT / "web" / "assets" / "3d_models" / f"vessel_{imo}.glb"
        if not glb.exists():
            glb = ROOT / "output" / "assets" / "3d_models" / f"vessel_{imo}.glb"
        meta = json.loads(glb.with_suffix(".meta.json").read_text(encoding="utf-8"))
        mesh = trimesh.load(str(glb), force="mesh")
        atlas = load_atlas(mesh)
        atlas.save(OUT / f"atlas_{rank}_{imo}.jpg", quality=90)

        ortho = Image.open(ROOT / "assets" / "7000" / f"{rank}-1.jpg").convert("RGB")
        ortho_r = ortho.resize((900, 360), Image.Resampling.LANCZOS)
        aw, ah = atlas.size
        side_chart = atlas.crop((0, 0, aw // 2, ah)).resize((450, 360), Image.Resampling.LANCZOS)
        strip = Image.new("RGB", (900 + 450 + 16, 400), (12, 18, 28))
        strip.paste(ortho_r, (0, 20))
        strip.paste(side_chart, (916, 20))
        d = ImageDraw.Draw(strip)
        d.text((8, 2), f"{name} · IMO {imo} · ORTHO SIDE", fill=(200, 220, 230))
        d.text((916, 2), "TRIPLANAR BAKE (side chart)", fill=(200, 220, 230))
        strip.save(OUT / f"compare_{rank}_{imo}.jpg", quality=92)

        sc = np.asarray(side_chart, dtype=np.float32)
        oc = np.asarray(ortho_r.resize(side_chart.size), dtype=np.float32)
        bs = meta.get("bbox_smoothing") or {}
        row = {
            "imo": imo,
            "rank": rank,
            "name": name,
            "bytes": meta.get("bytes"),
            "alg": meta.get("alg"),
            "texturing": meta.get("texturing"),
            "mask_dilate_px": meta.get("mask_dilate_px"),
            "taubin_iters": meta.get("taubin_iters"),
            "facet_fix": meta.get("facet_fix"),
            "bbox_drift_pct": bs.get("bbox_drift_pct"),
            "bbox_drift_ok": bs.get("bbox_drift_ok"),
            "blue_frac_side_chart": blue_frac(sc.astype(np.uint8)),
            "mean_rgb_atlas_side": sc.mean(axis=(0, 1)).round(1).tolist(),
            "mean_rgb_ortho": oc.mean(axis=(0, 1)).round(1).tolist(),
            "dihedral": dihedral_stats(mesh),
        }
        report.append(row)
        print(
            f"{name}: alg={row['alg']} blue={row['blue_frac_side_chart']:.3f} "
            f"drift_ok={row['bbox_drift_ok']} dihedral_mean={row['dihedral'].get('mean_deg')} "
            f"p90={row['dihedral'].get('p90_deg')}"
        )

    (OUT / "qa_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"WROTE {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
