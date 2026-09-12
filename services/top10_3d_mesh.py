"""
TOP-10 photogrammetric hull → binary GLB (Oracle-1001 / Sentinel).

Algorithm (Prompt graphics):
  1. Silhouette masks from Side (XZ), Top (XY), Bow (YZ) orthographics
  2. Multi-planar voxel carving (CSG intersection of three extrusions)
  3. Marching-cubes mesh + PHOTO-COMPOSITE triplanar albedo bake
  4. Export JPEG-embedded .glb (geometry unchanged; atlas carries digital-twin look)

Source images (canonical repo layout):
  assets/7000/{rank}-1.jpg  side
  assets/7000/{rank}-2.jpg  bow
  assets/7000/{rank}-3.jpg  top / sat
Optional aliases also accepted:
  {IMO}_side.jpg / {IMO}_bow.jpg / {IMO}_sat.jpg
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import numpy as np
from PIL import Image, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parents[1]
# Bump invalidates skip-fresh for ALL vessels (source_hash includes ALG_VERSION).
# v3.4.0 = geometry∥presentation decouple on top of v3.3.0 content-bbox carving.
ALG_VERSION = "top10-glb-v3.4.1-triplanar-bounds"
MAX_GLB_BYTES = int(2.0 * 1024 * 1024)  # photo albedo may exceed prior 1.5 MB envelope
DEFAULT_VOXEL = 80  # higher MC resolution → fewer silhouette stair-steps
DEFAULT_TARGET_FACES = 6500
ATLAS_SIZE = 2048  # higher bake res — budget ~2MB still has headroom at ~0.6MB/vessel
ORTHO_TEX_SIZE = 1024  # per-projection sample resolution (= shared silhouette mask size)
TRIPLANAR_POWER = 4.0  # higher → sharper projection dominance, still soft seams
MASK_DILATE_PX = 10  # sky-bleed guard after flood-fill + chroma FG purge
TAUBIN_ITERS = 3  # volume-preserving facet soften; raw drift then design-extent refit
TAUBIN_LAMB = 0.32
TAUBIN_NU = -0.33
MIN_MESH_FACES = 64
MIN_ASPECT = 0.045  # min_extent / max_extent — reject paper-thin collapses
BBOX_DRIFT_MAX = 0.02  # 2% raw Taubin drift before design-extent refit

# Naval proportion band — visual air-draft from Side silhouette (not bare draft).
# Draft alone (L/D≈37) is underwater only; Side photo includes freeboard + tanks/bridge.
LB_RATIO_MIN, LB_RATIO_MAX = 5.5, 8.5
BD_RATIO_TARGET = 1.55  # beam / visual_depth — Side ortho tanks/bridge silhouette
BD_RATIO_MIN, BD_RATIO_MAX = 1.20, 2.40
FREEBOARD_FACTOR = 2.5  # draft floor when photo estimate unavailable
TARGET_LD_MAX = 10.0  # soft ceiling for design envelope (was 8.5 presentation hack)
# Content-bbox UV mapping fills design Z/Y from silhouette; anisotropic inflate
# previously masked empty-margin collapse and is now OFF by default.
Z_INFLATE_ITERS = 0
# Post-MC bbox stretch masked ribbon occupancy for 6 cycles — keep as opt-in safety only.
FIT_MESH_TO_EXTENTS = False
MASK_CONTENT_PAD_FRAC = 0.03  # pad tight silhouette bbox before UV map

WEB_GLB_DIR = ROOT / "web" / "assets" / "3d_models"
OUT_GLB_DIR = ROOT / "output" / "assets" / "3d_models"

# ── Coordinate system contract (naval mesh frame) ────────────────────────────
# Single source of truth for carving AND the production viewer:
#   +X = LOA (bow → stern direction along length; bow toward +X)
#   +Y = Beam (port → starboard)
#   +Z = height / air-draft (keel → deck), Z-up
# Viewer MUST frame via camera.up=(0,0,1) + camera.position/lookAt only —
# never mesh.rotation / anisotropic mesh.scale / vertex mutation.


@dataclass(frozen=True)
class RawMesh:
    """Immutable MC output. Presentation must never write into ``vertices``."""

    vertices: np.ndarray
    faces: np.ndarray
    hx: float
    hy: float
    hz: float
    geom_report: Mapping[str, Any] = field(default_factory=dict)
    masks_meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        verts = np.ascontiguousarray(self.vertices, dtype=np.float64).copy()
        faces = np.ascontiguousarray(self.faces, dtype=np.int64).copy()
        verts.setflags(write=False)
        faces.setflags(write=False)
        object.__setattr__(self, "vertices", verts)
        object.__setattr__(self, "faces", faces)
        object.__setattr__(self, "geom_report", dict(self.geom_report))
        object.__setattr__(self, "masks_meta", dict(self.masks_meta))


@dataclass(frozen=True)
class PresentedMesh:
    """Textured export payload. Geometry positions come from a *copy* of RawMesh."""

    vertices: np.ndarray
    faces: np.ndarray
    uvs: np.ndarray
    atlas: Image.Image
    hx: float
    hy: float
    hz: float
    material: Mapping[str, float] = field(
        default_factory=lambda: {
            "roughness": 0.72,
            "metalness": 0.05,
            "envMapIntensity": 0.55,
        }
    )
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "vertices", np.ascontiguousarray(self.vertices, dtype=np.float64).copy()
        )
        object.__setattr__(
            self, "faces", np.ascontiguousarray(self.faces, dtype=np.int64).copy()
        )
        object.__setattr__(
            self, "uvs", np.ascontiguousarray(self.uvs, dtype=np.float64).copy()
        )
        object.__setattr__(self, "material", dict(self.material))
        object.__setattr__(self, "meta", dict(self.meta))


def hash_vertex_positions(raw: RawMesh | np.ndarray) -> str:
    """SHA-256 of contiguous float64 vertex buffer (immutability / live QA)."""
    if isinstance(raw, RawMesh):
        buf = np.ascontiguousarray(raw.vertices, dtype=np.float64)
    else:
        buf = np.ascontiguousarray(raw, dtype=np.float64)
    return hashlib.sha256(buf.tobytes()).hexdigest()


def assert_naval_axis_contract(extents: np.ndarray, *, where: str = "mesh") -> None:
    """
    Executable axis contract after carving/MC:
      extent_X (LOA) > extent_Y (Beam) > extent_Z (height).
    """
    e = np.asarray(extents, dtype=np.float64).reshape(3)
    lx, by, dz = float(e[0]), float(e[1]), float(e[2])
    if not (lx > by > dz > 0):
        raise AssertionError(
            f"naval axis contract violated at {where}: "
            f"need LOA({lx:.4f}) > Beam({by:.4f}) > height({dz:.4f})"
        )


def mask_bbox_xyxy(mask: np.ndarray) -> tuple[int, int, int, int]:
    """Tight integer bbox (x0,y0,x1,y1) of True pixels; empty → full frame."""
    ys, xs = np.where(mask)
    if len(xs) == 0:
        h, w = mask.shape
        return 0, 0, w, h
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def estimate_visual_depth_m(
    side_mask: np.ndarray,
    loa_m: float,
    draft_m: float,
    beam_m: float,
) -> dict[str, float]:
    """
    Hull/air-draft height from Side silhouette bbox scaled by LOA.

    Registry has draft only (no moulded depth). Side bbox height/LOA is the photo
    truth for visual height, clamped to a naval band so mast/reflection outliers
    cannot inflate the voxel slab.
    """
    loa = max(float(loa_m), 1.0)
    draft = max(float(draft_m), 0.5)
    beam = max(float(beam_m), 0.5)
    x0, y0, x1, y1 = mask_bbox_xyxy(side_mask)
    w_px = max(x1 - x0, 1)
    h_px = max(y1 - y0, 1)
    raw = h_px * (loa / w_px)
    lo = draft * FREEBOARD_FACTOR
    hi = min(loa / 7.0, beam * 1.15)  # ~L/D≥7, B/D≳0.87
    clamped = float(np.clip(raw, lo, hi))
    return {
        "raw_side_height_m": float(raw),
        "visual_depth_m": clamped,
        "clamp_lo_m": float(lo),
        "clamp_hi_m": float(hi),
    }


def _to_unit(
    loa_m: float,
    beam_m: float,
    draft_m: float,
    *,
    visual_depth_m: float | None = None,
) -> tuple[float, float, float]:
    """
    Map registry LOA / Beam / visual depth (metres) → half-extents.

    Axes: X=LOA, Y=Beam, Z=visual air-draft (keel → tanks / bridge).
    Prefer photo-estimated `visual_depth_m` over draft×factor hacks.
    """
    loa = max(float(loa_m) or 345.0, 1.0)
    beam = max(float(beam_m) or 53.8, 0.5)
    draft = max(float(draft_m) or 9.4, 0.5)

    # Clamp L/B into LNG acceptance band
    lb = loa / beam
    if lb < LB_RATIO_MIN:
        beam = loa / LB_RATIO_MIN
    elif lb > LB_RATIO_MAX:
        beam = loa / LB_RATIO_MAX

    if visual_depth_m is not None and float(visual_depth_m) > 0:
        depth = float(visual_depth_m)
    else:
        depth = max(draft * FREEBOARD_FACTOR, beam / BD_RATIO_TARGET)

    bd = beam / max(depth, 1e-6)
    if bd < BD_RATIO_MIN:
        depth = beam / BD_RATIO_MIN
    elif bd > BD_RATIO_MAX:
        depth = beam / BD_RATIO_MAX

    if loa / max(depth, 1e-6) > TARGET_LD_MAX:
        depth = loa / TARGET_LD_MAX

    hx = 1.0
    hy = beam / loa
    hz = depth / loa
    m = max(hx, hy, hz, 1e-6)
    return hx / m, hy / m, hz / m


def mask_content_uv_bounds(
    mask: np.ndarray, *, pad_frac: float = MASK_CONTENT_PAD_FRAC
) -> tuple[float, float, float, float]:
    """
    Content-aware UV window for a silhouette mask.

    Returns (u0, u1, v0, v1) in the same convention as carve sampling:
      u=-1..+1 → image left→right; v=+1..-1 → image top→bottom.
    World extents are mapped into THIS window (not the full square frame), so
    empty sky/water margins no longer collapse the visual-hull intersection.
    """
    h, w = mask.shape
    x0, y0, x1, y1 = mask_bbox_xyxy(mask)
    pad_x = float(pad_frac) * w
    pad_y = float(pad_frac) * h
    x0 = max(0.0, x0 - pad_x)
    y0 = max(0.0, y0 - pad_y)
    x1 = min(float(w), x1 + pad_x)
    y1 = min(float(h), y1 + pad_y)
    # Avoid degenerate windows
    if x1 - x0 < 2:
        x0, x1 = 0.0, float(w)
    if y1 - y0 < 2:
        y0, y1 = 0.0, float(h)
    u0 = (x0 / max(w - 1, 1)) * 2.0 - 1.0
    u1 = ((x1 - 1.0) / max(w - 1, 1)) * 2.0 - 1.0
    v_top = 1.0 - (y0 / max(h - 1, 1)) * 2.0
    v_bot = 1.0 - ((y1 - 1.0) / max(h - 1, 1)) * 2.0
    # v0 = lower (keel-ish), v1 = upper (deck-ish) in world-like sense
    return float(u0), float(u1), float(min(v_bot, v_top)), float(max(v_bot, v_top))


def anisotropic_pitch(
    nx: int, ny: int, nz: int, hx: float, hy: float, hz: float
) -> np.ndarray:
    """
    Explicit per-axis voxel pitch [px, py, pz].
    NEVER average axes — isotropic mean pitch flattens beam/draft into a pancake.
    """
    return np.array(
        [
            (2.0 * hx) / max(int(nx) - 1, 1),
            (2.0 * hy) / max(int(ny) - 1, 1),
            (2.0 * hz) / max(int(nz) - 1, 1),
        ],
        dtype=np.float64,
    )


def mesh_projection_frame(
    vertices: np.ndarray,
) -> tuple[np.ndarray, float, float, float]:
    """
    Content-bbox projection frame for triplanar / chart UVs.

    Returns (center_xyz, hx, hy, hz) from the ACTUAL mesh AABB — never from
    design half-extents alone. After sanitize ``rezero`` the mesh lives in the
    positive octant; dividing by design hx then clips most verts to u=+1 and
    the atlas collapses to edge/median beige (Prompt-2 visual FAIL).
    """
    v = np.ascontiguousarray(vertices, dtype=np.float64)
    if v.ndim != 2 or v.shape[0] == 0:
        return np.zeros(3, dtype=np.float64), 1.0, 1.0, 1.0
    vmin = v.min(axis=0)
    vmax = v.max(axis=0)
    center = 0.5 * (vmin + vmax)
    half = 0.5 * np.maximum(vmax - vmin, 1e-9)
    return center, float(half[0]), float(half[1]), float(half[2])


def _center_mesh_at_origin(mesh):
    """Translate mesh so AABB center → origin (naval frame). Not anisotropic fit."""
    bounds = np.asarray(mesh.bounds, dtype=np.float64)
    center = 0.5 * (bounds[0] + bounds[1])
    mesh.apply_translation(-center)
    return mesh


def naval_proportion_report(extents: np.ndarray) -> dict[str, float]:
    """L/B and B/D from mesh (or design) full extents [Lx, By, Dz]."""
    lx, by, dz = (float(extents[0]), float(extents[1]), float(extents[2]))
    return {
        "loa_extent": lx,
        "beam_extent": by,
        "depth_extent": dz,
        "lb_ratio": lx / max(by, 1e-9),
        "bd_ratio": by / max(dz, 1e-9),
    }


def resolve_ortho_paths(vessel: dict[str, Any], ref_base: Path) -> dict[str, Path]:
    """Prefer rank-indexed Desktop/repo orthographics; fall back to IMO-named files."""
    rank = int(vessel["rank"])
    imo = str(vessel["imo"])
    ranked = {
        "side": ref_base / f"{rank}-1.jpg",
        "bow": ref_base / f"{rank}-2.jpg",
        "sat": ref_base / f"{rank}-3.jpg",
    }
    imo_named = {
        "side": ref_base / f"{imo}_side.jpg",
        "bow": ref_base / f"{imo}_bow.jpg",
        "sat": ref_base / f"{imo}_sat.jpg",
    }
    out: dict[str, Path] = {}
    for key in ("side", "bow", "sat"):
        if ranked[key].exists():
            out[key] = ranked[key]
        elif imo_named[key].exists():
            out[key] = imo_named[key]
        else:
            out[key] = ranked[key]
    return out


def _sky_water_chroma(arr: np.ndarray) -> np.ndarray:
    """True where RGB looks like open sky / sea (must not stay in FG for texturing)."""
    r = arr[..., 0]
    g = arr[..., 1]
    b = arr[..., 2]
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    # Pale / mid sky: blue-dominant, not too dark
    sky = (b > r + 0.06) & (b > g + 0.02) & (b > 0.42) & (gray > 0.32)
    # Open water: blue-green dominant, mid-dark
    water = (b > r + 0.04) & (b >= g - 0.02) & (gray < 0.55) & (gray > 0.12) & (b > 0.22)
    # Near-white haze / overcast sky
    haze = (gray > 0.78) & (np.abs(b - r) < 0.08) & (np.abs(b - g) < 0.08)
    return sky | water | haze


def extract_silhouette(path: Path, size: int = ORTHO_TEX_SIZE) -> np.ndarray:
    """
    Binary mask True=foreground (ship). Same mask drives carving AND texture prep.

    Background = border-connected flood of corner-similar pixels, PLUS chroma purge of
    residual sky/water that survived as false FG (root cause of atlas sky bleed).
    """
    img = Image.open(path).convert("RGB")
    img = ImageOps.exif_transpose(img)
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    gray = 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]

    blur = np.asarray(
        Image.fromarray((gray * 255).astype(np.uint8)).filter(ImageFilter.MedianFilter(3)),
        dtype=np.float32,
    ) / 255.0
    corners = np.concatenate(
        [
            blur[:8, :8].ravel(),
            blur[:8, -8:].ravel(),
            blur[-8:, :8].ravel(),
            blur[-8:, -8:].ravel(),
        ]
    )
    bg_val = float(np.median(corners))

    from scipy import ndimage

    # Pixels similar to corner background (sky / sea / studio)
    is_bg = np.abs(blur - bg_val) < 0.145
    is_bg |= _sky_water_chroma(arr)

    labeled, n = ndimage.label(is_bg)
    border = np.zeros_like(is_bg, dtype=bool)
    border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
    bg_mask = np.zeros_like(is_bg, dtype=bool)
    for i in range(1, n + 1):
        comp = labeled == i
        if np.any(comp & border):
            bg_mask |= comp

    mask = ~bg_mask
    # Chroma purge inside putative FG (sky pockets trapped in hull silhouette)
    mask &= ~_sky_water_chroma(arr)
    mask = ndimage.binary_opening(mask, iterations=1)
    mask = ndimage.binary_closing(mask, iterations=3)
    mask = ndimage.binary_fill_holes(mask)

    labeled, n = ndimage.label(mask)
    if n > 0:
        counts = ndimage.sum(mask, labeled, index=range(1, n + 1))
        keep = int(np.argmax(counts)) + 1
        mask = labeled == keep

    # Reject pathological masks (almost full frame = sky misclassified as ship)
    frac = float(mask.mean()) if mask.size else 0.0
    if frac > 0.72 or frac < 0.02 or not mask.any():
        yy, xx = np.ogrid[:size, :size]
        cy, cx = size / 2.0, size / 2.0
        mask = ((xx - cx) / (size * 0.42)) ** 2 + ((yy - cy) / (size * 0.22)) ** 2 <= 1.0
    return mask.astype(bool)


def prepare_masked_ortho(
    rgb: np.ndarray,
    mask: np.ndarray,
    *,
    dilate_px: int = MASK_DILATE_PX,
) -> np.ndarray:
    """
    Mask-aware texture prep (sky-bleed fix):
      1) Align mask to RGB resolution (must be the SAME carve silhouette, resized).
      2) Dilate ship mask by dilate_px.
      3) Push-pull / nearest-neighbour inpaint: every non-ship pixel (incl. dilated ring
         and full background) takes the colour of the nearest ship pixel.
    Triplanar samples of UV edges then hit hull paint, never raw sky/water.
    """
    from scipy import ndimage

    rgb = np.asarray(rgb, dtype=np.float32)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("rgb must be HxWx3")
    h, w = rgb.shape[:2]
    m = np.asarray(mask, dtype=bool)
    if m.shape != (h, w):
        m_img = Image.fromarray((m.astype(np.uint8) * 255))
        m_img = m_img.resize((w, h), Image.Resampling.NEAREST)
        m = np.asarray(m_img) > 127

    if not m.any():
        return rgb.copy()

    # Dilate for a safe band past the silhouette (bilinear UV overshoot)
    iters = max(0, int(dilate_px))
    dilated = ndimage.binary_dilation(m, iterations=iters) if iters else m

    # Seed colours: ship pixels that are NOT sky/water chroma
    seed = m & ~_sky_water_chroma(rgb)
    if not seed.any():
        seed = m

    # Nearest seed-pixel for every non-seed cell
    _dist, (ri, ci) = ndimage.distance_transform_edt(~seed, return_indices=True)
    out = rgb.copy()
    # Full background + residual sky-in-mask → hull paint (Never-Black / Never-Sky)
    replace = (~seed) | (~m)
    out[replace] = rgb[ri[replace], ci[replace]]
    # Dilated ring already covered; keep dilated for meta/debug consumers
    _ = dilated
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def carve_voxels(
    side: np.ndarray,
    top: np.ndarray,
    bow: np.ndarray,
    *,
    hx: float,
    hy: float,
    hz: float,
    res: int = DEFAULT_VOXEL,
    shape: tuple[int, int, int] | None = None,
) -> np.ndarray:
    """
    Multi-planar extrusion intersection inside axis-aligned extents.
    Grid axes: i→X (bow→stern), j→Y (port→starboard), k→Z (keel→deck).
    Side mask: rows=Z (top→bottom in image ≈ deck→keel), cols=X
    Top mask:  rows=Y, cols=X
    Bow mask:  rows=Z, cols=Y

    CRITICAL: each mask is sampled via its content bbox UV window, not the full
    square frame. Full-frame mapping left Side height at ~24% of design Z and
    produced ribbon occupancy (midship z_span_frac≈0.21).

    ``shape=(nx,ny,nz)`` overrides isotropic ``res`` — used by equal-metric-pitch
    cube-grid export without changing the production MC path (still ``res=80``).
    """
    if shape is not None:
        nx, ny, nz = (int(shape[0]), int(shape[1]), int(shape[2]))
    else:
        nx = ny = nz = int(res)
    nx, ny, nz = max(nx, 2), max(ny, 2), max(nz, 2)
    xs = np.linspace(-hx, hx, nx, dtype=np.float32)
    ys = np.linspace(-hy, hy, ny, dtype=np.float32)
    zs = np.linspace(-hz, hz, nz, dtype=np.float32)

    def sample_mask(
        mask: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        bounds: tuple[float, float, float, float],
    ) -> np.ndarray:
        """u,v in [-1,1] world → content window → mask[row,col]; v=+1 image top."""
        h, w = mask.shape
        u0, u1, v0, v1 = bounds
        u_img = u0 + (u + 1.0) * 0.5 * (u1 - u0)
        v_img = v0 + (v + 1.0) * 0.5 * (v1 - v0)
        col = ((u_img + 1.0) * 0.5 * (w - 1)).clip(0, w - 1).astype(np.int32)
        row = ((1.0 - v_img) * 0.5 * (h - 1)).clip(0, h - 1).astype(np.int32)
        return mask[row, col]

    side_b = mask_content_uv_bounds(side)
    top_b = mask_content_uv_bounds(top)
    bow_b = mask_content_uv_bounds(bow)

    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    ux = (X / max(hx, 1e-6)).clip(-1, 1)
    uy = (Y / max(hy, 1e-6)).clip(-1, 1)
    uz = (Z / max(hz, 1e-6)).clip(-1, 1)

    m_side = sample_mask(side, ux.ravel(), uz.ravel(), side_b).reshape(X.shape)
    m_top = sample_mask(top, ux.ravel(), uy.ravel(), top_b).reshape(X.shape)
    m_bow = sample_mask(bow, uy.ravel(), uz.ravel(), bow_b).reshape(X.shape)

    occupied = m_side & m_top & m_bow
    # Bow photo often clips Moss tanks / bridge — keep side∩top volume in the
    # upper air-draft band so Z extrusion is not hollowed into a flat slab.
    upper = uz >= 0.05
    occupied_upper = m_side & m_top
    occupied = np.where(upper, occupied | occupied_upper, occupied)

    from scipy import ndimage

    occupied = ndimage.binary_closing(occupied, iterations=2)
    occupied = ndimage.binary_dilation(occupied, iterations=1)
    # Should be unnecessary after content-bbox UV fix; kept as safety net (default 0).
    if Z_INFLATE_ITERS > 0:
        struct_z = np.zeros((1, 1, 3), dtype=bool)
        struct_z[0, 0, :] = True
        occupied = ndimage.binary_dilation(
            occupied, structure=struct_z, iterations=int(Z_INFLATE_ITERS)
        )
    if occupied.sum() < 64:
        # Degenerate photos → ellipsoid fallback scaled to extents (full volume)
        occupied = (ux ** 2 + (uy / 0.85) ** 2 + (uz / 0.75) ** 2) <= 1.0
    return occupied


def _hull_fallback_mesh(hx: float, hy: float, hz: float):
    """Watertight elongated hull when voxel carve collapses.

    Built at exact design extents — does NOT call ``_fit_mesh_to_extents``
    (that helper is opt-in safety only; generate_geometry must never stretch).
    """
    import trimesh

    # Capsule along LOA (X) with beam/draft cross-section — volumetric, not flat
    try:
        r = float(min(hy, hz) * 0.92)
        height = float(max(2.0 * hx - 2.0 * r, hx))
        mesh = trimesh.creation.capsule(radius=r, height=height, count=[16, 16])
        # Capsule is Z-up; rotate to X-forward ship frame
        T = trimesh.transformations.rotation_matrix(np.pi / 2.0, [0, 1, 0])
        mesh.apply_transform(T)
        # Uniform per-axis scale from current bbox → design extents (analytic
        # construction, not post-hoc ribbon masking of a bad carve).
        bounds = np.asarray(mesh.bounds, dtype=np.float64)
        spans = np.maximum(bounds[1] - bounds[0], 1e-9)
        targets = np.array([2.0 * hx, 2.0 * hy, 2.0 * hz], dtype=np.float64)
        scale = targets / spans
        mesh.apply_scale(scale)
        mesh.rezero()
        mesh.apply_translation(-np.asarray(mesh.bounds).mean(axis=0))
    except Exception:
        mesh = trimesh.creation.box(extents=[2 * hx, 2 * hy, 2 * hz])
    return mesh


def _fit_mesh_to_extents(mesh, hx: float, hy: float, hz: float):
    """Uniform-axis remap bbox → exact [-hx,hx]×[-hy,hy]×[-hz,hz]."""
    import trimesh

    if mesh is None or len(mesh.vertices) == 0:
        return trimesh.creation.box(extents=[2 * hx, 2 * hy, 2 * hz])
    bounds = np.asarray(mesh.bounds, dtype=np.float64)
    mins = bounds[0]
    spans = np.maximum(bounds[1] - bounds[0], 1e-9)
    targets = np.array([2.0 * hx, 2.0 * hy, 2.0 * hz], dtype=np.float64)
    scale = targets / spans
    verts = (np.asarray(mesh.vertices, dtype=np.float64) - mins) * scale
    verts -= targets * 0.5  # center at origin
    mesh = trimesh.Trimesh(vertices=verts, faces=np.asarray(mesh.faces), process=False)
    return mesh


def _mesh_is_degenerate(mesh, hx: float, hy: float, hz: float) -> bool:
    if mesh is None or len(getattr(mesh, "faces", [])) < MIN_MESH_FACES:
        return True
    if len(getattr(mesh, "vertices", [])) < 8:
        return True
    extents = np.asarray(mesh.extents, dtype=np.float64)
    if not np.isfinite(extents).all() or (extents <= 1e-6).any():
        return True
    aspect = float(extents.min() / max(extents.max(), 1e-9))
    if aspect < MIN_ASPECT:
        return True
    # Volume must be a non-trivial fraction of the AABB (reject crumpled sheets)
    try:
        vol = float(abs(mesh.volume))
        aabb = float(np.prod(np.maximum(extents, 1e-9)))
        if aabb > 0 and vol / aabb < 0.02:
            return True
    except Exception:
        return True
    # Expected aspect vs design extents (catch isotropic-pitch squash)
    expect = np.array([2.0 * hx, 2.0 * hy, 2.0 * hz], dtype=np.float64)
    ratio = extents / np.maximum(expect, 1e-9)
    if float(ratio.max() / max(ratio.min(), 1e-9)) > 8.0:
        return True
    # Naval band: collapsed L/B or flat B/D → capsule fallback
    props = naval_proportion_report(extents)
    if props["lb_ratio"] < 3.0 or props["lb_ratio"] > 14.0:
        return True
    # Presentation air-draft B/D ≈ 1.45–2.4; reject moulded-depth pancakes (>4.5)
    # and absurd towers (<1.1)
    if props["bd_ratio"] < 1.1 or props["bd_ratio"] > 4.8:
        return True
    if props["loa_extent"] / max(props["depth_extent"], 1e-9) > (TARGET_LD_MAX + 2.5):
        return True
    return False


def sanitize_hull_mesh(mesh, hx: float, hy: float, hz: float, *, report: dict | None = None):
    """
    Force watertight-ish topology, outward normals, and volumetric thickness.
    Taubin smoothing (volume-preserving) removes MC stair-steps.
    Falls back to analytic capsule if carve/MC produced a crumpled sheet.
    Optional `report` collects bbox extents before/after Taubin for drift audit.
    """
    import trimesh

    if mesh is None or len(getattr(mesh, "vertices", [])) == 0:
        return _hull_fallback_mesh(hx, hy, hz)

    try:
        mesh.merge_vertices()
    except Exception:
        pass
    try:
        mesh.update_faces(mesh.nondegenerate_faces())
    except Exception:
        pass
    try:
        mesh.remove_unreferenced_vertices()
    except Exception:
        pass

    # Remap to design extents — OFF by default after content-bbox fix (masks ribbon).
    if FIT_MESH_TO_EXTENTS:
        mesh = _fit_mesh_to_extents(mesh, hx, hy, hz)

    try:
        trimesh.repair.fix_normals(mesh)
    except Exception:
        try:
            mesh.fix_normals()
        except Exception:
            pass
    try:
        # Keep AABB centered at origin (naval frame). ``rezero`` pushed the mesh
        # into the positive octant and broke triplanar (design ÷ hx → u=+1 clip).
        _center_mesh_at_origin(mesh)
        if FIT_MESH_TO_EXTENTS:
            mesh = _fit_mesh_to_extents(mesh, hx, hy, hz)
    except Exception:
        pass

    before = np.asarray(mesh.extents, dtype=np.float64).copy()

    # Volume-preserving Taubin — stronger pass for v3.1 facet reduction
    try:
        trimesh.smoothing.filter_taubin(
            mesh, lamb=TAUBIN_LAMB, nu=TAUBIN_NU, iterations=TAUBIN_ITERS
        )
    except Exception:
        pass

    # Drift measured BEFORE optional re-fit
    after_raw = np.asarray(mesh.extents, dtype=np.float64).copy()
    if report is not None:
        drift = np.abs(after_raw - before) / np.maximum(before, 1e-9)
        report["bbox_before"] = before.tolist()
        report["bbox_after_taubin_raw"] = after_raw.tolist()
        report["bbox_drift_pct"] = (drift * 100.0).tolist()
        report["bbox_drift_ok"] = bool(float(drift.max()) <= BBOX_DRIFT_MAX)
        report["fit_mesh_to_extents"] = bool(FIT_MESH_TO_EXTENTS)

    if FIT_MESH_TO_EXTENTS:
        mesh = _fit_mesh_to_extents(mesh, hx, hy, hz)
    else:
        try:
            _center_mesh_at_origin(mesh)
        except Exception:
            pass
    if report is not None:
        report["bbox_after_refit"] = np.asarray(mesh.extents, dtype=np.float64).tolist()

    if _mesh_is_degenerate(mesh, hx, hy, hz):
        return _hull_fallback_mesh(hx, hy, hz)

    # Explicit smooth (averaged) vertex normals — not raw face normals
    try:
        trimesh.repair.fix_normals(mesh)
        _ = mesh.vertex_normals  # force recompute / cache
    except Exception:
        pass
    return mesh


def voxels_to_mesh(occupied: np.ndarray, hx: float, hy: float, hz: float, *, report: dict | None = None):
    import trimesh
    from trimesh.voxel.ops import matrix_to_marching_cubes

    matrix = occupied.astype(bool)
    nx, ny, nz = matrix.shape
    pitch = anisotropic_pitch(nx, ny, nz, hx, hy, hz)
    try:
        mesh = matrix_to_marching_cubes(matrix=matrix, pitch=pitch)
        # Origin of MC grid is voxel corner (0,0,0); shift into centered ship frame
        mesh.apply_translation([-hx, -hy, -hz])
    except Exception:
        mesh = _hull_fallback_mesh(hx, hy, hz)

    if mesh is None or len(getattr(mesh, "vertices", [])) == 0:
        mesh = _hull_fallback_mesh(hx, hy, hz)

    target_faces = DEFAULT_TARGET_FACES
    if len(mesh.faces) > target_faces:
        try:
            mesh = mesh.simplify_quadric_decimation(face_count=target_faces)
        except Exception:
            try:
                from fast_simplification import simplify

                tr = max(0.05, min(0.95, 1.0 - target_faces / max(len(mesh.faces), 1)))
                verts, faces = simplify(
                    np.asarray(mesh.vertices, dtype=np.float64),
                    np.asarray(mesh.faces, dtype=np.int64),
                    target_reduction=tr,
                )
                mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
            except Exception:
                pass

    # Mandatory post-MC sanitize before any GLB serialization
    return sanitize_hull_mesh(mesh, hx, hy, hz, report=report)


def load_ortho_rgb(path: Path, size: int = ORTHO_TEX_SIZE) -> np.ndarray:
    """Load orthographic photo as float RGB HxWx3 in [0,1], EXIF-corrected."""
    im = Image.open(path).convert("RGB")
    im = ImageOps.exif_transpose(im)
    im = im.resize((size, size), Image.Resampling.LANCZOS)
    return np.asarray(im, dtype=np.float32) / 255.0


def _sample_ortho(
    img: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    bounds: tuple[float, float, float, float] | None = None,
) -> np.ndarray:
    """
    Bilinear sample. u,v in [-1,1] world; v=+1 → image top (carve convention).

    Optional ``bounds=(u0,u1,v0,v1)`` is the silhouette content window from
    ``mask_content_uv_bounds`` — same remap carve uses. Without it, [-1,1]
    covers the full square frame (legacy mesh path / inpainted textures).
    Returns (..., 3) RGB.
    """
    h, w = img.shape[:2]
    u = np.asarray(u, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    if bounds is not None:
        u0, u1, v0, v1 = bounds
        u = u0 + (u + 1.0) * 0.5 * (u1 - u0)
        v = v0 + (v + 1.0) * 0.5 * (v1 - v0)
    # col: -1→0, +1→w-1 ; row: +1→0 (top), -1→h-1 (bottom)
    col_f = ((u + 1.0) * 0.5 * (w - 1)).clip(0, w - 1.001)
    row_f = ((1.0 - v) * 0.5 * (h - 1)).clip(0, h - 1.001)
    c0 = np.floor(col_f).astype(np.int32)
    r0 = np.floor(row_f).astype(np.int32)
    c1 = np.minimum(c0 + 1, w - 1)
    r1 = np.minimum(r0 + 1, h - 1)
    tc = (col_f - c0).astype(np.float32)[..., None]
    tr = (row_f - r0).astype(np.float32)[..., None]
    ia = img[r0, c0]
    ib = img[r0, c1]
    ic = img[r1, c0]
    id_ = img[r1, c1]
    top = ia * (1.0 - tc) + ib * tc
    bot = ic * (1.0 - tc) + id_ * tc
    return top * (1.0 - tr) + bot * tr


def triplanar_colors(
    positions: np.ndarray,
    normals: np.ndarray,
    side: np.ndarray,
    bow: np.ndarray,
    sat: np.ndarray,
    hx: float,
    hy: float,
    hz: float,
    *,
    power: float = TRIPLANAR_POWER,
) -> np.ndarray:
    """
    Soft triplanar blend matching carve axes:
      Side (LATERAL): XZ ← weight |ny|
      Bow  (BOW):     YZ ← weight |nx|
      Sat  (TOP):     XY ← weight |nz|
    Stern (−X) and keel (−Z) get muted nearest-projection fills (Never-Black texture).
    """
    pos = np.asarray(positions, dtype=np.float64)
    nrm = np.asarray(normals, dtype=np.float64)
    if pos.ndim == 1:
        pos = pos.reshape(1, 3)
        nrm = nrm.reshape(1, 3)
    hx = max(float(hx), 1e-6)
    hy = max(float(hy), 1e-6)
    hz = max(float(hz), 1e-6)

    ux = (pos[:, 0] / hx).clip(-1.0, 1.0)
    uy = (pos[:, 1] / hy).clip(-1.0, 1.0)
    uz = (pos[:, 2] / hz).clip(-1.0, 1.0)

    c_side = _sample_ortho(side, ux, uz)
    c_bow = _sample_ortho(bow, uy, uz)
    c_sat = _sample_ortho(sat, ux, uy)

    # Sample-time sky/water kill (mesh UV may land on residual halo despite prep)
    def _desky(c: np.ndarray, donor: np.ndarray) -> np.ndarray:
        bad = _sky_water_chroma(c.reshape(-1, 1, 3)).reshape(-1)
        if not np.any(bad):
            return c
        out = c.copy()
        out[bad] = donor[bad]
        return out

    hull_prior = 0.55 * c_side + 0.30 * c_bow + 0.15 * c_sat
    c_side = _desky(c_side, hull_prior)
    c_bow = _desky(c_bow, hull_prior)
    c_sat = _desky(c_sat, hull_prior)
    # Second pass: if prior itself was sky-tainted, fall back to side median hull
    side_med = np.median(c_side, axis=0).astype(np.float32)
    for c in (c_side, c_bow, c_sat):
        bad = _sky_water_chroma(c.reshape(-1, 1, 3)).reshape(-1)
        if np.any(bad):
            c[bad] = side_med

    an = np.abs(nrm)
    # Avoid zero normals
    an = np.maximum(an, 1e-6)
    w = np.power(an, float(power))
    w = w / np.maximum(w.sum(axis=1, keepdims=True), 1e-9)

    # Stern-facing (−X): bow photo shows the bow, not the stern — transfer weight to side
    stern = nrm[:, 0] < -0.25
    if np.any(stern):
        transfer = w[stern, 0] * 0.85
        w[stern, 1] = w[stern, 1] + transfer
        w[stern, 0] = w[stern, 0] * 0.15
        # Muted stand-in for residual bow weight (Never-Black)
        c_bow = c_bow.copy()
        c_bow[stern] = c_side[stern] * 0.82

    # Keel / underside (−Z): deck sat is wrong — muted hull from side/bow
    keel = nrm[:, 2] < -0.20
    if np.any(keel):
        hull = 0.65 * c_side[keel] + 0.35 * c_bow[keel]
        hull = hull * 0.55  # darker below waterline
        transfer = w[keel, 2] * 0.75
        w[keel, 1] = w[keel, 1] + transfer * 0.7
        w[keel, 0] = w[keel, 0] + transfer * 0.3
        w[keel, 2] = w[keel, 2] * 0.25
        c_sat = c_sat.copy()
        c_sat[keel] = hull

    w = w / np.maximum(w.sum(axis=1, keepdims=True), 1e-9)
    # w columns: [nx→bow, ny→side, nz→sat]
    out = w[:, 0:1] * c_bow + w[:, 1:2] * c_side + w[:, 2:3] * c_sat
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def _pack_face_island_uvs(n_faces: int, atlas_px: int, margin: float = 0.012) -> np.ndarray:
    """
    Unique UV island per face (3 corners) in [0,1]² — avoids shared-vertex bake conflicts.
    Returns (n_faces, 3, 2).
    """
    cols = max(1, int(np.ceil(np.sqrt(n_faces * 1.15))))
    rows = max(1, int(np.ceil(n_faces / cols)))
    cell_u = (1.0 - 2 * margin) / cols
    cell_v = (1.0 - 2 * margin) / rows
    # Leave a thin gutter inside each cell
    pad_u = cell_u * 0.08
    pad_v = cell_v * 0.08
    usable_u = cell_u - 2 * pad_u
    usable_v = cell_v - 2 * pad_v

    uvs = np.zeros((n_faces, 3, 2), dtype=np.float64)
    for fi in range(n_faces):
        cx = fi % cols
        cy = fi // cols
        x0 = margin + cx * cell_u + pad_u
        y0 = margin + cy * cell_v + pad_v
        # Right triangle covering most of the cell
        uvs[fi, 0] = (x0, y0)
        uvs[fi, 1] = (x0 + usable_u, y0)
        uvs[fi, 2] = (x0 + 0.5 * usable_u, y0 + usable_v)
    return uvs


def _rasterize_face_triplanar(
    atlas: np.ndarray,
    weight: np.ndarray,
    uv_tri: np.ndarray,
    pos_tri: np.ndarray,
    nrm_face: np.ndarray,
    side: np.ndarray,
    bow: np.ndarray,
    sat: np.ndarray,
    hx: float,
    hy: float,
    hz: float,
) -> None:
    """Barycentric rasterize one face into atlas with triplanar sampling."""
    h, w = atlas.shape[:2]
    # UV → pixel
    pts = uv_tri.copy()
    pts[:, 0] *= w - 1
    pts[:, 1] *= h - 1
    # Flip V for image row (UV v=0 at bottom in glTF → row near h)
    rows_y = (h - 1) - pts[:, 1]
    cols_x = pts[:, 0]

    min_x = int(np.floor(cols_x.min()))
    max_x = int(np.ceil(cols_x.max()))
    min_y = int(np.floor(rows_y.min()))
    max_y = int(np.ceil(rows_y.max()))
    min_x = max(0, min_x)
    max_x = min(w - 1, max_x)
    min_y = max(0, min_y)
    max_y = min(h - 1, max_y)
    if min_x > max_x or min_y > max_y:
        return

    # Edge function barycentric in pixel space
    ax, ay = cols_x[0], rows_y[0]
    bx, by = cols_x[1], rows_y[1]
    cx, cy = cols_x[2], rows_y[2]
    denom = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(denom) < 1e-9:
        return

    ys = np.arange(min_y, max_y + 1)
    xs = np.arange(min_x, max_x + 1)
    XX, YY = np.meshgrid(xs, ys)
    wa = ((by - cy) * (XX - cx) + (cx - bx) * (YY - cy)) / denom
    wb = ((cy - ay) * (XX - cx) + (ax - cx) * (YY - cy)) / denom
    wc = 1.0 - wa - wb
    mask = (wa >= -0.01) & (wb >= -0.01) & (wc >= -0.01)
    if not np.any(mask):
        return

    # Clamp barycentrics for stable interp
    wa_m = np.clip(wa[mask], 0, 1)
    wb_m = np.clip(wb[mask], 0, 1)
    wc_m = np.clip(wc[mask], 0, 1)
    s = wa_m + wb_m + wc_m
    s = np.maximum(s, 1e-9)
    wa_m, wb_m, wc_m = wa_m / s, wb_m / s, wc_m / s

    pos = (
        wa_m[:, None] * pos_tri[0]
        + wb_m[:, None] * pos_tri[1]
        + wc_m[:, None] * pos_tri[2]
    )
    # Constant face normal (smooth enough at MC density); slight tilt from verts optional
    nrm = np.repeat(nrm_face.reshape(1, 3), pos.shape[0], axis=0)
    cols = triplanar_colors(pos, nrm, side, bow, sat, hx, hy, hz)

    yy = YY[mask]
    xx = XX[mask]
    atlas[yy, xx] += cols
    weight[yy, xx] += 1.0


def bake_triplanar_albedo(
    mesh,
    side_rgb: np.ndarray,
    bow_rgb: np.ndarray,
    sat_rgb: np.ndarray,
    hx: float,
    hy: float,
    hz: float,
    *,
    atlas_size: int = ATLAS_SIZE,
) -> tuple[Image.Image, np.ndarray, Any]:
    """
    Bake soft triplanar photo-composite into a JPEG-ready atlas.
    `*_rgb` MUST be mask-prepared (prepare_masked_ortho) — never raw JPEG.
    Projection frame is derived from the ACTUAL mesh AABB (content-bbox analog),
    not from design hx/hy/hz alone — design extents diverge after carve+Taubin.
    Returns (atlas_RGB, vertex_uvs, mesh).
    """
    import trimesh

    side = np.asarray(side_rgb, dtype=np.float32)
    bow = np.asarray(bow_rgb, dtype=np.float32)
    sat = np.asarray(sat_rgb, dtype=np.float32)

    try:
        trimesh.repair.fix_normals(mesh)
    except Exception:
        pass

    faces = np.asarray(mesh.faces, dtype=np.int64)
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    try:
        vnormals = np.asarray(mesh.vertex_normals, dtype=np.float64)
    except Exception:
        vnormals = np.zeros_like(verts)
        vnormals[:, 1] = 1.0

    # Content-bbox projection: center + actual half-extents (ignore stale design h*)
    _center, phx, phy, phz = mesh_projection_frame(verts)
    verts_local = verts - _center
    # Prefer actual frame; design h* only as sanity floor if mesh collapsed
    bx = max(phx, float(hx) * 0.25, 1e-6)
    by = max(phy, float(hy) * 0.25, 1e-6)
    bz = max(phz, float(hz) * 0.25, 1e-6)

    uvs = _chart_uvs_for_bake_local(verts_local, vnormals, bx, by, bz)
    vcols = triplanar_colors(verts_local, vnormals, side, bow, sat, bx, by, bz)

    atlas = np.zeros((atlas_size, atlas_size, 3), dtype=np.float32)
    weight = np.zeros((atlas_size, atlas_size), dtype=np.float32)
    h = w = atlas_size

    for tri in faces:
        uv_tri = uvs[tri]
        col_tri = vcols[tri]
        pts = uv_tri.copy()
        pts[:, 0] *= w - 1
        rows_y = (h - 1) - pts[:, 1] * (h - 1)
        cols_x = pts[:, 0]

        min_x = max(0, int(np.floor(cols_x.min())))
        max_x = min(w - 1, int(np.ceil(cols_x.max())))
        min_y = max(0, int(np.floor(rows_y.min())))
        max_y = min(h - 1, int(np.ceil(rows_y.max())))
        if min_x > max_x or min_y > max_y:
            continue

        # Skip absurdly large spans (degenerate UV) — stamp centroid only
        if (max_x - min_x) * (max_y - min_y) > 12000:
            cx = int(np.clip(cols_x.mean(), 0, w - 1))
            cy = int(np.clip(rows_y.mean(), 0, h - 1))
            atlas[cy, cx] += col_tri.mean(axis=0)
            weight[cy, cx] += 1.0
            continue

        ax, ay = cols_x[0], rows_y[0]
        bx, by = cols_x[1], rows_y[1]
        cx_, cy_ = cols_x[2], rows_y[2]
        denom = (by - cy_) * (ax - cx_) + (cx_ - bx) * (ay - cy_)
        if abs(denom) < 1e-9:
            continue

        ys = np.arange(min_y, max_y + 1)
        xs = np.arange(min_x, max_x + 1)
        XX, YY = np.meshgrid(xs, ys)
        wa = ((by - cy_) * (XX - cx_) + (cx_ - bx) * (YY - cy_)) / denom
        wb = ((cy_ - ay) * (XX - cx_) + (ax - cx_) * (YY - cy_)) / denom
        wc = 1.0 - wa - wb
        mask = (wa >= -0.02) & (wb >= -0.02) & (wc >= -0.02)
        if not np.any(mask):
            continue
        wa_m = np.clip(wa[mask], 0, 1)
        wb_m = np.clip(wb[mask], 0, 1)
        wc_m = np.clip(wc[mask], 0, 1)
        s = np.maximum(wa_m + wb_m + wc_m, 1e-9)
        wa_m, wb_m, wc_m = wa_m / s, wb_m / s, wc_m / s
        cols = (
            wa_m[:, None] * col_tri[0]
            + wb_m[:, None] * col_tri[1]
            + wc_m[:, None] * col_tri[2]
        )
        yy = YY[mask]
        xx = XX[mask]
        atlas[yy, xx] += cols
        weight[yy, xx] += 1.0

    painted = weight > 0
    if np.any(painted):
        atlas[painted] /= weight[painted][:, None]
        # Robust hull fill — exclude residual sky/water texels from average
        painted_rgb = atlas[painted]
        keep = ~_sky_water_chroma(painted_rgb.reshape(-1, 1, 3)).reshape(-1)
        if np.any(keep):
            avg = np.median(painted_rgb[keep], axis=0).astype(np.float32)
        else:
            avg = np.median(painted_rgb, axis=0).astype(np.float32)
        # Re-stamp sky-like painted texels with hull median (second-pass bleed kill)
        skyish = _sky_water_chroma(atlas).reshape(h, w)
        fix = painted & skyish
        if np.any(fix):
            atlas[fix] = avg
    else:
        avg = np.array([0.45, 0.42, 0.38], dtype=np.float32)
    if np.any(~painted):
        atlas[~painted] = avg
    try:
        from scipy import ndimage

        for c in range(3):
            ch = atlas[..., c]
            sm = ndimage.gaussian_filter(ch, sigma=0.85)
            edge = (weight > 0) & (ndimage.binary_dilation(weight == 0, iterations=2))
            ch[edge] = 0.45 * ch[edge] + 0.55 * sm[edge]
            atlas[..., c] = ch
    except Exception:
        pass

    img = Image.fromarray((np.clip(atlas, 0, 1) * 255.0).astype(np.uint8), mode="RGB")
    return img, uvs, mesh


def _chart_uvs_for_bake_local(
    verts_local: np.ndarray,
    vn: np.ndarray,
    hx: float,
    hy: float,
    hz: float,
) -> np.ndarray:
    """Atlas chart UVs from origin-centered verts + projection half-extents."""
    v = np.asarray(verts_local, dtype=np.float64)
    vn = np.asarray(vn, dtype=np.float64)
    hx, hy, hz = max(hx, 1e-6), max(hy, 1e-6), max(hz, 1e-6)
    uvs = np.zeros((len(v), 2), dtype=np.float64)
    absn = np.abs(vn)
    dominant = absn.argmax(axis=1)
    for i in range(len(v)):
        ax = int(dominant[i])
        x, y, z = v[i]
        if ax == 1:  # ±Y → side
            u = 0.5 * ((x / hx) * 0.5 + 0.5)
            vv = 1.0 - ((z / hz) * 0.5 + 0.5)
            uvs[i] = (float(np.clip(u, 0.01, 0.49)), float(np.clip(vv, 0.01, 0.99)))
        elif ax == 2:  # ±Z → sat
            u = 0.5 + 0.5 * ((x / hx) * 0.5 + 0.5)
            vv = 0.5 * (1.0 - ((y / hy) * 0.5 + 0.5))
            uvs[i] = (float(np.clip(u, 0.51, 0.99)), float(np.clip(vv, 0.01, 0.49)))
        else:  # ±X → bow
            u = 0.5 + 0.5 * ((y / hy) * 0.5 + 0.5)
            vv = 0.5 + 0.5 * (1.0 - ((z / hz) * 0.5 + 0.5))
            uvs[i] = (float(np.clip(u, 0.51, 0.99)), float(np.clip(vv, 0.51, 0.99)))
    return uvs


def _chart_uvs_for_bake(mesh, hx: float, hy: float, hz: float) -> np.ndarray:
    """Atlas chart UVs: left=side, TR=bow, BR=sat — uses actual mesh projection frame."""
    v = np.asarray(mesh.vertices, dtype=np.float64)
    try:
        vn = np.asarray(mesh.vertex_normals, dtype=np.float64)
    except Exception:
        vn = np.zeros_like(v)
        vn[:, 1] = 1.0
    center, phx, phy, phz = mesh_projection_frame(v)
    bx = max(phx, float(hx) * 0.25, 1e-6)
    by = max(phy, float(hy) * 0.25, 1e-6)
    bz = max(phz, float(hz) * 0.25, 1e-6)
    return _chart_uvs_for_bake_local(v - center, vn, bx, by, bz)


# Back-compat aliases (tests / callers expecting old names)
def build_atlas(side_path: Path, bow_path: Path, sat_path: Path, size: int = ATLAS_SIZE) -> Image.Image:
    """Deprecated pack layout — retained for imports; prefer bake_triplanar_albedo."""
    atlas = Image.new("RGB", (size, size), (40, 44, 52))
    half = size // 2

    def load_fit(p: Path, box: tuple[int, int, int, int]) -> None:
        im = Image.open(p).convert("RGB")
        im = ImageOps.exif_transpose(im)
        w, h = box[2] - box[0], box[3] - box[1]
        im = ImageOps.contain(im, (w, h), Image.Resampling.LANCZOS)
        ox = box[0] + (w - im.width) // 2
        oy = box[1] + (h - im.height) // 2
        atlas.paste(im, (ox, oy))

    load_fit(side_path, (0, 0, half, size))
    load_fit(bow_path, (half, 0, size, half))
    load_fit(sat_path, (half, half, size, size))
    return atlas


def assign_planar_uvs(mesh, hx: float, hy: float, hz: float) -> np.ndarray:
    """Deprecated hard-cutoff UVs — kept for API compatibility."""
    v = mesh.vertices
    uvs = np.zeros((len(v), 2), dtype=np.float64)
    try:
        vn = mesh.vertex_normals
    except Exception:
        vn = np.tile([0.0, 1.0, 0.0], (len(v), 1))
    absn = np.abs(vn)
    dominant = absn.argmax(axis=1)
    for i in range(len(v)):
        ax = int(dominant[i])
        x, y, z = v[i]
        if ax == 1:
            u = 0.5 * ((x / max(hx, 1e-6)) * 0.5 + 0.5)
            vv = 1.0 - ((z / max(hz, 1e-6)) * 0.5 + 0.5)
            uvs[i] = (np.clip(u, 0.01, 0.49), np.clip(vv, 0.01, 0.99))
        elif ax == 2:
            u = 0.5 + 0.5 * ((x / max(hx, 1e-6)) * 0.5 + 0.5)
            vv = 0.5 * (1.0 - ((y / max(hy, 1e-6)) * 0.5 + 0.5))
            uvs[i] = (np.clip(u, 0.51, 0.99), np.clip(vv, 0.01, 0.49))
        else:
            u = 0.5 + 0.5 * ((y / max(hy, 1e-6)) * 0.5 + 0.5)
            vv = 0.5 + 0.5 * (1.0 - ((z / max(hz, 1e-6)) * 0.5 + 0.5))
            uvs[i] = (np.clip(u, 0.51, 0.99), np.clip(vv, 0.51, 0.99))
    return uvs


def source_hash(paths: dict[str, Path], vessel: dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(ALG_VERSION.encode())
    for key in ("side", "bow", "sat"):
        p = paths[key]
        h.update(key.encode())
        if p.exists():
            h.update(p.read_bytes())
        else:
            h.update(b"MISSING")
    meta = f"{vessel.get('imo')}|{vessel.get('loa_m')}|{vessel.get('beam_m')}|{vessel.get('draft_m')}"
    h.update(meta.encode())
    return h.hexdigest()[:16]


def glb_paths(imo: str) -> tuple[Path, Path]:
    name = f"vessel_{imo}.glb"
    return WEB_GLB_DIR / name, OUT_GLB_DIR / name


def needs_rebuild(vessel: dict[str, Any], paths: dict[str, Path]) -> bool:
    web, out = glb_paths(str(vessel["imo"]))
    meta = web.with_suffix(".meta.json")
    if not web.exists() or not out.exists():
        return True
    if web.stat().st_size > MAX_GLB_BYTES or web.stat().st_size < 500:
        return True
    digest = source_hash(paths, vessel)
    if meta.exists():
        try:
            prev = json.loads(meta.read_text(encoding="utf-8"))
            if prev.get("source_hash") == digest and prev.get("alg") == ALG_VERSION:
                return False
        except (OSError, json.JSONDecodeError):
            pass
    return True



def generate_geometry(
    silhouette_masks: dict[str, np.ndarray],
    *,
    loa_m: float,
    beam_m: float,
    draft_m: float,
    voxel_res: int = DEFAULT_VOXEL,
) -> RawMesh:
    """
    Geometry stage ONLY: content-bbox carve → CSG ∩ → marching cubes → RawMesh.

    Forbidden inside this function / its call graph for the happy path:
      - anisotropic inflate (Z_INFLATE_ITERS must stay 0)
      - ``_fit_mesh_to_extents`` (FIT_MESH_TO_EXTENTS must stay False)
      - presentation scale / axis remap / heightBoost
    Returns MC(+sanitize topology) vertices as-is under the naval axis contract.
    """
    if int(Z_INFLATE_ITERS) != 0:
        raise RuntimeError(
            "generate_geometry invariant: Z_INFLATE_ITERS must be 0 "
            f"(got {Z_INFLATE_ITERS})"
        )
    if bool(FIT_MESH_TO_EXTENTS):
        raise RuntimeError(
            "generate_geometry invariant: FIT_MESH_TO_EXTENTS must be False"
        )

    side_m = np.asarray(silhouette_masks["side"], dtype=bool)
    top_m = np.asarray(silhouette_masks["sat"], dtype=bool)
    bow_m = np.asarray(silhouette_masks["bow"], dtype=bool)

    depth_est = estimate_visual_depth_m(side_m, loa_m, draft_m, beam_m)
    hx, hy, hz = _to_unit(
        loa_m, beam_m, draft_m, visual_depth_m=depth_est["visual_depth_m"]
    )
    geom_report: dict[str, Any] = {
        "visual_depth": depth_est,
        "stage": "generate_geometry",
        "alg_carving": "top10-glb-v3.3.0-content-bbox",
        "z_inflate_iters": int(Z_INFLATE_ITERS),
        "fit_mesh_to_extents": bool(FIT_MESH_TO_EXTENTS),
    }

    occupied = carve_voxels(side_m, top_m, bow_m, hx=hx, hy=hy, hz=hz, res=voxel_res)
    mesh = voxels_to_mesh(occupied, hx, hy, hz, report=geom_report)

    extents = np.asarray(mesh.extents, dtype=np.float64)
    assert_naval_axis_contract(extents, where="generate_geometry")
    geom_report["mesh_extents"] = extents.tolist()
    geom_report["mesh_proportions"] = naval_proportion_report(extents)

    return RawMesh(
        vertices=np.asarray(mesh.vertices, dtype=np.float64),
        faces=np.asarray(mesh.faces, dtype=np.int64),
        hx=float(hx),
        hy=float(hy),
        hz=float(hz),
        geom_report=geom_report,
        masks_meta={
            "side_fg_frac": float(side_m.mean()),
            "sat_fg_frac": float(top_m.mean()),
            "bow_fg_frac": float(bow_m.mean()),
        },
    )


def apply_presentation(
    raw_mesh: RawMesh,
    source_photos: dict[str, Any],
    *,
    atlas_size: int = ATLAS_SIZE,
) -> PresentedMesh:
    """
    Presentation stage ONLY: mask-aware triplanar albedo + material metadata.

    Contract: ``raw_mesh.vertices`` is never written. Work happens on a deep copy.
    Lighting / camera live in the viewer — not here.
    """
    import trimesh

    before = hash_vertex_positions(raw_mesh)

    # Deep copy — never alias RawMesh buffers
    verts = np.array(raw_mesh.vertices, dtype=np.float64, copy=True)
    faces = np.array(raw_mesh.faces, dtype=np.int64, copy=True)
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)

    side_rgb = np.asarray(source_photos["side"], dtype=np.float32)
    bow_rgb = np.asarray(source_photos["bow"], dtype=np.float32)
    sat_rgb = np.asarray(source_photos["sat"], dtype=np.float32)

    atlas_img, uvs, mesh = bake_triplanar_albedo(
        mesh,
        side_rgb,
        bow_rgb,
        sat_rgb,
        raw_mesh.hx,
        raw_mesh.hy,
        raw_mesh.hz,
        atlas_size=atlas_size,
    )

    after = hash_vertex_positions(raw_mesh)
    if before != after:
        raise RuntimeError(
            "apply_presentation mutated RawMesh vertex buffer "
            f"(hash {before[:12]}… → {after[:12]}…)"
        )

    presented_verts = np.asarray(mesh.vertices, dtype=np.float64)
    if presented_verts.shape != raw_mesh.vertices.shape:
        raise RuntimeError(
            "apply_presentation changed vertex count "
            f"{raw_mesh.vertices.shape} → {presented_verts.shape}"
        )

    return PresentedMesh(
        vertices=presented_verts,
        faces=np.asarray(mesh.faces, dtype=np.int64),
        uvs=np.asarray(uvs, dtype=np.float64),
        atlas=atlas_img,
        hx=raw_mesh.hx,
        hy=raw_mesh.hy,
        hz=raw_mesh.hz,
        material={
            "roughness": 0.72,
            "metalness": 0.05,
            "envMapIntensity": 0.55,
        },
        meta={
            "texturing": "photo_composite_triplanar_mask_aware",
            "mask_dilate_px": MASK_DILATE_PX,
            "raw_vertex_hash": before,
        },
    )


def _export_presented_glb(presented: PresentedMesh) -> tuple[bytes, int, int]:
    """Serialize PresentedMesh → GLB bytes under MAX_GLB_BYTES (atlas shrink ladder)."""
    import trimesh
    from trimesh.visual.texture import TextureVisuals

    mesh = trimesh.Trimesh(
        vertices=np.array(presented.vertices, copy=True),
        faces=np.array(presented.faces, copy=True),
        process=False,
    )
    uvs = np.asarray(presented.uvs, dtype=np.float64)
    atlas_img = presented.atlas
    rough = float(presented.material.get("roughness", 0.72))
    metal = float(presented.material.get("metalness", 0.05))

    export_bytes: Optional[bytes] = None
    used_atlas_px = atlas_img.size[0]
    used_quality = 72
    for quality, atlas_px in (
        (70, ATLAS_SIZE),
        (66, 1536),
        (62, 1280),
        (58, 1024),
        (52, 768),
        (46, 640),
    ):
        a = (
            atlas_img.resize((atlas_px, atlas_px), Image.Resampling.LANCZOS)
            if atlas_px != atlas_img.size[0]
            else atlas_img
        )
        buf = io.BytesIO()
        a.save(buf, format="JPEG", quality=quality, optimize=True)
        buf.seek(0)
        mesh.visual = TextureVisuals(uv=uvs, image=Image.open(buf).convert("RGB"))
        try:
            mesh.visual.material.baseColorFactor = [1.0, 1.0, 1.0, 1.0]
            mesh.visual.material.roughnessFactor = rough
            mesh.visual.material.metallicFactor = metal
        except Exception:
            pass
        data = mesh.export(file_type="glb")
        if isinstance(data, bytes) and len(data) <= MAX_GLB_BYTES:
            return data, atlas_px, quality
        export_bytes = data if isinstance(data, bytes) else None
        used_atlas_px = atlas_px
        used_quality = quality

    if not export_bytes:
        raise RuntimeError("GLB export failed")

    if len(export_bytes) > MAX_GLB_BYTES:
        mesh.visual = trimesh.visual.ColorVisuals(
            mesh, vertex_colors=[200, 190, 170, 255]
        )
        export_bytes = mesh.export(file_type="glb")
        if not isinstance(export_bytes, bytes):
            raise RuntimeError("GLB fallback export failed")

    return export_bytes, used_atlas_px, used_quality


def build_vessel_glb(
    vessel: dict[str, Any],
    *,
    ref_base: Path,
    voxel_res: int = DEFAULT_VOXEL,
    force: bool = False,
) -> dict[str, Any]:
    imo = str(vessel["imo"])
    paths = resolve_ortho_paths(vessel, ref_base)
    web_path, out_path = glb_paths(imo)
    WEB_GLB_DIR.mkdir(parents=True, exist_ok=True)
    OUT_GLB_DIR.mkdir(parents=True, exist_ok=True)

    digest = source_hash(paths, vessel)
    if not force and not needs_rebuild(vessel, paths):
        return {
            "imo": imo,
            "status": "SKIP_FRESH",
            "path": str(web_path),
            "bytes": web_path.stat().st_size,
            "source_hash": digest,
        }

    for key, p in paths.items():
        if not p.exists():
            return {"imo": imo, "status": "MISSING_ORTHO", "missing": str(p)}

    loa = float(vessel.get("loa_m") or 345.3)
    beam = float(vessel.get("beam_m") or 53.8)
    draft = float(vessel.get("draft_m") or 9.4)

    side_m = extract_silhouette(paths["side"], size=ORTHO_TEX_SIZE)
    top_m = extract_silhouette(paths["sat"], size=ORTHO_TEX_SIZE)
    bow_m = extract_silhouette(paths["bow"], size=ORTHO_TEX_SIZE)

    raw = generate_geometry(
        {"side": side_m, "sat": top_m, "bow": bow_m},
        loa_m=loa,
        beam_m=beam,
        draft_m=draft,
        voxel_res=voxel_res,
    )
    design_extents = np.array(
        [2.0 * raw.hx, 2.0 * raw.hy, 2.0 * raw.hz], dtype=np.float64
    )
    design_props = naval_proportion_report(design_extents)
    mesh_props = dict(raw.geom_report.get("mesh_proportions") or {})
    pitch = anisotropic_pitch(voxel_res, voxel_res, voxel_res, raw.hx, raw.hy, raw.hz)
    geom_report = dict(raw.geom_report)

    side_rgb = prepare_masked_ortho(
        load_ortho_rgb(paths["side"], size=ORTHO_TEX_SIZE), side_m, dilate_px=MASK_DILATE_PX
    )
    bow_rgb = prepare_masked_ortho(
        load_ortho_rgb(paths["bow"], size=ORTHO_TEX_SIZE), bow_m, dilate_px=MASK_DILATE_PX
    )
    sat_rgb = prepare_masked_ortho(
        load_ortho_rgb(paths["sat"], size=ORTHO_TEX_SIZE), top_m, dilate_px=MASK_DILATE_PX
    )

    presented = apply_presentation(
        raw,
        {"side": side_rgb, "bow": bow_rgb, "sat": sat_rgb},
        atlas_size=ATLAS_SIZE,
    )

    try:
        export_bytes, used_atlas_px, used_quality = _export_presented_glb(presented)
    except RuntimeError:
        return {"imo": imo, "status": "EXPORT_FAILED"}

    web_path.write_bytes(export_bytes)
    out_path.write_bytes(export_bytes)
    meta = {
        "imo": imo,
        "rank": vessel.get("rank"),
        "alg": ALG_VERSION,
        "pipeline": "generate_geometry||apply_presentation",
        "texturing": "photo_composite_triplanar_mask_aware",
        "mask_dilate_px": MASK_DILATE_PX,
        "taubin_iters": TAUBIN_ITERS,
        "facet_fix": "geometry_taubin_plus_smooth_normals",
        "source_hash": digest,
        "bytes": len(export_bytes),
        "atlas_px": used_atlas_px,
        "jpeg_quality": used_quality,
        "loa_m": loa,
        "beam_m": beam,
        "draft_m": draft,
        "voxel_res": voxel_res,
        "anisotropic_pitch": [float(x) for x in pitch.tolist()],
        "half_extents": {"hx": float(raw.hx), "hy": float(raw.hy), "hz": float(raw.hz)},
        "design_proportions": design_props,
        "mesh_proportions": mesh_props,
        "bbox_smoothing": geom_report,
        "raw_vertex_hash": presented.meta.get("raw_vertex_hash"),
        "axis_contract": "X=LOA Y=Beam Z=height (Z-up)",
        "glb_url": f"/output/assets/3d_models/vessel_{imo}.glb",
        "web_glb": f"/web/assets/3d_models/vessel_{imo}.glb",
    }
    web_path.with_suffix(".meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    out_path.with_suffix(".meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return {
        "imo": imo,
        "status": "BUILT",
        "path": str(web_path),
        "bytes": len(export_bytes),
        "source_hash": digest,
        "under_budget": len(export_bytes) <= MAX_GLB_BYTES,
        "atlas_px": used_atlas_px,
        "jpeg_quality": used_quality,
        "lb_ratio": mesh_props.get("lb_ratio"),
        "bd_ratio": mesh_props.get("bd_ratio"),
        "pitch": [float(x) for x in pitch.tolist()],
        "bbox_drift_pct": geom_report.get("bbox_drift_pct"),
        "bbox_drift_ok": geom_report.get("bbox_drift_ok"),
        "mask_dilate_px": MASK_DILATE_PX,
        "raw_vertex_hash": presented.meta.get("raw_vertex_hash"),
    }



def build_all_top10(
    *,
    force: bool = False,
    voxel_res: int = DEFAULT_VOXEL,
) -> dict[str, Any]:
    from services.top10_vessels import REF_BASE, TOP10_VESSELS

    results = []
    for v in TOP10_VESSELS:
        results.append(
            build_vessel_glb(v, ref_base=REF_BASE, voxel_res=voxel_res, force=force)
        )
    built = sum(1 for r in results if r.get("status") == "BUILT")
    skipped = sum(1 for r in results if r.get("status") == "SKIP_FRESH")
    failed = [r for r in results if r.get("status") not in ("BUILT", "SKIP_FRESH")]
    return {
        "ok": len(failed) == 0,
        "built": built,
        "skipped": skipped,
        "failed": failed,
        "results": results,
        "alg": ALG_VERSION,
        "max_bytes": MAX_GLB_BYTES,
    }
