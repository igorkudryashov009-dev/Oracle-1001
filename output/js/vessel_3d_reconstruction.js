/**
 * Oracle-1001 / Sentinel — Photogrammetric Q-Max PBR engine
 * Shared WebGL (setScissor) · MeshPhysicalMaterial · animated ocean · ortho projection
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
// GLTFLoader intentionally NOT used in card grid — modal owns .glb (top10_3d_viewer.js)

const M = 1 / 26;
const DEFAULT_LOA = 345;
const DEFAULT_BEAM = 53.8;
const DEFAULT_DRAFT = 9.5;

let sharedInstance = null;

function physical(opts = {}) {
  return new THREE.MeshPhysicalMaterial({
    color: opts.color ?? 0xc8c2b4,
    map: opts.map || null,
    metalness: opts.metalness ?? 0.22,
    roughness: opts.roughness ?? 0.32,
    clearcoat: opts.clearcoat ?? 0.5,
    clearcoatRoughness: opts.clearcoatRoughness ?? 0.28,
    envMapIntensity: opts.envMapIntensity ?? 1.0,
    transparent: !!opts.transparent,
    opacity: opts.opacity ?? 1,
    side: opts.side ?? THREE.FrontSide,
    emissive: opts.emissive ?? 0x000000,
    emissiveIntensity: opts.emissiveIntensity ?? 0,
  });
}

/** Parametric Membrane / Q-Max LNG hull with bow taper + deck features. */
export function buildMembraneLngCarrier(spec = {}, sideMap = null, bowMap = null, topMap = null) {
  const loa = Number(spec.loa_m || DEFAULT_LOA) * M;
  const beam = Number(spec.beam_m || DEFAULT_BEAM) * M;
  const draft = Number(spec.draft_m || DEFAULT_DRAFT) * M;
  const halfL = loa / 2;
  const halfB = beam / 2;
  const hullH = draft * 1.7;
  const root = new THREE.Group();
  root.name = `qmax_${spec.imo || "vessel"}`;

  [sideMap, bowMap, topMap].forEach((tex) => {
    if (!tex) return;
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping;
    tex.anisotropy = 8;
  });

  const lowerMat = physical({
    color: 0x5c1a1c, map: sideMap, metalness: 0.35, roughness: 0.42, clearcoat: 0.35,
  });
  const upperMat = physical({
    color: 0xd2c7b0, map: sideMap, metalness: 0.18, roughness: 0.38, clearcoat: 0.55,
  });
  const bowMat = physical({
    color: 0xcfc4ae, map: bowMap, metalness: 0.2, roughness: 0.34, clearcoat: 0.45,
  });
  const deckMat = physical({
    color: 0x2a303a, map: topMap, metalness: 0.55, roughness: 0.4, clearcoat: 0.25,
  });
  const tankMat = physical({
    color: 0x3e4858, metalness: 0.85, roughness: 0.22, clearcoat: 0.7, clearcoatRoughness: 0.15,
  });
  const pipeMat = physical({ color: 0x9aa3b0, metalness: 0.92, roughness: 0.18, clearcoat: 0.4 });
  const bridgeMat = physical({ color: 0xe8e4dc, metalness: 0.12, roughness: 0.36, clearcoat: 0.6 });
  const accent = physical({
    color: 0xffb300, metalness: 1, roughness: 0.25, clearcoat: 0.8,
    emissive: 0xffb300, emissiveIntensity: 0.12,
  });

  const bodyLen = loa * 0.72;
  const hull = new THREE.Group();

  // Parallel mid-body (slightly rounded via scaled boxes)
  const lower = new THREE.Mesh(new THREE.BoxGeometry(bodyLen, hullH * 0.52, beam * 0.96), lowerMat);
  lower.position.set(0, hullH * 0.02, 0);
  lower.castShadow = true;
  hull.add(lower);

  const upper = new THREE.Mesh(new THREE.BoxGeometry(bodyLen * 0.998, hullH * 0.4, beam * 0.94), upperMat);
  upper.position.set(0, hullH * 0.46, 0);
  upper.castShadow = true;
  hull.add(upper);

  // Photogrammetric side projection planes
  if (sideMap) {
    for (const side of [-1, 1]) {
      const plane = new THREE.Mesh(
        new THREE.PlaneGeometry(bodyLen * 0.99, hullH * 0.92),
        physical({
          map: sideMap, metalness: 0.2, roughness: 0.4, clearcoat: 0.35,
          transparent: true, opacity: 0.94, side: THREE.FrontSide,
        })
      );
      plane.position.set(0, hullH * 0.26, side * halfB * 0.99);
      if (side < 0) plane.rotation.y = Math.PI;
      hull.add(plane);
    }
  }

  // Bow taper — wedge + projected face
  const bowGeo = new THREE.ConeGeometry(beam * 0.48, loa * 0.15, 4);
  const bow = new THREE.Mesh(bowGeo, bowMat);
  bow.rotation.z = -Math.PI / 2;
  bow.rotation.y = Math.PI / 4;
  bow.position.set(halfL - loa * 0.045, hullH * 0.2, 0);
  bow.scale.set(1, 1.08, 0.88);
  bow.castShadow = true;
  hull.add(bow);

  if (bowMap) {
    const bowFace = new THREE.Mesh(
      new THREE.PlaneGeometry(beam * 0.82, hullH * 0.88),
      physical({ map: bowMap, metalness: 0.2, roughness: 0.36, clearcoat: 0.4, transparent: true, opacity: 0.96 })
    );
    bowFace.position.set(halfL - loa * 0.005, hullH * 0.28, 0);
    bowFace.rotation.y = Math.PI / 2;
    hull.add(bowFace);
  }

  // Stern block
  const stern = new THREE.Mesh(new THREE.BoxGeometry(loa * 0.095, hullH * 1.05, beam * 0.95), upperMat.clone());
  stern.position.set(-halfL + loa * 0.05, hullH * 0.24, 0);
  stern.castShadow = true;
  hull.add(stern);

  // Waterline titanium accent
  const wl = new THREE.Mesh(new THREE.BoxGeometry(loa * 0.97, 0.04, beam * 1.02), accent);
  wl.position.set(0, -hullH * 0.1, 0);
  hull.add(wl);

  // Deck + overhead satellite projection
  const deck = new THREE.Mesh(new THREE.PlaneGeometry(bodyLen * 0.98, beam * 0.88), deckMat);
  deck.rotation.x = -Math.PI / 2;
  deck.position.set(0, hullH * 0.7, 0);
  deck.receiveShadow = true;
  hull.add(deck);

  // Membrane tank covers + hemispheres (4)
  const tankW = beam * 0.7;
  const tankL = bodyLen * 0.155;
  [-0.3, -0.1, 0.1, 0.3].map((f) => f * bodyLen).forEach((x, i) => {
    const cover = new THREE.Mesh(new THREE.BoxGeometry(tankL, hullH * 0.32, tankW), tankMat);
    cover.position.set(x, hullH * 0.86, 0);
    cover.castShadow = true;
    hull.add(cover);
    const dome = new THREE.Mesh(
      new THREE.SphereGeometry(tankW * 0.3, 24, 14, 0, Math.PI * 2, 0, Math.PI * 0.52),
      tankMat.clone()
    );
    dome.position.set(x, hullH * 1.02, 0);
    dome.castShadow = true;
    hull.add(dome);
    // Manifold rings
    const ring = new THREE.Mesh(new THREE.TorusGeometry(tankW * 0.32, 0.03, 8, 32), accent);
    ring.rotation.x = Math.PI / 2;
    ring.position.set(x, hullH * 1.02, 0);
    hull.add(ring);
  });

  // Deck piping
  for (let i = 0; i < 5; i++) {
    const tube = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, bodyLen * 0.5, 8), pipeMat);
    tube.rotation.z = Math.PI / 2;
    tube.position.set(0, hullH * 0.76, (i - 2) * beam * 0.09);
    hull.add(tube);
  }
  const manifold = new THREE.Mesh(new THREE.BoxGeometry(loa * 0.055, 0.2, beam * 0.5), pipeMat);
  manifold.position.set(loa * 0.06, hullH * 0.82, 0);
  hull.add(manifold);

  // Bridge / accommodation
  const block = new THREE.Mesh(new THREE.BoxGeometry(loa * 0.09, hullH * 1.55, beam * 0.68), bridgeMat);
  block.position.set(-halfL + loa * 0.115, hullH * 1.12, 0);
  block.castShadow = true;
  hull.add(block);
  const wing = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.26, beam * 0.9), accent);
  wing.position.set(-halfL + loa * 0.115, hullH * 1.72, 0);
  hull.add(wing);
  const funnel = new THREE.Mesh(
    new THREE.CylinderGeometry(0.16, 0.22, hullH * 0.6, 14),
    physical({ color: 0x1a1e26, metalness: 0.9, roughness: 0.25, clearcoat: 0.3 })
  );
  funnel.position.set(-halfL + loa * 0.08, hullH * 1.55, 0);
  hull.add(funnel);

  root.add(hull);
  root.userData.dims = { loa, beam, draft, hullH };
  return root;
}

function buildOcean(loa, draft) {
  const geo = new THREE.PlaneGeometry(loa * 5.2, loa * 5.2, 64, 64);
  const mat = new THREE.ShaderMaterial({
    transparent: true,
    uniforms: {
      uTime: { value: 0 },
      uDeep: { value: new THREE.Color(0x061018) },
      uFoam: { value: new THREE.Color(0x1a3a4a) },
    },
    vertexShader: /* glsl */ `
      uniform float uTime;
      varying vec2 vUv;
      varying float vWave;
      void main() {
        vUv = uv;
        vec3 p = position;
        float w = sin(p.x * 0.55 + uTime * 1.3) * 0.05
                + cos(p.y * 0.4 + uTime * 1.05) * 0.035;
        p.z += w;
        vWave = w;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform vec3 uDeep;
      uniform vec3 uFoam;
      varying vec2 vUv;
      varying float vWave;
      void main() {
        float f = smoothstep(-0.02, 0.06, vWave);
        vec3 col = mix(uDeep, uFoam, f * 0.55);
        float edge = smoothstep(0.0, 0.35, min(min(vUv.x, 1.0 - vUv.x), min(vUv.y, 1.0 - vUv.y)));
        gl_FragColor = vec4(col, 0.78 * edge + 0.15);
      }
    `,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = -draft * 0.42;
  mesh.receiveShadow = true;
  mesh.name = "ocean";
  return mesh;
}

function loadTexture(loader, url) {
  return new Promise((resolve) => {
    if (!url) {
      resolve(null);
      return;
    }
    loader.setCrossOrigin("anonymous");
    loader.load(
      url,
      (tex) => {
        tex.colorSpace = THREE.SRGBColorSpace;
        tex.anisotropy = 8;
        tex.needsUpdate = true;
        resolve(tex);
      },
      undefined,
      () => resolve(null)
    );
  });
}

async function loadTextureWithFallback(loader, primary, fallback) {
  let tex = await loadTexture(loader, primary);
  if (!tex && fallback && fallback !== primary) tex = await loadTexture(loader, fallback);
  return tex;
}

/**
 * Zero-Black Contract: inject a DOM <img> UNDER the WebGL canvas on every viewport.
 * This image is always visible regardless of WebGL context state, texture load lag,
 * AIS stale lag, or modal transitions. It's the last line of defense against black cards.
 */
export function showPhotoFallback(host, vessel, reason = "") {
  if (!host) return null;
  // Guarantee viewport geometry even before CSS loads
  if (!host.style.minHeight) host.style.minHeight = "220px";
  host.style.position = "relative";
  host.style.overflow = "hidden";

  const refs = vessel?.refs || {};
  const rank = vessel?.rank;
  // Primary URL: /assets/7000/{rank}-1.jpg (served by Docker bind-mount or legacy path)
  const primarySrc = refs.side?.url || (rank != null ? `/assets/7000/${rank}-1.jpg` : "");
  // Fallback 1: output/assets/top10 (copied by build pipeline)
  const fallback1 = refs.side?.fallback_url || (rank != null ? `assets/top10/${rank}-1.jpg` : "");
  // Fallback 2: relative path without leading slash (edge case for file:// or misconfig)
  const fallback2 = rank != null ? `output/assets/top10/${rank}-1.jpg` : "";

  let img = host.querySelector(".t10-photo-fallback");
  if (!img) {
    img = document.createElement("img");
    img.className = "t10-photo-fallback";
    img.alt = `${vessel?.name || "vessel"} side profile`;
    img.decoding = "async";
    img.loading = "eager";
    // Spec: object-fit:cover; width:100%; height:220px — NEVER-BLACK contract
    Object.assign(img.style, {
      objectFit: "cover",
      width: "100%",
      height: "220px",
      position: "absolute",
      inset: "0",
      zIndex: "1",
      display: "block",
    });
    // Cascade through fallbacks — never leave the card dark
    img.dataset.fb1 = fallback1;
    img.dataset.fb2 = fallback2;
    img.onerror = function () {
      if (!this.dataset.tried1 && this.dataset.fb1) {
        this.dataset.tried1 = "1";
        this.src = this.dataset.fb1;
      } else if (!this.dataset.tried2 && this.dataset.fb2) {
        this.dataset.tried2 = "1";
        this.src = this.dataset.fb2;
      }
      // Final state: image element stays in DOM with broken src (card never goes black
      // because CSS background on .t10-viewport provides deep-space charcoal #07090e)
    };
    // ALWAYS insert as FIRST child — sits below WebGL canvas (z-index: 1 via CSS)
    host.insertBefore(img, host.firstChild);
  }

  // Update src only when it actually changes (avoid redundant reloads)
  if (primarySrc && img.getAttribute("src") !== primarySrc) {
    // Reset fallback state on src change
    delete img.dataset.tried1;
    delete img.dataset.tried2;
    img.src = primarySrc;
  }

  const hint = host.querySelector(".t10-vp-hint");
  if (hint && reason) hint.textContent = reason;
  return img;
}

export class SharedTop10Renderer {
  constructor() {
    this.canvas = document.createElement("canvas");
    this.canvas.id = "t10-shared-webgl";
    Object.assign(this.canvas.style, {
      position: "fixed", left: "0", top: "0", width: "100%", height: "100%",
      pointerEvents: "none", zIndex: "5", display: "block",
      background: "transparent", visibility: "visible", opacity: "1",
    });
    document.body.appendChild(this.canvas);

    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas, antialias: true, alpha: true,
      premultipliedAlpha: false,
      powerPreference: "high-performance", failIfMajorPerformanceCaveat: false,
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.35));
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.setClearAlpha(0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.12;
    this.renderer.autoClear = false;

    this.loader = new THREE.TextureLoader();
    this.loader.setCrossOrigin("anonymous");
    this.slots = new Map();
    this.focusImo = null;
    this.controls = null;
    this.raf = 0;
    this.running = false;
    this.contextLost = false;
    this.clock = new THREE.Clock();
    this._onResize = () => this._syncCanvasSize();
    window.addEventListener("resize", this._onResize);

    // ── ZERO-BLACK CONTRACT: WebGL context loss recovery ──────────────────────
    // On GPU context loss, immediately mark all viewports with photo-fallback
    // so the user NEVER sees a black card. On restore, resume renders.
    this.canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      this.contextLost = true;
      this.stop();
      console.warn("[Sentinel 3D] WebGL context lost — enabling photo fallback for all slots");
      this.slots.forEach((slot) => {
        if (slot.elem) {
          slot.elem.classList.add("webgl-context-lost");
          slot.elem.classList.remove("is-3d-ready");
          const fb = slot.elem.querySelector(".t10-photo-fallback");
          if (fb) { fb.style.opacity = "1"; fb.style.zIndex = "4"; }
        }
      });
    }, false);

    this.canvas.addEventListener("webglcontextrestored", () => {
      this.contextLost = false;
      console.info("[Sentinel 3D] WebGL context restored — resuming PBR renders");
      this.slots.forEach((slot) => {
        if (slot.elem) {
          slot.elem.classList.remove("webgl-context-lost");
          slot.elem.classList.add("is-3d-ready");
        }
      });
      this.start();
    }, false);

    this._syncCanvasSize();
  }

  _syncCanvasSize() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    this.renderer.setSize(w, h, false);
    this.canvas.style.width = `${w}px`;
    this.canvas.style.height = `${h}px`;
  }

  async register(viewportEl, vessel) {
    const imo = String(vessel.imo);
    if (this.slots.has(imo)) {
      const existing = this.slots.get(imo);
      existing.elem = viewportEl;
      existing.visible = true;
      this.start();
      return existing;
    }

    // ── ZERO-BLACK CONTRACT: photo fallback FIRST, WebGL second ───────────────
    // The DOM img element must be in the DOM before any async operation begins.
    // This ensures the card is NEVER dark during texture loading, AIS lag, or
    // any WebGL initialization delay.
    showPhotoFallback(viewportEl, vessel, "PBR · HYDRATING");

    const refs = vessel.refs || {};
    const [sideMap, bowMap, topMap] = await Promise.all([
      loadTextureWithFallback(this.loader, refs.side?.url, refs.side?.fallback_url),
      loadTextureWithFallback(this.loader, refs.bow?.url, refs.bow?.fallback_url),
      loadTextureWithFallback(this.loader, refs.overhead?.url, refs.overhead?.fallback_url),
    ]);

    const scene = new THREE.Scene();
    scene.background = null;
    scene.fog = new THREE.FogExp2(0x07090e, 0.016);

    const loaM = Number(vessel.loa_m || DEFAULT_LOA) * M;
    const draftM = Number(vessel.draft_m || DEFAULT_DRAFT) * M;
    const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 400);
    camera.position.set(loaM * 0.9, loaM * 0.36, loaM * 0.72);
    const target = new THREE.Vector3(0, draftM * 0.5, 0);
    camera.lookAt(target);

    scene.add(new THREE.AmbientLight(0xe0f7fc, 0.55));
    const sun = new THREE.DirectionalLight(0xfff1dd, 1.35);
    sun.position.set(loaM * 0.6, loaM * 1.0, loaM * 0.4);
    scene.add(sun);
    const bounce = new THREE.DirectionalLight(0x6eb6ff, 0.35);
    bounce.position.set(-loaM * 0.3, loaM * 0.15, -loaM * 0.5);
    scene.add(bounce);
    const rim = new THREE.DirectionalLight(0x00f0ff, 0.22);
    rim.position.set(0, loaM * 0.4, -loaM * 0.8);
    scene.add(rim);

    // Cards: parametric PBR only — never preload 10× .glb (Context Lost + network).
    // GLB hull envelope lives exclusively in the modal inspector (lazy, one at a time).
    const mesh = buildMembraneLngCarrier(vessel, sideMap, bowMap, topMap);
    scene.add(mesh);
    const ocean = buildOcean(loaM, draftM);
    scene.add(ocean);

    const slot = {
      imo, vessel, elem: viewportEl, scene, camera, mesh, ocean,
      visible: true, ready: true, angle: Math.random() * Math.PI * 2, target,
    };
    this.slots.set(imo, slot);
    viewportEl.classList.add("is-3d-ready");
    const hint = viewportEl.querySelector(".t10-vp-hint");
    if (hint) hint.textContent = "HOVER · ORBIT · NASA PBR";

    viewportEl.addEventListener("pointerenter", () => this.setFocus(imo));
    viewportEl.addEventListener("pointerleave", () => {
      if (this.focusImo === imo) this.setFocus(null);
    });

    this.start();
    return slot;
  }

  setVisible(imo, visible) {
    const slot = this.slots.get(String(imo));
    if (slot) slot.visible = !!visible;
  }

  setFocus(imo) {
    this.focusImo = imo ? String(imo) : null;
    if (this.controls) {
      this.controls.dispose();
      this.controls = null;
    }
    this.canvas.style.pointerEvents = "none";
    if (!this.focusImo) return;
    const slot = this.slots.get(this.focusImo);
    if (!slot) return;
    this.canvas.style.pointerEvents = "auto";
    this.controls = new OrbitControls(slot.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.07;
    this.controls.target.copy(slot.target);
    this.controls.autoRotate = false;
    this.controls.enableZoom = true;
    this.controls.maxPolarAngle = Math.PI * 0.49;
  }

  start() {
    if (this.running) return;
    this.running = true;
    const tick = () => {
      if (!this.running) return;
      this.raf = requestAnimationFrame(tick);
      this.renderFrame();
    };
    tick();
  }

  stop() {
    this.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
  }

  renderFrame() {
    // ── NEVER-BLACK CONTRACT: early exits must NOT affect DOM photo fallbacks ──
    // Photo fallbacks are <img> elements governed by CSS z-index/opacity rules.
    // Pausing WebGL here only pauses THREE.js — the DOM images remain fully visible.

    // 1. Context lost — skip GPU work entirely
    if (this.contextLost) return;

    // 2. Modal open — pause animation to avoid painting over the overlay backdrop.
    //    Photo fallbacks remain visible via CSS: body.t10-modal-open .t10-photo-fallback { opacity: 0.96 }
    if (document.body.classList.contains("t10-modal-open")) return;

    // 3. Sheet is not top10 — pause to save GPU when user is on balance/route/etc.
    const activeSheet = document.documentElement.dataset.sheet;
    if (activeSheet && activeSheet !== "top10") return;

    this._syncCanvasSize();
    const { renderer, canvas } = this;
    const canvasHeight = canvas.clientHeight;
    const t = this.clock.getElapsedTime();

    renderer.setClearColor(0x000000, 0);
    renderer.setClearAlpha(0);
    renderer.setScissorTest(false);
    renderer.clear(true, true, true);
    renderer.setScissorTest(true);
    if (this.controls) this.controls.update();

    this.slots.forEach((slot) => {
      if (!slot.visible || !slot.ready || !slot.elem) return;
      const rect = slot.elem.getBoundingClientRect();
      const width = Math.floor(rect.right - rect.left);
      const height = Math.floor(rect.bottom - rect.top);
      if (width < 8 || height < 8) return;
      if (rect.bottom < 0 || rect.top > window.innerHeight || rect.right < 0 || rect.left > window.innerWidth) return;

      if (slot.ocean?.material?.uniforms?.uTime) {
        slot.ocean.material.uniforms.uTime.value = t;
      }

      if (this.focusImo !== slot.imo) {
        slot.angle += 0.0055;
        const loaM = Number(slot.vessel.loa_m || DEFAULT_LOA) * M;
        const r = loaM * 1.08;
        slot.camera.position.x = Math.cos(slot.angle) * r;
        slot.camera.position.z = Math.sin(slot.angle) * r;
        slot.camera.position.y = loaM * 0.36;
        slot.camera.lookAt(slot.target);
      }

      slot.camera.aspect = width / height;
      slot.camera.updateProjectionMatrix();

      const left = Math.floor(rect.left);
      const bottom = Math.floor(canvasHeight - rect.bottom);
      renderer.setViewport(left, bottom, width, height);
      renderer.setScissor(left, bottom, width, height);
      renderer.render(slot.scene, slot.camera);
    });
  }

  pause() { this.stop(); }
  resume() { this.start(); }

  dispose() {
    this.stop();
    window.removeEventListener("resize", this._onResize);
    if (this.controls) this.controls.dispose();
    this.slots.forEach((slot) => {
      slot.scene.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) {
          const mats = Array.isArray(o.material) ? o.material : [o.material];
          mats.forEach((m) => {
            if (m.map) m.map.dispose();
            m.dispose();
          });
        }
      });
    });
    this.slots.clear();
    try { this.renderer.dispose(); } catch (_) { /* ignore */ }
    if (this.canvas.parentNode) this.canvas.parentNode.removeChild(this.canvas);
    if (sharedInstance === this) sharedInstance = null;
  }
}

export function getSharedTop10Renderer(create = true) {
  if (!sharedInstance && create) {
    try {
      sharedInstance = new SharedTop10Renderer();
    } catch (err) {
      console.error("Shared WebGL init failed", err);
      sharedInstance = null;
    }
  }
  return sharedInstance;
}

export async function mountVesselViewport(viewportOrCanvas, vessel) {
  const host =
    viewportOrCanvas instanceof HTMLCanvasElement
      ? viewportOrCanvas.parentElement
      : viewportOrCanvas;
  const viewport = host?.matches?.("[data-viewport]")
    ? host
    : host?.querySelector?.("[data-viewport]") || host;

  showPhotoFallback(viewport, vessel, "PBR · STANDBY");
  const shared = getSharedTop10Renderer();
  if (!shared) {
    showPhotoFallback(viewport, vessel, "PHOTO · NO WEBGL");
    const photo = viewport?.querySelector?.(".t10-photo-fallback");
    if (photo) photo.style.opacity = "1";
    return { mode: "photo", vessel, pause() {}, resume() {}, dispose() {} };
  }

  try {
    viewport.querySelectorAll("canvas.t10-canvas").forEach((c) => c.remove());
    await shared.register(viewport, vessel);
    return {
      mode: "webgl-shared",
      vessel,
      pause() { shared.setVisible(vessel.imo, false); },
      resume() { shared.setVisible(vessel.imo, true); shared.resume(); },
      dispose() { shared.setVisible(vessel.imo, false); },
    };
  } catch (err) {
    console.error("Shared slot register failed", vessel?.imo, err);
    showPhotoFallback(viewport, vessel, "PHOTO · GL FAIL");
    const photo = viewport?.querySelector?.(".t10-photo-fallback");
    if (photo) photo.style.opacity = "1";
    return { mode: "photo", vessel, pause() {}, resume() {}, dispose() {} };
  }
}

export default {
  buildMembraneLngCarrier,
  mountVesselViewport,
  showPhotoFallback,
  getSharedTop10Renderer,
  SharedTop10Renderer,
};
