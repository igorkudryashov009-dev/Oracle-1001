#!/usr/bin/env python3
"""
Equal-metric-pitch occupancy → InstancedMesh cube-grid assets (v3).

v3.0.0: ~100k cells (uniform scale from observed v2.1 BU SAMRA 41826) +
per-vessel k-means palette target 50 (elbow/silhouette in {45,50,55}).
Sampling / content-bbox UV unchanged from v2.1. Does NOT mutate GLB verts.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import numpy as np

from services.top10_3d_mesh import (
    MASK_DILATE_PX,
    ORTHO_TEX_SIZE,
    OUT_GLB_DIR,
    ROOT,
    WEB_GLB_DIR,
    _sample_ortho,
    _sky_water_chroma,
    _to_unit,
    anisotropic_pitch,
    carve_voxels,
    estimate_visual_depth_m,
    extract_silhouette,
    load_ortho_rgb,
    mask_content_uv_bounds,
    prepare_masked_ortho,
    resolve_ortho_paths,
)

ALG_VOXEL_CUBES = "top10-voxel-cubes-v3.0.0-100k-palette50"
PALETTE_K_MIN = 45
PALETTE_K_TARGET = 50
PALETTE_K_MAX = 55
PALETTE_K_CANDIDATES = (45, 50, 55)
COLOR_INDEX_BITS = 8  # uint8 still holds k≤55; do not change bit-width
FACE_SAMPLE_N = 9  # 3×3
FACE_VAR_LAB = 48.0  # mean Lab² deviation → treat as color-boundary face
SAMPLE_CELL_FRAC = 0.35  # 3×3 span as fraction of one cell in UV

# Frozen v1 / v2 pitches (do not re-derive).
V1_METRIC_PITCH = 0.02063256160476761
V1_OCCUPIED_BU_SAMRA = 10_345
TARGET_OCCUPIED_V2 = 50_000
SCALE_FACTOR_V2 = (TARGET_OCCUPIED_V2 / float(V1_OCCUPIED_BU_SAMRA)) ** (1.0 / 3.0)
V2_METRIC_PITCH = V1_METRIC_PITCH / SCALE_FACTOR_V2  # ≈0.01216
V2_OCCUPIED_BU_SAMRA = 41_826  # observed v2.1 BU SAMRA, not the 50k target

# v3: same relative scale on all 10 — do not retarget each vessel to exactly 100k.
TARGET_OCCUPIED_V3 = 100_000
SCALE_FACTOR_V3 = (TARGET_OCCUPIED_V3 / float(V2_OCCUPIED_BU_SAMRA)) ** (1.0 / 3.0)  # ≈1.337
V3_METRIC_PITCH = V2_METRIC_PITCH / SCALE_FACTOR_V3

SCALE_FACTOR = SCALE_FACTOR_V3
CALIBRATED_METRIC_PITCH: float = V3_METRIC_PITCH

OUT_VOXEL_DIR = OUT_GLB_DIR
WEB_VOXEL_DIR = WEB_GLB_DIR


def equal_metric_shape(
    hx: float, hy: float, hz: float, *, pitch: float
) -> tuple[int, int, int]:
    """Cell counts for equal metric pitch along X/Y/Z (cubes look cubic in world)."""
    p = max(float(pitch), 1e-6)
    nx = max(2, int(round(2.0 * float(hx) / p)) + 1)
    ny = max(2, int(round(2.0 * float(hy) / p)) + 1)
    nz = max(2, int(round(2.0 * float(hz) / p)) + 1)
    return nx, ny, nz


def calibrate_pitch_bu_samra(vessel: dict[str, Any], ref_base: Path) -> dict[str, Any]:
    """Verify v3 pitch on BU SAMRA — uniform scale from observed v2.1, not re-fit."""
    from services.top10_vessels import REF_BASE

    ref = ref_base or REF_BASE
    paths = resolve_ortho_paths(vessel, ref)
    side_m = extract_silhouette(paths["side"], size=ORTHO_TEX_SIZE)
    top_m = extract_silhouette(paths["sat"], size=ORTHO_TEX_SIZE)
    bow_m = extract_silhouette(paths["bow"], size=ORTHO_TEX_SIZE)
    depth = estimate_visual_depth_m(
        side_m, float(vessel["loa_m"]), float(vessel["draft_m"]), float(vessel["beam_m"])
    )
    hx, hy, hz = _to_unit(
        float(vessel["loa_m"]),
        float(vessel["beam_m"]),
        float(vessel["draft_m"]),
        visual_depth_m=depth["visual_depth_m"],
    )
    pitch = float(V3_METRIC_PITCH)
    shape = equal_metric_shape(hx, hy, hz, pitch=pitch)
    occupied = carve_voxels(side_m, top_m, bow_m, hx=hx, hy=hy, hz=hz, shape=shape)
    n_occ = int(occupied.sum())
    total = int(np.prod(occupied.shape))
    return {
        "alg": ALG_VOXEL_CUBES,
        "v1_pitch": V1_METRIC_PITCH,
        "scale_factor": SCALE_FACTOR,
        "pitch": pitch,
        "shape": list(shape),
        "hx": hx,
        "hy": hy,
        "hz": hz,
        "n_occupied": n_occ,
        "n_total": total,
        "occupied_frac": n_occ / max(total, 1),
        "target_occupied": TARGET_OCCUPIED_V3,
        "visual_depth_m": depth["visual_depth_m"],
    }


def _rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """rgb float01 → Lab (skimage)."""
    from skimage.color import rgb2lab

    x = np.asarray(rgb, dtype=np.float64)
    if x.ndim == 1:
        x = x.reshape(1, 1, 3)
    elif x.ndim == 2:
        x = x.reshape(-1, 1, 3)
    return rgb2lab(np.clip(x, 0, 1)).reshape(-1, 3)


def _fallback_palette(k: int) -> tuple[np.ndarray, list[str]]:
    k = max(int(k), PALETTE_K_MIN)
    t = np.linspace(0.06, 0.92, k)
    # Maroon keel → hull rust → deck gray → superstructure cream
    r = np.clip(0.12 + 0.75 * t + 0.18 * np.sin(t * 3.1), 0, 1)
    g = np.clip(0.05 + 0.70 * t * t, 0, 1)
    b = np.clip(0.06 + 0.55 * t, 0, 1)
    palette_u8 = (np.stack([r, g, b], axis=1) * 255.0).round().clip(0, 255).astype(np.uint8)
    hexes = [f"#{int(a):02x}{int(c):02x}{int(d):02x}" for a, c, d in palette_u8]
    return palette_u8, hexes


def choose_palette_k(lab: np.ndarray) -> tuple[int, dict[str, Any]]:
    """
    Pick k ∈ {45, 50, 55} via silhouette (target 50).
    Never returns below PALETTE_K_MIN.
    """
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    n = int(len(lab))
    rng = np.random.default_rng(42)
    fit_n = min(n, 8000)
    sil_n = min(n, 2500)
    x = lab[rng.choice(n, size=fit_n, replace=False)] if n > fit_n else lab
    records: list[dict[str, Any]] = []
    best_k = PALETTE_K_MIN
    best_sil = -1.0
    prev_inertia = None
    for k in PALETTE_K_CANDIDATES:
        if k >= len(x):
            continue
        km = KMeans(n_clusters=int(k), n_init=4, random_state=42)
        labels = km.fit_predict(x)
        inertia = float(km.inertia_)
        sil = None
        try:
            si = rng.choice(len(x), size=min(sil_n, len(x)), replace=False)
            sil = float(silhouette_score(x[si], labels[si]))
        except Exception:
            sil = None
        drop = None if prev_inertia is None else float(1.0 - inertia / max(prev_inertia, 1e-9))
        records.append({"k": int(k), "inertia": inertia, "silhouette": sil, "inertia_drop": drop})
        prev_inertia = inertia
        if sil is not None and sil > best_sil:
            best_sil = sil
            best_k = int(k)

    # Flat silhouette → target 50 (not the floor)
    sils = [r["silhouette"] for r in records if r["silhouette"] is not None]
    if sils and (max(sils) - min(sils)) < 0.015:
        best_k = PALETTE_K_TARGET
    return max(int(best_k), PALETTE_K_MIN), {"candidates": records, "chosen": int(best_k)}


def extract_ship_palette(
    side_rgb: np.ndarray,
    bow_rgb: np.ndarray,
    sat_rgb: np.ndarray,
    side_m: np.ndarray,
    bow_m: np.ndarray,
    sat_m: np.ndarray,
    *,
    k: int | None = None,
    max_samples: int = 40_000,
) -> tuple[np.ndarray, list[str], dict[str, Any]]:
    """
    k-means (Lab) on ship-only pixels from 3 ortho views.
    k=None → per-vessel elbow/silhouette in [21, 32].
    Returns (palette_rgb_u8 Kx3, palette_hex, k_report).
    """
    from sklearn.cluster import KMeans

    chunks: list[np.ndarray] = []
    for rgb, mask in (
        (side_rgb, side_m),
        (bow_rgb, bow_m),
        (sat_rgb, sat_m),
    ):
        m = np.asarray(mask, dtype=bool)
        arr = np.asarray(rgb, dtype=np.float32)
        if m.shape[:2] != arr.shape[:2]:
            continue
        pix = arr[m]
        if len(pix) == 0:
            continue
        keep = ~_sky_water_chroma(pix.reshape(-1, 1, 3)).reshape(-1)
        pix = pix[keep] if np.any(keep) else pix
        chunks.append(pix)

    if not chunks:
        pal, hexes = _fallback_palette(k or PALETTE_K_MIN)
        return pal, hexes, {"chosen": int(len(pal)), "fallback": True}

    pixels = np.concatenate(chunks, axis=0)
    if len(pixels) > max_samples:
        rng = np.random.default_rng(42)
        idx = rng.choice(len(pixels), size=max_samples, replace=False)
        pixels = pixels[idx]

    lab = _rgb_to_lab(pixels)
    k_report: dict[str, Any]
    if k is None:
        k_use, k_report = choose_palette_k(lab)
    else:
        k_use = max(int(k), PALETTE_K_MIN)
        k_report = {"chosen": k_use, "forced": True}

    km = KMeans(n_clusters=int(k_use), n_init=8, random_state=42)
    km.fit(lab)
    centers_lab = km.cluster_centers_

    from skimage.color import lab2rgb

    centers_rgb = lab2rgb(centers_lab.reshape(1, -1, 3)).reshape(-1, 3)
    centers_rgb = np.clip(centers_rgb, 0, 1)
    lum = 0.299 * centers_rgb[:, 0] + 0.587 * centers_rgb[:, 1] + 0.114 * centers_rgb[:, 2]
    order = np.argsort(lum)
    centers_rgb = centers_rgb[order]
    palette_u8 = (centers_rgb * 255.0).round().clip(0, 255).astype(np.uint8)
    hexes = [f"#{int(r):02x}{int(g):02x}{int(b):02x}" for r, g, b in palette_u8]
    k_report["k"] = int(len(palette_u8))
    return palette_u8, hexes, k_report


def quantize_rgb_to_palette(colors01: np.ndarray, palette_u8: np.ndarray) -> np.ndarray:
    """Map Nx3 float01 RGB → nearest palette index (Lab Euclidean)."""
    pal01 = palette_u8.astype(np.float64) / 255.0
    lab_c = _rgb_to_lab(colors01)
    lab_p = _rgb_to_lab(pal01)
    d = ((lab_c[:, None, :] - lab_p[None, :, :]) ** 2).sum(axis=2)
    return d.argmin(axis=1).astype(np.uint8)


def _row_mode(idx_2d: np.ndarray, k_max: int) -> np.ndarray:
    """Mode of integer labels along axis=1."""
    n, _m = idx_2d.shape
    counts = np.zeros((n, max(int(k_max), 1)), dtype=np.int16)
    np.add.at(counts, (np.arange(n)[:, None], idx_2d.astype(np.int32)), 1)
    return counts.argmax(axis=1).astype(np.uint8)


def _maroon_keel_color(side_rgb: np.ndarray) -> np.ndarray:
    """Never-Black underside: darkened waterline maroon from Side photo."""
    h, w = side_rgb.shape[:2]
    band = side_rgb[int(h * 0.55) : int(h * 0.92), int(w * 0.15) : int(w * 0.85)]
    if band.size == 0:
        return np.array([0.28, 0.08, 0.08], dtype=np.float32)
    flat = band.reshape(-1, 3)
    keep = ~_sky_water_chroma(flat.reshape(-1, 1, 3)).reshape(-1)
    sample = flat[keep] if np.any(keep) else flat
    med = np.median(sample, axis=0).astype(np.float32)
    redish = sample[(sample[:, 0] > sample[:, 1] + 0.04) & (sample[:, 0] > 0.25)]
    if len(redish) > 20:
        med = np.median(redish, axis=0).astype(np.float32)
    return np.clip(med * 0.42, 0.05, 0.55).astype(np.float32)


def _sample_grid(
    img: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    du: float,
    dv: float,
    bounds: tuple[float, float, float, float] | None = None,
) -> np.ndarray:
    """3×3 bilinear samples around (u,v). Returns (N, 9, 3)."""
    lin = np.linspace(-1.0, 1.0, 3, dtype=np.float64)
    ou, ov = np.meshgrid(lin, lin, indexing="xy")
    uu = u[:, None] + (ou.ravel()[None, :] * float(du))
    vv = v[:, None] + (ov.ravel()[None, :] * float(dv))
    flat = _sample_ortho(img, uu.ravel(), vv.ravel(), bounds=bounds)
    return flat.reshape(len(u), FACE_SAMPLE_N, 3).astype(np.float32)


def _robust_face_colors(
    samples: np.ndarray,
    palette_u8: np.ndarray,
    *,
    donor: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """
    samples (N, 9, 3) → (colors01 N×3, indices N).
    Low Lab variance: Lab medoid (real sample, not a mean hybrid).
    High variance (waterline etc.): mode of per-sample palette indices.
    """
    n = int(samples.shape[0])
    flat = samples.reshape(-1, 3)
    if donor is not None:
        bad = _sky_water_chroma(flat.reshape(-1, 1, 3)).reshape(-1)
        if np.any(bad):
            flat = flat.copy()
            don = np.repeat(donor, FACE_SAMPLE_N, axis=0)
            flat[bad] = don[bad]
            samples = flat.reshape(n, FACE_SAMPLE_N, 3)

    lab = _rgb_to_lab(samples.reshape(-1, 3)).reshape(n, FACE_SAMPLE_N, 3)
    mu = lab.mean(axis=1, keepdims=True)
    var = ((lab - mu) ** 2).sum(axis=2).mean(axis=1)
    d = ((lab - mu) ** 2).sum(axis=2)
    medoid_j = d.argmin(axis=1)
    medoid_rgb = samples[np.arange(n), medoid_j]

    q_all = quantize_rgb_to_palette(samples.reshape(-1, 3), palette_u8).reshape(n, FACE_SAMPLE_N)
    mode_idx = _row_mode(q_all, len(palette_u8))
    pal01 = palette_u8.astype(np.float32) / 255.0
    mode_rgb = pal01[mode_idx]

    hi = var >= FACE_VAR_LAB
    colors = medoid_rgb.copy()
    colors[hi] = mode_rgb[hi]
    idx = quantize_rgb_to_palette(colors, palette_u8)
    idx[hi] = mode_idx[hi]
    return colors.astype(np.float32), idx


def color_occupied_voxels(
    occupied: np.ndarray,
    *,
    hx: float,
    hy: float,
    hz: float,
    side_rgb: np.ndarray,
    bow_rgb: np.ndarray,
    sat_rgb: np.ndarray,
    palette_u8: np.ndarray | None = None,
    sampling: str = "multisample",
    side_bounds: tuple[float, float, float, float] | None = None,
    bow_bounds: tuple[float, float, float, float] | None = None,
    sat_bounds: tuple[float, float, float, float] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Per-occupied-cell color.

    sampling='mean'     — legacy: 1 bilinear point / face, mean of 6 faces.
    sampling='multisample' — 3×3 Lab-medoid per face; mode on high-variance
      faces; cell index = mode of the 6 face indices (side weighted ×3).

    Returns (centers Nx3, colors Nx3 float01, pitch 3, indices N).
    """
    nx, ny, nz = occupied.shape
    pitch = anisotropic_pitch(nx, ny, nz, hx, hy, hz)
    xs = np.linspace(-hx, hx, nx, dtype=np.float64)
    ys = np.linspace(-hy, hy, ny, dtype=np.float64)
    zs = np.linspace(-hz, hz, nz, dtype=np.float64)

    ii, jj, kk = np.where(occupied)
    cx = xs[ii]
    cy = ys[jj]
    cz = zs[kk]
    centers = np.stack([cx, cy, cz], axis=1)

    bx = max(float(hx), 1e-6)
    by = max(float(hy), 1e-6)
    bz = max(float(hz), 1e-6)

    ux = (cx / bx).clip(-1, 1)
    uy = (cy / by).clip(-1, 1)
    uz = (cz / bz).clip(-1, 1)

    keel = _maroon_keel_color(side_rgb)
    c_sat_m = np.broadcast_to(keel, (len(cx), 3)).copy()

    if sampling != "multisample":
        c_side_p = _sample_ortho(side_rgb, ux, uz, bounds=side_bounds)
        c_bow_p = _sample_ortho(bow_rgb, uy, uz, bounds=bow_bounds)
        c_bow_m = _sample_ortho(side_rgb, ux, uz, bounds=side_bounds) * 0.88
        c_sat_p = _sample_ortho(sat_rgb, ux, uy, bounds=sat_bounds)

        def _desky(c: np.ndarray, donor: np.ndarray) -> np.ndarray:
            bad = _sky_water_chroma(c.reshape(-1, 1, 3)).reshape(-1)
            if not np.any(bad):
                return c
            out = c.copy()
            out[bad] = donor[bad]
            return out

        prior = 0.55 * c_side_p + 0.30 * c_bow_p + 0.15 * c_sat_p
        c_side_p = _desky(c_side_p, prior)
        c_bow_p = _desky(c_bow_p, prior)
        c_bow_m = _desky(c_bow_m, prior)
        c_sat_p = _desky(c_sat_p, prior)
        colors = (c_side_p + c_side_p + c_bow_p + c_bow_m + c_sat_p + c_sat_m) / 6.0
        colors = np.clip(colors, 0.0, 1.0).astype(np.float32)
        if palette_u8 is None:
            idx = np.zeros(len(colors), dtype=np.uint8)
            idx_sat = idx
        else:
            idx = quantize_rgb_to_palette(colors, palette_u8)
            colors = (palette_u8.astype(np.float32) / 255.0)[idx]
            idx_sat = quantize_rgb_to_palette(c_sat_p, palette_u8)
        return centers.astype(np.float32), colors, pitch.astype(np.float64), idx, idx_sat

    if palette_u8 is None:
        raise ValueError("multisample coloring requires palette_u8")

    du_x = float(pitch[0] / bx) * SAMPLE_CELL_FRAC
    du_y = float(pitch[1] / by) * SAMPLE_CELL_FRAC
    du_z = float(pitch[2] / bz) * SAMPLE_CELL_FRAC

    s_side = _sample_grid(side_rgb, ux, uz, du_x, du_z, bounds=side_bounds)
    s_bow = _sample_grid(bow_rgb, uy, uz, du_y, du_z, bounds=bow_bounds)
    s_bow_m = _sample_grid(side_rgb, ux, uz, du_x, du_z, bounds=side_bounds) * 0.88
    s_sat = _sample_grid(sat_rgb, ux, uy, du_x, du_y, bounds=sat_bounds)

    prior = 0.55 * s_side[:, 4, :] + 0.30 * s_bow[:, 4, :] + 0.15 * s_sat[:, 4, :]
    c_side, i_side = _robust_face_colors(s_side, palette_u8, donor=prior)
    c_bow, i_bow = _robust_face_colors(s_bow, palette_u8, donor=prior)
    c_bow_m, i_bow_m = _robust_face_colors(s_bow_m, palette_u8, donor=prior)
    c_sat, i_sat = _robust_face_colors(s_sat, palette_u8, donor=prior)
    i_keel = quantize_rgb_to_palette(c_sat_m, palette_u8)

    # Side faces weighted ×3 so hull/waterline dominates over sat/keel noise
    packed = np.stack([i_side, i_side, i_side, i_bow, i_bow_m, i_sat, i_keel], axis=1)
    idx = _row_mode(packed, len(palette_u8))
    colors = (palette_u8.astype(np.float32) / 255.0)[idx]
    return centers.astype(np.float32), colors, pitch.astype(np.float64), idx, i_sat


def uv_to_pixel(
    u: np.ndarray,
    v: np.ndarray,
    *,
    size: int = ORTHO_TEX_SIZE,
    bounds: tuple[float, float, float, float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Same convention as _sample_ortho: world u,v ∈ [-1,1], v=+1 → image top."""
    uu = np.asarray(u, dtype=np.float64)
    vv = np.asarray(v, dtype=np.float64)
    if bounds is not None:
        u0, u1, v0, v1 = bounds
        uu = u0 + (uu + 1.0) * 0.5 * (u1 - u0)
        vv = v0 + (vv + 1.0) * 0.5 * (v1 - v0)
    s = int(size)
    col = ((uu + 1.0) * 0.5 * (s - 1)).clip(0, s - 1)
    row = ((1.0 - vv) * 0.5 * (s - 1)).clip(0, s - 1)
    return col, row


def diagnose_voxel_projection(
    vessel: dict[str, Any],
    *,
    ref_base: Path | None = None,
    pitch: float | None = None,
    n_marks: int = 24,
) -> dict[str, Any]:
    """
    Landmark projection check (Prompt 3): do waterline / superstructure
    voxel centers land in the expected Side-photo color zones?

    Uses the SAME ux=x/hx, uz=z/hz frame as coloring (design half-extents,
    identical to carve). Does not invent a second voxel-only calibration.
    """
    from services.top10_vessels import REF_BASE

    ref = ref_base or REF_BASE
    paths = resolve_ortho_paths(vessel, ref)
    side_m = extract_silhouette(paths["side"], size=ORTHO_TEX_SIZE)
    top_m = extract_silhouette(paths["sat"], size=ORTHO_TEX_SIZE)
    bow_m = extract_silhouette(paths["bow"], size=ORTHO_TEX_SIZE)
    depth = estimate_visual_depth_m(
        side_m, float(vessel["loa_m"]), float(vessel["draft_m"]), float(vessel["beam_m"])
    )
    hx, hy, hz = _to_unit(
        float(vessel["loa_m"]),
        float(vessel["beam_m"]),
        float(vessel["draft_m"]),
        visual_depth_m=depth["visual_depth_m"],
    )
    p = float(pitch if pitch is not None else V3_METRIC_PITCH)
    shape = equal_metric_shape(hx, hy, hz, pitch=p)
    occupied = carve_voxels(side_m, top_m, bow_m, hx=hx, hy=hy, hz=hz, shape=shape)
    side_rgb = load_ortho_rgb(paths["side"], size=ORTHO_TEX_SIZE)
    side_b = mask_content_uv_bounds(side_m)

    nx, ny, nz = occupied.shape
    xs = np.linspace(-hx, hx, nx)
    ys = np.linspace(-hy, hy, ny)
    zs = np.linspace(-hz, hz, nz)
    ii, jj, kk = np.where(occupied)
    ux = (xs[ii] / max(hx, 1e-6)).clip(-1, 1)
    uy = (ys[jj] / max(hy, 1e-6)).clip(-1, 1)
    uz = (zs[kk] / max(hz, 1e-6)).clip(-1, 1)

    rng = np.random.default_rng(0)
    # Waterline band on Side: lower hull, mid-length
    wl = (uz > -0.72) & (uz < -0.22) & (np.abs(ux) < 0.55) & (np.abs(uy) < 0.45)
    # Superstructure / deck-house: high Z, mid X
    ss = (uz > 0.15) & (np.abs(ux) < 0.35)

    def _zone(mask: np.ndarray, expect: str) -> dict[str, Any]:
        sel = np.where(mask)[0]
        if len(sel) == 0:
            return {"n": 0, "expect": expect}
        take = sel if len(sel) <= n_marks else sel[rng.choice(len(sel), size=n_marks, replace=False)]
        col, row = uv_to_pixel(ux[take], uz[take], bounds=side_b)
        rgb = _sample_ortho(side_rgb, ux[take], uz[take], bounds=side_b)
        red_ratio = float(np.mean((rgb[:, 0] > rgb[:, 1] + 0.06) & (rgb[:, 0] > 0.28)))
        lum = float(np.mean(0.299 * rgb[:, 0] + 0.587 * rgb[:, 1] + 0.114 * rgb[:, 2]))
        return {
            "n": int(len(sel)),
            "sampled": int(len(take)),
            "expect": expect,
            "mean_rgb": [float(x) for x in rgb.mean(axis=0)],
            "red_ratio": red_ratio,
            "luminance": lum,
            "pixels": [[float(c), float(r)] for c, r in zip(col, row)],
        }

    waterline = _zone(wl, "maroon/red waterline on Side")
    superstruct = _zone(ss, "light deckhouse / white-cream on Side")

    # Systematic shift test: waterline pixels should sit in the lower-mid image
    # (row ~ 0.55–0.85 of height). If mean row is way off, UV is biased.
    rows = [p[1] for p in waterline.get("pixels") or []]
    mean_row_frac = float(np.mean(rows) / max(ORTHO_TEX_SIZE - 1, 1)) if rows else None
    # Expected waterline on a side profile: roughly 60–85% down the frame
    shift = None
    verdict = "no_systematic_shift"
    if mean_row_frac is not None:
        if mean_row_frac < 0.45:
            shift = "up"  # projected too high on photo
            verdict = "possible_upward_bias"
        elif mean_row_frac > 0.92:
            shift = "down"
            verdict = "possible_downward_bias"
        else:
            verdict = "aligned"

    # Color sanity: waterline should be redder/darker than superstructure
    color_ok = True
    if waterline.get("sampled") and superstruct.get("sampled"):
        color_ok = waterline["red_ratio"] >= 0.25 or waterline["luminance"] < superstruct["luminance"]

    return {
        "imo": str(vessel.get("imo")),
        "alg": ALG_VOXEL_CUBES,
        "frame": "design_half_extents + mask_content_uv_bounds (same as carve)",
        "mesh_projection_frame_used": False,
        "note": (
            "Voxel coloring now remaps world UV through mask_content_uv_bounds — the "
            "same window carve_voxels already used. mesh_projection_frame() stays "
            "mesh-AABB-only; _sample_ortho(bounds=) is the shared sample primitive."
        ),
        "waterline": waterline,
        "superstructure": superstruct,
        "mean_waterline_row_frac": mean_row_frac,
        "shift": shift,
        "verdict": verdict,
        "color_zones_consistent": color_ok,
        "calibration_patch_needed": verdict not in ("aligned", "no_systematic_shift") and not color_ok,
    }


def build_voxel_cube_asset(
    vessel: dict[str, Any],
    *,
    ref_base: Path | None = None,
    pitch: float | None = None,
    palette_override: list[list[int]] | None = None,
    sampling: str = "multisample",
    k: int | None = None,
) -> dict[str, Any]:
    from services.top10_vessels import REF_BASE

    ref = ref_base or REF_BASE
    imo = str(vessel["imo"])
    paths = resolve_ortho_paths(vessel, ref)
    side_m = extract_silhouette(paths["side"], size=ORTHO_TEX_SIZE)
    top_m = extract_silhouette(paths["sat"], size=ORTHO_TEX_SIZE)
    bow_m = extract_silhouette(paths["bow"], size=ORTHO_TEX_SIZE)

    depth = estimate_visual_depth_m(
        side_m, float(vessel["loa_m"]), float(vessel["draft_m"]), float(vessel["beam_m"])
    )
    hx, hy, hz = _to_unit(
        float(vessel["loa_m"]),
        float(vessel["beam_m"]),
        float(vessel["draft_m"]),
        visual_depth_m=depth["visual_depth_m"],
    )

    p = float(pitch if pitch is not None else V3_METRIC_PITCH)
    shape = equal_metric_shape(hx, hy, hz, pitch=p)
    occupied = carve_voxels(side_m, top_m, bow_m, hx=hx, hy=hy, hz=hz, shape=shape)

    side_rgb = prepare_masked_ortho(
        load_ortho_rgb(paths["side"], size=ORTHO_TEX_SIZE), side_m, dilate_px=MASK_DILATE_PX
    )
    bow_rgb = prepare_masked_ortho(
        load_ortho_rgb(paths["bow"], size=ORTHO_TEX_SIZE), bow_m, dilate_px=MASK_DILATE_PX
    )
    sat_rgb = prepare_masked_ortho(
        load_ortho_rgb(paths["sat"], size=ORTHO_TEX_SIZE), top_m, dilate_px=MASK_DILATE_PX
    )
    side_b = mask_content_uv_bounds(side_m)
    bow_b = mask_content_uv_bounds(bow_m)
    sat_b = mask_content_uv_bounds(top_m)

    k_report: dict[str, Any]
    if palette_override is not None:
        palette_u8 = np.asarray(palette_override, dtype=np.uint8).reshape(-1, 3)
        if len(palette_u8) < PALETTE_K_MIN:
            extra, _ = _fallback_palette(PALETTE_K_MIN)
            palette_u8 = np.vstack([palette_u8, extra[: PALETTE_K_MIN - len(palette_u8)]])
        hexes = [f"#{int(r):02x}{int(g):02x}{int(b):02x}" for r, g, b in palette_u8]
        k_report = {"chosen": int(len(palette_u8)), "forced": True}
    else:
        palette_u8, hexes, k_report = extract_ship_palette(
            side_rgb, bow_rgb, sat_rgb, side_m, bow_m, top_m, k=k
        )

    centers, colors_raw, pitch_xyz, indices, sat_indices = color_occupied_voxels(
        occupied,
        hx=hx,
        hy=hy,
        hz=hz,
        side_rgb=side_rgb,
        bow_rgb=bow_rgb,
        sat_rgb=sat_rgb,
        palette_u8=palette_u8,
        sampling=sampling,
        side_bounds=side_b,
        bow_bounds=bow_b,
        sat_bounds=sat_b,
    )

    if len(centers):
        mid = 0.5 * (centers.min(axis=0) + centers.max(axis=0))
        centers = centers - mid.astype(np.float32)

    n = int(len(centers))
    pos_b64 = base64.b64encode(centers.astype("<f4").tobytes()).decode("ascii")
    # uint8 index: 8 bits ≥ 5-bit requirement (k≤32). Not 3-bit packed.
    idx_b64 = base64.b64encode(indices.astype(np.uint8).tobytes()).decode("ascii")
    sat_b64 = base64.b64encode(np.asarray(sat_indices, dtype=np.uint8).tobytes()).decode("ascii")

    cube_size = float(min(pitch_xyz)) * 0.98
    payload = {
        "alg": ALG_VOXEL_CUBES,
        "imo": imo,
        "rank": vessel.get("rank"),
        "name": vessel.get("name"),
        "n_occupied": n,
        "shape": list(shape),
        "half_extents": [float(hx), float(hy), float(hz)],
        "pitch": [float(x) for x in pitch_xyz],
        "metric_pitch": p,
        "scale_factor_from_v1": SCALE_FACTOR,
        "cube_size": cube_size,
        "occupied_frac": float(n / max(int(np.prod(shape)), 1)),
        "positions_f32_b64": pos_b64,
        "palette_rgb_u8": palette_u8.tolist(),
        "palette_hex": hexes,
        "color_indices_u8_b64": idx_b64,
        "sat_indices_u8_b64": sat_b64,
        "palette_k": int(len(palette_u8)),
        "palette_k_report": k_report,
        "color_encoding": "palette_index_u8",
        "color_index_bits": COLOR_INDEX_BITS,
        "sampling": sampling,
        "carving": "top10-glb-v3.3.0-content-bbox",
        "projection": (
            f"v3.4.1-content-bbox-uv-face-{sampling}+lab-kmeans{int(len(palette_u8))}"
        ),
        "bytes_payload": len(pos_b64) + len(idx_b64),
    }
    return payload


def write_voxel_cube_asset(
    vessel: dict[str, Any],
    *,
    pitch: float | None = None,
    palette_override: list[list[int]] | None = None,
    sampling: str = "multisample",
    k: int | None = None,
) -> Path:
    payload = build_voxel_cube_asset(
        vessel, pitch=pitch, palette_override=palette_override, sampling=sampling, k=k
    )
    imo = str(vessel["imo"])
    name = f"vessel_{imo}_voxels.json"
    OUT_VOXEL_DIR.mkdir(parents=True, exist_ok=True)
    WEB_VOXEL_DIR.mkdir(parents=True, exist_ok=True)
    out_p = OUT_VOXEL_DIR / name
    web_p = WEB_VOXEL_DIR / name
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    out_p.write_text(text, encoding="utf-8")
    web_p.write_text(text, encoding="utf-8")
    meta = {
        "alg": ALG_VOXEL_CUBES,
        "imo": imo,
        "n_occupied": payload["n_occupied"],
        "shape": payload["shape"],
        "metric_pitch": payload["metric_pitch"],
        "scale_factor_from_v1": SCALE_FACTOR,
        "palette_hex": payload["palette_hex"],
        "palette_rgb_u8": payload["palette_rgb_u8"],
        "palette_k": payload["palette_k"],
        "sampling": payload["sampling"],
        "color_index_bits": COLOR_INDEX_BITS,
        "bytes": out_p.stat().st_size,
        "url": f"/output/assets/3d_models/{name}",
        "color_encoding": "palette_index_u8",
    }
    meta_text = json.dumps(meta, indent=2)
    out_p.with_suffix(".meta.json").write_text(meta_text, encoding="utf-8")
    web_p.with_suffix(".meta.json").write_text(meta_text, encoding="utf-8")
    return out_p
