#!/usr/bin/env python3
"""Probe production GLB bbox + write Z-up diagnostic viewer (camera-only)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parents[1]
GLB = ROOT / "output" / "assets" / "3d_models" / "vessel_9388833.glb"
OUT = ROOT / "logs" / "decouple_glb_probe.json"
VIEW = ROOT / "logs" / "decouple_zup_probe_viewer.html"


def main() -> int:
    mesh = trimesh.load(GLB, force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(tuple(mesh.geometry.values()))
    ext = np.asarray(mesh.extents, dtype=np.float64)
    lb = float(ext[0] / max(ext[1], 1e-9))
    ld = float(ext[0] / max(ext[2], 1e-9))
    bd = float(ext[1] / max(ext[2], 1e-9))
    report = {
        "glb": str(GLB.relative_to(ROOT)).replace("\\", "/"),
        "bytes": GLB.stat().st_size,
        "extents_xyz": ext.tolist(),
        "lb": lb,
        "ld": ld,
        "bd": bd,
        "faces": int(len(mesh.faces)),
        "verts": int(len(mesh.vertices)),
        "axis_contract_ok": bool(ext[0] > ext[1] > ext[2] > 0),
        "ribbon_suspect_ld_gt_18": bool(ld > 18),
    }
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    VIEW.write_text(
        VIEW_HTML.replace("__GLB__", "/output/assets/3d_models/vessel_9388833.glb"),
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2))
    return 0


VIEW_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>Z-up decouple probe</title>
<style>html,body{margin:0;height:100%;background:#0b1220;color:#cbd5e1;font:13px Consolas,monospace}
#c{width:100vw;height:100vh;display:block}#hud{position:fixed;left:10px;top:10px;background:rgba(0,0,0,.55);padding:8px 10px;border:1px solid #334155}</style>
</head><body>
<div id="hud">Z-up probe · production GLB · camera-only<br/><span id="m">…</span></div>
<canvas id="c"></canvas>
<script type="importmap">{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}</script>
<script type="module">
import * as THREE from "three";
import {OrbitControls} from "three/addons/controls/OrbitControls.js";
import {GLTFLoader} from "three/addons/loaders/GLTFLoader.js";
const canvas=document.getElementById("c");
const renderer=new THREE.WebGLRenderer({canvas,antialias:true,preserveDrawingBuffer:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,2));
renderer.setSize(innerWidth,innerHeight);
renderer.outputColorSpace=THREE.SRGBColorSpace;
const scene=new THREE.Scene(); scene.background=new THREE.Color(0x0b1220);
const camera=new THREE.PerspectiveCamera(38,innerWidth/innerHeight,0.01,100);
camera.up.set(0,0,1);
const controls=new OrbitControls(camera,canvas); controls.enableDamping=true;
scene.add(new THREE.AmbientLight(0xffffff,1.2));
const key=new THREE.DirectionalLight(0xfff1de,2.0); key.position.set(4,14,16); scene.add(key);
const fill=new THREE.DirectionalLight(0xb4c8dc,0.85); fill.position.set(-12,-6,8); scene.add(fill);
new GLTFLoader().load("__GLB__",(gltf)=>{
  const root=gltf.scene;
  // NO rotation / NO scale
  scene.add(root);
  const box=new THREE.Box3().setFromObject(root);
  const size=box.getSize(new THREE.Vector3());
  const center=box.getCenter(new THREE.Vector3());
  controls.target.copy(center);
  const maxDim=Math.max(size.x,size.y,size.z,0.001);
  const dist=maxDim*2.1;
  // Strong 3/4 lateral: Beam (+Y) + height (+Z)
  camera.position.set(center.x+dist*0.65, center.y+dist*0.75, center.z+dist*0.42);
  camera.near=maxDim/100; camera.far=maxDim*80; camera.updateProjectionMatrix();
  controls.update();
  document.getElementById("m").textContent =
    `size=${size.x.toFixed(3)},${size.y.toFixed(3)},${size.z.toFixed(3)}  L/B=${(size.x/size.y).toFixed(2)} L/D=${(size.x/size.z).toFixed(2)}`;
  window.__PROBE__={size:size.toArray(), lb:size.x/size.y, ld:size.x/size.z};
});
addEventListener("resize",()=>{camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);});
(function tick(){requestAnimationFrame(tick);controls.update();renderer.render(scene,camera);})();
</script></body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
