#!/usr/bin/env python3
"""Hypothesis split: voxel XY footprint vs SAT mask (no camera)."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

IMOS = ("9388833", "9337755", "9372731")  # BU SAMRA, MOZAH, UMM SLAL


def b64_f32(s: str) -> np.ndarray:
    raw = base64.b64decode(s)
    return np.frombuffer(raw, dtype="<f4")


def taper_side(mask: np.ndarray, axis: str) -> str:
    """Which end is narrower (bow-like). axis='x' → left/right of image."""
    m = np.asarray(mask) > 0
    if axis == "x":
        col = m.sum(axis=0)
        n = len(col)
        left = float(col[: n // 5].mean()) if n else 0
        right = float(col[4 * n // 5 :].mean()) if n else 0
        if left < right * 0.85:
            return "left"
        if right < left * 0.85:
            return "right"
        return "ambiguous"
    row = m.sum(axis=1)
    n = len(row)
    top = float(row[: n // 5].mean()) if n else 0
    bot = float(row[4 * n // 5 :].mean()) if n else 0
    if top < bot * 0.85:
        return "top"
    if bot < top * 0.85:
        return "bottom"
    return "ambiguous"


def iou(a: np.ndarray, b: np.ndarray) -> float:
    aa = np.asarray(a) > 0
    bb = np.asarray(b) > 0
    inter = np.logical_and(aa, bb).sum()
    union = np.logical_or(aa, bb).sum()
    return float(inter / union) if union else 0.0


def world_xy_to_sat_mask(ux: np.ndarray, uy: np.ndarray, sat_m: np.ndarray, bounds) -> np.ndarray:
    from services.top10_voxel_cubes import uv_to_pixel

    h, w = sat_m.shape
    col, row = uv_to_pixel(ux, uy, size=w, bounds=bounds)
    fp = np.zeros_like(sat_m, dtype=bool)
    rr = np.clip(np.round(row).astype(np.int32), 0, h - 1)
    cc = np.clip(np.round(col).astype(np.int32), 0, w - 1)
    fp[rr, cc] = True
    return fp


def diagnose_one(imo: str) -> dict:
    from services.top10_3d_mesh import (
        ORTHO_TEX_SIZE,
        _to_unit,
        estimate_visual_depth_m,
        extract_silhouette,
        load_ortho_rgb,
        mask_content_uv_bounds,
        resolve_ortho_paths,
    )
    from services.top10_vessels import REF_BASE, TOP10_VESSELS
    from services.top10_voxel_cubes import (
        V3_METRIC_PITCH,
        carve_voxels,
        equal_metric_shape,
        _sample_ortho,
    )
    from services.top10_3d_mesh import carve_voxels as carve

    v = next(x for x in TOP10_VESSELS if str(x["imo"]) == str(imo))
    paths = resolve_ortho_paths(v, REF_BASE)
    sat_m = extract_silhouette(paths["sat"], size=ORTHO_TEX_SIZE)
    side_m = extract_silhouette(paths["side"], size=ORTHO_TEX_SIZE)
    bow_m = extract_silhouette(paths["bow"], size=ORTHO_TEX_SIZE)
    sat_b = mask_content_uv_bounds(sat_m)
    depth = estimate_visual_depth_m(
        side_m, float(v["loa_m"]), float(v["draft_m"]), float(v["beam_m"])
    )
    hx, hy, hz = _to_unit(
        float(v["loa_m"]),
        float(v["beam_m"]),
        float(v["draft_m"]),
        visual_depth_m=depth["visual_depth_m"],
    )
    shape = equal_metric_shape(hx, hy, hz, pitch=V3_METRIC_PITCH)
    occupied = carve(side_m, sat_m, bow_m, hx=hx, hy=hy, hz=hz, shape=shape)
    nx, ny, nz = occupied.shape
    xs = np.linspace(-hx, hx, nx)
    ys = np.linspace(-hy, hy, ny)
    occ_xy = occupied.any(axis=2)
    ux_g = (xs / max(hx, 1e-6)).clip(-1, 1)
    uy_g = (ys / max(hy, 1e-6)).clip(-1, 1)
    UX, UY = np.meshgrid(ux_g, uy_g, indexing="ij")
    from services.top10_3d_mesh import carve_voxels as _cv

    def sample_mask(mask, u, v, bounds):
        h, w = mask.shape
        u0, u1, v0, v1 = bounds
        u_img = u0 + (u + 1.0) * 0.5 * (u1 - u0)
        v_img = v0 + (v + 1.0) * 0.5 * (v1 - v0)
        col = ((u_img + 1.0) * 0.5 * (w - 1)).clip(0, w - 1).astype(np.int32)
        row = ((1.0 - v_img) * 0.5 * (h - 1)).clip(0, h - 1).astype(np.int32)
        return mask[row, col]

    sat_on_grid = sample_mask(sat_m, UX.ravel(), UY.ravel(), sat_b).reshape(occ_xy.shape)
    h1_iou = iou(occ_xy, sat_on_grid)

    ii, jj = np.where(occ_xy)
    ux = UX[ii, jj]
    uy = UY[ii, jj]
    fp_carve = world_xy_to_sat_mask(ux, uy, sat_m, sat_b)
    # Dilate 1px so sparse grid still fills silhouette for IoU
    from scipy import ndimage

    fp_carve_d = ndimage.binary_dilation(fp_carve, iterations=2)

    json_path = ROOT / "output" / "assets" / "3d_models" / f"vessel_{imo}_voxels.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    pos = b64_f32(payload["positions_f32_b64"]).reshape(-1, 3)
    hxj, hyj, hzj = payload["half_extents"]
    uxj = (pos[:, 0] / max(hxj, 1e-6)).clip(-1, 1)
    uyj = (pos[:, 1] / max(hyj, 1e-6)).clip(-1, 1)
    fp_json = world_xy_to_sat_mask(uxj, uyj, sat_m, sat_b)
    fp_json_d = ndimage.binary_dilation(fp_json, iterations=2)

    sat_rgb = load_ortho_rgb(paths["sat"], size=ORTHO_TEX_SIZE)
    pal = np.asarray(payload["palette_rgb_u8"], dtype=np.uint8)
    idx = np.frombuffer(base64.b64decode(payload["color_indices_u8_b64"]), dtype=np.uint8)
    cell_rgb = pal[np.clip(idx, 0, len(pal) - 1)] / 255.0
    sat_at = _sample_ortho(sat_rgb, uxj, uyj, bounds=sat_b)
    # top layer: max z per rounded xy bin
    key = np.round(np.stack([uxj, uyj], axis=1) * 200).astype(np.int32)
    order = np.argsort(pos[:, 2])
    top_idx = {}
    for i in order:
        top_idx[tuple(key[i])] = int(i)
    top_i = np.array(list(top_idx.values()), dtype=np.int32)
    d = cell_rgb[top_i] - sat_at[top_i]
    lab_approx = float(np.median(np.sqrt((d * d).sum(axis=1))) * 100.0)  # 0-173 scale-ish

    # SAT face would be sat_at; side-weighted stored color vs SAT
    h1 = {
        "iou_grid_xy_vs_sat": round(h1_iou, 4),
        "iou_carve_xy_vs_sat": round(iou(fp_carve_d, sat_m), 4),
        "iou_json_xy_vs_sat": round(iou(fp_json_d, sat_m), 4),
        "sat_on_grid_frac": round(float(sat_on_grid.mean()), 4),
        "carve_xy_frac": round(float(occ_xy.mean()), 4),
        "n_xy": int(occ_xy.sum()),
        "n_occ": int(occupied.sum()),
        "precision": round(float((occ_xy & sat_on_grid).sum() / max(occ_xy.sum(), 1)), 4),
        "recall": round(float((occ_xy & sat_on_grid).sum() / max(sat_on_grid.sum(), 1)), 4),
    }
    h2 = {
        "sat_mask_narrower": taper_side(sat_m, "x"),
        "carve_fp_narrower": taper_side(fp_carve_d, "x"),
        "json_fp_narrower": taper_side(fp_json_d, "x"),
        "sat_mask_narrower_y": taper_side(sat_m, "y"),
        "carve_fp_narrower_y": taper_side(fp_carve_d, "y"),
        "contract": "+X bow, +Y starboard, +Z up",
        "camera_overhead": "look -Z, up=+Y ⇒ screen-right = +X (bow if contract holds)",
    }
    h2["orientation_match_x"] = h2["sat_mask_narrower"] == h2["carve_fp_narrower"]
    h4 = {
        "median_rgb_l2_top_vs_sat_x100": round(lab_approx, 2),
        "note": "cell color is 6-face mode with side×3; SAT is 1 of 7 votes",
    }

    # Save overlay PNG
    h, w = sat_m.shape
    overlay = np.zeros((h, w, 3), dtype=np.uint8)
    overlay[sat_m] = (40, 80, 40)
    overlay[fp_carve_d] = np.maximum(overlay[fp_carve_d], (180, 180, 180))
    both = sat_m & fp_carve_d
    overlay[both] = (80, 220, 120)
    only_sat = sat_m & ~fp_carve_d
    only_fp = fp_carve_d & ~sat_m
    overlay[only_sat] = (220, 60, 60)
    overlay[only_fp] = (60, 120, 255)

    out = {
        "imo": imo,
        "name": v["name"],
        "h1_footprint": h1,
        "h2_orientation": h2,
        "h4_color": h4,
        "h1_pass": h1["iou_grid_xy_vs_sat"] >= 0.5,
        "h1_strong": h1["iou_grid_xy_vs_sat"] >= 0.8,
    }
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    Image.fromarray(overlay).save(logs / f"overhead_h1_overlay_{imo}.png")
    return out


def main() -> int:
    rows = [diagnose_one(imo) for imo in IMOS]
    report = {"vessels": rows}
    (ROOT / "logs" / "overhead_h1h2h4_diag.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
