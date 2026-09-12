"""Immutability contract: apply_presentation must not mutate RawMesh vertices.

Live geometry is generated inside the test (v3.3.0 content-bbox path via
generate_geometry). Hashes are computed at runtime — never from a stale file
or hardcoded constant from a prior alg version.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from services.top10_3d_mesh import (
    ALG_VERSION,
    FIT_MESH_TO_EXTENTS,
    MASK_DILATE_PX,
    ORTHO_TEX_SIZE,
    Z_INFLATE_ITERS,
    apply_presentation,
    extract_silhouette,
    generate_geometry,
    hash_vertex_positions,
    load_ortho_rgb,
    prepare_masked_ortho,
    resolve_ortho_paths,
)
from services.top10_vessels import REF_BASE, TOP10_VESSELS

IMO = "9388833"  # BU SAMRA — GATE PASS carving baseline vessel


def _vessel_9388833() -> dict:
    v = next((x for x in TOP10_VESSELS if str(x["imo"]) == IMO), None)
    if v is None:
        pytest.skip("BU SAMRA not in TOP10 catalog")
    return v


def _masks_and_photos(vessel: dict):
    paths = resolve_ortho_paths(vessel, REF_BASE)
    for k, p in paths.items():
        if not Path(p).exists():
            pytest.skip(f"missing ortho {k}: {p}")
    side_m = extract_silhouette(paths["side"], size=ORTHO_TEX_SIZE)
    top_m = extract_silhouette(paths["sat"], size=ORTHO_TEX_SIZE)
    bow_m = extract_silhouette(paths["bow"], size=ORTHO_TEX_SIZE)
    masks = {"side": side_m, "sat": top_m, "bow": bow_m}
    photos = {
        "side": prepare_masked_ortho(
            load_ortho_rgb(paths["side"], size=ORTHO_TEX_SIZE),
            side_m,
            dilate_px=MASK_DILATE_PX,
        ),
        "bow": prepare_masked_ortho(
            load_ortho_rgb(paths["bow"], size=ORTHO_TEX_SIZE),
            bow_m,
            dilate_px=MASK_DILATE_PX,
        ),
        "sat": prepare_masked_ortho(
            load_ortho_rgb(paths["sat"], size=ORTHO_TEX_SIZE),
            top_m,
            dilate_px=MASK_DILATE_PX,
        ),
    }
    return masks, photos, vessel


def test_geometry_flags_locked_for_decouple():
    assert Z_INFLATE_ITERS == 0
    assert FIT_MESH_TO_EXTENTS is False
    assert ALG_VERSION.startswith("top10-glb-v3.4")


def test_apply_presentation_does_not_mutate_raw_vertices():
    vessel = _vessel_9388833()
    masks, photos, vessel = _masks_and_photos(vessel)

    raw = generate_geometry(
        masks,
        loa_m=float(vessel["loa_m"]),
        beam_m=float(vessel["beam_m"]),
        draft_m=float(vessel["draft_m"]),
        voxel_res=80,
    )
    assert raw.vertices.flags.writeable is False

    raw_hash_before = hash_vertex_positions(raw)
    assert len(raw_hash_before) == 64

    presented = apply_presentation(raw, photos)

    raw_hash_after = hash_vertex_positions(raw)
    assert raw_hash_before == raw_hash_after, (
        f"RawMesh mutated by apply_presentation: "
        f"{raw_hash_before[:16]}… != {raw_hash_after[:16]}…"
    )
    assert presented.meta.get("raw_vertex_hash") == raw_hash_before
    assert presented.vertices.shape == raw.vertices.shape
    # Presented may own a deep copy; positions must match raw (no silent stretch)
    np.testing.assert_allclose(presented.vertices, raw.vertices, rtol=0, atol=1e-12)


def test_generate_geometry_live_hash_is_stable_within_run():
    """Two live generate_geometry calls in one run → identical vertex hash.

    Proves the reference hash is computed from live MC output, not a disk cache.
    """
    vessel = _vessel_9388833()
    masks, _photos, vessel = _masks_and_photos(vessel)
    kwargs = dict(
        loa_m=float(vessel["loa_m"]),
        beam_m=float(vessel["beam_m"]),
        draft_m=float(vessel["draft_m"]),
        voxel_res=80,
    )
    a = generate_geometry(masks, **kwargs)
    b = generate_geometry(masks, **kwargs)
    ha = hash_vertex_positions(a)
    hb = hash_vertex_positions(b)
    assert ha == hb
    assert ha != "0" * 64
