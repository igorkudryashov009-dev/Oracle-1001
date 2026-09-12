#!/usr/bin/env python3
"""Prompt-3 final: height-profile corr for all TOP-10 + meta/alg/budget audit."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

N_BINS = 40
OUT = ROOT / "logs" / "prompt3_height_profile_all10.json"
CORR_LOW = 0.50
CORR_OK_LO = 0.85


def height_corr_for_vessel(vessel: dict) -> dict:
    from services.top10_3d_mesh import (
        DEFAULT_VOXEL,
        MASK_CONTENT_PAD_FRAC,
        ORTHO_TEX_SIZE,
        estimate_visual_depth_m,
        extract_silhouette,
        generate_geometry,
        mask_bbox_xyxy,
        resolve_ortho_paths,
    )
    from services.top10_vessels import REF_BASE

    imo = str(vessel["imo"])
    loa = float(vessel["loa_m"])
    beam = float(vessel["beam_m"])
    draft = float(vessel["draft_m"])
    paths = resolve_ortho_paths(vessel, REF_BASE)
    for k, p in paths.items():
        if not Path(p).exists():
            return {"imo": imo, "ok": False, "error": f"missing {k}: {p}"}

    side = extract_silhouette(paths["side"], size=ORTHO_TEX_SIZE)
    top = extract_silhouette(paths["sat"], size=ORTHO_TEX_SIZE)
    bow = extract_silhouette(paths["bow"], size=ORTHO_TEX_SIZE)
    depth = float(estimate_visual_depth_m(side, loa, draft, beam)["visual_depth_m"])

    raw = generate_geometry(
        {"side": side, "sat": top, "bow": bow},
        loa_m=loa,
        beam_m=beam,
        draft_m=draft,
        voxel_res=DEFAULT_VOXEL,
    )
    v = np.asarray(raw.vertices, dtype=np.float64)
    x, z = v[:, 0], v[:, 2]
    xmin, xmax = float(x.min()), float(x.max())
    edges = np.linspace(xmin, xmax, N_BINS + 1)
    mesh_z = []
    for i in range(N_BINS):
        lo, hi = edges[i], edges[i + 1]
        m = (x >= lo) & (x < hi if i < N_BINS - 1 else x <= hi)
        if not np.any(m):
            mesh_z.append(0.0)
        else:
            zz = z[m]
            mesh_z.append(float(zz.max() - zz.min()))
    mesh_z = np.asarray(mesh_z, dtype=np.float64)

    h_px, w_px = side.shape
    x0, y0, x1, y1 = mask_bbox_xyxy(side)
    pad_x = MASK_CONTENT_PAD_FRAC * w_px
    pad_y = MASK_CONTENT_PAD_FRAC * h_px
    cx0 = max(0, int(x0 - pad_x))
    cy0 = max(0, int(y0 - pad_y))
    cx1 = min(w_px, int(x1 + pad_x))
    cy1 = min(h_px, int(y1 + pad_y))
    content_w = max(cx1 - cx0, 1)
    content_h = max(cy1 - cy0, 1)
    m_per_px_v = depth / content_h

    mask_z = []
    for i in range(N_BINS):
        c_lo = cx0 + int(i * content_w / N_BINS)
        c_hi = cx0 + int((i + 1) * content_w / N_BINS)
        c_hi = max(c_hi, c_lo + 1)
        strip = side[:, c_lo:c_hi]
        heights = []
        for c in range(strip.shape[1]):
            ys = np.where(strip[:, c])[0]
            heights.append(0.0 if len(ys) == 0 else float(ys.max() - ys.min() + 1))
        mask_z.append(float(np.mean(heights)) * m_per_px_v)
    mask_z = np.asarray(mask_z, dtype=np.float64)

    mn = mesh_z / max(float(mesh_z.max()), 1e-9)
    sn = mask_z / max(float(mask_z.max()), 1e-9)
    corr = float(np.corrcoef(mn, sn)[0, 1]) if len(mn) > 2 else 0.0
    if not np.isfinite(corr):
        corr = 0.0

    extents = np.asarray(raw.geom_report.get("mesh_extents") or [0, 0, 0], dtype=np.float64)
    lb = float(extents[0] / max(extents[1], 1e-9)) if extents[1] else 0.0

    flag = "ok"
    if corr < CORR_LOW:
        flag = "individual_geometry_issue"
    elif corr < CORR_OK_LO:
        flag = "corr_below_band_but_not_critical"

    return {
        "imo": imo,
        "rank": vessel.get("rank"),
        "name": vessel.get("name"),
        "ok": True,
        "corr": corr,
        "flag": flag,
        "lb_ratio": lb,
        "mesh_z_max": float(mesh_z.max()),
        "mask_z_max": float(mask_z.max()),
    }


def audit_metas() -> list[dict]:
    from services.top10_3d_mesh import ALG_VERSION, MAX_GLB_BYTES, OUT_GLB_DIR
    from services.top10_vessels import TOP10_VESSELS

    rows = []
    for v in TOP10_VESSELS:
        imo = str(v["imo"])
        glb = OUT_GLB_DIR / f"vessel_{imo}.glb"
        meta_p = glb.with_suffix(".meta.json")
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
        nbytes = glb.stat().st_size if glb.exists() else 0
        rows.append(
            {
                "imo": imo,
                "alg": meta.get("alg"),
                "pipeline": meta.get("pipeline"),
                "bytes": nbytes,
                "under_budget": nbytes <= MAX_GLB_BYTES,
                "alg_ok": meta.get("alg") == ALG_VERSION,
            }
        )
    return rows


def main() -> int:
    from services.top10_3d_mesh import ALG_VERSION
    from services.top10_vessels import TOP10_VESSELS

    profiles = [height_corr_for_vessel(v) for v in TOP10_VESSELS]
    metas = audit_metas()
    report = {
        "alg": ALG_VERSION,
        "n_bins": N_BINS,
        "corr_ok_band": [CORR_OK_LO, 0.95],
        "corr_anomaly_lt": CORR_LOW,
        "profiles": profiles,
        "metas": metas,
        "all_alg_ok": all(m["alg_ok"] for m in metas),
        "all_under_budget": all(m["under_budget"] for m in metas),
        "max_glb_bytes": max(m["bytes"] for m in metas),
        "individual_geometry_issues": [
            p for p in profiles if p.get("flag") == "individual_geometry_issue"
        ],
    }
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in report if k not in ("profiles", "metas")}, indent=2))
    for p in profiles:
        print(
            f"  rank={p.get('rank')} IMO={p['imo']} corr={p.get('corr', 0):.3f} "
            f"flag={p.get('flag')} L/B={p.get('lb_ratio', 0):.2f}"
        )
    for m in metas:
        print(f"  meta IMO={m['imo']} alg={m['alg']} bytes={m['bytes']} ok={m['alg_ok']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
