/**
 * TOP-10 GLB inspector — Three.js GLTFLoader + OrbitControls + studio PBR lighting.
 * Geometry rescue + frustum framing: bbox zoom-to-fit × 1.05 padding, near/far from maxDim, smooth reset.
 *
 * ARCHITECTURE (Prompt integration):
 *  - ONE live WebGL context for modal GLB (never per-card / never 10 concurrent).
 *  - Lazy load via mountTop10GlbViewer — caller must not prefetch all 10 .glb.
 *  - Honest fidelity: silhouette→voxel CSG→MC yields a hull ENVELOPE, not photogrammetry.
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const AUTO_ROTATE_SPEED = 0.5; // ≈ 0.5 RPM
const MIN_DIST = 1.2;
const MAX_DIST = 48.0;
const HULL_FALLBACK = 0x1e293b; // corporate naval hull
/** Zoom-to-fit multiplier: 100% bbox visibility + 5% edge margin (not a second ad-hoc pad). */
export const FIT_PADDING = 1.05;
/** Catalog ¾ (Z-up): side + deck + bow. Fit changes distance only — never this angle. */
const DEFAULT_VIEW_DIR = Object.freeze({ x: 0.72, y: 0.55, z: 0.42 });
const RESET_MS = 480;
const MIN_MESH_EXTENT = 1e-4;
const MIN_ASPECT = 0.025; // reject paper-thin collapses; real hulls ~0.04–0.08
const ZERO_AREA_EPS = 1e-20;
const MAX_ZERO_AREA_RATIO = 0.35; // >35% zero-area samples → degenerate pancake
const MAX_NAN_RATIO = 0.02; // >2% non-finite verts → corrupt buffer

function makeStudioEnvMap(renderer) {
  // Soft maritime sky→horizon→sea gradient for cheap IBL (not chrome HDRI)
  const size = 256;
  const data = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y++) {
    const t = y / (size - 1); // 0 = top (sky), 1 = bottom (sea)
    let r;
    let g;
    let b;
    if (t < 0.42) {
      // sky: pale blue
      const u = t / 0.42;
      r = Math.round(170 + (210 - 170) * u);
      g = Math.round(195 + (220 - 195) * u);
      b = Math.round(220 + (235 - 220) * u);
    } else if (t < 0.55) {
      // horizon haze
      const u = (t - 0.42) / 0.13;
      r = Math.round(210 + (190 - 210) * u);
      g = Math.round(220 + (200 - 220) * u);
      b = Math.round(235 + (210 - 235) * u);
    } else {
      // sea: deeper blue-green
      const u = (t - 0.55) / 0.45;
      r = Math.round(40 + (70 - 40) * (1 - u));
      g = Math.round(70 + (100 - 70) * (1 - u));
      b = Math.round(95 + (120 - 95) * (1 - u));
    }
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      data[i] = r;
      data[i + 1] = g;
      data[i + 2] = b;
      data[i + 3] = 255;
    }
  }
  const tex = new THREE.DataTexture(data, size, size, THREE.RGBAFormat);
  tex.needsUpdate = true;
  tex.mapping = THREE.EquirectangularReflectionMapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  const pmrem = new THREE.PMREMGenerator(renderer);
  const env = pmrem.fromEquirectangular(tex).texture;
  tex.dispose();
  pmrem.dispose();
  return env;
}

const GLB_CACHE_TAG = "v340decouple";
/** Naval mesh frame matches carve: X=LOA, Y=Beam, Z=height. Viewer stays Z-up. */
const NAVAL_CAMERA_UP = Object.freeze({ x: 0, y: 0, z: 1 });
const CANVAS_FALLBACK_W = 600;
const CANVAS_FALLBACK_H = 400;

/** Cached WebGL capability — never leave a probe context alive (16-context budget). */
let _webglCapableCache = null;

function isWebGLCapable() {
  if (_webglCapableCache != null) return _webglCapableCache;
  let gl = null;
  try {
    const c = document.createElement("canvas");
    gl = c.getContext("webgl2") || c.getContext("webgl");
    _webglCapableCache = !!gl;
  } catch {
    _webglCapableCache = false;
  }
  try {
    gl?.getExtension?.("WEBGL_lose_context")?.loseContext?.();
  } catch {
    /* */
  }
  return _webglCapableCache;
}

function resolveGlbUrl(vessel) {
  const imo = String(vessel?.imo || "");
  let path = String(vessel?.glb?.url || `/output/assets/3d_models/vessel_${imo}.glb`);
  // Absolute origin — avoids relative resolution under /output/… HTML base edge-cases
  if (path.startsWith("/") && typeof location !== "undefined" && location.origin) {
    path = `${location.origin}${path}`;
  }
  const ver = vessel?.glb?.bytes || vessel?.glb?.source_hash || GLB_CACHE_TAG;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}v=${encodeURIComponent(String(ver))}`;
}

/** Host box size with hard fallback — never mount a 0×0 WebGL canvas. */
function measureHostBox(host) {
  const cw = Math.floor(host?.clientWidth || 0);
  const ch = Math.floor(host?.clientHeight || 0);
  const rect = host?.getBoundingClientRect?.();
  const rw = Math.floor(rect?.width || 0);
  const rh = Math.floor(rect?.height || 0);
  const w = Math.max(cw, rw, CANVAS_FALLBACK_W);
  const h = Math.max(ch, rh, CANVAS_FALLBACK_H);
  return { w, h, usedFallback: cw < 32 || ch < 32 };
}

function colorIsNearBlack(color) {
  if (!color || typeof color.getHex !== "function") return true;
  const hex = color.getHex();
  return hex === 0x000000 || hex < 0x101010;
}

function hasUsableMap(mat) {
  return !!(mat.map || mat.emissiveMap || mat.metalnessMap || mat.roughnessMap || mat.normalMap);
}

/**
 * Recompute normals + audit position buffer for NaN / Inf.
 * Returns false if geometry is unusable.
 */
function sanitizeGeometry(geometry) {
  if (!geometry) return false;
  try {
    const audit = auditGeometryBuffers(geometry);
    if (!audit.ok) return false;

    geometry.computeBoundingBox();
    geometry.computeBoundingSphere();
    // Always regenerate normals — GLB from procedural MC often ships inverted / zeroed
    geometry.computeVertexNormals();
    if (typeof geometry.normalizeNormals === "function") {
      geometry.normalizeNormals();
    }
    geometry.computeBoundingBox();
    return true;
  } catch {
    return false;
  }
}

/** NaN / Inf positions + zero-surface-area triangle sampling. */
function auditGeometryBuffers(geometry) {
  const pos = geometry?.getAttribute?.("position");
  if (!pos || pos.count < 3) {
    return { ok: false, reason: "empty-position" };
  }

  let finite = 0;
  let nonFinite = 0;
  for (let i = 0; i < pos.count; i++) {
    const x = pos.getX(i);
    const y = pos.getY(i);
    const z = pos.getZ(i);
    if (Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(z)) finite += 1;
    else nonFinite += 1;
  }
  if (finite < 3) return { ok: false, reason: "nan-vertices" };
  if (nonFinite / pos.count > MAX_NAN_RATIO) {
    return { ok: false, reason: "nan-vertex-ratio" };
  }

  const area = sampleZeroAreaTriangles(geometry);
  if (area.total >= 24 && area.ratio > MAX_ZERO_AREA_RATIO) {
    return { ok: false, reason: "zero-area-triangles" };
  }
  return { ok: true, reason: "" };
}

function sampleZeroAreaTriangles(geometry) {
  const pos = geometry.getAttribute("position");
  const index = geometry.index;
  const a = new THREE.Vector3();
  const b = new THREE.Vector3();
  const c = new THREE.Vector3();
  const ab = new THREE.Vector3();
  const ac = new THREE.Vector3();
  const cross = new THREE.Vector3();
  let zero = 0;
  let total = 0;
  const sampleBudget = 2400;

  const testTri = (ia, ib, ic) => {
    a.fromBufferAttribute(pos, ia);
    b.fromBufferAttribute(pos, ib);
    c.fromBufferAttribute(pos, ic);
    if (![a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z].every(Number.isFinite)) {
      zero += 1;
      total += 1;
      return;
    }
    ab.subVectors(b, a);
    ac.subVectors(c, a);
    cross.crossVectors(ab, ac);
    if (cross.lengthSq() < ZERO_AREA_EPS) zero += 1;
    total += 1;
  };

  if (index && index.count >= 3) {
    const step = Math.max(3, Math.floor(index.count / sampleBudget) * 3 || 3);
    for (let i = 0; i + 2 < index.count; i += step) {
      testTri(index.getX(i), index.getX(i + 1), index.getX(i + 2));
    }
  } else {
    const step = Math.max(3, Math.floor(pos.count / sampleBudget) * 3 || 3);
    for (let i = 0; i + 2 < pos.count; i += step) {
      testTri(i, i + 1, i + 2);
    }
  }
  return { zero, total, ratio: total ? zero / total : 1 };
}

function isDegenerateSize(size) {
  if (!size) return true;
  const x = size.x;
  const y = size.y;
  const z = size.z;
  if (![x, y, z].every((v) => Number.isFinite(v))) return true;
  if (x <= MIN_MESH_EXTENT || y <= MIN_MESH_EXTENT || z <= MIN_MESH_EXTENT) return true;
  const maxDim = Math.max(x, y, z);
  const minDim = Math.min(x, y, z);
  if (maxDim <= MIN_MESH_EXTENT) return true;
  // Flattened / failed voxel mesh (pancake) — any axis near-zero vs LOA
  if (minDim / maxDim < MIN_ASPECT) return true;
  const len = size.length();
  return !Number.isFinite(len) || len <= MIN_MESH_EXTENT;
}

/**
 * Pre-scene audit of the full GLB root: buffers, bbox, pancake thickness.
 * @returns {{ ok: boolean, reason: string }}
 */
function auditLoadedRoot(root) {
  if (!root) return { ok: false, reason: "empty-scene" };
  let meshCount = 0;
  let geoOk = 0;
  let bufferFail = "";

  root.traverse((obj) => {
    if (!obj.isMesh) return;
    meshCount += 1;
    if (!obj.geometry) {
      bufferFail = bufferFail || "missing-geometry";
      return;
    }
    const buf = auditGeometryBuffers(obj.geometry);
    if (!buf.ok) {
      bufferFail = bufferFail || buf.reason;
      return;
    }
    geoOk += 1;
  });

  if (meshCount === 0) return { ok: false, reason: "no-meshes" };
  if (geoOk === 0) return { ok: false, reason: bufferFail || "no-valid-geometry" };

  const box = new THREE.Box3().setFromObject(root);
  const size = box.getSize(new THREE.Vector3());
  if (isDegenerateSize(size)) {
    // Distinguish flat Z (typical voxel pancake) for operator telemetry
    const dims = [size.x, size.y, size.z];
    const minAxis = dims.indexOf(Math.min(...dims));
    const axisName = minAxis === 0 ? "X" : minAxis === 1 ? "Y" : "Z";
    return { ok: false, reason: `degenerate-bbox-${axisName}` };
  }
  return { ok: true, reason: "" };
}

function hardenMaterial(mat) {
  if (!mat) return mat;
  const type = mat.type || "";
  const isPbr =
    type === "MeshStandardMaterial" ||
    type === "MeshPhysicalMaterial" ||
    mat.isMeshStandardMaterial ||
    mat.isMeshPhysicalMaterial;

  // Industrial marine paint: matte/satin hull paint — not chrome, not plastic
  const applyMarinePaint = (m) => {
    m.wireframe = false; // solid shaded default — wireframe is HUD toggle only
    m.flatShading = false;
    m.metalness = 0.05;
    m.roughness = 0.72;
    if ("envMapIntensity" in m) m.envMapIntensity = 0.55;
    m.side = THREE.DoubleSide; // inverted winding / backfaces stay visible
    if (m.map) {
      try {
        m.map.colorSpace = THREE.SRGBColorSpace;
        m.map.anisotropy = 8;
        m.map.needsUpdate = true;
      } catch {
        /* */
      }
      // Photo-composite albedo must not be tinted by dark baseColor
      m.color = new THREE.Color(0xffffff);
    }
    // Physical clearcoat amplifies mirror look — clamp if present
    if ("clearcoat" in m) m.clearcoat = 0;
    if ("clearcoatRoughness" in m) m.clearcoatRoughness = 1;
    if ("specularIntensity" in m) m.specularIntensity = 0.18;
    m.needsUpdate = true;
  };

  if (isPbr) {
    applyMarinePaint(mat);
    const mapsOk = hasUsableMap(mat);
    if (!mapsOk && colorIsNearBlack(mat.color)) {
      mat.color = new THREE.Color(HULL_FALLBACK);
    }
    if (mat.emissive && colorIsNearBlack(mat.emissive) && !mat.emissiveMap) {
      mat.emissive = new THREE.Color(0x0b1220);
      mat.emissiveIntensity = 0.05;
    }
    return mat;
  }

  // Non-PBR / failed bind → naval hull standard material (matte marine)
  const fallback = new THREE.MeshStandardMaterial({
    color: HULL_FALLBACK,
    roughness: 0.72,
    metalness: 0.05,
    envMapIntensity: 0.55,
    side: THREE.DoubleSide,
    wireframe: false,
    flatShading: false,
    emissive: new THREE.Color(0x0b1220),
    emissiveIntensity: 0.04,
  });
  if (mat.map) {
    fallback.map = mat.map;
    try {
      fallback.map.colorSpace = THREE.SRGBColorSpace;
    } catch {
      /* */
    }
  }
  if (mat.normalMap) fallback.normalMap = mat.normalMap;
  fallback.needsUpdate = true;
  try {
    mat.dispose?.();
  } catch {
    /* */
  }
  return fallback;
}

/**
 * World AABB that includes InstancedMesh instances (Three r160 setFromObject
 * only sees the prototype cube — that is why Voxel Grid was framed too tight).
 */
export function computeObjectBoundingBox(object) {
  const box = new THREE.Box3();
  if (!object) return box;
  const cached = object.userData?._worldBBox;
  if (cached && cached.isBox3 && !cached.isEmpty()) {
    return cached.clone();
  }
  object.updateWorldMatrix(true, true);
  const instBox = new THREE.Box3();
  object.traverse((obj) => {
    if (obj.isInstancedMesh && obj.count > 0) {
      if (!obj.geometry.boundingBox) obj.geometry.computeBoundingBox();
      obj.computeBoundingBox();
      if (obj.boundingBox && !obj.boundingBox.isEmpty()) {
        instBox.copy(obj.boundingBox).applyMatrix4(obj.matrixWorld);
        box.union(instBox);
      }
      return;
    }
    if (obj.isMesh && obj.geometry) {
      if (!obj.geometry.boundingBox) obj.geometry.computeBoundingBox();
      if (obj.geometry.boundingBox && !obj.geometry.boundingBox.isEmpty()) {
        instBox.copy(obj.geometry.boundingBox).applyMatrix4(obj.matrixWorld);
        box.union(instBox);
      }
    }
  });
  if (box.isEmpty()) {
    box.setFromObject(object);
  }
  if (!box.isEmpty() && object.userData) {
    object.userData._worldBBox = box.clone();
  }
  return box;
}

function collectBoxCorners(box, out) {
  const { min, max } = box;
  out[0].set(min.x, min.y, min.z);
  out[1].set(max.x, min.y, min.z);
  out[2].set(min.x, max.y, min.z);
  out[3].set(max.x, max.y, min.z);
  out[4].set(min.x, min.y, max.z);
  out[5].set(max.x, min.y, max.z);
  out[6].set(min.x, max.y, max.z);
  out[7].set(max.x, max.y, max.z);
  return out;
}

const _fitCorners = Array.from({ length: 8 }, () => new THREE.Vector3());
const _fitRel = new THREE.Vector3();
const _fitDir = new THREE.Vector3();
const _fitRight = new THREE.Vector3();
const _fitCamUp = new THREE.Vector3();
const _fitSize = new THREE.Vector3();
const _fitCenter = new THREE.Vector3();

/**
 * Perspective zoom-to-fit: bbox fully inside vertical AND horizontal FOV,
 * then × paddingFactor (1.05 = +5% frame). Preserves current view angle.
 *
 * @param {THREE.Object3D} object mesh, Group, or InstancedMesh root
 * @param {THREE.PerspectiveCamera} camera
 * @param {import("three/addons/controls/OrbitControls.js").OrbitControls|null} controls
 * @param {number} [paddingFactor=1.05]
 * @returns {{ center: THREE.Vector3, size: THREE.Vector3, dist: number, box: THREE.Box3 } | null}
 */
/**
 * Strict top-down (world −Z look, screen-up = +Y / beam).
 * camera.up cannot stay on +Z — that is parallel to the look axis.
 */
function applyOverheadViewAngle(camera, controls, center) {
  camera.up.set(0, 1, 0);
  camera.position.set(center.x, center.y, center.z + 1);
  camera.lookAt(center);
  if (controls) controls.target.copy(center);
}

/**
 * Orthographic zoom-to-fit: AABB projected onto camera right/up, then × padding.
 * left/right/top/bottom — not fov/distance. Position only sets near/far slab.
 */
export function fitOrthographicCameraToBoundingBox(
  object,
  camera,
  controls,
  paddingFactor = FIT_PADDING
) {
  if (!object || !camera || !camera.isOrthographicCamera) return null;
  const box = computeObjectBoundingBox(object);
  if (box.isEmpty()) return null;
  const size = box.getSize(_fitSize);
  const center = box.getCenter(_fitCenter);
  if (![size.x, size.y, size.z, center.x, center.y, center.z].every(Number.isFinite)) {
    return null;
  }
  if (size.length() <= MIN_MESH_EXTENT) return null;

  const pad = Number.isFinite(paddingFactor) && paddingFactor > 0 ? paddingFactor : FIT_PADDING;
  const target = controls?.target || center;
  _fitDir.copy(camera.position).sub(target);
  if (_fitDir.lengthSq() < 1e-12) _fitDir.set(0, 0, 1);
  _fitDir.normalize();

  _fitRight.crossVectors(camera.up, _fitDir);
  if (_fitRight.lengthSq() < 1e-12) {
    _fitRight.crossVectors(new THREE.Vector3(0, 1, 0), _fitDir);
  }
  if (_fitRight.lengthSq() < 1e-12) {
    _fitRight.crossVectors(new THREE.Vector3(1, 0, 0), _fitDir);
  }
  _fitRight.normalize();
  _fitCamUp.crossVectors(_fitDir, _fitRight).normalize();

  collectBoxCorners(box, _fitCorners);
  let maxX = 0;
  let maxY = 0;
  for (let i = 0; i < 8; i++) {
    _fitRel.subVectors(_fitCorners[i], center);
    maxX = Math.max(maxX, Math.abs(_fitRel.dot(_fitRight)));
    maxY = Math.max(maxY, Math.abs(_fitRel.dot(_fitCamUp)));
  }
  const aspect = Math.max(
    1e-4,
    (camera.right - camera.left) / Math.max(1e-6, camera.top - camera.bottom) ||
      camera.aspect ||
      1
  );
  let halfW = Math.max(maxX, 1e-4) * pad;
  let halfH = Math.max(maxY, 1e-4) * pad;
  if (halfW / halfH > aspect) halfH = halfW / aspect;
  else halfW = halfH * aspect;

  camera.left = -halfW;
  camera.right = halfW;
  camera.top = halfH;
  camera.bottom = -halfH;
  camera.zoom = 1;
  const along = Math.max(size.z, size.x, size.y, 0.5) + 1.5;
  camera.position.copy(center).addScaledVector(_fitDir, along);
  camera.lookAt(center);
  camera.updateProjectionMatrix();
  if (controls) {
    controls.target.copy(center);
    controls.update();
  }
  return {
    center: center.clone(),
    size: size.clone(),
    dist: along,
    box: box.clone(),
    halfW,
    halfH,
  };
}

export function fitCameraToBoundingBox(object, camera, controls, paddingFactor = FIT_PADDING) {
  if (!object || !camera) return null;
  const box = computeObjectBoundingBox(object);
  if (box.isEmpty()) return null;

  const size = box.getSize(_fitSize);
  const center = box.getCenter(_fitCenter);
  if (![size.x, size.y, size.z, center.x, center.y, center.z].every(Number.isFinite)) {
    return null;
  }
  if (size.length() <= MIN_MESH_EXTENT) return null;

  const pad = Number.isFinite(paddingFactor) && paddingFactor > 0 ? paddingFactor : FIT_PADDING;
  const target = controls?.target || center;
  _fitDir.copy(camera.position).sub(target);
  if (_fitDir.lengthSq() < 1e-12) {
    _fitDir.set(DEFAULT_VIEW_DIR.x, DEFAULT_VIEW_DIR.y, DEFAULT_VIEW_DIR.z);
  }
  _fitDir.normalize();

  const fovY = THREE.MathUtils.degToRad(Math.max(1e-3, camera.fov || 50));
  const aspect = Math.max(1e-4, camera.aspect || 1);
  const halfH = Math.tan(fovY / 2);
  const halfW = halfH * aspect;

  // Three.js lookAt basis: +Z = position − target (= dir), +X = up × Z, +Y = Z × X
  _fitRight.crossVectors(camera.up, _fitDir);
  if (_fitRight.lengthSq() < 1e-12) {
    _fitRight.crossVectors(new THREE.Vector3(0, 1, 0), _fitDir);
  }
  if (_fitRight.lengthSq() < 1e-12) {
    _fitRight.crossVectors(new THREE.Vector3(1, 0, 0), _fitDir);
  }
  _fitRight.normalize();
  _fitCamUp.crossVectors(_fitDir, _fitRight).normalize();

  collectBoxCorners(box, _fitCorners);
  let needed = 0;
  for (let i = 0; i < 8; i++) {
    _fitRel.subVectors(_fitCorners[i], center);
    // Perspective: near-side corners enlarge in NDC — include depth term
    // so the whole AABB stays inside both FOV axes (not just the center plane).
    const along = _fitRel.dot(_fitDir);
    const x = Math.abs(_fitRel.dot(_fitRight));
    const y = Math.abs(_fitRel.dot(_fitCamUp));
    needed = Math.max(needed, x / halfW + along, y / halfH + along);
  }
  const dist = Math.max(needed, 1e-4) * pad;

  camera.position.copy(center).addScaledVector(_fitDir, dist);
  camera.lookAt(center);
  if (controls) {
    controls.target.copy(center);
    controls.update();
  }
  return {
    center: center.clone(),
    size: size.clone(),
    dist,
    box: box.clone(),
  };
}

function applyDefaultViewAngle(camera, controls, center) {
  const dir = new THREE.Vector3(DEFAULT_VIEW_DIR.x, DEFAULT_VIEW_DIR.y, DEFAULT_VIEW_DIR.z).normalize();
  camera.up.set(NAVAL_CAMERA_UP.x, NAVAL_CAMERA_UP.y, NAVAL_CAMERA_UP.z);
  camera.position.copy(center).add(dir);
  camera.lookAt(center);
  if (controls) controls.target.copy(center);
}

/**
 * Modal / host GLB viewer. One instance per mount; dispose() on close.
 */
export class Top10GlbViewer {
  constructor(hostEl, vessel, opts = {}) {
    this.host = hostEl;
    this.vessel = vessel;
    this.opts = opts;
    this.disposed = false;
    this.wireframe = false;
    this.measureOn = false;
    this._raf = 0;
    this._root = null;
    this._meshes = [];
    this._homeTarget = new THREE.Vector3(0, 0.35, 0);
    this._homePosition = new THREE.Vector3(3.4, 1.7, 3.6);
    this._homeNear = 0.05;
    this._homeFar = 200;
    this._homeMinDist = MIN_DIST;
    this._homeMaxDist = MAX_DIST;
    this._resetRaf = 0;
    this._userInteracted = false;
    this._viewPreset = "catalog";
    this.ok = false;
    this._failReason = "";
  }

  _triggerFallback(reason) {
    this._failReason = reason || "glb";
    console.warn(
      `[Sentinel 3D] Degenerate/failed mesh IMO ${this.vessel?.imo || "?"} — ${this._failReason}. Falling back to Tri-View.`
    );
    this.host?.classList.remove("is-glb-ready");
    this.host?.classList.add("is-glb-fallback");
    if (typeof this.opts.onFallback === "function") {
      try {
        this.opts.onFallback(this._failReason);
      } catch {
        /* swallow */
      }
    }
  }

  /** Alias for UI state machine — same as _triggerFallback. */
  triggerTriViewFallback(reason) {
    this._triggerFallback(reason);
  }

  async mount() {
    if (!this.host || this.disposed) return false;
    try {
      const canvas = document.createElement("canvas");
      canvas.className = "t10-glb-canvas";
      canvas.setAttribute("aria-label", `3D model IMO ${this.vessel.imo}`);
      this.host.appendChild(canvas);

      const renderer = new THREE.WebGLRenderer({
        canvas,
        antialias: true,
        alpha: false,
        preserveDrawingBuffer: true, // readable canvas / no black snapshot after present
        powerPreference: "high-performance",
      });
      // Immediate non-zero backbuffer — host may still be laying out the modal
      const bootBox = measureHostBox(this.host);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setSize(bootBox.w, bootBox.h, true);
      renderer.setClearColor(0x0b1220, 1);
      // Three r152+: SRGBColorSpace supersedes legacy sRGBEncoding
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 0.95;
      renderer.shadowMap.enabled = true;
      renderer.shadowMap.type = THREE.PCFSoftShadowMap;

      const scene = new THREE.Scene();
      scene.background = new THREE.Color(0x0b1220);
      // Unit-scale hull (~2) — keep fog far so it never crush silhouette
      scene.fog = new THREE.Fog(0x0b1220, 12, 40);

      const camera = new THREE.PerspectiveCamera(38, bootBox.w / bootBox.h, 0.05, 200);
      // Z-up naval frame (matches carve / RawMesh) — never remap mesh to Y-up
      camera.up.set(NAVAL_CAMERA_UP.x, NAVAL_CAMERA_UP.y, NAVAL_CAMERA_UP.z);
      camera.position.copy(this._homePosition);

      // Spec floor: Ambient ≥1.2, primary Directional ≥2.0 (angled key)
      const ambient = new THREE.AmbientLight(0xd0dbe8, 1.25);
      scene.add(ambient);

      const hemi = new THREE.HemisphereLight(0xe8f0f8, 0x1a2433, 0.45);
      scene.add(hemi);

      // Key ≈ sunlight on ortho Side shots (warm; +Y beam, +Z above in Z-up)
      const key = new THREE.DirectionalLight(0xfff1de, 2.15);
      key.position.set(4, 14, 16);
      key.castShadow = true;
      key.shadow.mapSize.set(2048, 2048);
      key.shadow.camera.near = 0.5;
      key.shadow.camera.far = 100;
      key.shadow.camera.left = -18;
      key.shadow.camera.right = 18;
      key.shadow.camera.top = 18;
      key.shadow.camera.bottom = -18;
      key.shadow.bias = -0.00025;
      scene.add(key);

      // Fill — opposite cooler, softer (reads volume without flattening)
      const fill = new THREE.DirectionalLight(0xb4c8dc, 0.85);
      fill.position.set(-12, -6, 8);
      scene.add(fill);

      // Soft rim for silhouette separation from dark HUD bg
      const rim = new THREE.DirectionalLight(0x9ec9e0, 0.55);
      rim.position.set(-2, -14, 10);
      scene.add(rim);

      try {
        scene.environment = makeStudioEnvMap(renderer);
      } catch {
        /* env optional */
      }

      // Waterplane in XY (Z-up) — do NOT rotate to XZ (that was Y-up convention)
      const ground = new THREE.Mesh(
        new THREE.CircleGeometry(10, 64),
        new THREE.ShadowMaterial({ opacity: 0.42 })
      );
      ground.position.z = 0;
      ground.receiveShadow = true;
      scene.add(ground);
      this._ground = ground;

      const controls = new OrbitControls(camera, canvas);
      controls.enableDamping = true;
      controls.dampingFactor = 0.06;
      controls.enablePan = true;
      controls.enableZoom = true;
      controls.autoRotate = false; // start as still "4th photo"; user can enable Orbit
      controls.autoRotateSpeed = AUTO_ROTATE_SPEED;
      controls.minDistance = MIN_DIST;
      controls.maxDistance = MAX_DIST;
      // Z-up naval frame: polar measured from +Z. Allow full orbit except
      // locking exactly through the up-pole singularity.
      controls.minPolarAngle = 0.08;
      controls.maxPolarAngle = Math.PI - 0.08;
      controls.target.copy(this._homeTarget);
      this._onControlStart = () => {
        this._userInteracted = true;
      };
      controls.addEventListener("start", this._onControlStart);

      this.renderer = renderer;
      this.scene = scene;
      this.camera = camera;
      this._perspCamera = camera;
      this.controls = controls;
      this.canvas = canvas;

      canvas.addEventListener(
        "webglcontextlost",
        (e) => {
          e.preventDefault();
          this._onContextLost();
        },
        false
      );

      this._buildHud();
      this._resize();
      this._onResize = () => this._resize();
      window.addEventListener("resize", this._onResize);

      const url = resolveGlbUrl(this.vessel);
      const loaded = await this._loadGlb(url);
      if (!loaded || this.disposed) {
        this._triggerFallback(this._failReason || "glb-load");
        this.dispose();
        return false;
      }

      this.ok = true;
      this.host.classList.add("is-glb-ready");
      this.host.classList.remove("is-glb-fallback");
      requestAnimationFrame(() => {
        if (this.disposed || !this._root) return;
        this._resize();
      });
      this._tick();
      return true;
    } catch (err) {
      console.error(`[Sentinel 3D] Failed to mount GLB for IMO ${this.vessel?.imo}:`, err);
      this._triggerFallback("mount-error");
      this.dispose();
      return false;
    }
  }

  /**
   * Default home pose = catalog ¾, then shared zoom-to-fit × 1.05.
   * CAMERA ONLY — never writes mesh.rotation / mesh.scale.
   * Axis contract: world X=LOA, Y=Beam, Z=height (catalog camera.up = +Z).
   * Overhead preset: OrthographicCamera looking −Z, camera.up = +Y.
   */
  _frameCameraToRoot(root) {
    if (!root || !this.camera || !this.controls) return;

    root.updateWorldMatrix(true, true);
    const box = computeObjectBoundingBox(root);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z, 0.001);

    if (this._viewPreset === "overhead") {
      this._activateOverheadCamera();
      applyOverheadViewAngle(this.camera, this.controls, center);
      const fitted = fitOrthographicCameraToBoundingBox(root, this.camera, this.controls, FIT_PADDING);
      if (!fitted) return;
      this.camera.near = Math.max(1e-4, maxDim / 100);
      this.camera.far = Math.max(maxDim * 40, this.camera.near * 200);
      this.camera.updateProjectionMatrix();
      this._homeTarget.copy(fitted.center);
      this._homePosition.copy(this.camera.position);
      this._homeNear = this.camera.near;
      this._homeFar = this.camera.far;
      if (this._ground) this._ground.visible = false;
      this.controls.update();
      return;
    }

    this._activateCatalogCamera();
    this.camera.near = Math.max(1e-4, maxDim / 100);
    this.camera.far = Math.max(maxDim * 100, this.camera.near * 2000);
    this.camera.updateProjectionMatrix();

    applyDefaultViewAngle(this.camera, this.controls, center);
    const fitted = fitCameraToBoundingBox(root, this.camera, this.controls, FIT_PADDING);
    if (!fitted) return;

    this._homeTarget.copy(fitted.center);
    this._homePosition.copy(this.camera.position);
    this._homeNear = this.camera.near;
    this._homeFar = this.camera.far;

    const dist = fitted.dist;
    this.controls.minDistance = Math.max(this.camera.near * 8, maxDim * 0.22, 0.15);
    this.controls.maxDistance = Math.max(
      this.controls.minDistance * 10,
      maxDim * 14,
      dist * 4.5,
      MIN_DIST
    );
    this._homeMinDist = this.controls.minDistance;
    this._homeMaxDist = this.controls.maxDistance;
    this.controls.update();

    if (this._ground) {
      this._ground.visible = true;
      this._ground.rotation.set(0, 0, 0);
      this._ground.position.set(fitted.center.x, fitted.center.y, box.min.z - 0.02);
      const gScale = Math.max(6, maxDim * 1.4);
      this._ground.scale.setScalar(gScale / 10);
    }

    try {
      window.__T10_FIT_DEBUG__ = {
        imo: this.vessel?.imo || null,
        paddingFactor: FIT_PADDING,
        dist,
        size: fitted.size.toArray(),
        center: fitted.center.toArray(),
        cameraPos: this.camera.position.toArray(),
        target: this.controls.target.toArray(),
        aspect: this.camera.aspect,
        fov: this.camera.fov,
      };
    } catch {
      /* */
    }
  }

  _hostAspect() {
    const { w, h } = measureHostBox(this.host);
    return w / Math.max(h, 1);
  }

  _activateCatalogCamera() {
    if (!this._perspCamera) {
      this._perspCamera = new THREE.PerspectiveCamera(38, this._hostAspect(), 0.05, 200);
    }
    this._perspCamera.aspect = this._hostAspect();
    this._perspCamera.up.set(NAVAL_CAMERA_UP.x, NAVAL_CAMERA_UP.y, NAVAL_CAMERA_UP.z);
    this.camera = this._perspCamera;
    if (this.controls) {
      this.controls.object = this.camera;
      this.controls.enableRotate = true;
      this.controls.minPolarAngle = 0.08;
      this.controls.maxPolarAngle = Math.PI - 0.08;
    }
  }

  _activateOverheadCamera() {
    const aspect = this._hostAspect();
    if (!this._orthoCamera) {
      this._orthoCamera = new THREE.OrthographicCamera(-aspect, aspect, 1, -1, 0.05, 200);
    }
    this._orthoCamera.left = -aspect;
    this._orthoCamera.right = aspect;
    this._orthoCamera.top = 1;
    this._orthoCamera.bottom = -1;
    this._orthoCamera.up.set(0, 1, 0);
    this.camera = this._orthoCamera;
    if (this.controls) {
      this.controls.object = this.camera;
      this.controls.enableRotate = false;
      this.controls.minPolarAngle = 0;
      this.controls.maxPolarAngle = Math.PI;
    }
  }

  /**
   * 'catalog' = ¾ PerspectiveCamera (Digital Twin / Voxel tabs).
   * 'overhead' = −Z OrthographicCamera (OVERHEAD tab only).
   */
  setViewPreset(preset) {
    const next = preset === "overhead" ? "overhead" : "catalog";
    this._viewPreset = next;
    this._userInteracted = false;
    this._cancelResetTween();
    if (this._root) this._frameCameraToRoot(this._root);
    try {
      window.__T10_VIEW__ = {
        preset: next,
        isOrthographic: !!this.camera?.isOrthographicCamera,
        isPerspective: !!this.camera?.isPerspectiveCamera,
        up: this.camera?.up?.toArray?.() || null,
        pos: this.camera?.position?.toArray?.() || null,
      };
    } catch {
      /* */
    }
    return next;
  }

  _cancelResetTween() {
    if (this._resetRaf) {
      cancelAnimationFrame(this._resetRaf);
      this._resetRaf = 0;
    }
  }

  /**
   * HUD Reset Camera — recompute fitted ¾ pose, then tween back to it.
   * Same zoom-to-fit × 1.05 as first open (not a separate hardcoded camera).
   */
  resetCamera() {
    if (!this.camera || !this.controls || this.disposed) return;
    this._cancelResetTween();
    if (this._viewPreset === "overhead") {
      if (this._root) this._frameCameraToRoot(this._root);
      this._userInteracted = false;
      this.controls.autoRotate = false;
      this._hud?.querySelector('[data-act="orbit"]')?.classList.remove("is-on");
      return;
    }

    const startPos = this.camera.position.clone();
    const startTarget = this.controls.target.clone();
    const startNear = this.camera.near;
    const startFar = this.camera.far;
    if (this._root) {
      this._frameCameraToRoot(this._root);
    }
    const endPos = this._homePosition.clone();
    const endTarget = this._homeTarget.clone();
    this.camera.position.copy(startPos);
    this.controls.target.copy(startTarget);
    this.camera.near = startNear;
    this.camera.far = startFar;
    this.camera.updateProjectionMatrix();

    const t0 = performance.now();
    const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);

    this.controls.minDistance = this._homeMinDist;
    this.controls.maxDistance = this._homeMaxDist;
    this.controls.autoRotate = false;
    this._userInteracted = false;

    const step = (now) => {
      if (this.disposed || !this.camera || !this.controls) {
        this._resetRaf = 0;
        return;
      }
      const u = Math.min(1, (now - t0) / RESET_MS);
      const e = easeOutCubic(u);
      this.camera.position.lerpVectors(startPos, endPos, e);
      this.controls.target.lerpVectors(startTarget, endTarget, e);
      this.camera.near = THREE.MathUtils.lerp(startNear, this._homeNear, e);
      this.camera.far = THREE.MathUtils.lerp(startFar, this._homeFar, e);
      this.camera.updateProjectionMatrix();
      this.controls.update();
      if (u < 1) {
        this._resetRaf = requestAnimationFrame(step);
      } else {
        this._resetRaf = 0;
        // Canonical home pose stays still — Orbit is an explicit user toggle.
        this.controls.autoRotate = false;
        this._hud?.querySelector('[data-act="orbit"]')?.classList.remove("is-on");
      }
    };
    this._resetRaf = requestAnimationFrame(step);
  }

  _loadGlb(url) {
    return new Promise((resolve) => {
      const loader = new GLTFLoader();
      const done = (ok, reason) => {
        if (!ok && reason) this._failReason = reason;
        resolve(!!ok);
      };
      try {
        loader.load(
          url,
          (gltf) => {
            if (this.disposed) return done(false, "disposed");
            const root = gltf.scene || gltf.scenes?.[0];
            if (!root) return done(false, "empty-scene");

            let meshCount = 0;
            let geoOk = 0;
            root.traverse((obj) => {
              if (!obj.isMesh) return;
              meshCount += 1;
              obj.castShadow = true;
              obj.receiveShadow = true;
              this._meshes.push(obj);

              if (obj.geometry) {
                if (sanitizeGeometry(obj.geometry)) geoOk += 1;
              }

              if (obj.material) {
                if (Array.isArray(obj.material)) {
                  obj.material = obj.material.map((m) => hardenMaterial(m));
                } else {
                  obj.material = hardenMaterial(obj.material);
                }
              } else {
                obj.material = new THREE.MeshStandardMaterial({
                  color: HULL_FALLBACK,
                  roughness: 0.72,
                  metalness: 0.05,
                  envMapIntensity: 0.55,
                  side: THREE.DoubleSide,
                });
              }
            });

            if (meshCount === 0 || geoOk === 0) {
              return done(false, "no-valid-geometry");
            }

            // Pre-scene degenerate audit (NaN / zero-area / pancake bbox)
            const preAudit = auditLoadedRoot(root);
            if (!preAudit.ok) {
              return done(false, preAudit.reason || "degenerate-pre");
            }

            let box = new THREE.Box3().setFromObject(root);
            let size = box.getSize(new THREE.Vector3());
            if (isDegenerateSize(size)) {
              return done(false, "degenerate-bbox");
            }

            // ── Axis contract (naval GLB → Three.js) ─────────────────────────
            // Mesh LOCAL = world (builder): X=LOA, Y=Beam, Z=air-draft (Z-up).
            // Framing is CAMERA-ONLY (camera.up=+Z). Forbidden: mesh.rotation,
            // anisotropic mesh.scale / heightBoost — those mutated geometry
            // presentation and caused the v3.2.2 ribbon/splinter class of bugs.
            this.camera.up.set(NAVAL_CAMERA_UP.x, NAVAL_CAMERA_UP.y, NAVAL_CAMERA_UP.z);

            const catLoa = Math.max(Number(this.vessel?.loa_m) || size.x, 1);
            const catBeam = Math.max(Number(this.vessel?.beam_m) || size.y, 0.5);
            const catalogLB = catLoa / catBeam; // ~6.4 for BU SAMRA
            const worldLB = size.x / Math.max(size.y, 1e-6); // LOA / Beam
            if (worldLB > catalogLB * 2.8 || worldLB > 18) {
              return done(false, "splinter-world-lb");
            }

            // World LOA/depth (side silhouette) — reject pancake hairline
            const worldLD = size.x / Math.max(size.z, 1e-6);
            if (worldLD > 18) {
              return done(false, "pancake-world-ld");
            }
            // Persist measured world ratios for Spec Plate / QA (Z-up frame)
            root.userData.presWorld = {
              loa: size.x,
              beam: size.y,
              depth: size.z,
              lb: worldLB,
              ld: worldLD,
              catalogLB,
            };

            const postAudit = auditLoadedRoot(root);
            if (!postAudit.ok) {
              return done(false, postAudit.reason || "degenerate-post");
            }

            this.scene.add(root);
            this._root = root;
            // Debug telemetry for architecture gates (read-only)
            try {
              window.__T10_GLB_DEBUG__ = {
                imo: this.vessel?.imo,
                scale: root.scale.toArray(),
                rotation: [root.rotation.x, root.rotation.y, root.rotation.z],
                position: root.position.toArray(),
                cameraUp: this.camera.up.toArray(),
                cameraPos: this.camera.position.toArray(),
                target: this.controls.target.toArray(),
                presWorld: root.userData.presWorld || null,
              };
            } catch {
              /* */
            }
            this._frameCameraToRoot(root);
            // Hard-lock presentation invariants after mount
            this.controls.autoRotate = false;
            root.scale.set(1, 1, 1);
            root.rotation.set(0, 0, 0);
            try {
              if (window.__T10_GLB_DEBUG__) {
                window.__T10_GLB_DEBUG__.cameraPos = this.camera.position.toArray();
                window.__T10_GLB_DEBUG__.target = this.controls.target.toArray();
                window.__T10_GLB_DEBUG__.cameraUp = this.camera.up.toArray();
                window.__T10_GLB_DEBUG__.scale = root.scale.toArray();
                window.__T10_GLB_DEBUG__.rotation = [
                  root.rotation.x,
                  root.rotation.y,
                  root.rotation.z,
                ];
              }
            } catch {
              /* */
            }
            done(true);
          },
          undefined,
          (err) => {
            console.error(`[Sentinel 3D] Failed to load GLB for IMO ${this.vessel?.imo}:`, err);
            done(false, "glb-http");
          }
        );
      } catch (err) {
        console.error(`[Sentinel 3D] GLB loader exception:`, err);
        done(false, "glb-exception");
      }
    });
  }

  _buildHud() {
    const hud = document.createElement("div");
    hud.className = "t10-glb-hud";
    hud.innerHTML = `
      <div class="t10-glb-fidelity" title="Photo-composite on simplified hull — not a verified structural CAD model">PHOTO-COMPOSITE 3D RECONSTRUCTION · TEXTURE FROM ORTHO TRIPLET · NOT VERIFIED STRUCTURAL MODEL</div>
      <button type="button" class="t10-glb-btn" data-act="orbit" title="Toggle auto-orbit">Orbit</button>
      <button type="button" class="t10-glb-btn" data-act="wire" title="Wireframe">Wireframe</button>
      <button type="button" class="t10-glb-btn" data-act="measure" title="Catalog LOA / Beam / Draft (not mesh-measured)">Spec Plate</button>
      <button type="button" class="t10-glb-btn" data-act="reset" title="Reset camera">Reset Camera</button>
      <div class="t10-glb-measure" hidden></div>`;
    this.host.appendChild(hud);
    this._hud = hud;
    this._measureEl = hud.querySelector(".t10-glb-measure");
    hud.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const act = btn.getAttribute("data-act");
      if (act === "orbit") {
        this.controls.autoRotate = !this.controls.autoRotate;
        btn.classList.toggle("is-on", this.controls.autoRotate);
      } else if (act === "wire") {
        this.wireframe = !this.wireframe;
        this._meshes.forEach((m) => {
          const mats = Array.isArray(m.material) ? m.material : [m.material];
          mats.forEach((mat) => {
            if (mat) mat.wireframe = this.wireframe;
          });
        });
        btn.classList.toggle("is-on", this.wireframe);
      } else if (act === "measure") {
        this.measureOn = !this.measureOn;
        btn.classList.toggle("is-on", this.measureOn);
        this._updateMeasure();
      } else if (act === "reset") {
        this.resetCamera();
      }
    });
    hud.querySelector('[data-act="orbit"]')?.classList.remove("is-on");
    // Solid shaded by default — do NOT enable wireframe or auto-orbit on mount
    this.controls.autoRotate = false;
    this._updateMeasure();
  }

  _updateMeasure() {
    if (!this._measureEl) return;
    if (!this.measureOn) {
      this._measureEl.hidden = true;
      return;
    }
    const v = this.vessel || {};
    this._measureEl.hidden = false;
    this._measureEl.innerHTML = `
      <div>LOA <strong>${Number(v.loa_m || 0).toFixed(1)} m</strong></div>
      <div>BEAM <strong>${Number(v.beam_m || 0).toFixed(1)} m</strong></div>
      <div>DRAFT <strong>${Number(v.draft_m || 0).toFixed(1)} m</strong></div>
      <div>IMO <strong>${v.imo || "—"}</strong></div>`;
  }

  _resize() {
    if (!this.renderer || !this.host || !this.camera) return;
    const { w, h } = measureHostBox(this.host);
    // updateStyle=true — keep CSS box aspect locked to drawing buffer (no squash)
    this.renderer.setSize(w, h, true);
    if (this.camera.isPerspectiveCamera) {
      this.camera.aspect = w / Math.max(h, 1);
    }
    this.camera.updateProjectionMatrix();
    // Re-fit only the default pose — never steal a live OrbitControls session
    if (this._root && this.ok && !this._userInteracted) {
      this._frameCameraToRoot(this._root);
    }
  }

  _tick = () => {
    if (this.disposed || !this.ok) return;
    this._raf = requestAnimationFrame(this._tick);
    this.controls?.update();
    this.renderer?.render(this.scene, this.camera);
  };

  _onContextLost() {
    this.ok = false;
    this._triggerFallback("contextlost");
    this.dispose();
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.ok = false;
    this._cancelResetTween();
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = 0;
    if (this._onResize) window.removeEventListener("resize", this._onResize);
    try {
      if (this._onControlStart) this.controls?.removeEventListener("start", this._onControlStart);
    } catch {
      /* */
    }
    try {
      this.controls?.dispose();
    } catch {
      /* */
    }
    try {
      this.scene?.traverse((obj) => {
        if (obj.geometry) obj.geometry.dispose?.();
        if (obj.material) {
          const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
          mats.forEach((m) => {
            m.map?.dispose?.();
            m.dispose?.();
          });
        }
      });
    } catch {
      /* */
    }
    try {
      this.renderer?.dispose();
      // Do NOT forceContextLoss — sequential open/close would burn the browser's
      // WebGL context budget. Geometry/material dispose above is enough.
    } catch {
      /* */
    }
    this._hud?.remove();
    this.canvas?.remove();
    this.host?.classList.remove("is-glb-ready");
    this.renderer = null;
    this.scene = null;
    this.camera = null;
    this.controls = null;
    this._root = null;
    this._meshes = [];
  }
}

/** True when manifest marks a ready .glb (never invent availability). */
export function isGlbReady(vessel) {
  return vessel?.glb?.ready === true && !!(vessel?.glb?.url || vessel?.imo);
}

/**
 * Single live modal GLB instance for the whole app.
 * Cards never mount GLTFLoader — only this path does (lazy, one at a time).
 */
let _activeModalViewer = null;

/** Try mount GLB viewer; returns viewer or null if ortho fallback required. */
export async function mountTop10GlbViewer(hostEl, vessel, opts = {}) {
  if (!hostEl || !vessel) return null;
  if (!isGlbReady(vessel)) {
    console.info(
      `[Sentinel 3D] GLB unavailable for IMO ${vessel?.imo || "?"} (ready=${vessel?.glb?.ready})`
    );
    return null;
  }
  // Tear down any previous modal instance before creating a new one (single context).
  if (_activeModalViewer && !_activeModalViewer.disposed) {
    try {
      _activeModalViewer.dispose();
    } catch {
      /* */
    }
    _activeModalViewer = null;
  }
  if (!isWebGLCapable()) return null;
  const viewer = new Top10GlbViewer(hostEl, vessel, opts);
  _activeModalViewer = viewer;
  const ok = await viewer.mount();
  if (!ok) {
    viewer.dispose();
    if (_activeModalViewer === viewer) _activeModalViewer = null;
    return null;
  }
  const origDispose = viewer.dispose.bind(viewer);
  viewer.dispose = (...args) => {
    origDispose(...args);
    if (_activeModalViewer === viewer) _activeModalViewer = null;
  };
  return viewer;
}

export default {
  Top10GlbViewer,
  mountTop10GlbViewer,
  isGlbReady,
  isVoxelCubesReady,
  mountTop10VoxelCubeViewer,
  fitCameraToBoundingBox,
  fitOrthographicCameraToBoundingBox,
  computeObjectBoundingBox,
  FIT_PADDING,
};

function resolveVoxelUrl(vessel) {
  const imo = String(vessel?.imo || "");
  let path = String(
    vessel?.voxel_cubes?.url || `/output/assets/3d_models/vessel_${imo}_voxels.json`
  );
  if (!path.startsWith("/") && !/^https?:/i.test(path)) path = `/${path}`;
  const ver = vessel?.voxel_cubes?.bytes || vessel?.voxel_cubes?.n_occupied || "1";
  const join = path.includes("?") ? "&" : "?";
  return `${path}${join}v=${encodeURIComponent(String(ver))}`;
}

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** True when manifest marks ready voxel-cube JSON. */
export function isVoxelCubesReady(vessel) {
  return vessel?.voxel_cubes?.ready === true && !!(vessel?.voxel_cubes?.url || vessel?.imo);
}

/**
 * InstancedMesh cube-grid viewer — same shared WebGL singleton slot as GLB.
 * Never mounts alongside Digital Twin; dispose tears down the only live context.
 */
export class Top10VoxelCubeViewer {
  constructor(hostEl, vessel, opts = {}) {
    this.host = hostEl;
    this.vessel = vessel;
    this.opts = opts;
    this.disposed = false;
    this.ok = false;
    this._raf = 0;
    this._root = null;
    this._failReason = "";
    this._homeTarget = new THREE.Vector3(0, 0.35, 0);
    this._homePosition = new THREE.Vector3(3.4, 1.7, 3.6);
    this._homeNear = 0.05;
    this._homeFar = 200;
    this._homeMinDist = MIN_DIST;
    this._homeMaxDist = MAX_DIST;
    this._resetRaf = 0;
    this._userInteracted = false;
    this._viewPreset = "catalog";
    this._nCells = 0;
    this._colorsCatalog = null;
    this._colorsSat = null;
  }

  _triggerFallback(reason) {
    this._failReason = reason || "voxel";
    this.host?.classList.remove("is-glb-ready");
    this.host?.classList.add("is-glb-fallback");
    if (typeof this.opts.onFallback === "function") {
      try {
        this.opts.onFallback(this._failReason);
      } catch {
        /* */
      }
    }
  }

  async mount() {
    if (!this.host || this.disposed) return false;
    try {
      const canvas = document.createElement("canvas");
      canvas.className = "t10-glb-canvas";
      canvas.setAttribute("aria-label", `Voxel reconstruction IMO ${this.vessel.imo}`);
      this.host.appendChild(canvas);

      const renderer = new THREE.WebGLRenderer({
        canvas,
        antialias: true,
        alpha: false,
        preserveDrawingBuffer: true,
        powerPreference: "high-performance",
      });
      const bootBox = measureHostBox(this.host);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setSize(bootBox.w, bootBox.h, true);
      renderer.setClearColor(0x0b1220, 1);
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 0.95;

      const scene = new THREE.Scene();
      scene.background = new THREE.Color(0x0b1220);
      scene.fog = new THREE.Fog(0x0b1220, 12, 40);

      const camera = new THREE.PerspectiveCamera(38, bootBox.w / bootBox.h, 0.05, 200);
      camera.up.set(NAVAL_CAMERA_UP.x, NAVAL_CAMERA_UP.y, NAVAL_CAMERA_UP.z);
      camera.position.copy(this._homePosition);

      scene.add(new THREE.AmbientLight(0xd0dbe8, 1.25));
      scene.add(new THREE.HemisphereLight(0xe8f0f8, 0x1a2433, 0.45));
      const key = new THREE.DirectionalLight(0xfff1de, 2.15);
      key.position.set(4, 14, 16);
      scene.add(key);
      const fill = new THREE.DirectionalLight(0xb4c8dc, 0.85);
      fill.position.set(-12, -6, 8);
      scene.add(fill);

      const controls = new OrbitControls(camera, canvas);
      controls.enableDamping = true;
      controls.dampingFactor = 0.06;
      controls.autoRotate = false;
      controls.minDistance = MIN_DIST;
      controls.maxDistance = MAX_DIST;
      controls.minPolarAngle = 0.08;
      controls.maxPolarAngle = Math.PI - 0.08;
      controls.target.copy(this._homeTarget);
      this._onControlStart = () => {
        this._userInteracted = true;
      };
      controls.addEventListener("start", this._onControlStart);

      this.renderer = renderer;
      this.scene = scene;
      this.camera = camera;
      this._perspCamera = camera;
      this.controls = controls;
      this.canvas = canvas;

      this._buildHud();
      this._resize();
      this._onResize = () => this._resize();
      window.addEventListener("resize", this._onResize);

      const url = resolveVoxelUrl(this.vessel);
      const loaded = await this._loadVoxels(url);
      if (!loaded || this.disposed) {
        this._triggerFallback(this._failReason || "voxel-load");
        this.dispose();
        return false;
      }

      this.ok = true;
      this.host.classList.add("is-glb-ready");
      this.host.classList.remove("is-glb-fallback");
      requestAnimationFrame(() => {
        if (this.disposed || !this._root) return;
        this._resize();
      });
      this._tick();
      return true;
    } catch (err) {
      console.error(`[Sentinel Voxel] mount failed IMO ${this.vessel?.imo}:`, err);
      this._triggerFallback("mount-error");
      this.dispose();
      return false;
    }
  }

  async _loadVoxels(url) {
    const res = await fetch(url, { cache: "no-cache" });
    if (!res.ok) {
      this._failReason = "voxel-http";
      return false;
    }
    const data = await res.json();
    const n = Number(data.n_occupied || 0);
    if (!n || !data.positions_f32_b64) {
      this._failReason = "voxel-empty";
      return false;
    }
    this._nCells = n;
    this._alg = data.alg || "";
    this._paletteK =
      Number(data.palette_k) ||
      (Array.isArray(data.palette_rgb_u8) ? data.palette_rgb_u8.length : 0);
    const posBytes = b64ToBytes(data.positions_f32_b64);
    const pos = new Float32Array(
      posBytes.buffer,
      posBytes.byteOffset,
      Math.floor(posBytes.byteLength / 4)
    );
    if (pos.length < n * 3) {
      this._failReason = "voxel-decode";
      return false;
    }

    // v2: palette + indices; v1 fallback: packed RGB u8
    let colorsRgb = null; // Float32 [r,g,b] 0-1 per instance
    let colorsSat = null;
    const pal = Array.isArray(data.palette_rgb_u8) ? data.palette_rgb_u8 : null;
    if (data.color_indices_u8_b64 && pal && pal.length) {
      const idx = b64ToBytes(data.color_indices_u8_b64);
      if (idx.length < n) {
        this._failReason = "voxel-decode-idx";
        return false;
      }
      colorsRgb = new Float32Array(n * 3);
      for (let i = 0; i < n; i++) {
        const p = pal[Math.min(idx[i] | 0, pal.length - 1)] || [128, 128, 128];
        colorsRgb[i * 3] = (p[0] | 0) / 255;
        colorsRgb[i * 3 + 1] = (p[1] | 0) / 255;
        colorsRgb[i * 3 + 2] = (p[2] | 0) / 255;
      }
      if (data.sat_indices_u8_b64) {
        const sidx = b64ToBytes(data.sat_indices_u8_b64);
        if (sidx.length >= n) {
          colorsSat = new Float32Array(n * 3);
          for (let i = 0; i < n; i++) {
            const p = pal[Math.min(sidx[i] | 0, pal.length - 1)] || [128, 128, 128];
            colorsSat[i * 3] = (p[0] | 0) / 255;
            colorsSat[i * 3 + 1] = (p[1] | 0) / 255;
            colorsSat[i * 3 + 2] = (p[2] | 0) / 255;
          }
        }
      }
    } else if (data.colors_u8_b64) {
      const col = b64ToBytes(data.colors_u8_b64);
      if (col.length < n * 3) {
        this._failReason = "voxel-decode";
        return false;
      }
      colorsRgb = new Float32Array(n * 3);
      for (let i = 0; i < n; i++) {
        colorsRgb[i * 3] = col[i * 3] / 255;
        colorsRgb[i * 3 + 1] = col[i * 3 + 1] / 255;
        colorsRgb[i * 3 + 2] = col[i * 3 + 2] / 255;
      }
    } else {
      this._failReason = "voxel-empty-color";
      return false;
    }

    const size = Math.max(Number(data.cube_size) || 0.02, 1e-4);
    const geo = new THREE.BoxGeometry(size, size, size);
    const mat = new THREE.MeshStandardMaterial({
      roughness: 0.78,
      metalness: 0.05,
      side: THREE.FrontSide,
    });
    const mesh = new THREE.InstancedMesh(geo, mat, n);
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    const dummy = new THREE.Object3D();
    const c = new THREE.Color();
    for (let i = 0; i < n; i++) {
      dummy.position.set(pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]);
      dummy.updateMatrix();
      mesh.setMatrixAt(i, dummy.matrix);
      c.setRGB(colorsRgb[i * 3], colorsRgb[i * 3 + 1], colorsRgb[i * 3 + 2]);
      mesh.setColorAt(i, c);
    }
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    mesh.frustumCulled = true;
    mesh.material.depthTest = true;
    mesh.material.depthWrite = true;

    const root = new THREE.Group();
    root.name = `voxel_cubes_${this.vessel.imo}`;
    root.add(mesh);
    this.scene.add(root);
    this._root = root;
    this._instanced = mesh;
    this._matStd = mat;
    this._colorsCatalog = colorsRgb;
    this._colorsSat = colorsSat;

    this._frameCameraToRoot(root);
    this._applyOverheadPresentation(this._viewPreset === "overhead");
    this._updateFidelityBadge();
    return true;
  }

  _frameCameraToRoot(root) {
    return Top10GlbViewer.prototype._frameCameraToRoot.call(this, root);
  }

  setViewPreset(preset) {
    const next = Top10GlbViewer.prototype.setViewPreset.call(this, preset);
    this._applyOverheadPresentation(next === "overhead");
    this._updateFidelityBadge?.();
    return next;
  }

  _applyInstanceColors(buf) {
    const mesh = this._instanced;
    if (!mesh || !buf) return;
    const n = this._nCells | 0;
    const c = new THREE.Color();
    for (let i = 0; i < n; i++) {
      c.setRGB(buf[i * 3], buf[i * 3 + 1], buf[i * 3 + 2]);
      mesh.setColorAt(i, c);
    }
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  }

  _applyOverheadPresentation(on) {
    if (!this._instanced) return;
    this._applyInstanceColors(on && this._colorsSat ? this._colorsSat : this._colorsCatalog);
    const mat = this._instanced.material;
    if (mat) {
      mat.depthTest = true;
      mat.depthWrite = true;
      mat.side = THREE.FrontSide;
    }
  }

  _hostAspect() {
    return Top10GlbViewer.prototype._hostAspect.call(this);
  }

  _activateCatalogCamera() {
    return Top10GlbViewer.prototype._activateCatalogCamera.call(this);
  }

  _activateOverheadCamera() {
    return Top10GlbViewer.prototype._activateOverheadCamera.call(this);
  }

  resetCamera() {
    return Top10GlbViewer.prototype.resetCamera.call(this);
  }

  _cancelResetTween() {
    return Top10GlbViewer.prototype._cancelResetTween.call(this);
  }

  _updateFidelityBadge() {
    const el = this._hud?.querySelector?.(".t10-glb-fidelity");
    if (!el) return;
    const n = this._nCells || this.vessel?.voxel_cubes?.n_occupied || "?";
    const k =
      this._paletteK ||
      this.vessel?.voxel_cubes?.palette_k ||
      (this.vessel?.voxel_cubes?.palette_hex || []).length ||
      "?";
    const pal = Number(k) > 0 ? ` · ${k}-COLOR PALETTE` : "";
    const view =
      this._viewPreset === "overhead"
        ? " · ORTHOGRAPHIC TOP-DOWN · SAT DECK COLORS"
        : "";
    el.textContent = `VOXEL GRID RECONSTRUCTION · ~${n} CELLS${pal}${view} · DERIVED FROM 3-VIEW SILHOUETTE OCCUPANCY · NOT A CONTINUOUS SURFACE MODEL`;
    el.title =
      "Equal-metric occupancy cubes from validated carve — not a continuous mesh / CAD model";
  }

  _buildHud() {
    const hud = document.createElement("div");
    hud.className = "t10-glb-hud";
    hud.innerHTML = `
      <div class="t10-glb-fidelity" title="Voxel occupancy grid">VOXEL GRID RECONSTRUCTION · DERIVED FROM 3-VIEW SILHOUETTE OCCUPANCY · NOT A CONTINUOUS SURFACE MODEL</div>
      <button type="button" class="t10-glb-btn" data-act="orbit" title="Toggle auto-orbit">Orbit</button>
      <button type="button" class="t10-glb-btn" data-act="reset" title="Reset camera">Reset Camera</button>`;
    this.host.appendChild(hud);
    this._hud = hud;
    hud.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const act = btn.getAttribute("data-act");
      if (act === "orbit") {
        this.controls.autoRotate = !this.controls.autoRotate;
        btn.classList.toggle("is-on", this.controls.autoRotate);
      } else if (act === "reset") {
        this.resetCamera();
        hud.querySelector('[data-act="orbit"]')?.classList.remove("is-on");
      }
    });
  }

  _resize() {
    if (!this.renderer || !this.camera || !this.host) return;
    const box = measureHostBox(this.host);
    this.renderer.setSize(box.w, box.h, true);
    if (this.camera.isPerspectiveCamera) {
      this.camera.aspect = box.w / Math.max(box.h, 1);
    }
    this.camera.updateProjectionMatrix();
    if (this._root && this.ok && !this._userInteracted) {
      this._frameCameraToRoot(this._root);
    }
  }

  _tick() {
    if (this.disposed) return;
    this._raf = requestAnimationFrame(() => this._tick());
    this.controls?.update?.();
    this.renderer?.render(this.scene, this.camera);
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.ok = false;
    this._cancelResetTween();
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = 0;
    if (this._onResize) window.removeEventListener("resize", this._onResize);
    try {
      if (this._onControlStart) this.controls?.removeEventListener("start", this._onControlStart);
    } catch {
      /* */
    }
    try {
      this.controls?.dispose();
    } catch {
      /* */
    }
    try {
      this._instanced?.geometry?.dispose?.();
      this._matStd?.dispose?.();
      this._instanced?.dispose?.();
    } catch {
      /* */
    }
    try {
      this.renderer?.dispose();
    } catch {
      /* */
    }
    this._hud?.remove();
    this.canvas?.remove();
    this.host?.classList.remove("is-glb-ready");
    this.renderer = null;
    this.scene = null;
    this.camera = null;
    this.controls = null;
    this._root = null;
    this._instanced = null;
    this._matStd = null;
    this._colorsCatalog = null;
    this._colorsSat = null;
  }
}

/** Mount voxel InstancedMesh viewer into the single shared WebGL slot. */
export async function mountTop10VoxelCubeViewer(hostEl, vessel, opts = {}) {
  if (!hostEl || !vessel) return null;
  if (!isVoxelCubesReady(vessel)) {
    console.info(
      `[Sentinel Voxel] unavailable for IMO ${vessel?.imo || "?"} (ready=${vessel?.voxel_cubes?.ready})`
    );
    return null;
  }
  if (_activeModalViewer && !_activeModalViewer.disposed) {
    try {
      _activeModalViewer.dispose();
    } catch {
      /* */
    }
    _activeModalViewer = null;
  }
  if (!isWebGLCapable()) return null;
  const viewer = new Top10VoxelCubeViewer(hostEl, vessel, opts);
  _activeModalViewer = viewer;
  const ok = await viewer.mount();
  if (!ok) {
    viewer.dispose();
    if (_activeModalViewer === viewer) _activeModalViewer = null;
    return null;
  }
  const origDispose = viewer.dispose.bind(viewer);
  viewer.dispose = (...args) => {
    origDispose(...args);
    if (_activeModalViewer === viewer) _activeModalViewer = null;
  };
  return viewer;
}

