#!/usr/bin/env python3
"""Diagnose triplanar projection bounds vs actual RawMesh vertex ranges (BU SAMRA)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    from services.top10_3d_mesh import (
        MASK_DILATE_PX,
        ORTHO_TEX_SIZE,
        extract_silhouette,
        generate_geometry,
        load_ortho_rgb,
        prepare_masked_ortho,
        resolve_ortho_paths,
        triplanar_colors,
        _chart_uvs_for_bake,
    )
    from services.top10_vessels import REF_BASE, TOP10_VESSELS

    v = next(x for x in TOP10_VESSELS if str(x["imo"]) == "9388833")
    paths = resolve_ortho_paths(v, REF_BASE)
    side_m = extract_silhouette(paths["side"], size=ORTHO_TEX_SIZE)
    top_m = extract_silhouette(paths["sat"], size=ORTHO_TEX_SIZE)
    bow_m = extract_silhouette(paths["bow"], size=ORTHO_TEX_SIZE)
    raw = generate_geometry(
        {"side": side_m, "sat": top_m, "bow": bow_m},
        loa_m=float(v["loa_m"]),
        beam_m=float(v["beam_m"]),
        draft_m=float(v["draft_m"]),
        voxel_res=80,
    )
    verts = np.asarray(raw.vertices, dtype=np.float64)
    vmin = verts.min(axis=0)
    vmax = verts.max(axis=0)
    vspan = np.maximum(vmax - vmin, 1e-9)
    center = 0.5 * (vmin + vmax)
    half_actual = 0.5 * vspan

    hx, hy, hz = raw.hx, raw.hy, raw.hz
    # What current triplanar does: ux = (pos / hx).clip(-1,1)
    ux_design = (verts[:, 0] / max(hx, 1e-6)).clip(-1, 1)
    uy_design = (verts[:, 1] / max(hy, 1e-6)).clip(-1, 1)
    uz_design = (verts[:, 2] / max(hz, 1e-6)).clip(-1, 1)

    # Correct (content-bbox analog): map actual bbox → [-1,1]
    ux_fix = ((verts[:, 0] - center[0]) / half_actual[0]).clip(-1, 1)
    uy_fix = ((verts[:, 1] - center[1]) / half_actual[1]).clip(-1, 1)
    uz_fix = ((verts[:, 2] - center[2]) / half_actual[2]).clip(-1, 1)

    # Saturation at clip rails = projection miss indicator
    def rail_frac(u: np.ndarray) -> dict:
        return {
            "min": float(u.min()),
            "max": float(u.max()),
            "mean": float(u.mean()),
            "frac_at_neg1": float(np.mean(np.abs(u + 1.0) < 1e-6)),
            "frac_at_pos1": float(np.mean(np.abs(u - 1.0) < 1e-6)),
            "frac_outside_pm08": float(np.mean(np.abs(u) > 0.8)),
        }

    side_rgb = prepare_masked_ortho(
        load_ortho_rgb(paths["side"], size=ORTHO_TEX_SIZE), side_m, dilate_px=MASK_DILATE_PX
    )
    bow_rgb = prepare_masked_ortho(
        load_ortho_rgb(paths["bow"], size=ORTHO_TEX_SIZE), bow_m, dilate_px=MASK_DILATE_PX
    )
    sat_rgb = prepare_masked_ortho(
        load_ortho_rgb(paths["sat"], size=ORTHO_TEX_SIZE), top_m, dilate_px=MASK_DILATE_PX
    )

    # Sample a few representative verts with BOTH bound systems
    rng = np.random.default_rng(42)
    idx = rng.choice(len(verts), size=min(12, len(verts)), replace=False)
    try:
        import trimesh

        mesh = trimesh.Trimesh(vertices=verts.copy(), faces=np.array(raw.faces), process=False)
        nrm = np.asarray(mesh.vertex_normals, dtype=np.float64)
    except Exception:
        nrm = np.zeros_like(verts)
        nrm[:, 1] = 1.0

    cols_design = triplanar_colors(
        verts[idx], nrm[idx], side_rgb, bow_rgb, sat_rgb, hx, hy, hz
    )
    cols_fix = triplanar_colors(
        verts[idx],
        nrm[idx],
        side_rgb,
        bow_rgb,
        sat_rgb,
        float(half_actual[0]),
        float(half_actual[1]),
        float(half_actual[2]),
    )
    # For fix path we also need centered positions when using half_actual
    verts_centered = verts - center
    cols_centered = triplanar_colors(
        verts_centered[idx],
        nrm[idx],
        side_rgb,
        bow_rgb,
        sat_rgb,
        float(half_actual[0]),
        float(half_actual[1]),
        float(half_actual[2]),
    )

    # Atlas color stats if we bake with current vs fixed bounds
    report = {
        "design_half_extents": {"hx": hx, "hy": hy, "hz": hz},
        "vertex_min": vmin.tolist(),
        "vertex_max": vmax.tolist(),
        "vertex_span": vspan.tolist(),
        "vertex_center": center.tolist(),
        "actual_half_extents": half_actual.tolist(),
        "mismatch": {
            "span_vs_2h": (vspan / np.array([2 * hx, 2 * hy, 2 * hz])).tolist(),
            "rezeroed_positive_octant": bool(np.all(vmin >= -1e-6)),
            "design_assumes_centered_pm_h": True,
        },
        "uv_design_div_h": {
            "ux": rail_frac(ux_design),
            "uy": rail_frac(uy_design),
            "uz": rail_frac(uz_design),
        },
        "uv_actual_centered": {
            "ux": rail_frac(ux_fix),
            "uy": rail_frac(uy_fix),
            "uz": rail_frac(uz_fix),
        },
        "sample_verts": [
            {
                "i": int(i),
                "pos": verts[i].tolist(),
                "ux_design": float(ux_design[i]),
                "uy_design": float(uy_design[i]),
                "uz_design": float(uz_design[i]),
                "ux_fix": float(ux_fix[i]),
                "rgb_design": cols_design[j].tolist(),
                "rgb_centered_actual_h": cols_centered[j].tolist(),
            }
            for j, i in enumerate(idx)
        ],
        "rgb_mean_design": cols_design.mean(axis=0).tolist(),
        "rgb_mean_centered_fix": cols_centered.mean(axis=0).tolist(),
        "diagnosis": None,
    }

    # Rail saturation on design path = miss
    sat_pos = (
        report["uv_design_div_h"]["ux"]["frac_at_pos1"]
        + report["uv_design_div_h"]["uy"]["frac_at_pos1"]
        + report["uv_design_div_h"]["uz"]["frac_at_pos1"]
    ) / 3.0
    if report["mismatch"]["rezeroed_positive_octant"] and sat_pos > 0.15:
        report["diagnosis"] = (
            "CONFIRMED: mesh is rezero'd into positive octant while triplanar "
            "divides by design hx/hy/hz assuming centered [-h,+h]. "
            "Many verts clip to u=+1 → sample photo edge/median → beige-white atlas."
        )
    else:
        report["diagnosis"] = "No strong rail-saturation; inspect samples manually."

    out = ROOT / "logs" / "triplanar_uv_mismatch_9388833.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
