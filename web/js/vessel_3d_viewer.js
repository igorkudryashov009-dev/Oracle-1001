/**
 * Oracle-1001 / Sentinel — Interactive LNG Tanker WebGL viewer
 * Target: IMO 9001772 (LARA) · LOA 239 m · Beam 40 m
 * Modes R1–R5 · ACESFilmic · shadows · PMREM env · ≥60 FPS adaptive
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import gsap from "https://cdn.jsdelivr.net/npm/gsap@3.12.5/+esm";

/** Physical identity — LARA / IMO 9001772 */
export const VESSEL_SPEC = Object.freeze({
  imo: "9001772",
  name: "LARA",
  loa_m: 239,
  beam_m: 40,
  draft_m: 11.2,
  type: "LNG Tanker (Moss)",
});

/** Scene units per metre — keeps camera comfortable while preserving ratios */
const M = 1 / 20;
const LOA = VESSEL_SPEC.loa_m * M;
const BEAM = VESSEL_SPEC.beam_m * M;
const DRAFT = VESSEL_SPEC.draft_m * M;
const HALF_L = LOA / 2;
const HALF_B = BEAM / 2;

const MODE_META = [
  { id: "r1", btn: "mode-r1", label: "R1 · Cinematic", hint: "PBR Titanium / Dark Chrome · 360° orbit" },
  { id: "r2", btn: "mode-r2", label: "R2 · X-Ray", hint: "Fresnel hull · Moss spheres / membrane" },
  { id: "r3", btn: "mode-r3", label: "R3 · Hydrodynamics", hint: "Ocean displacement · wake around hull" },
  { id: "r4", btn: "mode-r4", label: "R4 · Risk Scan", hint: "OSINT heatmap Green → Orange → Red" },
  { id: "r5", btn: "mode-r5", label: "R5 · Spec Plate", hint: "IMO 9001772 · 239 m × 40 m identity" },
];

function fresnelMaterial(opts = {}) {
  return new THREE.ShaderMaterial({
    transparent: true,
    side: THREE.DoubleSide,
    depthWrite: false,
    uniforms: {
      uColor: { value: new THREE.Color(opts.color || 0x7ec8ff) },
      uGlow: { value: new THREE.Color(opts.glow || 0x49e0ff) },
      uPower: { value: opts.power ?? 2.4 },
      uOpacity: { value: opts.opacity ?? 0.55 },
      uTime: { value: 0 },
    },
    vertexShader: /* glsl */ `
      varying vec3 vNormal;
      varying vec3 vWorldPos;
      void main() {
        vNormal = normalize(normalMatrix * normal);
        vec4 wp = modelMatrix * vec4(position, 1.0);
        vWorldPos = wp.xyz;
        gl_Position = projectionMatrix * viewMatrix * wp;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform vec3 uColor;
      uniform vec3 uGlow;
      uniform float uPower;
      uniform float uOpacity;
      uniform float uTime;
      varying vec3 vNormal;
      varying vec3 vWorldPos;
      void main() {
        vec3 viewDir = normalize(cameraPosition - vWorldPos);
        float fres = pow(1.0 - max(dot(normalize(vNormal), viewDir), 0.0), uPower);
        float pulse = 0.85 + 0.15 * sin(uTime * 2.0 + vWorldPos.x * 0.2);
        vec3 col = mix(uColor, uGlow, fres) * pulse;
        gl_FragColor = vec4(col, fres * uOpacity + 0.08);
      }
    `,
  });
}

function riskHeatMaterial() {
  return new THREE.ShaderMaterial({
    side: THREE.DoubleSide,
    uniforms: {
      uTime: { value: 0 },
      uScan: { value: 0 },
    },
    vertexShader: /* glsl */ `
      varying float vRisk;
      varying vec3 vPos;
      void main() {
        vPos = position;
        // Fore / mid / aft risk gradient (OSINT proxy)
        float along = (position.x + ${HALF_L.toFixed(3)}) / ${LOA.toFixed(3)};
        vRisk = clamp(along * 0.55 + abs(position.z) / ${HALF_B.toFixed(3)} * 0.35, 0.0, 1.0);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform float uScan;
      varying float vRisk;
      varying vec3 vPos;
      void main() {
        vec3 g = vec3(0.12, 0.78, 0.42);
        vec3 o = vec3(0.95, 0.55, 0.12);
        vec3 r = vec3(0.92, 0.18, 0.22);
        float t = vRisk;
        vec3 col = t < 0.45 ? mix(g, o, t / 0.45) : mix(o, r, (t - 0.45) / 0.55);
        float band = abs(vPos.x - uScan);
        float hot = smoothstep(1.2, 0.0, band);
        col = mix(col, vec3(1.0, 0.95, 0.7), hot * 0.55);
        float pulse = 0.85 + 0.15 * sin(uTime * 3.0 + vRisk * 6.0);
        gl_FragColor = vec4(col * pulse, 0.92);
      }
    `,
  });
}

/** Procedural Moss-type LNG tanker — exact LOA/beam ratio for IMO 9001772 */
function buildLngTanker() {
  const root = new THREE.Group();
  root.name = "lng_tanker_9001772";

  const titanium = new THREE.MeshStandardMaterial({
    color: 0x8a9099,
    metalness: 0.96,
    roughness: 0.22,
    envMapIntensity: 1.35,
  });
  const darkChrome = new THREE.MeshStandardMaterial({
    color: 0x1a1e26,
    metalness: 0.98,
    roughness: 0.18,
    envMapIntensity: 1.5,
  });
  const deckMat = new THREE.MeshStandardMaterial({
    color: 0x2c323c,
    metalness: 0.7,
    roughness: 0.4,
    envMapIntensity: 1.0,
  });
  const accent = new THREE.MeshStandardMaterial({
    color: 0xc9a227,
    metalness: 1.0,
    roughness: 0.28,
    emissive: 0xc9a227,
    emissiveIntensity: 0.12,
  });
  const mossShell = new THREE.MeshStandardMaterial({
    color: 0x3a4250,
    metalness: 0.92,
    roughness: 0.2,
    envMapIntensity: 1.4,
  });

  const hullH = DRAFT * 1.55;
  const hullGroup = new THREE.Group();
  hullGroup.name = "hull";

  // Main parallel mid-body
  const bodyLen = LOA * 0.72;
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(bodyLen, hullH, BEAM * 0.96),
    darkChrome
  );
  body.position.set(0, hullH * 0.15, 0);
  body.castShadow = true;
  body.receiveShadow = true;
  body.name = "hull_body";
  hullGroup.add(body);

  // Bow (wedge)
  const bow = new THREE.Mesh(new THREE.ConeGeometry(BEAM * 0.52, LOA * 0.16, 4), darkChrome);
  bow.rotation.z = -Math.PI / 2;
  bow.rotation.y = Math.PI / 4;
  bow.position.set(HALF_L - LOA * 0.06, hullH * 0.12, 0);
  bow.scale.set(1, 1.05, 0.92);
  bow.castShadow = true;
  bow.name = "bow";
  hullGroup.add(bow);

  // Stern block
  const stern = new THREE.Mesh(
    new THREE.BoxGeometry(LOA * 0.1, hullH * 1.1, BEAM * 0.98),
    darkChrome
  );
  stern.position.set(-HALF_L + LOA * 0.05, hullH * 0.2, 0);
  stern.castShadow = true;
  hullGroup.add(stern);

  // Waterline accent
  const wl = new THREE.Mesh(
    new THREE.BoxGeometry(LOA * 0.98, 0.04, BEAM * 1.02),
    accent
  );
  wl.position.set(0, -hullH * 0.35, 0);
  wl.name = "waterline";
  hullGroup.add(wl);

  // Deck
  const deck = new THREE.Mesh(
    new THREE.BoxGeometry(bodyLen * 0.98, 0.08, BEAM * 0.9),
    deckMat
  );
  deck.position.set(0, hullH * 0.55, 0);
  deck.receiveShadow = true;
  hullGroup.add(deck);

  // Moss spherical tanks (4) + membrane block proxies
  const tanks = new THREE.Group();
  tanks.name = "tanks";
  const tankR = BEAM * 0.38;
  const tankYs = hullH * 0.55;
  const tankXs = [-0.28, -0.08, 0.12, 0.32].map((f) => f * LOA);
  tankXs.forEach((x, i) => {
    const sphere = new THREE.Mesh(
      new THREE.SphereGeometry(tankR, 28, 20, 0, Math.PI * 2, 0, Math.PI * 0.58),
      mossShell.clone()
    );
    sphere.position.set(x, tankYs, 0);
    sphere.castShadow = true;
    sphere.name = `moss_${i}`;
    tanks.add(sphere);

    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(tankR * 1.02, 0.035, 8, 40),
      accent
    );
    ring.rotation.x = Math.PI / 2;
    ring.position.set(x, tankYs + 0.02, 0);
    tanks.add(ring);

    // Membrane / prismatic block under dome (x-ray internals)
    const membrane = new THREE.Mesh(
      new THREE.BoxGeometry(tankR * 1.35, tankR * 0.85, tankR * 1.35),
      new THREE.MeshStandardMaterial({
        color: 0x5ec8ff,
        metalness: 0.3,
        roughness: 0.35,
        emissive: 0x1a6a9a,
        emissiveIntensity: 0.45,
        transparent: true,
        opacity: 0.8,
      })
    );
    membrane.position.set(x, hullH * 0.05, 0);
    membrane.name = `membrane_${i}`;
    membrane.visible = false;
    tanks.add(membrane);
  });
  hullGroup.add(tanks);

  // Accommodation / bridge aft
  const bridge = new THREE.Group();
  bridge.name = "bridge";
  const block = new THREE.Mesh(
    new THREE.BoxGeometry(LOA * 0.09, hullH * 1.6, BEAM * 0.72),
    titanium
  );
  block.position.set(-HALF_L + LOA * 0.12, hullH * 1.1, 0);
  block.castShadow = true;
  bridge.add(block);
  const wing = new THREE.Mesh(
    new THREE.BoxGeometry(0.12, 0.35, BEAM * 0.95),
    accent
  );
  wing.position.set(-HALF_L + LOA * 0.12, hullH * 1.7, 0);
  bridge.add(wing);
  const funnel = new THREE.Mesh(
    new THREE.CylinderGeometry(0.22, 0.28, hullH * 0.7, 12),
    darkChrome
  );
  funnel.position.set(-HALF_L + LOA * 0.08, hullH * 1.55, 0);
  bridge.add(funnel);
  const mast = new THREE.Mesh(
    new THREE.CylinderGeometry(0.04, 0.05, hullH * 1.1, 8),
    accent
  );
  mast.position.set(-HALF_L + LOA * 0.12, hullH * 2.15, 0);
  bridge.add(mast);
  hullGroup.add(bridge);

  // Railings (cheap detail)
  for (const z of [-HALF_B * 0.92, HALF_B * 0.92]) {
    const rail = new THREE.Mesh(
      new THREE.BoxGeometry(bodyLen * 0.9, 0.04, 0.04),
      titanium
    );
    rail.position.set(0, hullH * 0.62, z);
    hullGroup.add(rail);
  }

  root.add(hullGroup);

  // X-ray fresnel overlay (cloned hull silhouette)
  const xrayShell = new THREE.Mesh(
    new THREE.BoxGeometry(bodyLen * 1.02, hullH * 1.05, BEAM),
    fresnelMaterial()
  );
  xrayShell.position.copy(body.position);
  xrayShell.visible = false;
  xrayShell.name = "xray_shell";
  root.add(xrayShell);

  // Risk heatmap hull overlay
  const riskHull = new THREE.Mesh(
    new THREE.BoxGeometry(bodyLen * 1.01, hullH * 1.02, BEAM * 0.98),
    riskHeatMaterial()
  );
  riskHull.position.copy(body.position);
  riskHull.visible = false;
  riskHull.name = "risk_hull";
  root.add(riskHull);

  // Scan plane
  const scan = new THREE.Mesh(
    new THREE.PlaneGeometry(0.15, BEAM * 1.6),
    new THREE.MeshBasicMaterial({
      color: 0xffcc66,
      transparent: true,
      opacity: 0.5,
      side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    })
  );
  scan.rotation.y = Math.PI / 2;
  scan.position.set(HALF_L, hullH * 0.4, 0);
  scan.visible = false;
  scan.name = "scanner";
  root.add(scan);

  root.userData = {
    hullGroup,
    tanks,
    xrayShell,
    riskHull,
    scan,
    mats: { titanium, darkChrome, deckMat, accent, mossShell },
    baseMaterials: [],
  };

  hullGroup.traverse((o) => {
    if (o.isMesh && o.material) {
      root.userData.baseMaterials.push({ mesh: o, mat: o.material });
    }
  });

  return root;
}

function buildOcean() {
  const geo = new THREE.PlaneGeometry(LOA * 6, LOA * 6, 72, 72);
  const mat = new THREE.MeshStandardMaterial({
    color: 0x0a2038,
    metalness: 0.65,
    roughness: 0.28,
    transparent: true,
    opacity: 0.88,
    envMapIntensity: 1.1,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = -DRAFT * 0.55;
  mesh.receiveShadow = true;
  mesh.name = "ocean";
  const pos = geo.attributes.position;
  mesh.userData.base = new Float32Array(pos.array.length);
  mesh.userData.base.set(pos.array);
  mesh.visible = false;
  return mesh;
}

export function initVessel3DViewer(rootEl, flagship = {}) {
  if (!rootEl) return null;

  const stage =
    rootEl.querySelector("[data-lv-stage]") ||
    rootEl.querySelector("#vessel-3d-stage") ||
    rootEl;
  const modeTitle = rootEl.querySelector("[data-lv-mode-title]");
  const modeHint = rootEl.querySelector("[data-lv-mode-hint]");
  const fpsEl = rootEl.querySelector("[data-lv-fps]");

  const spec = {
    ...VESSEL_SPEC,
    name: flagship.vessel_name || flagship.name || VESSEL_SPEC.name,
    imo: String(flagship.imo || VESSEL_SPEC.imo),
    loa_m: Number(flagship.loa_m || VESSEL_SPEC.loa_m),
    beam_m: Number(flagship.beam_m || VESSEL_SPEC.beam_m),
    dwt: flagship.dwt_tons || 48817,
    flag: flagship.flag || "—",
  };

  const w = () => stage.clientWidth || 640;
  const h = () => Math.max(380, stage.clientHeight || 440);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x07090e);
  scene.fog = new THREE.FogExp2(0x07090e, 0.012);

  const camera = new THREE.PerspectiveCamera(40, w() / h(), 0.1, 400);
  camera.position.set(LOA * 0.85, LOA * 0.35, LOA * 0.75);

  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    powerPreference: "high-performance",
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  renderer.setSize(w(), h());
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.08;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  stage.appendChild(renderer.domElement);

  // HDRI-like environment reflections (procedural RoomEnvironment → PMREM)
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

  const labelRenderer = new CSS2DRenderer();
  labelRenderer.setSize(w(), h());
  Object.assign(labelRenderer.domElement.style, {
    position: "absolute",
    inset: "0",
    pointerEvents: "none",
  });
  stage.appendChild(labelRenderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.055;
  controls.minDistance = LOA * 0.35;
  controls.maxDistance = LOA * 3.2;
  controls.maxPolarAngle = Math.PI * 0.495;
  controls.target.set(0, DRAFT * 0.6, 0);

  const amb = new THREE.AmbientLight(0x2a3340, 0.45);
  scene.add(amb);
  const key = new THREE.DirectionalLight(0xfff0d8, 1.55);
  key.position.set(LOA * 0.6, LOA * 0.9, LOA * 0.4);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.near = 1;
  key.shadow.camera.far = LOA * 4;
  const sc = LOA * 1.2;
  key.shadow.camera.left = -sc;
  key.shadow.camera.right = sc;
  key.shadow.camera.top = sc;
  key.shadow.camera.bottom = -sc;
  key.shadow.bias = -0.00015;
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xa8c4e8, 0.55);
  rim.position.set(-LOA * 0.5, LOA * 0.25, -LOA * 0.6);
  scene.add(rim);

  const ground = new THREE.Mesh(
    new THREE.CircleGeometry(LOA * 2.2, 64),
    new THREE.MeshStandardMaterial({ color: 0x05070b, metalness: 0.8, roughness: 0.4 })
  );
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -DRAFT * 0.58;
  ground.receiveShadow = true;
  scene.add(ground);

  const vessel = buildLngTanker();
  scene.add(vessel);
  const ocean = buildOcean();
  scene.add(ocean);

  // R5 identity badges
  const badgeDefs = [
    { pos: [0, DRAFT * 2.2, 0], html: `<b>Vessel</b><span>${spec.name}</span>` },
    { pos: [HALF_L * 0.6, DRAFT * 1.4, HALF_B], html: `<b>IMO</b><span>${spec.imo}</span>` },
    { pos: [0, DRAFT * 0.2, -HALF_B * 1.1], html: `<b>LOA × Beam</b><span>${spec.loa_m} m × ${spec.beam_m} m</span>` },
    { pos: [-HALF_L * 0.55, DRAFT * 2.4, 0], html: `<b>DWT</b><span>${Number(spec.dwt).toLocaleString("en-US")} t</span>` },
  ];
  const badgeEls = [];
  badgeDefs.forEach((b) => {
    const el = document.createElement("div");
    el.className = "lv-badge3d";
    el.innerHTML = b.html;
    el.style.opacity = "0";
    const obj = new CSS2DObject(el);
    obj.position.set(...b.pos);
    vessel.add(obj);
    badgeEls.push(el);
  });

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(w(), h()), 0.35, 0.5, 0.85);
  composer.addPass(bloom);

  let mode = "r1";
  let modeTime = 0;
  let camAuto = true;
  let orbitTween = null;
  let active = true;
  let heavy = true;
  let last = performance.now();
  let frames = 0;
  let fpsAcc = 0;
  let fpsSmooth = 60;

  function setModeUI(id) {
    const meta = MODE_META.find((m) => m.id === id) || MODE_META[0];
    if (modeTitle) modeTitle.textContent = meta.label;
    if (modeHint) modeHint.textContent = meta.hint;
    MODE_META.forEach((m) => {
      const btn = document.getElementById(m.btn) || rootEl.querySelector(`[data-mode="${m.id}"]`);
      if (!btn) return;
      const on = m.id === id;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    rootEl.dataset.mode = id;
    rootEl.classList.remove("lv-pulse");
    void rootEl.offsetWidth;
    rootEl.classList.add("lv-pulse");
  }

  function applyModeVisuals(id) {
    const { hullGroup, tanks, xrayShell, riskHull, scan } = vessel.userData;
    const isXray = id === "r2";
    const isHydro = id === "r3";
    const isRisk = id === "r4";
    const isSpec = id === "r5";

    ocean.visible = isHydro || id === "r1";
    ground.visible = !isHydro;
    xrayShell.visible = isXray;
    riskHull.visible = isRisk;
    scan.visible = isRisk;
    badgeEls.forEach((el) => {
      el.style.opacity = isSpec ? "1" : "0";
    });

    tanks.children.forEach((c) => {
      if (String(c.name).startsWith("membrane_")) c.visible = isXray;
    });

    hullGroup.traverse((o) => {
      if (!o.isMesh) return;
      if (isXray) {
        if (!o.userData._bak) o.userData._bak = o.material;
        if (o.name !== "waterline") {
          const m = o.material?.clone?.() || o.material;
          if (m && m.transparent !== undefined) {
            m.transparent = true;
            m.opacity = 0.18;
            m.depthWrite = false;
            o.material = m;
          }
        }
      } else if (o.userData._bak) {
        o.material = o.userData._bak;
      }
      if (isRisk && o.name === "hull_body") {
        o.visible = false;
      } else if (o.name === "hull_body") {
        o.visible = true;
      }
    });

    bloom.strength = id === "r1" || id === "r5" ? 0.38 : id === "r4" ? 0.55 : 0.28;
  }

  function flyCamera(pos, target, dur = 1.4) {
    camAuto = false;
    if (orbitTween) orbitTween.kill();
    const from = {
      x: camera.position.x,
      y: camera.position.y,
      z: camera.position.z,
      tx: controls.target.x,
      ty: controls.target.y,
      tz: controls.target.z,
    };
    const to = {
      x: pos.x,
      y: pos.y,
      z: pos.z,
      tx: target.x,
      ty: target.y,
      tz: target.z,
    };
    orbitTween = gsap.to(from, {
      ...to,
      duration: dur,
      ease: "power3.inOut",
      onUpdate: () => {
        camera.position.set(from.x, from.y, from.z);
        controls.target.set(from.tx, from.ty, from.tz);
        controls.update();
      },
      onComplete: () => {
        camAuto = mode === "r1" || mode === "r3";
        if (mode === "r1") startCinematicOrbit();
      },
    });
  }

  function startCinematicOrbit() {
    if (orbitTween) orbitTween.kill();
    const state = { a: Math.atan2(camera.position.z, camera.position.x), r: Math.hypot(camera.position.x, camera.position.z) };
    orbitTween = gsap.to(state, {
      a: state.a + Math.PI * 2,
      duration: 28,
      ease: "none",
      repeat: -1,
      onUpdate: () => {
        if (!camAuto || mode !== "r1") return;
        const y = LOA * 0.28 + Math.sin(state.a * 2) * LOA * 0.04;
        camera.position.set(Math.cos(state.a) * state.r, y, Math.sin(state.a) * state.r);
        controls.target.set(0, DRAFT * 0.55, 0);
        controls.update();
      },
    });
  }

  function switchMode(id) {
    if (!MODE_META.some((m) => m.id === id)) return;
    if (id === mode && modeTime > 0.2) return;
    mode = id;
    modeTime = 0;
    setModeUI(id);
    applyModeVisuals(id);

    const shots = {
      r1: { pos: new THREE.Vector3(LOA * 0.85, LOA * 0.35, LOA * 0.75), tgt: new THREE.Vector3(0, DRAFT * 0.55, 0) },
      r2: { pos: new THREE.Vector3(0.01, LOA * 0.95, 0.01), tgt: new THREE.Vector3(0, DRAFT * 0.2, 0) },
      r3: { pos: new THREE.Vector3(LOA * 0.55, LOA * 0.18, LOA * 0.7), tgt: new THREE.Vector3(-LOA * 0.05, 0, 0) },
      r4: { pos: new THREE.Vector3(LOA * 0.15, LOA * 0.3, LOA * 0.95), tgt: new THREE.Vector3(0, DRAFT * 0.4, 0) },
      r5: { pos: new THREE.Vector3(-LOA * 0.45, LOA * 0.32, LOA * 0.35), tgt: new THREE.Vector3(-HALF_L * 0.4, DRAFT * 1.2, 0) },
    };
    const s = shots[id] || shots.r1;
    flyCamera(s.pos, s.tgt, 1.35);
  }

  // Bind sidebar buttons #mode-r1 … #mode-r5
  MODE_META.forEach((m) => {
    const btn = document.getElementById(m.btn) || rootEl.querySelector(`[data-mode="${m.id}"]`);
    if (btn) {
      btn.addEventListener("click", () => switchMode(m.id));
    }
  });
  // Fallback rail click
  rootEl.querySelector("[data-lv-modes]")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-mode], [id^='mode-r']");
    if (!btn) return;
    const id = btn.dataset.mode || String(btn.id || "").replace("mode-", "");
    if (id) switchMode(id);
  });

  window.addEventListener("keydown", (e) => {
    if (!active) return;
    const n = Number(e.key);
    if (n >= 1 && n <= 5) switchMode(`r${n}`);
  });

  function onResize() {
    const width = w();
    const height = h();
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
    labelRenderer.setSize(width, height);
    composer.setSize(width, height);
    bloom.setSize(width, height);
  }
  window.addEventListener("resize", onResize);

  const io = new IntersectionObserver(
    (entries) => {
      active = entries.some((en) => en.isIntersecting && en.intersectionRatio > 0.12);
      if (!active) {
        heavy = false;
        renderer.shadowMap.enabled = false;
      }
    },
    { threshold: [0, 0.12, 0.4] }
  );
  io.observe(rootEl);

  let scrollTimer = null;
  window.addEventListener(
    "scroll",
    () => {
      heavy = false;
      renderer.shadowMap.enabled = false;
      clearTimeout(scrollTimer);
      scrollTimer = setTimeout(() => {
        if (active) {
          heavy = true;
          renderer.shadowMap.enabled = true;
        }
      }, 160);
    },
    { passive: true }
  );

  setModeUI("r1");
  applyModeVisuals("r1");
  startCinematicOrbit();

  function animate(now) {
    requestAnimationFrame(animate);
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    frames++;
    fpsAcc += dt;
    if (fpsAcc >= 0.5) {
      fpsSmooth = Math.round(frames / fpsAcc);
      if (fpsEl) fpsEl.textContent = `${fpsSmooth} FPS`;
      if (fpsSmooth < 45) {
        bloom.strength = Math.min(bloom.strength, 0.2);
        renderer.setPixelRatio(1);
        renderer.shadowMap.enabled = false;
        ocean.geometry.dispose();
        // degrade wave resolution once
        if (!ocean.userData.degraded) {
          ocean.userData.degraded = true;
        }
      } else if (fpsSmooth >= 58 && heavy && active) {
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
        renderer.shadowMap.enabled = true;
      }
      frames = 0;
      fpsAcc = 0;
    }

    if (!active && document.hidden) return;
    modeTime += dt;
    controls.update();

    if (mode === "r2") {
      const sh = vessel.userData.xrayShell;
      if (sh?.material?.uniforms?.uTime) sh.material.uniforms.uTime.value = modeTime;
      vessel.rotation.y = Math.sin(modeTime * 0.2) * 0.12;
      vessel.userData.tanks.children.forEach((c, i) => {
        if (c.material?.emissiveIntensity != null) {
          c.material.emissiveIntensity = 0.35 + Math.sin(modeTime * 2.2 + i) * 0.25;
        }
      });
    } else {
      vessel.rotation.y *= 0.9;
    }

    if (mode === "r3") {
      vessel.position.y = Math.sin(modeTime * 1.35) * 0.06;
      vessel.rotation.z = Math.sin(modeTime * 1.05) * 0.015;
      const pos = ocean.geometry.attributes.position;
      const base = ocean.userData.base;
      if (base && !ocean.userData.degraded) {
        for (let i = 0; i < pos.count; i++) {
          const ix = i * 3;
          const x = base[ix];
          const z = base[ix + 2];
          // Hull proximity wake boost
          const dx = Math.max(-HALF_L, Math.min(HALF_L, x));
          const near = Math.exp(-(z * z) / (HALF_B * HALF_B * 4)) * Math.exp(-(dx * dx) / (LOA * LOA * 0.15));
          pos.array[ix + 1] =
            Math.sin(x * 0.55 + modeTime * 2.1) * 0.1 +
            Math.cos(z * 0.48 + modeTime * 1.5) * 0.07 +
            near * Math.sin(modeTime * 3.5 + x) * 0.12;
        }
        pos.needsUpdate = true;
        if ((frames & 1) === 0) ocean.geometry.computeVertexNormals();
      }
      if (camAuto) {
        const a = modeTime * 0.15;
        camera.position.x = LOA * 0.55 + Math.cos(a) * LOA * 0.08;
        camera.position.z = LOA * 0.7 + Math.sin(a) * LOA * 0.12;
      }
    } else {
      vessel.position.y *= 0.88;
    }

    if (mode === "r4") {
      const scan = vessel.userData.scan;
      const rh = vessel.userData.riskHull;
      const t = (modeTime * 0.28) % 1;
      const sx = HALF_L - t * LOA;
      scan.position.x = sx;
      if (rh?.material?.uniforms) {
        rh.material.uniforms.uTime.value = modeTime;
        rh.material.uniforms.uScan.value = sx;
      }
      scan.material.opacity = 0.35 + Math.sin(modeTime * 5) * 0.15;
    }

    if (heavy && active && (mode === "r1" || mode === "r4" || mode === "r5")) {
      composer.render();
    } else {
      renderer.render(scene, camera);
    }
    labelRenderer.render(scene, camera);
  }
  requestAnimationFrame(animate);

  return { switchMode, MODE_META, VESSEL_SPEC: spec };
}

// Auto-boot
function boot() {
  const el = document.getElementById("lvVesselExhibit");
  if (!el || el.dataset.booted) return;
  el.dataset.booted = "1";
  const flagship = (typeof window !== "undefined" && window.__LV3D_FLAGSHIP__) || {};
  initVessel3DViewer(el, flagship);
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
