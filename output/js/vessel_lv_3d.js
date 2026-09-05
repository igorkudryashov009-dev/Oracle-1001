/**
 * ORACLE-1001 · LV Vessel Exhibit
 * Тяжелый люкс / Haute Horlogerie 3D-визуализатор газовоза
 * Three.js PBR + Bloom · 5 режимов · русская локализация
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";

const GOLD = 0xd4af37;
const GOLD_SOFT = 0xf3e5ab;
const TITANIUM = 0x8a9099;
const OBSIDIAN = 0x0a0c10;
const CHROME = 0xc5c8ce;

const MODE_META = [
  { id: "cinematic", label: "Кинематографический обзор", hint: "Элегантный облёт 360°" },
  { id: "xray", label: "Рентген и профилирование", hint: "Отсеки · DWT · балласт" },
  { id: "hydro", label: "Гидродинамика и ход", hint: "Кильватер · ватерлиния" },
  { id: "scan", label: "Сканирование риска", hint: "Комплаенс-развёртка OSINT" },
  { id: "flagship", label: "Режим флагмана", hint: "PRELUDE · мегаструктура" },
];

function easePower3InOut(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

function tween(from, to, dur, onUpdate, onComplete) {
  const t0 = performance.now();
  const keys = Object.keys(to);
  function frame(now) {
    const u = Math.min(1, (now - t0) / dur);
    const e = easePower3InOut(u);
    const cur = {};
    keys.forEach((k) => {
      cur[k] = from[k] + (to[k] - from[k]) * e;
    });
    onUpdate(cur, e);
    if (u < 1) requestAnimationFrame(frame);
    else if (onComplete) onComplete();
  }
  requestAnimationFrame(frame);
}

function buildHullMaterials() {
  const hull = new THREE.MeshStandardMaterial({
    color: 0x1a1d24,
    metalness: 0.92,
    roughness: 0.28,
    envMapIntensity: 1.2,
  });
  const accent = new THREE.MeshStandardMaterial({
    color: GOLD,
    metalness: 1.0,
    roughness: 0.22,
    emissive: GOLD,
    emissiveIntensity: 0.15,
  });
  const glass = new THREE.MeshPhysicalMaterial({
    color: 0x9eb4c8,
    metalness: 0.1,
    roughness: 0.05,
    transmission: 0.55,
    thickness: 0.6,
    transparent: true,
    opacity: 0.85,
  });
  const xray = new THREE.MeshPhysicalMaterial({
    color: 0xa8b0bc,
    metalness: 0.85,
    roughness: 0.15,
    transparent: true,
    opacity: 0.22,
    transmission: 0.7,
    thickness: 1.2,
    side: THREE.DoubleSide,
  });
  const goldGlow = new THREE.MeshStandardMaterial({
    color: GOLD_SOFT,
    metalness: 0.6,
    roughness: 0.35,
    emissive: GOLD,
    emissiveIntensity: 0.55,
    transparent: true,
    opacity: 0.85,
  });
  return { hull, accent, glass, xray, goldGlow };
}

/** Процедурный LNG / танкер премиальной детализации */
function buildVessel(mats) {
  const root = new THREE.Group();
  root.name = "vessel";

  const hullGroup = new THREE.Group();
  hullGroup.name = "hull";

  // Основной корпус
  const body = new THREE.Mesh(new THREE.BoxGeometry(14, 2.2, 3.2), mats.hull);
  body.position.set(0, 0.2, 0);
  body.castShadow = true;
  hullGroup.add(body);

  // Нос (клиновидный)
  const bow = new THREE.Mesh(new THREE.ConeGeometry(1.7, 3.2, 4), mats.hull);
  bow.rotation.z = -Math.PI / 2;
  bow.rotation.y = Math.PI / 4;
  bow.position.set(8.2, 0.15, 0);
  bow.scale.set(1, 1.05, 0.95);
  hullGroup.add(bow);

  // Корма
  const stern = new THREE.Mesh(new THREE.BoxGeometry(1.8, 2.4, 3.3), mats.hull);
  stern.position.set(-7.2, 0.3, 0);
  hullGroup.add(stern);

  // Золотая ватерлиния
  const waterline = new THREE.Mesh(
    new THREE.BoxGeometry(15.5, 0.06, 3.35),
    mats.accent
  );
  waterline.position.set(0.3, -0.55, 0);
  waterline.name = "waterline";
  hullGroup.add(waterline);

  // Палуба
  const deck = new THREE.Mesh(new THREE.BoxGeometry(13.2, 0.12, 3.0), mats.hull);
  deck.position.set(0, 1.35, 0);
  hullGroup.add(deck);

  // Танки / купола LNG
  const tanks = new THREE.Group();
  tanks.name = "tanks";
  for (let i = 0; i < 4; i++) {
    const dome = new THREE.Mesh(
      new THREE.SphereGeometry(1.05, 24, 16, 0, Math.PI * 2, 0, Math.PI / 2),
      mats.hull.clone()
    );
    dome.material.color.set(0x2a3038);
    dome.material.metalness = 0.95;
    dome.material.roughness = 0.18;
    dome.position.set(3.5 - i * 2.4, 1.4, 0);
    tanks.add(dome);
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(1.08, 0.04, 8, 32),
      mats.accent
    );
    ring.rotation.x = Math.PI / 2;
    ring.position.copy(dome.position);
    ring.position.y = 1.42;
    tanks.add(ring);
  }
  hullGroup.add(tanks);

  // Надстройка
  const bridge = new THREE.Group();
  bridge.name = "bridge";
  const block = new THREE.Mesh(new THREE.BoxGeometry(2.4, 2.6, 2.8), mats.hull);
  block.position.set(-5.6, 2.6, 0);
  bridge.add(block);
  const wing = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.8, 3.4), mats.accent);
  wing.position.set(-5.6, 3.4, 0);
  bridge.add(wing);
  for (let i = 0; i < 3; i++) {
    const win = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.35, 0.5), mats.glass);
    win.position.set(-4.38, 2.4 + i * 0.45, 0.7 - i * 0.05);
    bridge.add(win);
  }
  const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.08, 2.2, 8), mats.accent);
  mast.position.set(-5.6, 4.6, 0);
  bridge.add(mast);
  const funnel = new THREE.Mesh(new THREE.CylinderGeometry(0.35, 0.4, 1.4, 12), mats.hull);
  funnel.position.set(-6.5, 3.5, 0);
  bridge.add(funnel);
  hullGroup.add(bridge);

  // Внутренние отсеки (для рентгена)
  const internals = new THREE.Group();
  internals.name = "internals";
  internals.visible = false;
  const holds = [
    { x: 4.5, c: 0xd4af37, label: "трюм" },
    { x: 1.5, c: 0xf3e5ab, label: "DWT" },
    { x: -1.2, c: 0xd4af37, label: "газ" },
    { x: -3.5, c: 0xb8962e, label: "балласт" },
  ];
  holds.forEach((h) => {
    const box = new THREE.Mesh(
      new THREE.BoxGeometry(2.0, 1.4, 2.4),
      new THREE.MeshStandardMaterial({
        color: h.c,
        metalness: 0.4,
        roughness: 0.4,
        emissive: h.c,
        emissiveIntensity: 0.35,
        transparent: true,
        opacity: 0.75,
      })
    );
    box.position.set(h.x, 0.3, 0);
    internals.add(box);
  });
  // Переборки
  for (let i = 0; i < 5; i++) {
    const bulk = new THREE.Mesh(
      new THREE.BoxGeometry(0.08, 1.6, 2.6),
      new THREE.MeshStandardMaterial({
        color: GOLD_SOFT,
        emissive: GOLD,
        emissiveIntensity: 0.4,
        transparent: true,
        opacity: 0.5,
      })
    );
    bulk.position.set(5.5 - i * 2.5, 0.3, 0);
    internals.add(bulk);
  }
  root.add(internals);
  root.add(hullGroup);

  // Скан-плоскость
  const scan = new THREE.Mesh(
    new THREE.PlaneGeometry(0.12, 4.5),
    new THREE.MeshBasicMaterial({
      color: GOLD,
      transparent: true,
      opacity: 0.55,
      side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    })
  );
  scan.rotation.y = Math.PI / 2;
  scan.position.set(8, 1.2, 0);
  scan.visible = false;
  scan.name = "scanner";
  root.add(scan);

  // Точки OSINT для скана
  const hotspots = new THREE.Group();
  hotspots.name = "hotspots";
  hotspots.visible = false;
  [
    [7.5, 1.2, 0],
    [-5.6, 3.2, 0],
    [0, 2.2, 0],
  ].forEach((p, i) => {
    const s = new THREE.Mesh(
      new THREE.SphereGeometry(0.18, 12, 12),
      new THREE.MeshStandardMaterial({
        color: GOLD,
        emissive: GOLD,
        emissiveIntensity: 0.9,
      })
    );
    s.position.set(...p);
    s.userData.idx = i;
    hotspots.add(s);
  });
  root.add(hotspots);

  root.userData = { hullGroup, internals, scan, hotspots, mats, body };
  return root;
}

function buildWater() {
  const geo = new THREE.PlaneGeometry(80, 80, 64, 64);
  const mat = new THREE.MeshStandardMaterial({
    color: 0x0c1828,
    metalness: 0.7,
    roughness: 0.25,
    transparent: true,
    opacity: 0.85,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = -0.9;
  mesh.receiveShadow = true;
  mesh.name = "water";
  // store base positions for wave
  const pos = geo.attributes.position;
  mesh.userData.base = new Float32Array(pos.array.length);
  mesh.userData.base.set(pos.array);
  return mesh;
}

function buildWake() {
  const group = new THREE.Group();
  group.name = "wake";
  group.visible = false;
  const pts = [];
  for (let i = 0; i < 40; i++) {
    const m = new THREE.Mesh(
      new THREE.SphereGeometry(0.08 + Math.random() * 0.1, 6, 6),
      new THREE.MeshBasicMaterial({
        color: GOLD_SOFT,
        transparent: true,
        opacity: 0.35,
      })
    );
    m.position.set(-8 - i * 0.55, -0.7, (Math.random() - 0.5) * 1.8);
    pts.push(m);
    group.add(m);
  }
  group.userData.pts = pts;
  return group;
}

function buildFlowLines() {
  const group = new THREE.Group();
  group.name = "flows";
  group.visible = false;
  for (let s = -1; s <= 1; s += 2) {
    for (let i = 0; i < 12; i++) {
      const curve = new THREE.CatmullRomCurve3([
        new THREE.Vector3(7, -0.6, s * (1.2 + i * 0.05)),
        new THREE.Vector3(2, -0.55, s * (1.5 + i * 0.04)),
        new THREE.Vector3(-2, -0.5, s * (1.6 + i * 0.03)),
        new THREE.Vector3(-8, -0.55, s * (1.3 + i * 0.05)),
      ]);
      const g = new THREE.TubeGeometry(curve, 32, 0.02, 5, false);
      const m = new THREE.Mesh(
        g,
        new THREE.MeshBasicMaterial({
          color: i % 2 ? GOLD : 0x7ec8ff,
          transparent: true,
          opacity: 0.45,
        })
      );
      group.add(m);
    }
  }
  return group;
}

export function initVesselExhibit(rootEl, flagship = {}) {
  if (!rootEl) return null;

  const stage = rootEl.querySelector("[data-lv-stage]");
  const hudModes = rootEl.querySelector("[data-lv-modes]");
  const modeTitle = rootEl.querySelector("[data-lv-mode-title]");
  const modeHint = rootEl.querySelector("[data-lv-mode-hint]");
  const badgeLayer = rootEl.querySelector("[data-lv-badges]");
  const fpsEl = rootEl.querySelector("[data-lv-fps]");

  // Mode buttons
  hudModes.innerHTML = MODE_META.map(
    (m, i) =>
      `<button type="button" class="lv-mode${i === 0 ? " on" : ""}" data-mode="${m.id}" aria-pressed="${i === 0}">
        <span class="lv-mode-idx">${String(i + 1).padStart(2, "0")}</span>
        <span class="lv-mode-label">${m.label}</span>
      </button>`
  ).join("");

  const w = () => stage.clientWidth || 640;
  const h = () => Math.max(360, stage.clientHeight || 420);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(OBSIDIAN);
  scene.fog = new THREE.FogExp2(OBSIDIAN, 0.018);

  const camera = new THREE.PerspectiveCamera(42, w() / h(), 0.1, 200);
  camera.position.set(16, 7, 14);

  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: false,
    powerPreference: "high-performance",
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(w(), h());
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  stage.appendChild(renderer.domElement);

  const labelRenderer = new CSS2DRenderer();
  labelRenderer.setSize(w(), h());
  labelRenderer.domElement.style.position = "absolute";
  labelRenderer.domElement.style.inset = "0";
  labelRenderer.domElement.style.pointerEvents = "none";
  stage.appendChild(labelRenderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.06;
  controls.minDistance = 6;
  controls.maxDistance = 40;
  controls.maxPolarAngle = Math.PI * 0.49;
  controls.target.set(0, 1.2, 0);

  // Soft-box lighting + rim
  const amb = new THREE.AmbientLight(0x2a3038, 0.55);
  scene.add(amb);
  const key = new THREE.DirectionalLight(0xfff2d6, 1.35);
  key.position.set(12, 18, 8);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.near = 2;
  key.shadow.camera.far = 50;
  scene.add(key);
  const rim = new THREE.DirectionalLight(GOLD_SOFT, 0.85);
  rim.position.set(-10, 4, -12);
  scene.add(rim);
  const fill = new THREE.DirectionalLight(0x6a90b8, 0.4);
  fill.position.set(-6, 8, 10);
  scene.add(fill);
  const goldPoint = new THREE.PointLight(GOLD, 1.2, 40);
  goldPoint.position.set(0, 6, 0);
  scene.add(goldPoint);

  // Soft ground plate (obsidian gloss)
  const ground = new THREE.Mesh(
    new THREE.CircleGeometry(28, 64),
    new THREE.MeshStandardMaterial({
      color: 0x05070b,
      metalness: 0.85,
      roughness: 0.35,
    })
  );
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.92;
  ground.receiveShadow = true;
  scene.add(ground);

  // Thin gold ring frame
  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(11, 0.02, 8, 128),
    new THREE.MeshStandardMaterial({
      color: GOLD,
      metalness: 1,
      roughness: 0.2,
      emissive: GOLD,
      emissiveIntensity: 0.25,
    })
  );
  ring.rotation.x = Math.PI / 2;
  ring.position.y = -0.88;
  scene.add(ring);

  const mats = buildHullMaterials();
  const vessel = buildVessel(mats);
  scene.add(vessel);
  const water = buildWater();
  scene.add(water);
  const wake = buildWake();
  scene.add(wake);
  const flows = buildFlowLines();
  scene.add(flows);

  // Badges (mode 5)
  const metrics = {
    name: flagship.vessel_name || "PRELUDE",
    imo: flagship.imo || "9648714",
    dwt: flagship.dwt_tons || 394330,
    gt: flagship.gt || 499167,
    flag: flagship.flag || "Австралия",
    loa: flagship.loa_m || 488.8,
  };
  const badgeDefs = [
    { pos: [0, 3.2, 0], html: `<b>Дедвейт</b><span>${Number(metrics.dwt).toLocaleString("ru-RU")} т</span>` },
    { pos: [-5.6, 4.8, 0], html: `<b>Надстройка</b><span>${metrics.name}</span>` },
    { pos: [7.2, 2.0, 0], html: `<b>IMO</b><span>${metrics.imo}</span>` },
    { pos: [2, 2.8, 1.8], html: `<b>Флаг</b><span>${metrics.flag}</span>` },
    { pos: [-2, 2.5, -1.6], html: `<b>GT</b><span>${Number(metrics.gt).toLocaleString("ru-RU")}</span>` },
  ];
  const badgeObjs = [];
  badgeDefs.forEach((b) => {
    const el = document.createElement("div");
    el.className = "lv-badge3d";
    el.innerHTML = b.html;
    el.style.opacity = "0";
    const obj = new CSS2DObject(el);
    obj.position.set(...b.pos);
    vessel.add(obj);
    badgeObjs.push(el);
  });

  // Bloom composer
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(w(), h()), 0.45, 0.55, 0.85);
  composer.addPass(bloom);

  let mode = "cinematic";
  let modeTime = 0;
  let camAuto = true;
  let active = true;
  let heavy = true;
  let last = performance.now();
  let frames = 0;
  let fpsAcc = 0;
  let hullOpacity = 1;
  const hullMats = [];
  vessel.traverse((o) => {
    if (o.isMesh && o.material && o.parent?.name !== "internals") {
      if (Array.isArray(o.material)) hullMats.push(...o.material);
      else hullMats.push(o.material);
    }
  });

  function setModeUI(id) {
    const meta = MODE_META.find((m) => m.id === id) || MODE_META[0];
    if (modeTitle) modeTitle.textContent = meta.label;
    if (modeHint) modeHint.textContent = meta.hint;
    hudModes.querySelectorAll(".lv-mode").forEach((btn) => {
      const on = btn.dataset.mode === id;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    rootEl.dataset.mode = id;
    // soft flash
    rootEl.classList.remove("lv-pulse");
    void rootEl.offsetWidth;
    rootEl.classList.add("lv-pulse");
  }

  function applyModeVisuals(id) {
    const { internals, scan, hotspots, hullGroup } = vessel.userData;
    internals.visible = id === "xray";
    scan.visible = id === "scan";
    hotspots.visible = id === "scan";
    wake.visible = id === "hydro";
    flows.visible = id === "hydro";
    water.visible = id === "hydro" || id === "cinematic";
    badgeObjs.forEach((el) => {
      el.style.opacity = id === "flagship" ? "1" : "0";
    });

    // X-ray hull
    hullGroup.traverse((o) => {
      if (!o.isMesh) return;
      if (id === "xray") {
        if (!o.userData._matBackup) o.userData._matBackup = o.material;
        if (o.name !== "waterline" && o.material?.emissive?.getHex?.() !== GOLD) {
          o.material = mats.xray;
        }
      } else if (o.userData._matBackup) {
        o.material = o.userData._matBackup;
      }
    });

    bloom.strength = id === "scan" || id === "flagship" ? 0.7 : 0.4;
  }

  function flyCamera(toPos, toTarget, dur = 1500) {
    camAuto = false;
    const fromPos = camera.position.clone();
    const fromT = controls.target.clone();
    tween(
      { x: fromPos.x, y: fromPos.y, z: fromPos.z, tx: fromT.x, ty: fromT.y, tz: fromT.z },
      { x: toPos.x, y: toPos.y, z: toPos.z, tx: toTarget.x, ty: toTarget.y, tz: toTarget.z },
      dur,
      (c) => {
        camera.position.set(c.x, c.y, c.z);
        controls.target.set(c.tx, c.ty, c.tz);
        controls.update();
      },
      () => {
        camAuto = mode === "cinematic" || mode === "hydro";
      }
    );
  }

  function switchMode(id) {
    if (id === mode) return;
    mode = id;
    modeTime = 0;
    setModeUI(id);
    applyModeVisuals(id);

    const targets = {
      cinematic: { pos: new THREE.Vector3(16, 7, 14), tgt: new THREE.Vector3(0, 1.2, 0), dur: 1600 },
      xray: { pos: new THREE.Vector3(0, 14, 0.01), tgt: new THREE.Vector3(0, 0.5, 0), dur: 1400 },
      hydro: { pos: new THREE.Vector3(10, 3.2, 12), tgt: new THREE.Vector3(-1, 0, 0), dur: 1500 },
      scan: { pos: new THREE.Vector3(2, 5, 16), tgt: new THREE.Vector3(0, 1.2, 0), dur: 1300 },
      flagship: { pos: new THREE.Vector3(-9, 5.5, 6), tgt: new THREE.Vector3(-5.2, 3.0, 0), dur: 1700 },
    };
    const t = targets[id] || targets.cinematic;
    flyCamera(t.pos, t.tgt, t.dur);
  }

  hudModes.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-mode]");
    if (btn) switchMode(btn.dataset.mode);
  });

  // Keyboard 1-5
  window.addEventListener("keydown", (e) => {
    if (!active) return;
    const n = Number(e.key);
    if (n >= 1 && n <= 5) switchMode(MODE_META[n - 1].id);
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

  // Pause when off-screen / scrolling away
  const io = new IntersectionObserver(
    (entries) => {
      active = entries.some((en) => en.isIntersecting && en.intersectionRatio > 0.15);
      if (!active) {
        heavy = false;
        renderer.shadowMap.enabled = false;
      }
    },
    { threshold: [0, 0.15, 0.4] }
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
      }, 180);
    },
    { passive: true }
  );

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) active = false;
  });

  setModeUI("cinematic");
  applyModeVisuals("cinematic");

  function animate(now) {
    requestAnimationFrame(animate);
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    frames++;
    fpsAcc += dt;
    if (fpsAcc >= 0.5) {
      const fps = Math.round(frames / fpsAcc);
      if (fpsEl) fpsEl.textContent = `${fps} FPS`;
      // adaptive quality
      if (fps < 28) {
        bloom.strength = Math.min(bloom.strength, 0.25);
        renderer.setPixelRatio(1);
        renderer.shadowMap.enabled = false;
      } else if (fps > 50 && heavy && active) {
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
        renderer.shadowMap.enabled = true;
      }
      frames = 0;
      fpsAcc = 0;
    }

    if (!active && document.hidden) return;

    modeTime += dt;
    controls.update();

    // Mode behaviours
    if (mode === "cinematic" && camAuto) {
      const a = modeTime * 0.22;
      const r = 15 + Math.sin(modeTime * 0.15) * 1.5;
      camera.position.x = Math.cos(a) * r;
      camera.position.z = Math.sin(a) * r;
      camera.position.y = 5.5 + Math.sin(modeTime * 0.3) * 1.2;
      controls.target.set(0, 1.1 + Math.sin(modeTime * 0.2) * 0.2, 0);
      // subtle DoF proxy via bloom pulse
      bloom.strength = 0.35 + Math.sin(modeTime * 0.5) * 0.08;
    }

    if (mode === "xray") {
      vessel.rotation.y = Math.sin(modeTime * 0.25) * 0.15;
      vessel.userData.internals.children.forEach((c, i) => {
        if (c.material?.emissiveIntensity != null) {
          c.material.emissiveIntensity = 0.3 + Math.sin(modeTime * 2 + i) * 0.25;
        }
      });
    } else {
      vessel.rotation.y *= 0.92;
    }

    if (mode === "hydro") {
      vessel.position.y = Math.sin(modeTime * 1.4) * 0.08;
      vessel.rotation.z = Math.sin(modeTime * 1.1) * 0.02;
      vessel.rotation.x = Math.sin(modeTime * 0.9) * 0.015;
      // waves
      const pos = water.geometry.attributes.position;
      const base = water.userData.base;
      for (let i = 0; i < pos.count; i++) {
        const ix = i * 3;
        const x = base[ix];
        const z = base[ix + 2];
        pos.array[ix + 1] =
          Math.sin(x * 0.35 + modeTime * 2.2) * 0.12 +
          Math.cos(z * 0.4 + modeTime * 1.6) * 0.08;
      }
      pos.needsUpdate = true;
      water.geometry.computeVertexNormals();
      wake.userData.pts.forEach((p, i) => {
        p.position.x = -8 - ((i * 0.55 + modeTime * 3.5) % 18);
        p.position.y = -0.65 + Math.sin(modeTime * 4 + i) * 0.05;
        p.material.opacity = 0.15 + (i / 40) * 0.4;
      });
      if (camAuto) {
        const a = modeTime * 0.18;
        camera.position.x = 10 + Math.cos(a) * 2;
        camera.position.z = 11 + Math.sin(a) * 3;
      }
    } else {
      vessel.position.y *= 0.9;
    }

    if (mode === "scan") {
      const scan = vessel.userData.scan;
      const t = (modeTime * 0.35) % 1;
      scan.position.x = 8 - t * 16;
      scan.material.opacity = 0.35 + Math.sin(modeTime * 6) * 0.2;
      vessel.userData.hotspots.children.forEach((s, i) => {
        const hit = Math.abs(scan.position.x - s.position.x) < 0.8;
        s.scale.setScalar(hit ? 1.6 : 1);
        s.material.emissiveIntensity = hit ? 1.4 : 0.5;
      });
    }

    if (mode === "flagship") {
      goldPoint.intensity = 1.2 + Math.sin(modeTime * 2) * 0.35;
      vessel.userData.hullGroup.getObjectByName("bridge")?.children.forEach((c) => {
        if (c.material?.emissive) {
          c.material.emissiveIntensity = 0.2 + Math.sin(modeTime * 3) * 0.15;
        }
      });
    }

    // thin gold ring idle spin
    ring.rotation.z = modeTime * 0.05;

    if (heavy && active) composer.render();
    else renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
  }
  requestAnimationFrame(animate);

  return { switchMode, MODE_META };
}

// Auto-boot if DOM ready
const boot = () => {
  const el = document.getElementById("lvVesselExhibit");
  if (!el || el.dataset.booted) return;
  el.dataset.booted = "1";
  const flagship =
    (typeof window !== "undefined" && window.__LV3D_FLAGSHIP__) || {};
  initVesselExhibit(el, flagship);
};
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
