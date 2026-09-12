#!/usr/bin/env python3
"""Full LOA height-profile: carved mesh vs Side silhouette mask (BU SAMRA).

Prompt: 40 bins along X; shape comparison (ramp vs flat+step); resolution/smoothing audit.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

N_BINS = 40
IMO = "9388833"
OUT_JSON = ROOT / "logs" / "height_profile_compare_9388833.json"
OUT_ASCII = ROOT / "logs" / "height_profile_compare_9388833.txt"


def ascii_dual_plot(
    x_frac: np.ndarray,
    mesh_n: np.ndarray,
    mask_n: np.ndarray,
    width: int = 56,
    height: int = 14,
) -> str:
    """Simple dual-series ASCII chart (normalized 0..1)."""
    canvas = [[" "] * width for _ in range(height)]
    for series, ch in ((mesh_n, "M"), (mask_n, "S")):
        for i, xf in enumerate(x_frac):
            col = int(np.clip(xf * (width - 1), 0, width - 1))
            row = int(np.clip((1.0 - float(series[i])) * (height - 1), 0, height - 1))
            if canvas[row][col] in (" ", ch):
                canvas[row][col] = ch
            else:
                canvas[row][col] = "*"  # overlap
    lines = ["".join(r) for r in canvas]
    axis = "-" * width
    return (
        "height ^ (norm)\n"
        + "\n".join(lines)
        + f"\n{axis}\n"
        + f"bow{' ' * (width // 2 - 6)}LOA ->{' ' * (width // 2 - 8)}stern\n"
        + "legend: M=mesh  S=side-mask  *=overlap\n"
    )


def profile_shape_stats(z: np.ndarray, x_frac: np.ndarray) -> dict:
    z = np.asarray(z, dtype=np.float64)
    zmax = float(np.max(z)) if len(z) else 1.0
    zn = z / max(zmax, 1e-9)
    # regions
    early = zn[x_frac <= 0.80]
    late = zn[x_frac > 0.80]
    mid = zn[(x_frac >= 0.20) & (x_frac <= 0.80)]
    # monotonicity: fraction of positive forward diffs
    d = np.diff(zn)
    mono = float(np.mean(d > 0)) if len(d) else 0.0
    # linear slope over full length
    coef = np.polyfit(x_frac, zn, 1)
    # step score: late mean vs early mean
    early_m = float(np.mean(early)) if len(early) else 0.0
    late_m = float(np.mean(late)) if len(late) else 0.0
    mid_std = float(np.std(mid)) if len(mid) else 0.0
    # classify
    if mid_std < 0.08 and late_m > early_m * 1.15:
        form = "flat_plus_aft_step"
    elif coef[0] > 0.25 and mono > 0.55:
        form = "smooth_ramp_full_length"
    elif mid_std < 0.12 and abs(late_m - early_m) < 0.12:
        form = "mostly_flat"
    else:
        form = "mixed_irregular"
    return {
        "form_class": form,
        "z_max": zmax,
        "zn_early80_mean": early_m,
        "zn_late20_mean": late_m,
        "late_over_early": late_m / max(early_m, 1e-9),
        "mid20_80_std": mid_std,
        "frac_positive_diffs": mono,
        "linear_slope_zn": float(coef[0]),
        "linear_intercept_zn": float(coef[1]),
        "endpoint_ratio_last_first": float(zn[-1] / max(zn[0], 1e-9)),
    }


def main() -> int:
    import trimesh

    from services.top10_3d_mesh import (
        DEFAULT_VOXEL,
        MASK_DILATE_PX,
        ORTHO_TEX_SIZE,
        TAUBIN_ITERS,
        Z_INFLATE_ITERS,
        anisotropic_pitch,
        carve_voxels,
        estimate_visual_depth_m,
        extract_silhouette,
        generate_geometry,
        mask_bbox_xyxy,
        mask_content_uv_bounds,
        resolve_ortho_paths,
        _to_unit,
    )
    from services.top10_vessels import REF_BASE, TOP10_VESSELS

    v = next(x for x in TOP10_VESSELS if str(x["imo"]) == IMO)
    loa = float(v["loa_m"])
    beam = float(v["beam_m"])
    draft = float(v["draft_m"])
    paths = resolve_ortho_paths(v, REF_BASE)

    side_m = extract_silhouette(paths["side"], size=ORTHO_TEX_SIZE)
    top_m = extract_silhouette(paths["sat"], size=ORTHO_TEX_SIZE)
    bow_m = extract_silhouette(paths["bow"], size=ORTHO_TEX_SIZE)
    depth_est = estimate_visual_depth_m(side_m, loa, draft, beam)
    hx, hy, hz = _to_unit(loa, beam, draft, visual_depth_m=depth_est["visual_depth_m"])

    # ── Mesh profile (production geometry path) ─────────────────────────────
    raw = generate_geometry(
        {"side": side_m, "sat": top_m, "bow": bow_m},
        loa_m=loa,
        beam_m=beam,
        draft_m=draft,
        voxel_res=DEFAULT_VOXEL,
    )
    verts = np.asarray(raw.vertices, dtype=np.float64)
    x = verts[:, 0]
    z = verts[:, 2]
    xmin, xmax = float(x.min()), float(x.max())
    edges = np.linspace(xmin, xmax, N_BINS + 1)
    mesh_rows = []
    for i in range(N_BINS):
        lo, hi = edges[i], edges[i + 1]
        mask = (x >= lo) & (x < hi if i < N_BINS - 1 else x <= hi)
        if not np.any(mask):
            z_span = 0.0
            n = 0
        else:
            zz = z[mask]
            z_span = float(zz.max() - zz.min())
            n = int(mask.sum())
        x_mid = 0.5 * (lo + hi)
        x_frac = (x_mid - xmin) / max(xmax - xmin, 1e-9)
        mesh_rows.append(
            {
                "bin": i,
                "x_mid": x_mid,
                "x_frac": float(x_frac),
                "x_m_from_bow": float(x_frac * loa),  # 0=bow-ish if +X=stern? check contract
                "z_span": z_span,
                "n_verts": n,
            }
        )

    # Axis note: carve docs say i→X bow→stern; after center, −X is one end +X the other.
    # Side photo cols: left→right. Content UV maps world X into content window.
    # For comparison we use x_frac 0→1 = mesh xmin→xmax (same ordering both series).

    # ── Side-mask profile (source of truth) ─────────────────────────────────
    # Content-bbox columns only (same window carve samples).
    h_px, w_px = side_m.shape
    x0, y0, x1, y1 = mask_bbox_xyxy(side_m)
    # pad like mask_content_uv_bounds
    from services.top10_3d_mesh import MASK_CONTENT_PAD_FRAC

    pad_x = MASK_CONTENT_PAD_FRAC * w_px
    pad_y = MASK_CONTENT_PAD_FRAC * h_px
    cx0 = max(0, int(x0 - pad_x))
    cy0 = max(0, int(y0 - pad_y))
    cx1 = min(w_px, int(x1 + pad_x))
    cy1 = min(h_px, int(y1 + pad_y))
    content_w = max(cx1 - cx0, 1)
    m_per_px = loa / content_w  # horizontal scale from LOA

    # Also meter scale from visual depth vs content height
    content_h = max(cy1 - cy0, 1)
    visual_depth = float(depth_est["visual_depth_m"])
    m_per_px_v = visual_depth / content_h

    mask_rows = []
    for i in range(N_BINS):
        # column range inside content bbox proportional to bin
        c_lo = cx0 + int(i * content_w / N_BINS)
        c_hi = cx0 + int((i + 1) * content_w / N_BINS)
        c_hi = max(c_hi, c_lo + 1)
        col_strip = side_m[:, c_lo:c_hi]
        # per-column heights then mean
        heights_px = []
        for c in range(col_strip.shape[1]):
            ys = np.where(col_strip[:, c])[0]
            if len(ys) == 0:
                heights_px.append(0.0)
            else:
                heights_px.append(float(ys.max() - ys.min() + 1))
        h_px_mean = float(np.mean(heights_px)) if heights_px else 0.0
        x_frac = (i + 0.5) / N_BINS
        mask_rows.append(
            {
                "bin": i,
                "x_frac": x_frac,
                "x_m_from_bow": float(x_frac * loa),
                "height_px": h_px_mean,
                "z_span_m_loa_scale": h_px_mean * m_per_px,
                "z_span_m_depth_scale": h_px_mean * m_per_px_v,
            }
        )

    mesh_z = np.array([r["z_span"] for r in mesh_rows], dtype=np.float64)
    mask_z = np.array([r["z_span_m_depth_scale"] for r in mask_rows], dtype=np.float64)
    # Prefer depth_scale for absolute meters (air-draft); also keep loa_scale series
    mask_z_loa = np.array([r["z_span_m_loa_scale"] for r in mask_rows], dtype=np.float64)
    x_frac = np.array([r["x_frac"] for r in mesh_rows], dtype=np.float64)

    # Align mask bins to same x_frac (already same N_BINS indexing)
    mesh_stats = profile_shape_stats(mesh_z, x_frac)
    mask_stats = profile_shape_stats(mask_z, x_frac)

    mesh_n = mesh_z / max(float(mesh_z.max()), 1e-9)
    mask_n = mask_z / max(float(mask_z.max()), 1e-9)
    corr = float(np.corrcoef(mesh_n, mask_n)[0, 1]) if len(mesh_n) > 2 else 0.0
    mae_norm = float(np.mean(np.abs(mesh_n - mask_n)))

    # Occupancy voxel profile (pre-MC) for resolution check
    occupied = carve_voxels(side_m, top_m, bow_m, hx=hx, hy=hy, hz=hz, res=DEFAULT_VOXEL)
    # occupied shape (nx,ny,nz); for each x-slice, z span of True
    nx, ny, nz = occupied.shape
    pitch = anisotropic_pitch(nx, ny, nz, hx, hy, hz)
    pitch_x_m = float(pitch[0] * loa)  # unit half-extent hx maps to loa/2 → full pitch in metres
    # More precisely: full LOA ≈ 2*hx in unit space maps to loa metres
    pitch_x_m = float(pitch[0] / max(2.0 * hx, 1e-9) * loa)
    pitch_z_m = float(pitch[2] / max(2.0 * hz, 1e-9) * visual_depth)

    voxel_rows = []
    for i in range(N_BINS):
        i0 = int(i * nx / N_BINS)
        i1 = int((i + 1) * nx / N_BINS)
        i1 = max(i1, i0 + 1)
        slab = occupied[i0:i1]
        z_hist = slab.any(axis=(0, 1))  # nz bool
        if not np.any(z_hist):
            z_span_u = 0.0
        else:
            zi = np.where(z_hist)[0]
            z_span_u = float((zi.max() - zi.min() + 1) * pitch[2])
        voxel_rows.append(
            {
                "bin": i,
                "x_frac": (i + 0.5) / N_BINS,
                "z_span_unit": z_span_u,
                "z_span_m": z_span_u / max(2.0 * hz, 1e-9) * visual_depth,
            }
        )
    voxel_z = np.array([r["z_span_m"] for r in voxel_rows], dtype=np.float64)
    voxel_stats = profile_shape_stats(voxel_z, x_frac)

    # Mechanism audit
    bins_on_loa = DEFAULT_VOXEL
    meters_per_voxel_x = loa / max(bins_on_loa - 1, 1)
    # Superstructure last 15-20% of LOA ≈ 0.15*345 ≈ 52m → how many voxels?
    voxels_in_aft_20 = int(0.20 * (bins_on_loa - 1))
    # Morphological closing on mask: iterations=3 with 3x3 ≈ expands/smooths ~3px
    # At ORTHO_TEX_SIZE=1024, content_w ~?, 3px in X as fraction of LOA:
    close_iters = 3
    open_iters = 1
    px_smooth_x = close_iters + open_iters  # rough kernel radius order
    smooth_frac_loa = px_smooth_x / max(content_w, 1)
    smooth_m = smooth_frac_loa * loa

    # Voxel 3D closing/dilation also smooths
    carve_close = 2
    carve_dilate = 1

    # Decision logic for artifact
    shapes_match = mesh_stats["form_class"] == mask_stats["form_class"] or (
        corr > 0.85 and mae_norm < 0.12
    )
    mask_is_flat_step = mask_stats["form_class"] == "flat_plus_aft_step"
    mesh_is_ramp = mesh_stats["form_class"] == "smooth_ramp_full_length"
    artifact = bool(mask_is_flat_step and mesh_is_ramp) or (
        mask_stats["mid20_80_std"] < 0.10
        and mesh_stats["mid20_80_std"] > mask_stats["mid20_80_std"] * 2.0
        and mesh_stats["linear_slope_zn"] > mask_stats["linear_slope_zn"] + 0.15
    )

    # Resolution vs smoothing
    resolution_insufficient = voxels_in_aft_20 < 12  # can't resolve sharp step
    # If mask already has flat+step at full pixel res, smoothing didn't destroy step in mask
    mask_still_has_step = mask_is_flat_step or (
        mask_stats["late_over_early"] > 1.15 and mask_stats["mid20_80_std"] < 0.12
    )
    smoothing_destroyed_mask_step = not mask_still_has_step and True  # only if we expected step
    # Primary: if mask retains step but mesh/voxel ramp → voxelization/MC/Taubin path
    voxel_is_ramp = voxel_stats["form_class"] == "smooth_ramp_full_length"
    mechanism = []
    if artifact or (mask_still_has_step and not shapes_match):
        if mask_still_has_step and (voxel_is_ramp or mesh_is_ramp):
            mechanism.append(
                "voxel_grid_and_or_post_mc_smoothing: Side mask retains flat+aft-step; "
                "occupied/mesh profiles are smoother ramp → loss happens at carve/MC/Taubin, "
                f"not in extract_silhouette morphology alone (mask mid_std={mask_stats['mid20_80_std']:.3f})."
            )
        if resolution_insufficient:
            mechanism.append(
                f"coarse_x_resolution: DEFAULT_VOXEL={DEFAULT_VOXEL} → ~{meters_per_voxel_x:.1f} m/voxel X; "
                f"aft 20% LOA spans only ~{voxels_in_aft_20} voxels — sharp superstructure step "
                "cannot be localized."
            )
        if close_iters >= 2:
            mechanism.append(
                f"mask_morphology: binary_opening×{open_iters}+closing×{close_iters} "
                f"(~{px_smooth_x}px / ~{smooth_m:.1f}m along LOA) — mild; mask still shows step "
                "so NOT primary cause of full-length ramp."
            )
        mechanism.append(
            f"carve_morphology: occupied binary_closing×{carve_close}+dilation×{carve_dilate}; "
            f"Taubin×{TAUBIN_ITERS}; Z_INFLATE={Z_INFLATE_ITERS}."
        )
    else:
        mechanism.append(
            "No clear mask→mesh shape mismatch under classifiers; ramp may be legitimate "
            "reproduction of mask shape (or classifiers inconclusive — see corr/MAE)."
        )

    if shapes_match and corr > 0.85:
        verdict = (
            "LEGITIMATE: carved height-profile SHAPE matches Side-mask profile "
            f"(corr={corr:.3f}, both≈{mesh_stats['form_class']}). Ramp/wedge is reproduction, not artifact."
        )
        ramp_is_artifact = False
    elif mask_still_has_step and (mesh_is_ramp or voxel_is_ramp) and corr < 0.85:
        verdict = (
            "ARTIFACT: Side mask is flat+aft-step; carved/voxel profile is smoother full-length ramp. "
            "Carving/voxelization (and possibly Taubin) is smoothing a sharp aft transition."
        )
        ramp_is_artifact = True
    elif corr >= 0.70:
        verdict = (
            f"PARTIAL MATCH: corr={corr:.3f} — shapes related but classifiers differ "
            f"(mask={mask_stats['form_class']}, mesh={mesh_stats['form_class']}). "
            "Not unambiguous enough for blind fix — see profiles."
        )
        ramp_is_artifact = None  # inconclusive
    else:
        verdict = (
            f"MISMATCH: corr={corr:.3f}, mask_form={mask_stats['form_class']}, "
            f"mesh_form={mesh_stats['form_class']}. Needs more analysis before fix."
        )
        ramp_is_artifact = None

    ascii_chart = ascii_dual_plot(x_frac, mesh_n, mask_n)

    report = {
        "imo": IMO,
        "n_bins": N_BINS,
        "loa_m": loa,
        "visual_depth_m": visual_depth,
        "axis_note": (
            "x_frac 0→1 = mesh xmin→xmax after centering; Side mask columns left→right "
            "in content-bbox (same UV window as carve)."
        ),
        "mesh_profile": mesh_rows,
        "mask_profile": mask_rows,
        "voxel_occupancy_profile": voxel_rows,
        "shape": {
            "mesh": mesh_stats,
            "mask_depth_scale": mask_stats,
            "voxel": voxel_stats,
            "corr_norm_mesh_vs_mask": corr,
            "mae_norm_mesh_vs_mask": mae_norm,
            "shapes_match": shapes_match,
        },
        "resolution_audit": {
            "DEFAULT_VOXEL": DEFAULT_VOXEL,
            "ORTHO_TEX_SIZE": ORTHO_TEX_SIZE,
            "content_bbox_xyxy": [cx0, cy0, cx1, cy1],
            "content_w_px": content_w,
            "content_h_px": content_h,
            "meters_per_voxel_x": meters_per_voxel_x,
            "meters_per_voxel_z_approx": pitch_z_m,
            "voxels_in_aft_20pct_loa": voxels_in_aft_20,
            "resolution_insufficient_for_sharp_aft_step": resolution_insufficient,
        },
        "smoothing_audit": {
            "extract_silhouette": [
                "MedianFilter(3) on gray before BG flood (not on binary mask geometry directly)",
                f"binary_opening iterations={open_iters}",
                f"binary_closing iterations={close_iters}",
                "binary_fill_holes",
            ],
            "approx_morph_smooth_m_along_loa": smooth_m,
            "carve_voxels": [
                f"binary_closing iterations={carve_close}",
                f"binary_dilation iterations={carve_dilate}",
                f"Z_INFLATE_ITERS={Z_INFLATE_ITERS}",
            ],
            "post_mc": [f"Taubin iterations={TAUBIN_ITERS}"],
            "MASK_DILATE_PX_texture_only": MASK_DILATE_PX,
            "mask_retains_aft_step": mask_still_has_step,
        },
        "mechanism": mechanism,
        "verdict": verdict,
        "ramp_is_artifact": ramp_is_artifact,
        "ascii_chart": ascii_chart,
        "proceed_to_fix": ramp_is_artifact is True,
        "hold_reason_if_any": None
        if ramp_is_artifact is not None
        else "Inconclusive shape comparison — do not fix blind; inspect profiles first.",
    }

    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Text table
    lines = [
        f"BU SAMRA height-profile compare  bins={N_BINS}  LOA={loa}m  visual_depth={visual_depth:.1f}m",
        f"corr(norm mesh, mask)={corr:.3f}  MAE_norm={mae_norm:.3f}",
        f"mask_form={mask_stats['form_class']}  mesh_form={mesh_stats['form_class']}  voxel_form={voxel_stats['form_class']}",
        "",
        f"{'bin':>3} {'x%':>6} {'mesh_z':>8} {'mask_z':>8} {'vox_z':>8} {'m_n':>5} {'s_n':>5}",
    ]
    for i in range(N_BINS):
        lines.append(
            f"{i:3d} {x_frac[i]*100:6.1f} {mesh_z[i]:8.4f} {mask_z[i]:8.2f} "
            f"{voxel_z[i]:8.2f} {mesh_n[i]:5.2f} {mask_n[i]:5.2f}"
        )
    lines += ["", ascii_chart, "", "VERDICT:", verdict, "", "MECHANISM:"]
    lines += [f"- {m}" for m in mechanism]
    OUT_ASCII.write_text("\n".join(lines), encoding="utf-8")
    # Windows consoles may be cp1251 — avoid UnicodeEncodeError
    sys.stdout.buffer.write(("\n".join(lines) + f"\n\nJSON -> {OUT_JSON}\n").encode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
