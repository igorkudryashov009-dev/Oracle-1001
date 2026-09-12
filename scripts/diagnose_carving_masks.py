#!/usr/bin/env python3
"""Carving deep-dive diagnostics: mask bbox scales + voxel occupancy (BU SAMRA).

Does NOT rebuild production GLBs. Writes:
  logs/carving_deepdive_9388833.json
  logs/carving_masks_9388833.png  (3 masks + bbox overlays)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IMO = "9388833"
OUT_JSON = ROOT / "logs" / "carving_deepdive_9388833.json"
OUT_PNG = ROOT / "logs" / "carving_masks_9388833.png"


def mask_bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return 0, 0, 0, 0
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def analyze_masks(side, bow, sat, *, loa_m: float, beam_m: float, draft_m: float) -> dict:
    """Independent per-mask pixel→metre via LOA/Beam reference scales."""
    out = {}
    for name, mask, ref_axis, ref_m in (
        ("side", side, "length_px→LOA", loa_m),
        ("bow", bow, "width_px→Beam", beam_m),
        ("sat", sat, "length_px→LOA", loa_m),
    ):
        x0, y0, x1, y1 = mask_bbox(mask)
        w_px = max(x1 - x0, 1)
        h_px = max(y1 - y0, 1)
        frac = float(mask.mean())
        entry = {
            "bbox_xyxy": [x0, y0, x1, y1],
            "length_or_width_px": w_px,
            "height_or_beam_px": h_px,
            "aspect_w_over_h": w_px / h_px,
            "fg_frac": frac,
            "bbox_frac_of_frame_w": w_px / mask.shape[1],
            "bbox_frac_of_frame_h": h_px / mask.shape[0],
            "bbox_center_norm": [
                ((x0 + x1) / 2) / mask.shape[1] * 2 - 1,
                1 - ((y0 + y1) / 2) / mask.shape[0] * 2,  # +1 = image top
            ],
        }
        if name == "side":
            # cols = length (LOA), rows = height
            m_per_px = loa_m / w_px
            entry["scale_ref"] = ref_axis
            entry["height_m_from_loa_scale"] = h_px * m_per_px
            entry["length_m"] = loa_m
        elif name == "bow":
            # cols = beam, rows = height
            m_per_px = beam_m / w_px
            entry["scale_ref"] = ref_axis
            entry["height_m_from_beam_scale"] = h_px * m_per_px
            entry["beam_m"] = beam_m
            # alternate: if we treated height as draft-like
            entry["beam_m_if_height_is_draft"] = w_px * (draft_m / h_px)
        elif name == "sat":
            # cols = length, rows = beam
            m_per_px_l = loa_m / w_px
            entry["scale_ref"] = ref_axis
            entry["beam_m_from_loa_scale"] = h_px * m_per_px_l
            entry["beam_m_from_beam_on_rows"] = beam_m  # if rows forced to beam
            m_per_px_b = beam_m / h_px
            entry["loa_m_from_beam_scale"] = w_px * m_per_px_b
        out[name] = entry

    side_h = out["side"]["height_m_from_loa_scale"]
    bow_h = out["bow"]["height_m_from_beam_scale"]
    out["height_sync"] = {
        "side_height_m": side_h,
        "bow_height_m": bow_h,
        "delta_m": abs(side_h - bow_h),
        "ratio_side_over_bow": side_h / max(bow_h, 1e-9),
        "sync_ok_within_25pct": abs(side_h - bow_h) / max(side_h, bow_h, 1e-9) < 0.25,
    }
    # Side image LOA:height pixel aspect vs physical
    out["side_ld_from_mask"] = loa_m / max(side_h, 1e-9)
    out["bow_bd_from_mask"] = beam_m / max(bow_h, 1e-9)
    return out


def occupancy_report(occupied: np.ndarray, hx, hy, hz) -> dict:
    """Per-axis occupancy BEFORE inflate/fit."""
    nx, ny, nz = occupied.shape
    # Mid-ship X slab (avoid bow/stern taper): central 20%
    x0, x1 = int(nx * 0.4), int(nx * 0.6)
    mid = occupied[x0:x1]
    z_hist = mid.sum(axis=(0, 1)).astype(int).tolist()  # per Z layer
    y_hist = mid.sum(axis=(0, 2)).astype(int).tolist()  # per Y layer
    # Per-X: mean Y-span and Z-span of occupied cells
    y_span = []
    z_span = []
    for i in range(nx):
        sl = occupied[i]
        if not sl.any():
            y_span.append(0)
            z_span.append(0)
            continue
        ys, zs = np.where(sl)
        y_span.append(int(ys.max() - ys.min() + 1) if len(ys) else 0)
        z_span.append(int(zs.max() - zs.min() + 1) if len(zs) else 0)
    mid_z = z_span[x0:x1]
    mid_y = y_span[x0:x1]
    return {
        "shape": [nx, ny, nz],
        "occupied_total": int(occupied.sum()),
        "occupied_frac": float(occupied.mean()),
        "midship_z_hist": z_hist,
        "midship_y_hist": y_hist,
        "midship_mean_z_span_voxels": float(np.mean(mid_z)) if mid_z else 0.0,
        "midship_mean_y_span_voxels": float(np.mean(mid_y)) if mid_y else 0.0,
        "midship_z_span_frac_of_nz": float(np.mean(mid_z)) / max(nz, 1),
        "midship_y_span_frac_of_ny": float(np.mean(mid_y)) / max(ny, 1),
        "design_half_extents": {"hx": hx, "hy": hy, "hz": hz},
        "note": "z_span_frac≪1 in midship ⇒ ribbon in height; y_span_frac≪1 ⇒ thin beam",
    }


def save_mask_sheet(side, bow, sat, analysis: dict, path: Path) -> None:
    size = side.shape[0]
    sheet = Image.new("RGB", (size * 3 + 40, size + 60), (12, 18, 28))
    draw = ImageDraw.Draw(sheet)
    for i, (name, mask) in enumerate((("side", side), ("bow", bow), ("sat", sat))):
        img = Image.fromarray((mask.astype(np.uint8) * 220))
        rgb = Image.merge("RGB", (img, img, img))
        x0, y0, x1, y1 = analysis[name]["bbox_xyxy"]
        d = ImageDraw.Draw(rgb)
        d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(0, 220, 120), width=2)
        sheet.paste(rgb, (10 + i * (size + 10), 40))
        draw.text((10 + i * (size + 10), 10), name.upper(), fill=(200, 220, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def estimate_hull_height_from_side(side_mask, loa_m: float) -> float:
    x0, y0, x1, y1 = mask_bbox(side_mask)
    w_px = max(x1 - x0, 1)
    h_px = max(y1 - y0, 1)
    return h_px * (loa_m / w_px)


def main() -> int:
    from services.top10_3d_mesh import (
        ORTHO_TEX_SIZE,
        carve_voxels,
        extract_silhouette,
        resolve_ortho_paths,
    )
    import services.top10_3d_mesh as mesh_mod
    from services.top10_vessels import REF_BASE, TOP10_VESSELS

    vessel = next(v for v in TOP10_VESSELS if str(v["imo"]) == IMO)
    loa = float(vessel["loa_m"])
    beam = float(vessel["beam_m"])
    draft = float(vessel["draft_m"])
    paths = resolve_ortho_paths(vessel, REF_BASE)

    side = extract_silhouette(paths["side"])
    bow = extract_silhouette(paths["bow"])
    sat = extract_silhouette(paths["sat"])

    masks = analyze_masks(side, bow, sat, loa_m=loa, beam_m=beam, draft_m=draft)
    est_h = estimate_hull_height_from_side(side, loa)
    save_mask_sheet(side, bow, sat, masks, OUT_PNG)

    # Physical extents with draft-only (current raw baseline)
    hx_d = 1.0
    hy_d = beam / loa
    hz_d = draft / loa
    m = max(hx_d, hy_d, hz_d)
    hx_d, hy_d, hz_d = hx_d / m, hy_d / m, hz_d / m

    prev = mesh_mod.Z_INFLATE_ITERS
    mesh_mod.Z_INFLATE_ITERS = 0
    try:
        occ_draft = carve_voxels(side, sat, bow, hx=hx_d, hy=hy_d, hz=hz_d, res=80)
    finally:
        mesh_mod.Z_INFLATE_ITERS = prev
    occ_draft_rep = occupancy_report(occ_draft, hx_d, hy_d, hz_d)

    # Extents with side-estimated hull height
    hx_e = 1.0
    hy_e = beam / loa
    hz_e = est_h / loa
    m = max(hx_e, hy_e, hz_e)
    hx_e, hy_e, hz_e = hx_e / m, hy_e / m, hz_e / m
    mesh_mod.Z_INFLATE_ITERS = 0
    try:
        occ_est = carve_voxels(side, sat, bow, hx=hx_e, hy=hy_e, hz=hz_e, res=80)
    finally:
        mesh_mod.Z_INFLATE_ITERS = prev
    occ_est_rep = occupancy_report(occ_est, hx_e, hy_e, hz_e)

    report = {
        "imo": IMO,
        "registry": {"loa_m": loa, "beam_m": beam, "draft_m": draft, "depth_m": None},
        "estimated_hull_height_m_from_side_mask": est_h,
        "physical_ld_draft_only": loa / draft,
        "physical_ld_est_height": loa / max(est_h, 1e-9),
        "physical_lb": loa / beam,
        "ortho_tex_size": ORTHO_TEX_SIZE,
        "masks": masks,
        "occupancy_draft_extents": occ_draft_rep,
        "occupancy_est_height_extents": occ_est_rep,
        "paths": {k: str(v) for k, v in paths.items()},
        "mask_sheet": str(OUT_PNG.relative_to(ROOT)).replace("\\", "/"),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
