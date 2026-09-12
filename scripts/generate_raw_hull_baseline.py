#!/usr/bin/env python3
"""Diagnostic RAW hull baseline — carving/CSG/MC only, no presentation transforms.

Prompt-1 diagnostic tool. Intentionally excludes:
  - viewer axis remap (rotation.x = -π/2)
  - scale.z / heightBoost / PRES_* multipliers
  - _fit_mesh_to_extents (bbox stretch that can fake healthy L/B)
  - Taubin sanitize + presentation air-draft TARGET_LD_MAX envelope
  - triplanar texture bake

Outputs:
  output/_raw_baseline/vessel_{imo}_raw.glb
  output/_raw_baseline/vessel_{imo}_raw.meta.json
  logs/raw_hull_baseline_viewer.html  (simple THREE.js, Z-up camera, no prod hacks)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "_raw_baseline"
LOG_DIR = ROOT / "logs"


def physical_half_extents(
    loa_m: float, beam_m: float, depth_m: float
) -> tuple[float, float, float]:
    """Metres → unit half-extents (no TARGET_LD_MAX / freeboard presentation hacks)."""
    loa = max(float(loa_m), 1.0)
    beam = max(float(beam_m), 0.5)
    depth = max(float(depth_m), 0.5)
    hx = 1.0
    hy = beam / loa
    hz = depth / loa
    m = max(hx, hy, hz, 1e-6)
    return hx / m, hy / m, hz / m


def occupancy_midship(occupied: np.ndarray) -> dict:
    nx, ny, nz = occupied.shape
    x0, x1 = int(nx * 0.4), int(nx * 0.6)
    mid = occupied[x0:x1]
    z_hist = mid.sum(axis=(0, 1)).astype(int)
    y_hist = mid.sum(axis=(0, 2)).astype(int)
    z_nz = int(np.count_nonzero(z_hist))
    y_nz = int(np.count_nonzero(y_hist))
    return {
        "occupied_total": int(occupied.sum()),
        "occupied_frac": float(occupied.mean()),
        "midship_z_nonzero_layers": z_nz,
        "midship_y_nonzero_layers": y_nz,
        "midship_z_span_frac": z_nz / max(nz, 1),
        "midship_y_span_frac": y_nz / max(ny, 1),
        "midship_z_hist": z_hist.tolist(),
        "midship_y_hist": y_hist.tolist(),
    }


def raw_mesh_from_voxels(occupied: np.ndarray, hx: float, hy: float, hz: float):
    """Marching cubes + centre shift only. No fit/sanitize/Taubin."""
    import trimesh
    from trimesh.voxel.ops import matrix_to_marching_cubes

    from services.top10_3d_mesh import anisotropic_pitch

    nx, ny, nz = occupied.shape
    pitch = anisotropic_pitch(nx, ny, nz, hx, hy, hz)
    mesh = matrix_to_marching_cubes(matrix=occupied.astype(bool), pitch=pitch)
    mesh.apply_translation([-hx, -hy, -hz])
    try:
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.remove_unreferenced_vertices()
    except Exception:
        pass
    return mesh


def build_raw_baseline(imo: str = "9388833", *, voxel_res: int = 80) -> dict:
    from services.top10_3d_mesh import (
        Z_INFLATE_ITERS,
        carve_voxels,
        estimate_visual_depth_m,
        extract_silhouette,
        resolve_ortho_paths,
    )
    from services.top10_vessels import REF_BASE, TOP10_VESSELS

    vessel = next((v for v in TOP10_VESSELS if str(v["imo"]) == str(imo)), None)
    if not vessel:
        raise SystemExit(f"IMO {imo} not in TOP10 catalog")

    loa = float(vessel.get("loa_m") or 345.3)
    beam = float(vessel.get("beam_m") or 53.8)
    draft = float(vessel.get("draft_m") or 9.4)

    paths = resolve_ortho_paths(vessel, REF_BASE)
    for k, p in paths.items():
        if not p.exists():
            raise SystemExit(f"missing ortho {k}: {p}")

    import services.top10_3d_mesh as m

    prev_inflate = m.Z_INFLATE_ITERS
    m.Z_INFLATE_ITERS = 0  # pure CSG — inflate must stay off for baseline
    try:
        side = extract_silhouette(paths["side"])
        top = extract_silhouette(paths["sat"])
        bow = extract_silhouette(paths["bow"])
        depth_est = estimate_visual_depth_m(side, loa, draft, beam)
        visual_depth = float(depth_est["visual_depth_m"])
        hx, hy, hz = physical_half_extents(loa, beam, visual_depth)
        occupied = carve_voxels(side, top, bow, hx=hx, hy=hy, hz=hz, res=int(voxel_res))
    finally:
        m.Z_INFLATE_ITERS = prev_inflate

    occ = occupancy_midship(occupied)
    mesh = raw_mesh_from_voxels(occupied, hx, hy, hz)
    extents = np.asarray(mesh.extents, dtype=np.float64)
    lb = float(extents[0] / max(extents[1], 1e-9))
    ld = float(extents[0] / max(extents[2], 1e-9))
    bd = float(extents[1] / max(extents[2], 1e-9))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    glb_path = OUT_DIR / f"vessel_{imo}_raw.glb"
    import trimesh

    mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=[180, 185, 190, 255])
    glb_path.write_bytes(mesh.export(file_type="glb"))

    meta = {
        "imo": str(imo),
        "alg": "raw-hull-baseline-v2-content-bbox",
        "note": "CSG∩MC + content-bbox UV; no fit/sanitize/remap/scale/heightBoost/texture",
        "loa_m": loa,
        "beam_m": beam,
        "draft_m": draft,
        "visual_depth_m": visual_depth,
        "depth_estimate": depth_est,
        "half_extents": {"hx": hx, "hy": hy, "hz": hz},
        "mesh_extents": extents.tolist(),
        "lb_ratio": lb,
        "ld_ratio": ld,
        "bd_ratio": bd,
        "faces": int(len(mesh.faces)),
        "verts": int(len(mesh.vertices)),
        "z_inflate_iters": 0,
        "module_z_inflate_default": int(Z_INFLATE_ITERS),
        "occupancy": occ,
        "glb": str(glb_path.relative_to(ROOT)).replace("\\", "/"),
    }
    meta_path = OUT_DIR / f"vessel_{imo}_raw.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


VIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>RAW hull baseline (no presentation)</title>
  <style>
    html,body{margin:0;height:100%;background:#0b1220;color:#cbd5e1;font:14px/1.4 Consolas,monospace}
    #c{width:100vw;height:100vh;display:block}
    #hud{position:fixed;left:12px;top:12px;z-index:2;background:rgba(0,0,0,.55);padding:10px 12px;border:1px solid #334155;max-width:420px}
  </style>
</head>
<body>
  <div id="hud">RAW baseline · Z-up mesh · no axis-remap / no scale hacks<br/>
    <span id="meta">loading…</span></div>
  <canvas id="c"></canvas>
  <script type="importmap">
  {"imports":{
    "three":"https://unpkg.com/three@0.160.0/build/three.module.js",
    "three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"
  }}
  </script>
  <script type="module">
  import * as THREE from "three";
  import { OrbitControls } from "three/addons/controls/OrbitControls.js";
  import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

  const params = new URLSearchParams(location.search);
  const glbUrl = params.get("glb") || "/output/_raw_baseline/vessel_9388833_raw.glb";
  const metaUrl = params.get("meta") || "/output/_raw_baseline/vessel_9388833_raw.meta.json";

  const canvas = document.getElementById("c");
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(innerWidth, innerHeight);
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  // Z-up world (match naval mesh frame) — camera.up = +Z, NO mesh rotation
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0b1220);
  const camera = new THREE.PerspectiveCamera(45, innerWidth / innerHeight, 0.01, 100);
  camera.up.set(0, 0, 1);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;

  scene.add(new THREE.AmbientLight(0xffffff, 0.85));
  const key = new THREE.DirectionalLight(0xffffff, 1.1);
  key.position.set(2, 3, 4);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xaabbff, 0.45);
  fill.position.set(-2, -1, 2);
  scene.add(fill);

  fetch(metaUrl).then(r => r.json()).then(m => {
    document.getElementById("meta").textContent =
      `IMO ${m.imo} · L/B=${m.lb_ratio.toFixed(2)} L/D=${m.ld_ratio.toFixed(2)} B/D=${m.bd_ratio.toFixed(2)} · faces=${m.faces}`;
  }).catch(() => {});

  new GLTFLoader().load(glbUrl, (gltf) => {
    const root = gltf.scene;
    // CRITICAL: do not rotate / anisotropically scale the mesh
    scene.add(root);
    const box = new THREE.Box3().setFromObject(root);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    controls.target.copy(center);
    // Bow-quarter view in Z-up frame: +X forward-ish, +Y beam, +Z deck
    const maxDim = Math.max(size.x, size.y, size.z, 0.001);
    const dist = maxDim * 1.85;
    camera.position.set(center.x + dist * 0.85, center.y + dist * 0.55, center.z + dist * 0.35);
    camera.near = maxDim / 100;
    camera.far = maxDim * 50;
    camera.updateProjectionMatrix();
    controls.update();
    window.__RAW_BASELINE__ = { size: size.toArray(), center: center.toArray(), lb: size.x / Math.max(size.y, 1e-6) };
  }, undefined, (e) => {
    document.getElementById("meta").textContent = "GLB load failed: " + e;
  });

  addEventListener("resize", () => {
    camera.aspect = innerWidth / innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(innerWidth, innerHeight);
  });
  (function tick() {
    requestAnimationFrame(tick);
    controls.update();
    renderer.render(scene, camera);
  })();
  </script>
</body>
</html>
"""


def write_viewer() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "raw_hull_baseline_viewer.html"
    path.write_text(VIEWER_HTML, encoding="utf-8")
    # also serve copy under output for static server
    out = OUT_DIR / "raw_hull_baseline_viewer.html"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(VIEWER_HTML, encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--imo", default="9388833")
    ap.add_argument("--voxel-res", type=int, default=80)
    args = ap.parse_args()
    meta = build_raw_baseline(args.imo, voxel_res=args.voxel_res)
    viewer = write_viewer()
    print(json.dumps({"ok": True, "meta": meta, "viewer": str(viewer)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
