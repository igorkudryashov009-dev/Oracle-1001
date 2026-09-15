/**
 * Q-Flex Digital Twin Fleet (sheet id: top10)
 * Shared PBR WebGL (cards) · Modal GLB inspector (GLTFLoader) with ortho triplet fallback
 *
 * NEVER-BLACK: if vessel_{IMO}.glb fails or WebGL context is lost, inspector
 * falls back to the 3-image orthographic triplet without throwing.
 */
import {
  TOP10_VESSELS,
  QFLEX_FLEET_BRAND,
  QFLEX_FLEET_SHORT,
} from "./top10_vessels_manifest.js";
import {
  showPhotoFallback,
  getSharedTop10Renderer,
} from "./vessel_3d_reconstruction.js";
import { mountTop10GlbViewer, isGlbReady, mountTop10VoxelCubeViewer, isVoxelCubesReady } from "./top10_3d_viewer.js?v=luma-v1";

const LUMA_VIDEO_BADGE =
  "AI-GENERATED VIDEO RECONSTRUCTION (LUMA.ai) · SPECULATIVE DETAIL · GEOMETRY AND MOTION NOT OSINT-VERIFIED";
const FLIGHT_VIDEO_BADGE =
  "Q-FLEX DIGITAL-TWIN FLIGHT LOOP · OPTIMIZED H.264 · MOTION NOT OSINT-VERIFIED";

function resolveVesselVideo(vessel) {
  const flight = vessel?.flight_video;
  if (flight?.ready && flight?.url) {
    return {
      url: String(flight.url),
      badge: String(flight.fidelity_badge || FLIGHT_VIDEO_BADGE),
      kind: "flight",
      bytes: Number(flight.bytes || 0),
    };
  }
  const luma = vessel?.luma_video;
  if (luma?.ready && luma?.url) {
    return {
      url: String(luma.url),
      badge: String(luma.fidelity_badge || LUMA_VIDEO_BADGE),
      kind: "luma",
      bytes: Number(luma.bytes || 0),
    };
  }
  return null;
}

function isLumaVideoReady(vessel) {
  return !!resolveVesselVideo(vessel);
}

function mountLumaVideo(modal, vessel) {
  const stage = modal.querySelector("#t10-video-stage");
  if (!stage || !vessel) return false;
  const clip = resolveVesselVideo(vessel);
  if (!clip?.url) return false;
  stage.hidden = false;
  stage.style.display = "";
  stage.innerHTML = "";
  const hud = document.createElement("div");
  hud.className = "t10-video-hud";
  const pathHint = vessel?.flight_video?.local_path || vessel?.real_video_path || clip.url;
  hud.innerHTML = `<div class="t10-glb-fidelity t10-video-fidelity" title="${clip.badge}">REAL VIDEO · ${clip.badge}</div>
    <div class="t10-video-path" title="${pathHint}">${pathHint}</div>`;
  const vid = document.createElement("video");
  vid.setAttribute("playsinline", "");
  vid.setAttribute("muted", "");
  vid.setAttribute("loop", "");
  vid.setAttribute("autoplay", "");
  vid.setAttribute("controls", "");
  vid.setAttribute("controlslist", "nodownload");
  vid.muted = true;
  vid.loop = true;
  vid.autoplay = true;
  vid.controls = true;
  // metadata: first request is cheap; avoids competing with card hover preload=auto storms.
  vid.preload = "metadata";
  vid.setAttribute(
    "aria-label",
    `REAL VIDEO flight IMO ${vessel.imo}`
  );
  vid.className = "t10-luma-video t10-real-video";
  // Kick network immediately (append before optional GLB teardown on the caller side).
  stage.dataset.videoSrc = clip.url;
  stage.dataset.videoKind = clip.kind || "real_video";
  stage.hidden = false;
  stage.style.display = "";
  stage.appendChild(hud);
  stage.appendChild(vid);
  vid.src = clip.url;
  try {
    vid.load();
  } catch (_) {
    /* ignore */
  }
  const t0 =
    typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
  const mark = (ev) => {
    const dt =
      (typeof performance !== "undefined" && performance.now ? performance.now() : Date.now()) - t0;
    try {
      console.info(`[REAL VIDEO] ${ev} IMO ${vessel.imo} +${Math.round(dt)}ms url=${clip.url}`);
    } catch (_) {
      /* ignore */
    }
  };
  vid.addEventListener("loadstart", () => mark("loadstart"), { once: true });
  vid.addEventListener("loadedmetadata", () => mark("loadedmetadata"), { once: true });
  vid.addEventListener("canplay", () => mark("canplay"), { once: true });
  const play = vid.play();
  if (play && typeof play.catch === "function") play.catch(() => {});
  return true;
}

const viewers = new Map();
let booted = false;
let modalEl = null;
let io = null;
let escBound = false;
/** IMO of vessel currently shown in inspector (null when closed). */
let activeInspectorImo = null;
/** Active modal GLB viewer (disposed on close / fallback). */
let modalGlbViewer = null;
/** Vessel object bound to the open inspector (for lazy 3D mount). */
let activeInspectorVessel = null;

function riskClass(level) {
  const L = String(level || "").toUpperCase();
  if (L === "EXTREME" || L === "HIGH") return "risk-hi";
  if (L === "MEDIUM") return "risk-mid";
  return "risk-lo";
}

function assertManifestUrls() {
  const missing = [];
  for (let rank = 1; rank <= 10; rank++) {
    const v = TOP10_VESSELS.find((x) => Number(x.rank) === rank);
    if (!v) {
      missing.push(`rank ${rank} missing`);
      continue;
    }
    const need = [`/assets/7000/${rank}-1.jpg`, `/assets/7000/${rank}-2.jpg`, `/assets/7000/${rank}-3.jpg`];
    const got = [v.refs?.side?.url, v.refs?.bow?.url, v.refs?.overhead?.url];
    need.forEach((u, i) => {
      if (got[i] !== u) missing.push(`IMO ${v.imo} expected ${u} got ${got[i]}`);
    });
  }
  if (missing.length) console.warn("[Q-FLEX] manifest URL audit:", missing);
  else console.info("[Q-FLEX] manifest URL audit PASS — 10×3 /assets/7000");
  return missing.length === 0;
}

function truthBanner() {
  const P = window.__SENTINEL_PAYLOAD__ || {};
  const tc = P.truth_contract || {};
  const top10 = tc.top10 || "http_assets_ok";
  return `<div class="t10-truth" title="Unified Truth Contract">
    <span class="dot"></span>
    TRUTH · Q-FLEX ${top10} · AIS ${tc.ais || "—"} · ROUTE ${tc.route || "—"} · TTF ${tc.ttf || "—"}
  </div>`;
}

function cardHtml(v) {
  const imo = String(v.imo);
  const rank = Number(v.rank);
  const rankLabel = String(rank).padStart(2, "0");
  const photo = `/assets/7000/${rank}-1.jpg`;
  const fb = v.refs?.side?.fallback_url || `assets/top10/${rank}-1.jpg`;
  const flag = String(v.flag || "—").toUpperCase();
  const glbOk = v?.glb?.ready === true;
  const clip = resolveVesselVideo(v);
  const videoOk = !!clip;
  const videoUrl = clip?.url ? String(clip.url).replace(/"/g, "&quot;") : "";
  const risk = String(v.destination_risk || "—").toUpperCase();
  const riskCls =
    risk === "HIGH" || risk === "EXTREME"
      ? "t10-pill--risk-hi"
      : risk === "MEDIUM"
        ? "t10-pill--risk-mid"
        : "";
  const twinDisabled = glbOk
    ? ""
    : ' disabled aria-disabled="true" title="3D model unavailable"';
  const videoDisabled = videoOk
    ? ""
    : ' disabled aria-disabled="true" title="REAL VIDEO unavailable"';
  const ais = Number(v.ais_integrity_pct || 0).toFixed(1);
  const hoverVideo = videoOk
    ? `<video class="t10-hover-video" muted loop playsinline preload="metadata"
        poster="${photo}" data-src="${videoUrl}" data-imo="${imo}"
        aria-label="Hover preview REAL VIDEO IMO ${imo}"></video>`
    : "";
  return `
  <article class="t10-card vessel-card" data-imo="${imo}" data-rank="${rank}" data-vessel-id="${imo}"${videoOk ? ` data-video-src="${videoUrl}"` : ""}>
    <div class="t10-media t10-viewport t10-parallax" data-viewport data-open-inspector="${imo}" data-parallax="1"${videoOk ? ` data-hover-video="1"` : ""}>
      <div class="t10-parallax-inner">
        <img class="t10-photo-fallback" src="${photo}" alt="${v.name || imo} side profile"
          decoding="async" loading="eager" data-fallback="${fb}"
          onerror="if(!this.dataset.fb){this.dataset.fb=1;this.src=this.dataset.fallback}"/>
        ${hoverVideo}
      </div>
      <div class="t10-media-fade" aria-hidden="true"></div>
      <div class="t10-seg" role="group" aria-label="Media mode">
        <button type="button" class="t10-seg-btn ${glbOk ? "is-primary" : ""}" data-open-glb="${imo}" data-vessel-id="${imo}" title="Open DIGITAL TWIN"${twinDisabled}>◈ DIGITAL TWIN</button>
        <button type="button" class="t10-seg-btn ${!glbOk && videoOk ? "is-primary" : ""}" data-open-video="${imo}" data-vessel-id="${imo}" title="Open REAL VIDEO"${videoDisabled}>▶ REAL VIDEO</button>
      </div>
    </div>
    <header class="t10-card-head">
      <div class="t10-title-row">
        <span class="t10-rank">#${rankLabel}</span>
        <h3 class="t10-name">${v.name || "—"}</h3>
      </div>
      <div class="t10-meta-row">
        <span class="t10-pill">IMO ${imo}</span>
        <span class="t10-pill">${flag}</span>
        <span class="t10-pill t10-pill--ok">STATUS · ACTIVE OSINT</span>
        <span class="t10-pill t10-pill--signal">SIGNAL · ${ais}%</span>
        <span class="t10-pill ${riskCls}">RISK · ${risk}</span>
      </div>
    </header>
    <div class="t10-telem" aria-label="Vessel telemetry">
      <div class="t10-telem-cell">
        <div class="lbl">LOA</div>
        <div class="val">${Number(v.loa_m).toFixed(1)}<span>m</span></div>
      </div>
      <div class="t10-telem-cell">
        <div class="lbl">BEAM</div>
        <div class="val">${Number(v.beam_m).toFixed(1)}<span>m</span></div>
      </div>
      <div class="t10-telem-cell">
        <div class="lbl">DRAFT</div>
        <div class="val">${Number(v.draft_m).toFixed(1)}<span>m</span></div>
      </div>
      <div class="t10-telem-cell">
        <div class="lbl">DWT</div>
        <div class="val">${Math.round(Number(v.dwt_tons || 0) / 1000)}<span>kt</span></div>
      </div>
    </div>
    <footer class="t10-card-foot">
      <button type="button" class="t10-foot-btn t10-ref-btn" data-refs="${imo}" data-vessel-id="${imo}">ORTHO TRIPLET</button>
      ${formatVideoBadge(v)}
    </footer>
  </article>`;
}

function formatVideoBadge(v) {
  const fv = v?.flight_video || {};
  if (!fv.ready) {
    return `<span class="t10-vid-badge is-missing"><span class="dot"></span>MISSING</span>`;
  }
  const mb = Number(fv.bytes || 0) / (1024 * 1024);
  const label = `OPTIMIZED MP4 · ${mb.toFixed(1)} MB`;
  return `<span class="t10-vid-badge" title="${fv.local_path || fv.url || ""}"><span class="dot"></span>${label}</span>`;
}

/** Frame-accurate poster↔video hover (frame-0 = 0.0001s) + 3D tilt + scrub. */
function bindParallaxHoverVideo(grid) {
  if (!grid || grid.dataset.parallaxBound === "1") return;
  grid.dataset.parallaxBound = "1";

  const MAX_TILT = 9;
  const FRAME0 = 0.0001;
  const reduceMotion =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let activeCard = null;

  function mediaOf(card) {
    return card?.querySelector?.(".t10-parallax") || null;
  }

  function seekFrame0(vid) {
    if (!vid) return;
    const apply = () => {
      try {
        vid.currentTime = FRAME0;
      } catch (_) {
        /* ignore */
      }
    };
    if (vid.readyState >= 1) apply();
    else vid.addEventListener("loadedmetadata", apply, { once: true });
  }

  /** Pre-warm: bind poster=img, assign src, decode frame-0 into VRAM. */
  function prewarmCardVideo(card) {
    const media = mediaOf(card);
    const vid = media?.querySelector?.(".t10-hover-video");
    if (!vid) return;
    const img = media.querySelector(".t10-photo-fallback");
    const imgSrc = img?.currentSrc || img?.src || vid.getAttribute("poster") || "";
    if (imgSrc) {
      try {
        vid.poster = imgSrc;
      } catch (_) {
        /* ignore */
      }
    }
    const src =
      vid.getAttribute("data-src") ||
      card.getAttribute("data-video-src") ||
      "";
    if (src && vid.getAttribute("src") !== src) {
      // metadata only on cards — full auto preload of 10×~2MB saturates the pipe before Inspector.
      vid.preload = "metadata";
      vid.setAttribute("muted", "");
      vid.setAttribute("playsinline", "");
      vid.setAttribute("loop", "");
      vid.muted = true;
      vid.defaultMuted = true;
      vid.loop = true;
      vid.playsInline = true;
      vid.src = src;
      vid.load();
    } else {
      vid.setAttribute("muted", "");
      vid.muted = true;
      vid.defaultMuted = true;
    }
    seekFrame0(vid);
    // Force decode of first frame without audible play
    const kick = vid.play();
    if (kick && typeof kick.then === "function") {
      kick
        .then(() => {
          vid.pause();
          seekFrame0(vid);
        })
        .catch(() => seekFrame0(vid));
    }
  }

  function deactivateCardVideo(card) {
    if (!card) return;
    card.classList.remove("is-video-active");
    card.style.transform = "perspective(1000px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)";
    const media = mediaOf(card);
    if (!media) return;
    media.classList.remove("is-hover-playing");
    const vid = media.querySelector(".t10-hover-video");
    if (vid) {
      try {
        vid.pause();
      } catch (_) {
        /* ignore */
      }
      seekFrame0(vid);
    }
    const inner = media.querySelector(".t10-parallax-inner");
    if (inner) inner.style.transform = "";
  }

  function activateCardVideo(card) {
    if (!card || reduceMotion) return;
    if (modalEl && !modalEl.hidden) return; // Lazy-unmount: never play background videos when modal inspector is open
    const media = mediaOf(card);
    if (!media || media.getAttribute("data-hover-video") !== "1") return;
    const vid = media.querySelector(".t10-hover-video");
    if (!vid) return;
    prewarmCardVideo(card);
    seekFrame0(vid);
    vid.setAttribute("muted", "");
    vid.muted = true;
    vid.defaultMuted = true;
    card.classList.add("is-video-active");
    media.classList.add("is-hover-playing");
    const p = vid.play();
    if (p && typeof p.then === "function") {
      p.catch((err) => {
        console.warn("[Sentinel HUD] Video playback blocked or missing asset:", err);
      });
    }
  }

  /** Chrome autoplay policy: unlock muted play after first user gesture. */
  function bindAutoplayUnlock() {
    if (grid.dataset.autoplayUnlock === "1") return;
    grid.dataset.autoplayUnlock = "1";
    const unlock = () => {
      grid.querySelectorAll(".t10-hover-video, .vessel-card video, video").forEach((v) => {
        try {
          v.setAttribute("muted", "");
          v.muted = true;
          v.defaultMuted = true;
          const kick = v.play();
          if (kick && typeof kick.then === "function") {
            kick
              .then(() => {
                v.pause();
                seekFrame0(v);
              })
              .catch(() => {});
          }
        } catch (_) {
          /* ignore */
        }
      });
      window.removeEventListener("pointerdown", unlock, true);
      window.removeEventListener("keydown", unlock, true);
    };
    window.addEventListener("pointerdown", unlock, true);
    window.addEventListener("keydown", unlock, true);
  }

  bindAutoplayUnlock();

  function tiltAndScrub(card, clientX, clientY) {
    if (reduceMotion || !card) return;
    const rect = card.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const x = clientX - rect.left - rect.width / 2;
    const y = clientY - rect.top - rect.height / 2;
    const rotX = Math.max(-MAX_TILT, Math.min(MAX_TILT, (y / (rect.height / 2)) * -MAX_TILT));
    const rotY = Math.max(-MAX_TILT, Math.min(MAX_TILT, (x / (rect.width / 2)) * MAX_TILT));
    card.style.transform =
      `perspective(1000px) rotateX(${rotX.toFixed(2)}deg) rotateY(${rotY.toFixed(2)}deg) scale3d(1.02, 1.02, 1.02)`;

    const media = mediaOf(card);
    const inner = media?.querySelector(".t10-parallax-inner");
    if (inner) {
      inner.style.transform =
        `perspective(1000px) rotateX(${(rotX * 0.85).toFixed(2)}deg) rotateY(${(rotY * 0.85).toFixed(2)}deg) scale(1.04)`;
    }
    // Scrub only after frame-0 is warm and duration known
    const vid = media?.querySelector(".t10-hover-video");
    if (vid && Number.isFinite(vid.duration) && vid.duration > 0.05) {
      const px = (clientX - rect.left) / rect.width;
      const t = Math.max(FRAME0, Math.min(vid.duration - 0.05, px * vid.duration));
      try {
        vid.currentTime = t;
      } catch (_) {
        /* ignore seek race */
      }
    }
  }

  // Pre-warm all card videos once (frame-0 decode)
  if (!reduceMotion) {
    grid.querySelectorAll(".t10-card[data-video-src], .vessel-card[data-video-src]").forEach((card) => {
      prewarmCardVideo(card);
    });
  }

  grid.__t10ActivateCardVideo = activateCardVideo;
  grid.__t10DeactivateCardVideo = deactivateCardVideo;

  grid.addEventListener("pointerover", (ev) => {
    const card = ev.target.closest?.(".t10-card, .vessel-card, [data-vessel-card]");
    if (!card || !grid.contains(card)) return;
    if (activeCard === card) return;
    if (activeCard) deactivateCardVideo(activeCard);
    activeCard = card;
    activateCardVideo(card);
  });

  grid.addEventListener("pointerout", (ev) => {
    const card = ev.target.closest?.(".t10-card, .vessel-card, [data-vessel-card]");
    if (!card || !grid.contains(card)) return;
    const related = ev.relatedTarget;
    if (related && card.contains(related)) return;
    deactivateCardVideo(card);
    if (activeCard === card) activeCard = null;
  });

  grid.addEventListener(
    "pointermove",
    (ev) => {
      const card = ev.target.closest?.(".t10-card, .vessel-card, [data-vessel-card]");
      if (!card || !grid.contains(card)) return;
      if (ev.target.closest?.(".t10-seg, .t10-foot-btn, button")) return;
      tiltAndScrub(card, ev.clientX, ev.clientY);
    },
    { passive: true }
  );

  document.addEventListener("visibilitychange", () => {
    if (document.hidden && activeCard) {
      deactivateCardVideo(activeCard);
      activeCard = null;
    }
  });
}

/** Registry table REAL VIDEO -> scroll to card + activate hover preview (IMO-keyed). */
function bindRegistryCardLinker(wrap, grid) {
  const regWrap = wrap?.querySelector?.(".t10-registry-wrap");
  if (!regWrap || !grid || regWrap.dataset.cardLinker === "1") return;
  regWrap.dataset.cardLinker = "1";

  regWrap.addEventListener("click", (ev) => {
    const btn = ev.target.closest?.("[data-open-video], .t10-reg-open");
    if (!btn || !regWrap.contains(btn)) return;
    const imo = btn.getAttribute("data-open-video") || btn.getAttribute("data-vessel-id");
    if (!imo) return;
    const card =
      grid.querySelector(`.t10-card[data-imo="${imo}"]`) ||
      grid.querySelector(`.vessel-card[data-imo="${imo}"]`);
    if (!card) return;
    // Preview on card first; modal still available via card segment button
    ev.preventDefault();
    ev.stopPropagation();
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("t10-card--flash");
    setTimeout(() => card.classList.remove("t10-card--flash"), 900);
    if (typeof grid.__t10ActivateCardVideo === "function") {
      grid.__t10ActivateCardVideo(card);
    }
  });
}

function registryTableHtml() {
  const rows = TOP10_VESSELS.slice(0, 10)
    .map((v) => {
      const fv = v.flight_video || {};
      const path = fv.local_path || v.real_video_path || fv.url || "—";
      const url = fv.url || "#";
      return `<tr data-imo="${v.imo}">
        <td>#${String(v.rank).padStart(2, "0")}</td>
        <td><strong>${v.name || "—"}</strong></td>
        <td>${v.imo}</td>
        <td><a href="${url}" target="_blank" rel="noopener" title="${path}">${path}</a></td>
        <td>${formatVideoBadge(v)}</td>
        <td><button type="button" class="t10-reg-open" data-open-video="${v.imo}">▶ REAL VIDEO</button></td>
      </tr>`;
    })
    .join("");
  return `<div class="t10-registry-wrap">
    <h3 class="t10-registry-title">${QFLEX_FLEET_BRAND || "Q-Flex Digital Twin & Video Fleet"}</h3>
    <table class="t10-registry" id="t10-registry">
      <thead>
        <tr>
          <th>#</th>
          <th>Vessel</th>
          <th>IMO</th>
          <th>Real Video Path</th>
          <th>Media</th>
          <th></th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

function getTop10Grid(root) {
  const scope = root || document;
  return (
    scope.querySelector?.("#top10-grid-container") ||
    scope.querySelector?.("#t10Grid") ||
    document.getElementById("top10-grid-container") ||
    document.getElementById("t10Grid")
  );
}

function lockGridVisibility(grid) {
  if (!grid) return;
  if (grid.id !== "top10-grid-container") {
    grid.id = "top10-grid-container";
  }
  grid.classList.add("t10-grid", "top10-grid");
  Object.assign(grid.style, {
    display: "grid",
    visibility: "visible",
    opacity: "1",
    gap: "28px",
    width: "100%",
    // Leave columns to CSS (strict 2-col / 1-col ≤1024) — do not force auto-fit inline
    gridTemplateColumns: "",
  });
  grid.style.removeProperty("grid-template-columns");
}

function setSharedCanvasModalMode(active) {
  // Cards are photo-only — tear down any leaked shared card WebGL (legacy scissor overlay).
  // Live 3D exists only inside the modal GLB viewer.
  const shared = getSharedTop10Renderer(false);
  const canvas = document.getElementById("t10-shared-webgl");
  if (shared || canvas) {
    try {
      shared?.dispose?.();
    } catch {
      /* */
    }
    canvas?.remove?.();
  }
  // Keep photo previews fully opaque regardless of modal state
  document
    .querySelectorAll("#top10-grid-container .t10-photo-fallback, #t10Grid .t10-photo-fallback")
    .forEach((img) => {
      img.style.opacity = "1";
      img.style.zIndex = "1";
    });
  void active; // modal no longer toggles a card WebGL layer
}

/**
 * Never-Black ortho image binder: primary → fallback → placeholder message.
 * No canvas / WebGL involved.
 */
function bindOrthoImage(img, { src, fallback, label }) {
  if (!img) return;
  const ph = img.parentElement?.querySelector(".t10-ref-unavailable");
  const showPlaceholder = () => {
    img.classList.add("is-broken");
    img.removeAttribute("src");
    img.alt = `${label} — unavailable`;
    if (ph) {
      ph.hidden = false;
      const title = ph.querySelector(".ua-title");
      const sub = ph.querySelector(".ua-sub");
      if (title) title.textContent = "Ortho reference unavailable";
      if (sub) sub.textContent = `${label} · asset missing or blocked (404 / CORS / timeout)`;
    }
  };
  const hidePlaceholder = () => {
    img.classList.remove("is-broken");
    if (ph) ph.hidden = true;
  };

  img.onload = () => hidePlaceholder();
  img.onerror = () => {
    if (!img.dataset.fbTried && fallback && fallback !== src) {
      img.dataset.fbTried = "1";
      img.src = fallback;
      return;
    }
    showPlaceholder();
  };
  img.dataset.fbTried = "";
  hidePlaceholder();
  img.src = src;
}

function resetInspectorDom(modal) {
  // Full overwrite contract — never merge with previous vessel state.
  teardownLumaVideo(modal);
  disposeModalGlb();
  const gauges = modal.querySelector("#t10-insp-gauges");
  const dimBar = modal.querySelector("#t10-insp-dim-bar");
  const grid = modal.querySelector("#t10-modal-grid");
  const stage = modal.querySelector("#t10-glb-stage");
  if (gauges) gauges.innerHTML = "";
  if (dimBar) dimBar.innerHTML = "";
  if (stage) {
    stage.classList.remove("is-glb-ready", "is-glb-fallback", "is-glb-loading");
    stage.style.display = "none";
    stage.querySelectorAll("canvas, .t10-glb-hud, .t10-glb-unavailable").forEach((n) => n.remove());
  }
  if (grid) {
    grid.querySelectorAll("img").forEach((img) => {
      img.onload = null;
      img.onerror = null;
      img.removeAttribute("src");
    });
    grid.innerHTML = "";
    grid.style.gridTemplateColumns = "";
    grid.hidden = false;
  }
  setMeshWarnBanner(modal, "", false);
  modal.querySelectorAll(".t10-insp-tab").forEach((t, i) => {
    // Default active = Ortho Triplet (data-tab="all")
    const tab = t.getAttribute("data-tab");
    t.classList.toggle("active", tab === "all");
    t.classList.remove("is-degraded", "is-disabled");
    t.removeAttribute("aria-disabled");
    t.removeAttribute("title");
  });
  const aisTag = modal.querySelector("#t10-insp-ais-tag");
  const riskTag = modal.querySelector("#t10-insp-risk-tag");
  if (aisTag) { aisTag.textContent = "AIS INTEGRITY: —"; aisTag.style.color = ""; }
  if (riskTag) { riskTag.textContent = "DEST RISK: —"; riskTag.style.color = ""; }
  modal.querySelector("#t10-insp-rank").textContent = "#—";
  modal.querySelector("#t10-insp-name").textContent = "—";
  const classEl = modal.querySelector("#t10-insp-class");
  if (classEl) classEl.textContent = "—";
  modal.querySelector("#t10-insp-sub").textContent = "IMO — · FLAG —";
  delete modal.dataset.vesselId;
  delete modal.dataset.vesselRank;
  delete modal.dataset.viewerMode;
  delete modal.dataset.fallbackApplied;
  delete modal.dataset.glbReady;
  delete modal.dataset.voxelReady;
  delete modal.dataset.videoReady;
  activeInspectorVessel = null;
}

function teardownLumaVideo(modal) {
  const stage = modal?.querySelector?.("#t10-video-stage");
  if (!stage) return;
  const vid = stage.querySelector("video");
  if (vid) {
    try {
      vid.pause();
      vid.removeAttribute("src");
      vid.removeAttribute("autoplay");
      vid.load();
    } catch {
      /* */
    }
    vid.remove();
  }
  stage.querySelectorAll(".t10-video-hud").forEach((n) => n.remove());
  stage.style.display = "none";
  stage.hidden = true;
  delete stage.dataset.videoSrc;
  delete stage.dataset.videoKind;
}

function disposeModalGlb() {
  if (modalGlbViewer) {
    try {
      modalGlbViewer.dispose();
    } catch {
      /* never throw on teardown */
    }
    modalGlbViewer = null;
  }
}

function setMeshWarnBanner(modal, reason, show) {
  const el = modal?.querySelector?.("#t10-insp-mesh-warn");
  if (!el) return;
  if (!show) {
    el.hidden = true;
    el.textContent = "";
    el.removeAttribute("data-reason");
    return;
  }
  el.hidden = false;
  el.setAttribute("data-reason", reason || "unavailable");
  const msg =
    reason === "not-ready" || reason === "glb-unavailable"
      ? "[3D MODEL UNAVAILABLE] · Ortho Triplet remains active · hull envelope not ready for this IMO"
      : reason === "glb-http" || reason === "glb-load" || reason === "glb-error"
        ? "[3D MODEL UNAVAILABLE] · load/parse failed · staying on Ortho Triplet (Never-Black)"
        : "[OSINT NOTE: 3D HULL ENVELOPE FAILED] · falling back to Orthographic Triplet" +
          (reason ? ` · ${reason}` : "");
  el.textContent = msg;
}

/**
 * Automated fallback state machine: unmount WebGL → Ortho Triplet.
 * Idempotent per modal open generation (no duplicate listeners / double teardown).
 */
function triggerTriViewFallback(modal, reason) {
  if (!modal) return;
  const gen = String(modal._openGen || 0);
  if (modal.dataset.fallbackApplied === gen) {
    setMeshWarnBanner(modal, reason, true);
    return;
  }
  modal.dataset.fallbackApplied = gen;

  disposeModalGlb();

  const stage = modal.querySelector("#t10-glb-stage");
  if (stage) {
    stage.classList.add("is-glb-fallback");
    stage.classList.remove("is-glb-ready", "is-glb-loading");
    stage.querySelectorAll("canvas, .t10-glb-hud, .t10-glb-unavailable").forEach((n) => n.remove());
    stage.style.display = "none";
  }

  const grid = modal.querySelector("#t10-modal-grid");
  if (grid) {
    grid.hidden = false;
    grid.style.gridTemplateColumns = "";
    grid.querySelectorAll(".t10-ref-fig").forEach((fig) => {
      fig.style.display = "";
    });
  }

  // Sync tabs: Ortho Triplet active; 3D View marked unavailable
  modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
    const mode = t.getAttribute("data-tab");
    t.classList.toggle("active", mode === "all");
    if (mode === "glb") {
      t.classList.add("is-degraded");
      t.setAttribute("aria-disabled", "true");
    } else {
      t.classList.remove("is-degraded");
      t.removeAttribute("aria-disabled");
    }
  });

  setMeshWarnBanner(modal, reason || "unavailable", true);
  modal.dataset.viewerMode = reason ? `ortho:${reason}` : "ortho:triplet";
}

/** @deprecated use triggerTriViewFallback */
function showOrthoFallback(modal, reason) {
  triggerTriViewFallback(modal, reason);
}

function showGlbUnavailableStage(stage, detail) {
  if (!stage) return;
  stage.style.display = "";
  stage.classList.remove("is-glb-ready", "is-glb-loading", "is-glb-fallback");
  stage.querySelectorAll("canvas, .t10-glb-hud, .t10-glb-unavailable").forEach((n) => n.remove());
  const box = document.createElement("div");
  box.className = "t10-glb-unavailable";
  box.setAttribute("role", "status");
  box.innerHTML = `
    <div class="ua-title">3D model unavailable</div>
    <div class="ua-sub">${detail || "Hull envelope missing or failed to load · Ortho Triplet stays active"}</div>`;
  stage.appendChild(box);
}

/**
 * Lazy-mount voxel InstancedMesh (mutually exclusive with Digital Twin GLB).
 */
async function ensureVoxelMounted(modal, vessel) {
  if (!modal || !vessel) return false;
  if (!isVoxelCubesReady(vessel)) {
    showGlbUnavailableStage(
      modal.querySelector("#t10-glb-stage"),
      "Manifest voxel_cubes.ready !== true"
    );
    setMeshWarnBanner(modal, "not-ready", true);
    return false;
  }

  const stage = modal.querySelector("#t10-glb-stage");
  if (!stage) return false;
  // Tear any prior GLB/voxel before mount (single shared context)
  disposeModalGlb();
  stage.style.display = "";
  stage.classList.add("is-glb-loading");
  stage.classList.remove("is-glb-fallback", "is-glb-ready");
  stage.querySelectorAll(".t10-glb-unavailable, canvas, .t10-glb-hud").forEach((n) => n.remove());

  const openGen = modal._openGen || 0;
  const mountGen = (modal._glbMountGen = (modal._glbMountGen || 0) + 1);
  try {
    const viewer = await mountTop10VoxelCubeViewer(stage, vessel, {
      onFallback: (reason) => {
        if (modal._openGen !== openGen || modal._glbMountGen !== mountGen) return;
        triggerTriViewFallback(modal, reason || "contextlost");
      },
    });
    if (
      modal._openGen !== openGen ||
      modal._glbMountGen !== mountGen ||
      modal.hidden ||
      modal.querySelector(".t10-insp-tab.active")?.getAttribute("data-tab") !== "voxel" &&
      modal.querySelector(".t10-insp-tab.active")?.getAttribute("data-tab") !== "overhead"
    ) {
      try {
        viewer?.dispose?.();
      } catch {
        /* */
      }
      return false;
    }
    if (!viewer || !viewer.ok) {
      setMeshWarnBanner(modal, "glb-load", true);
      showGlbUnavailableStage(stage, "Voxel grid failed to load");
      return false;
    }
    modalGlbViewer = viewer;
    stage.classList.add("is-glb-ready");
    stage.classList.remove("is-glb-fallback", "is-glb-loading");
    setMeshWarnBanner(modal, "", false);
    return true;
  } catch {
    if (modal._openGen !== openGen || modal._glbMountGen !== mountGen) return false;
    showGlbUnavailableStage(stage, "Voxel exception");
    setMeshWarnBanner(modal, "glb-error", true);
    return false;
  }
}

/**
 * Lazy-mount GLB only when operator requests 3D View (never on inspector open).
 */
async function ensureGlbMounted(modal, vessel) {
  if (!modal || !vessel) return false;
  if (modalGlbViewer?.ok) return true;
  if (!isGlbReady(vessel)) {
    showGlbUnavailableStage(modal.querySelector("#t10-glb-stage"), "Manifest glb.ready !== true");
    setMeshWarnBanner(modal, "not-ready", true);
    return false;
  }

  const stage = modal.querySelector("#t10-glb-stage");
  if (!stage) return false;
  stage.style.display = "";
  stage.classList.add("is-glb-loading");
  stage.classList.remove("is-glb-fallback", "is-glb-ready");
  stage.querySelectorAll(".t10-glb-unavailable").forEach((n) => n.remove());

  const openGen = modal._openGen || 0;
  const mountGen = (modal._glbMountGen = (modal._glbMountGen || 0) + 1);
  try {
    const viewer = await mountTop10GlbViewer(stage, vessel, {
      onFallback: (reason) => {
        if (modal._openGen !== openGen || modal._glbMountGen !== mountGen) return;
        triggerTriViewFallback(modal, reason || "contextlost");
      },
    });
    // Stale mount (closed / switched to Ortho / new vessel) — drop GPU payload
    const tabNow = modal.querySelector(".t10-insp-tab.active")?.getAttribute("data-tab");
    if (
      modal._openGen !== openGen ||
      modal._glbMountGen !== mountGen ||
      modal.hidden ||
      (tabNow !== "glb" && tabNow !== "overhead")
    ) {
      viewer?.dispose?.();
      return false;
    }
    if (!viewer?.ok) {
      showGlbUnavailableStage(stage, "GLB load/parse failed");
      setMeshWarnBanner(modal, "glb-load", true);
      return false;
    }
    modalGlbViewer = viewer;
    stage.classList.add("is-glb-ready");
    stage.classList.remove("is-glb-fallback", "is-glb-loading");
    setMeshWarnBanner(modal, "", false);
    delete modal.dataset.fallbackApplied;
    return true;
  } catch {
    if (modal._openGen !== openGen || modal._glbMountGen !== mountGen) return false;
    showGlbUnavailableStage(stage, "GLB exception");
    setMeshWarnBanner(modal, "glb-error", true);
    return false;
  }
}

function applyInspectorTab(modal, mode) {
  if (!modal || !mode) return;
  if (mode !== "video") teardownLumaVideo(modal);
  const stage = modal.querySelector("#t10-glb-stage");
  const grid = modal.querySelector("#t10-modal-grid");
  const videoStage = modal.querySelector("#t10-video-stage");
  const glbDegraded =
    modal.dataset.fallbackApplied === String(modal._openGen || 0) ||
    modal.dataset.glbReady === "0";

  if (mode === "glb") {
    if (glbDegraded || modal.dataset.glbReady === "0") {
      // Never-Black: keep Ortho Triplet, show unavailable message
      modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
        t.classList.toggle("active", t.getAttribute("data-tab") === "all");
      });
      if (stage) {
        showGlbUnavailableStage(stage, "3D model unavailable for this vessel");
        stage.style.display = "none";
      }
      if (grid) {
        grid.hidden = false;
        grid.style.gridTemplateColumns = "";
      }
      setMeshWarnBanner(modal, "not-ready", true);
      modal.dataset.viewerMode = "ortho:not-ready";
      return;
    }

    modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
      t.classList.toggle("active", t.getAttribute("data-tab") === "glb");
    });
    if (grid) grid.hidden = true;
    if (stage) stage.style.display = "";
    modal.dataset.viewerMode = "glb:loading";

    const openGen = modal._openGen || 0;
    const mountGenAtClick = (modal._glbMountGen || 0) + 1; // ensureGlbMounted bumps this
    // Ensure we don't keep a voxel viewer alive when opening Digital Twin
    disposeModalGlb();
    ensureGlbMounted(modal, activeInspectorVessel).then((ok) => {
      if (modal._openGen !== openGen || modal.hidden) return;
      // User left 3D View while loading — do not resurrect GLB mode
      if (modal.querySelector(".t10-insp-tab.active")?.getAttribute("data-tab") !== "glb") {
        disposeModalGlb();
        return;
      }
      if (!ok) {
        if (grid) grid.hidden = false;
        if (stage) stage.style.display = "none";
        modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
          t.classList.toggle("active", t.getAttribute("data-tab") === "all");
        });
        modal.dataset.viewerMode = "ortho:glb-fail";
        return;
      }
      if (modal._glbMountGen !== mountGenAtClick) return;
      modal.dataset.viewerMode = "glb";
      if (grid) grid.hidden = true;
      if (stage) stage.style.display = "";
    });
    return;
  }

  if (mode === "voxel") {
    if (modal.dataset.voxelReady === "0") {
      modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
        t.classList.toggle("active", t.getAttribute("data-tab") === "all");
      });
      if (stage) {
        showGlbUnavailableStage(stage, "Voxel grid unavailable for this vessel");
        stage.style.display = "none";
      }
      if (grid) {
        grid.hidden = false;
        grid.style.gridTemplateColumns = "";
      }
      setMeshWarnBanner(modal, "not-ready", true);
      modal.dataset.viewerMode = "ortho:voxel-not-ready";
      return;
    }

    modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
      t.classList.toggle("active", t.getAttribute("data-tab") === "voxel");
    });
    if (grid) grid.hidden = true;
    if (stage) stage.style.display = "";
    modal.dataset.viewerMode = "voxel:loading";

    if (modalGlbViewer?.ok && typeof modalGlbViewer.setViewPreset === "function" && modalGlbViewer._nCells != null) {
      modalGlbViewer.setViewPreset("catalog");
      modal.dataset.viewerMode = "voxel";
      return;
    }

    const openGen = modal._openGen || 0;
    const mountGenAtClick = (modal._glbMountGen || 0) + 1;
    ensureVoxelMounted(modal, activeInspectorVessel).then((ok) => {
      if (modal._openGen !== openGen || modal.hidden) return;
      if (modal.querySelector(".t10-insp-tab.active")?.getAttribute("data-tab") !== "voxel") {
        disposeModalGlb();
        return;
      }
      if (!ok) {
        if (grid) grid.hidden = false;
        if (stage) stage.style.display = "none";
        modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
          t.classList.toggle("active", t.getAttribute("data-tab") === "all");
        });
        modal.dataset.viewerMode = "ortho:voxel-fail";
        return;
      }
      if (modal._glbMountGen !== mountGenAtClick) return;
      modal.dataset.viewerMode = "voxel";
      if (grid) grid.hidden = true;
      if (stage) stage.style.display = "";
    });
    return;
  }

  if (mode === "overhead") {
    modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
      t.classList.toggle("active", t.getAttribute("data-tab") === "overhead");
    });
    if (grid) grid.hidden = true;
    if (stage) stage.style.display = "";

    const applyOverhead = () => {
      if (typeof modalGlbViewer.setViewPreset === "function") {
        modalGlbViewer.setViewPreset("overhead");
      }
      modal.dataset.viewerMode = modalGlbViewer._nCells != null ? "overhead:voxel" : "overhead:glb";
    };

    // Prefer Voxel Grid (SAT top-face colors). GLB only if voxel unavailable.
    if (modalGlbViewer?.ok && modalGlbViewer._nCells != null) {
      applyOverhead();
      return;
    }
    if (modalGlbViewer?.ok && modal.dataset.voxelReady !== "0") {
      disposeModalGlb();
    } else if (modalGlbViewer?.ok) {
      applyOverhead();
      return;
    }

    if (modal.dataset.voxelReady === "0" && modal.dataset.glbReady === "0") {
      if (grid) {
        grid.hidden = false;
        grid.style.gridTemplateColumns = "1fr";
      }
      if (stage) stage.style.display = "none";
      modal.querySelectorAll(".t10-ref-fig").forEach((fig) => {
        fig.style.display = fig.getAttribute("data-view") === "overhead" ? "" : "none";
      });
      modal.dataset.viewerMode = "ortho:overhead";
      return;
    }

    modal.dataset.viewerMode = "overhead:loading";
    const openGen = modal._openGen || 0;
    const preferVoxel = modal.dataset.voxelReady !== "0";
    const mountFn = preferVoxel ? ensureVoxelMounted : ensureGlbMounted;
    mountFn(modal, activeInspectorVessel).then((ok) => {
      if (modal._openGen !== openGen || modal.hidden) return;
      if (modal.querySelector(".t10-insp-tab.active")?.getAttribute("data-tab") !== "overhead") {
        return;
      }
      if (!ok || !modalGlbViewer?.ok) {
        if (grid) {
          grid.hidden = false;
          grid.style.gridTemplateColumns = "1fr";
        }
        if (stage) stage.style.display = "none";
        modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
          t.classList.toggle("active", t.getAttribute("data-tab") === "overhead");
        });
        modal.querySelectorAll(".t10-ref-fig").forEach((fig) => {
          fig.style.display = fig.getAttribute("data-view") === "overhead" ? "" : "none";
        });
        modal.dataset.viewerMode = "ortho:overhead";
        return;
      }
      applyOverhead();
      if (grid) grid.hidden = true;
      if (stage) stage.style.display = "";
    });
    return;
  }

  if (mode === "video") {
    if (!isLumaVideoReady(activeInspectorVessel)) {
      return;
    }
    // Start MP4 fetch FIRST — GLB dispose is main-thread heavy and delayed Network start.
    if (grid) grid.hidden = true;
    if (stage) {
      stage.style.display = "none";
      stage.classList.remove("is-glb-ready", "is-glb-loading");
    }
    modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
      t.classList.toggle("active", t.getAttribute("data-tab") === "video");
    });
    mountLumaVideo(modal, activeInspectorVessel);
    modal.dataset.viewerMode = "video:real";
    // Tear down WebGL after video element has src (non-blocking for Network timing).
    queueMicrotask(() => disposeModalGlb());
    return;
  }

  // Ortho modes — invalidate in-flight GLB mounts + dispose live viewer
  modal._glbMountGen = (modal._glbMountGen || 0) + 1;
  if (modalGlbViewer) {
    disposeModalGlb();
  }
  if (stage) {
    stage.style.display = "none";
    stage.classList.remove("is-glb-ready", "is-glb-loading");
    stage.querySelectorAll("canvas, .t10-glb-hud, .t10-glb-unavailable").forEach((n) => n.remove());
  }
  modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
    t.classList.toggle("active", t.getAttribute("data-tab") === mode);
  });
  if (grid) {
    grid.hidden = false;
    grid.style.gridTemplateColumns = mode === "all" ? "" : "1fr";
  }
  modal.querySelectorAll(".t10-ref-fig").forEach((fig) => {
    const key = fig.getAttribute("data-view");
    fig.style.display = mode === "all" || mode === key ? "" : "none";
  });
  modal.dataset.viewerMode = mode === "all" ? "ortho:triplet" : `ortho:${mode}`;
}

function ensureModal() {
  if (modalEl && document.body.contains(modalEl)) {
    const firstTab = modalEl.querySelector(".t10-insp-tab");
    if (firstTab && (firstTab.getAttribute("data-tab") === "glb" || firstTab.getAttribute("data-tab") === "video")) {
      return modalEl;
    }
    try {
      modalEl.remove();
    } catch {
      /* */
    }
    modalEl = null;
  }
  modalEl = document.createElement("div");
  modalEl.id = "t10-ref-modal";
  modalEl.className = "t10-modal sentinel-modal-overlay";
  modalEl.hidden = true;
  modalEl.setAttribute("data-inspector", "photogrammetric-primary-glb-optional");
  modalEl.innerHTML = `
    <div class="t10-modal-backdrop" data-close></div>
    <div class="t10-modal-panel" role="dialog" aria-modal="true" aria-labelledby="t10-insp-name">

      <!-- ── NASA Mission Control Header ── -->
      <div class="t10-insp-head">
        <div class="t10-insp-header-top">
          <div class="t10-insp-hud-label">ORACLE-1001 · Q-FLEX · 3D DIGITAL TWIN + REAL VIDEO + INSPECTOR</div>
          <div class="t10-insp-utc" id="t10-insp-utc">UTC —</div>
        </div>
        <div class="t10-insp-title-row">
          <span class="t10-insp-rank" id="t10-insp-rank">#—</span>
          <h3 class="t10-insp-name" id="t10-insp-name">—</h3>
          <span class="t10-insp-class-badge" id="t10-insp-class">Q-Max / Membrane</span>
        </div>
        <div class="t10-insp-sub" id="t10-insp-sub">IMO — · FLAG —</div>
        <div class="t10-insp-mesh-warn" id="t10-insp-mesh-warn" hidden role="status" aria-live="polite"></div>
        <div class="t10-insp-gauges" id="t10-insp-gauges"></div>
        <div class="t10-insp-dim-bar" id="t10-insp-dim-bar"></div>
        <button type="button" class="t10-modal-x" data-close aria-label="Close inspector">×</button>
      </div>

      <div class="t10-insp-tabs" role="tablist" data-tabs-bound="0">
        <button type="button" class="t10-insp-tab active" data-tab="glb" role="tab" title="Photo-composite digital twin on simplified hull (not verified structural CAD)">
          <span class="tab-icon">◈</span> DIGITAL TWIN
        </button>
        <button type="button" class="t10-insp-tab" data-tab="voxel" role="tab" title="Equal-metric occupancy cubes from 3-view silhouette carve (not a continuous surface)">
          <span class="tab-icon">▦</span> VOXEL GRID
        </button>
        <button type="button" class="t10-insp-tab t10-insp-tab--video" data-tab="video" role="tab"
          title="Q-Flex REAL VIDEO flight loop (MP4)">
          <span class="tab-icon">▶</span> REAL VIDEO
        </button>
        <button type="button" class="t10-insp-tab" data-tab="all" role="tab" title="Primary measurement view">
          <span class="tab-icon">⬡</span> ORTHO TRIPLET
        </button>
        <button type="button" class="t10-insp-tab" data-tab="side" role="tab">
          <span class="tab-icon">◧</span> LATERAL
        </button>
        <button type="button" class="t10-insp-tab" data-tab="bow" role="tab">
          <span class="tab-icon">◮</span> BOW
        </button>
        <button type="button" class="t10-insp-tab" data-tab="overhead" role="tab" title="Orthographic top-down of voxel grid (SAT-aligned, not a photo zoom)">
          <span class="tab-icon">⬛</span> OVERHEAD
        </button>
      </div>

      <!-- Ortho grid (default) + optional GLB stage (lazy, Never-Black) -->
      <div class="t10-modal-grid" id="t10-modal-grid"></div>
      <div class="t10-glb-stage" id="t10-glb-stage" aria-live="polite" style="display:none"></div>
      <div class="t10-video-stage" id="t10-video-stage" hidden aria-live="polite"></div>

      <div class="t10-insp-footer">
        <div class="t10-insp-footer-tag" id="t10-insp-ais-tag">AIS INTEGRITY: —</div>
        <div class="t10-insp-footer-tag" id="t10-insp-risk-tag">DEST RISK: —</div>
        <div class="t10-insp-footer-tag">SOURCE: ORACLE-1001 OSINT</div>
        <div class="t10-insp-footer-tag">3D = HULL ENVELOPE (OPTIONAL)</div>
      </div>
    </div>`;
  document.body.appendChild(modalEl);
  modalEl.addEventListener("click", (e) => {
    if (e.target.closest("[data-close]")) closeRefs();
  });
  // Single delegated tab binder — never re-attach on reopen (modal is singleton)
  const tablist = modalEl.querySelector(".t10-insp-tabs");
  if (tablist && tablist.getAttribute("data-tabs-bound") !== "1") {
    tablist.setAttribute("data-tabs-bound", "1");
    tablist.addEventListener("click", (e) => {
      const tab = e.target.closest(".t10-insp-tab[data-tab]");
      if (!tab || !modalEl.contains(tab)) return;
      if (tab.hidden || tab.hasAttribute("hidden")) {
        e.preventDefault();
        return;
      }
      if (tab.getAttribute("aria-disabled") === "true" || tab.classList.contains("is-disabled")) {
        e.preventDefault();
        return;
      }
      const mode = tab.getAttribute("data-tab");
      applyInspectorTab(modalEl, mode);
    });
  }
  if (!escBound) {
    escBound = true;
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && modalEl && !modalEl.hidden) closeRefs();
    });
  }
  return modalEl;
}

function _fmtUtc() {
  return new Date().toUTCString().replace("GMT", "UTC");
}

function openRefs(vessel, opts = {}) {
  const modal = ensureModal();
  // Hard reset BEFORE writing new vessel — prevents telemetry bleed across opens
  resetInspectorDom(modal);

  // Lazy-unmount: pause and deactivate any playing card hover videos in the grid
  document.querySelectorAll(".t10-card.is-video-active, .t10-media.is-hover-playing").forEach((el) => {
    el.classList.remove("is-video-active", "is-hover-playing");
  });
  document.querySelectorAll(".t10-hover-video").forEach((vid) => {
    try {
      vid.pause();
    } catch (_) {}
  });

  const glbOk = isGlbReady(vessel);
  const voxelOk = isVoxelCubesReady(vessel);
  const videoOk = isLumaVideoReady(vessel);
  const defaultTab = glbOk ? "glb" : (voxelOk ? "voxel" : (videoOk ? "video" : "all"));
  const initialTab = String(
    opts?.tab || opts?.initialTab || defaultTab
  ).toLowerCase();

  const rank = Number(vessel.rank);
  const imo = String(vessel.imo);
  const ais = Number(vessel.ais_integrity_pct || 0);
  const risk = String(vessel.destination_risk || "—").toUpperCase();
  const riskColor =
    risk === "HIGH" || risk === "EXTREME"
      ? "var(--t10-crimson)"
      : risk === "MEDIUM"
        ? "var(--t10-amber)"
        : "var(--t10-ok)";

  activeInspectorImo = imo;
  modal.dataset.vesselId = imo;
  modal.dataset.vesselRank = String(rank);

  // ── Header metadata ─────────────────────────────────────────────────────────
  modal.querySelector("#t10-insp-rank").textContent = `#${rank}`;
  modal.querySelector("#t10-insp-name").textContent = vessel.name || "—";
  const classEl = modal.querySelector("#t10-insp-class");
  if (classEl) classEl.textContent = vessel.class || "Q-Max / Membrane";
  const utcEl = modal.querySelector("#t10-insp-utc");
  if (utcEl) utcEl.textContent = _fmtUtc();
  modal.querySelector("#t10-insp-sub").textContent =
    `IMO ${vessel.imo} · FLAG ${vessel.flag || "—"} · LOA ${Number(vessel.loa_m).toFixed(1)} m · BEAM ${Number(vessel.beam_m).toFixed(1)} m · DRAFT ${Number(vessel.draft_m).toFixed(1)} m`;

  // ── AIS telemetry gauges (full replace, not merge) ──────────────────────────
  modal.querySelector("#t10-insp-gauges").innerHTML = `
    <div class="t10-gauge" data-telem="ais">
      <div class="lbl">AIS Signal Integrity</div>
      <div class="val">${ais.toFixed(1)}%</div>
      <div class="bar"><i style="width:${Math.max(4, Math.min(100, ais))}%;background:${ais > 90 ? "linear-gradient(90deg,var(--t10-cyan),#32d74b)" : ais > 75 ? "linear-gradient(90deg,var(--t10-amber),var(--t10-cyan))" : "linear-gradient(90deg,var(--t10-crimson),var(--t10-amber))"}"></i></div>
    </div>
    <div class="t10-gauge" data-telem="risk">
      <div class="lbl">Destination Risk</div>
      <div class="val" style="color:${riskColor}">${risk}</div>
      <div class="bar"><i style="width:${risk === "EXTREME" ? 100 : risk === "HIGH" ? 80 : risk === "MEDIUM" ? 50 : 20}%;background:${riskColor}"></i></div>
    </div>
    <div class="t10-gauge" data-telem="dwt">
      <div class="lbl">DWT Capacity</div>
      <div class="val">${Number(vessel.dwt_tons || 0).toLocaleString("en-US")} t</div>
      <div class="bar"><i style="width:${Math.min(100, Math.round(Number(vessel.dwt_tons || 0) / 1800))}%"></i></div>
    </div>
    <div class="t10-gauge" data-telem="status">
      <div class="lbl">OSINT Status</div>
      <div class="val" style="font-size:11px;color:var(--t10-ok)">${vessel.status || "ACTIVE OSINT TRACK"}</div>
    </div>`;

  // ── Dimensional telemetry bar ───────────────────────────────────────────────
  const dimBar = modal.querySelector("#t10-insp-dim-bar");
  if (dimBar) {
    const loa = Number(vessel.loa_m).toFixed(1);
    const beam = Number(vessel.beam_m).toFixed(1);
    const draft = Number(vessel.draft_m).toFixed(1);
    const scaleRatio = `${(Number(vessel.loa_m) / Number(vessel.beam_m)).toFixed(1)}:1`;
    dimBar.innerHTML = `
      <span class="dim-tag" data-telem="loa">LOA <strong>${loa} m</strong></span>
      <span class="dim-sep">·</span>
      <span class="dim-tag" data-telem="beam">BEAM <strong>${beam} m</strong></span>
      <span class="dim-sep">·</span>
      <span class="dim-tag" data-telem="draft">DRAFT <strong>${draft} m</strong></span>
      <span class="dim-sep">·</span>
      <span class="dim-tag">L/B RATIO <strong>${scaleRatio}</strong></span>
      <span class="dim-sep">·</span>
      <span class="dim-tag dim-tag--imo">IMO <strong>${vessel.imo}</strong></span>`;
  }

  // ── Footer AIS + risk tags ──────────────────────────────────────────────────
  const aisTag = modal.querySelector("#t10-insp-ais-tag");
  if (aisTag) {
    aisTag.textContent = `AIS INTEGRITY: ${ais.toFixed(1)}%`;
    aisTag.style.color = ais > 90 ? "var(--t10-ok)" : ais > 75 ? "var(--t10-amber)" : "var(--t10-crimson)";
  }
  const riskTag = modal.querySelector("#t10-insp-risk-tag");
  if (riskTag) {
    riskTag.textContent = `DEST RISK: ${risk}`;
    riskTag.style.color = riskColor;
  }

  // ── Ortho scan images (never-black fallback, always populated) ─────────────
  const order = [
    ["side",     "Lateral Profile",      `/assets/7000/${rank}-1.jpg`, "SIDE ·−1"],
    ["bow",      "Bow Orthographic",     `/assets/7000/${rank}-2.jpg`, "BOW · −2"],
    ["overhead", "Top-Deck Satellite",   `/assets/7000/${rank}-3.jpg`, "SAT · −3"],
  ];
  const refs = vessel.refs || {};
  const modalGrid = modal.querySelector("#t10-modal-grid");
  modalGrid.style.gridTemplateColumns = "";
  modalGrid.innerHTML = order
    .map(([key, label, hardUrl, telem]) => {
      const r = refs[key] || {};
      const src = r.url || hardUrl;
      const fbIdx = key === "side" ? 1 : key === "bow" ? 2 : 3;
      const fb = r.fallback_url || `assets/top10/${rank}-${fbIdx}.jpg`;
      const dim =
        key === "side"
          ? `${Number(vessel.loa_m).toFixed(0)} m LOA`
          : key === "bow"
            ? `${Number(vessel.beam_m).toFixed(1)} m BEAM`
            : `${Number(vessel.loa_m).toFixed(0)} × ${Number(vessel.beam_m).toFixed(0)} m`;
      return `<figure class="t10-ref-fig" data-view="${key}" data-vessel-id="${imo}">
        <span class="t10-ref-telem">${telem}</span>
        <div class="t10-ref-unavailable" hidden>
          <span class="ua-title">Ortho reference unavailable</span>
          <span class="ua-sub">${label}</span>
        </div>
        <img data-ortho="${key}" data-src="${src}" data-fallback="${fb}" alt="${label}" loading="eager"/>
        <div class="t10-ref-ruler" aria-hidden="true">
          <span class="ruler-dim">${dim}</span>
        </div>
        <figcaption>
          <span>${label}</span>
          <span class="fig-dim-tag">${dim}</span>
        </figcaption>
      </figure>`;
    })
    .join("");

  modalGrid.querySelectorAll("img[data-ortho]").forEach((img) => {
    bindOrthoImage(img, {
      src: img.getAttribute("data-src"),
      fallback: img.getAttribute("data-fallback"),
    });
  });

  modal.querySelectorAll(".t10-insp-tab").forEach((t) => {
    t.classList.toggle("active", t.getAttribute("data-tab") === "all");
  });

  // Pause/dispose any leaked card WebGL; modal owns its own GLB context lazily
  try {
    setSharedCanvasModalMode(true);
  } catch {
    /* */
  }

  const bgGrid = getTop10Grid();
  lockGridVisibility(bgGrid);

  modal._scrollY = window.scrollY || 0;
  modal.hidden = false;
  document.body.classList.add("t10-modal-open");

  // ── Default: Ortho Triplet. GLB mounts ONLY when operator toggles 3D View. ──
  const stage = modal.querySelector("#t10-glb-stage");
  if (stage) {
    stage.style.display = "none";
    stage.classList.remove("is-glb-ready", "is-glb-fallback", "is-glb-loading");
  }
  modalGrid.hidden = false;
  modal.dataset.viewerMode = "ortho:triplet";
  modal._openGen = (modal._openGen || 0) + 1;

  activeInspectorVessel = vessel;
  modal.dataset.glbReady = glbOk ? "1" : "0";
  const glbTab = modal.querySelector('.t10-insp-tab[data-tab="glb"]');
  if (glbTab) {
    if (glbOk) {
      glbTab.classList.remove("is-degraded", "is-disabled");
      glbTab.removeAttribute("aria-disabled");
      glbTab.title = "Photo-composite digital twin on simplified hull (not verified structural CAD)";
    } else {
      glbTab.classList.add("is-degraded", "is-disabled");
      glbTab.setAttribute("aria-disabled", "true");
      glbTab.title = "3D model unavailable (glb.ready !== true)";
    }
  }

  modal.dataset.voxelReady = voxelOk ? "1" : "0";
  const voxelTab = modal.querySelector('.t10-insp-tab[data-tab="voxel"]');
  if (voxelTab) {
    if (voxelOk) {
      voxelTab.classList.remove("is-degraded", "is-disabled");
      voxelTab.removeAttribute("aria-disabled");
      const n = vessel?.voxel_cubes?.n_occupied || "?";
      voxelTab.title = `Voxel grid reconstruction (~${n} cells) from 3-view silhouette occupancy`;
    } else {
      voxelTab.classList.add("is-degraded", "is-disabled");
      voxelTab.setAttribute("aria-disabled", "true");
      voxelTab.title = "Voxel grid unavailable (voxel_cubes.ready !== true)";
    }
  }

  modal.dataset.videoReady = videoOk ? "1" : "0";
  const videoTab = modal.querySelector('.t10-insp-tab[data-tab="video"]');
  if (videoTab) {
    if (videoOk) {
      const clip = resolveVesselVideo(vessel);
      videoTab.hidden = false;
      videoTab.removeAttribute("hidden");
      videoTab.classList.remove("is-degraded", "is-disabled");
      videoTab.removeAttribute("aria-disabled");
      videoTab.title = clip?.badge || FLIGHT_VIDEO_BADGE;
    } else {
      videoTab.hidden = false;
      videoTab.removeAttribute("hidden");
      videoTab.classList.add("is-degraded", "is-disabled");
      videoTab.setAttribute("aria-disabled", "true");
      videoTab.title = "REAL VIDEO unavailable for this vessel";
    }
  }

  // Open requested or default tab (Never-Black hierarchy: 3D GLB -> Voxel -> Video -> Ortho)
  if (initialTab === "glb") {
    applyInspectorTab(modal, glbOk ? "glb" : (voxelOk ? "voxel" : (videoOk ? "video" : "all")));
  } else if (initialTab === "voxel") {
    applyInspectorTab(modal, voxelOk ? "voxel" : (glbOk ? "glb" : (videoOk ? "video" : "all")));
  } else if (initialTab === "overhead") {
    applyInspectorTab(modal, "overhead");
  } else if (initialTab === "video") {
    applyInspectorTab(modal, videoOk ? "video" : "all");
  } else if (initialTab === "all" || initialTab === "side" || initialTab === "bow") {
    applyInspectorTab(modal, initialTab);
  } else {
    applyInspectorTab(modal, defaultTab);
  }
}

function closeRefs() {
  if (!modalEl) return;
  if (modalEl.hidden) return; // already closed — idempotent guard

  disposeModalGlb();
  teardownLumaVideo(modalEl);
  modalEl.hidden = true;
  document.body.classList.remove("t10-modal-open");

  const savedY = typeof modalEl._scrollY === "number" ? modalEl._scrollY : null;
  if (savedY !== null) {
    requestAnimationFrame(() => window.scrollTo({ top: savedY, behavior: "instant" }));
  }

  const bgGrid = getTop10Grid();
  lockGridVisibility(bgGrid);

  // Ensure no card WebGL overlay remains after close
  try {
    setSharedCanvasModalMode(false);
  } catch {
    /* */
  }

  resetInspectorDom(modalEl);
  activeInspectorImo = null;
  activeInspectorVessel = null;

  requestAnimationFrame(() => {
    const bgGridNow = getTop10Grid();
    if (bgGridNow) {
      bgGridNow.querySelectorAll(".t10-photo-fallback").forEach((img) => {
        img.style.removeProperty("opacity");
        img.style.removeProperty("z-index");
      });
      lockGridVisibility(bgGridNow);
    }
  });
}

/**
 * Cards are PHOTO-ONLY (contract): no WebGL in the grid — prevents Context Lost
 * and scroll-drift from the legacy fixed scissor overlay (#t10-shared-webgl).
 * Live 3D = modal DIGITAL TWIN only (lazy, single context).
 */
function mountCard(card, vessel) {
  const imo = String(vessel.imo);
  const vp = card.querySelector("[data-viewport]");
  if (!vp) return;
  vp.classList.remove("is-3d-ready", "webgl-context-lost");
  showPhotoFallback(vp, vessel, "ORTHO · SIDE");
  const photo = vp.querySelector(".t10-photo-fallback");
  if (photo) {
    photo.style.opacity = "1";
    photo.style.zIndex = "1";
  }
  viewers.set(imo, { mode: "photo", pause() {}, resume() {}, dispose() {} });
}

function observeCards(root) {
  // Tear down any leftover shared card WebGL from older builds / HMR
  try {
    getSharedTop10Renderer(false)?.dispose?.();
  } catch {
    /* */
  }
  document.getElementById("t10-shared-webgl")?.remove?.();

  const cards = [...root.querySelectorAll(".t10-card")];
  cards.forEach((card) => {
    const imo = card.getAttribute("data-imo");
    const vessel = TOP10_VESSELS.find((x) => String(x.imo) === imo);
    if (vessel) mountCard(card, vessel);
  });

  if (io) io.disconnect();
  io = null;
  // No IntersectionObserver WebGL mount — photos are eager in cardHtml
}

export function renderTop10Sheet() {
  const root = document.getElementById("sheet-top10");
  if (!root) return;
  const wrap = root.querySelector(".t10-wrap") || root;
  let grid = getTop10Grid(root);
  if (!grid) {
    grid = document.createElement("div");
    grid.className = "t10-grid";
    grid.id = "top10-grid-container";
    wrap.appendChild(grid);
  }
  grid.id = "top10-grid-container";
  grid.setAttribute("data-legacy-id", "t10Grid");
  grid.setAttribute("data-top10-grid", "1");
  window.__TOP10_GRID__ = grid;

  assertManifestUrls();

  let intro = wrap.querySelector(".t10-intro");
  if (!intro) {
    intro = document.createElement("p");
    intro.className = "t10-intro";
    wrap.insertBefore(intro, grid);
  }
  intro.textContent =
    "Q-Flex Digital Twin & Video Fleet — REAL VIDEO flight loops (default) · DIGITAL TWIN GLB · orthographic OSINT inspector.";

  let truth = wrap.querySelector(".t10-truth");
  if (!truth) {
    wrap.insertAdjacentHTML("afterbegin", truthBanner());
  } else {
    truth.outerHTML = truthBanner();
  }

  let registry = wrap.querySelector(".t10-registry-wrap");
  if (registry) registry.remove();
  wrap.insertAdjacentHTML("afterbegin", registryTableHtml());
  // Keep truth banner at very top
  const regEl = wrap.querySelector(".t10-registry-wrap");
  const truthEl = wrap.querySelector(".t10-truth");
  if (regEl && truthEl && truthEl.nextElementSibling !== regEl) {
    wrap.insertBefore(truthEl, wrap.firstChild);
    if (truthEl.nextSibling !== regEl) {
      wrap.insertBefore(regEl, truthEl.nextSibling);
    }
  }

  // Hydrate all 10 cards BEFORE any modal interaction
  grid.innerHTML = TOP10_VESSELS.slice(0, 10).map(cardHtml).join("");
  lockGridVisibility(grid);
  bindParallaxHoverVideo(grid);
  bindRegistryCardLinker(wrap, grid);
  // Event delegation via data-vessel-id / data-refs / data-open-glb / data-open-video
  if (!grid.dataset.refsDelegated) {
    grid.dataset.refsDelegated = "1";
    const openVideo = (imo) => {
      const vessel = TOP10_VESSELS.find((x) => String(x.imo) === String(imo));
      if (vessel) openRefs(vessel, { tab: "video" });
    };
    grid.addEventListener("click", (ev) => {
      const videoBtn = ev.target.closest?.("[data-open-video], .t10-video-btn");
      if (videoBtn && grid.contains(videoBtn)) {
        if (videoBtn.disabled || videoBtn.getAttribute("aria-disabled") === "true") {
          ev.preventDefault();
          return;
        }
        ev.preventDefault();
        ev.stopPropagation();
        openVideo(videoBtn.getAttribute("data-open-video") || videoBtn.getAttribute("data-vessel-id"));
        return;
      }
      const twinBtn = ev.target.closest?.("[data-open-glb], .t10-twin-btn");
      if (twinBtn && grid.contains(twinBtn)) {
        if (twinBtn.disabled || twinBtn.getAttribute("aria-disabled") === "true") {
          ev.preventDefault();
          return;
        }
        ev.preventDefault();
        ev.stopPropagation();
        const imo = twinBtn.getAttribute("data-open-glb") || twinBtn.getAttribute("data-vessel-id");
        const vessel = TOP10_VESSELS.find((x) => String(x.imo) === String(imo));
        if (vessel) openRefs(vessel, { tab: "glb" });
        return;
      }
      const viewport = ev.target.closest?.("[data-open-inspector], .t10-viewport");
      if (viewport && grid.contains(viewport)) {
        ev.preventDefault();
        const imo = viewport.getAttribute("data-open-inspector") || viewport.closest("[data-imo]")?.getAttribute("data-imo");
        const vessel = TOP10_VESSELS.find((x) => String(x.imo) === String(imo));
        if (vessel) openRefs(vessel);
        return;
      }
      const btn = ev.target.closest?.("[data-refs], [data-vessel-id].t10-ref-btn, .t10-ref-btn");
      if (!btn || !grid.contains(btn)) return;
      ev.preventDefault();
      ev.stopPropagation();
      const imo = btn.getAttribute("data-refs") || btn.getAttribute("data-vessel-id");
      const vessel = TOP10_VESSELS.find((x) => String(x.imo) === String(imo));
      if (vessel) openRefs(vessel, { tab: "all" });
    });
  }

  const regWrap = wrap.querySelector(".t10-registry-wrap");
  if (regWrap && !regWrap.dataset.bound) {
    regWrap.dataset.bound = "1";
    // Registry REAL VIDEO: handled by bindRegistryCardLinker (scroll+preview).
    // Double-click / Alt+click still opens full inspector modal.
    regWrap.addEventListener("click", (ev) => {
      const btn = ev.target.closest?.("[data-open-video]");
      if (!btn) return;
      if (!(ev.altKey || ev.detail >= 2)) return;
      ev.preventDefault();
      const imo = btn.getAttribute("data-open-video");
      const vessel = TOP10_VESSELS.find((x) => String(x.imo) === String(imo));
      if (vessel) openRefs(vessel, { tab: "video" });
    });
  }
  observeCards(root);
  lockGridVisibility(grid);
  booted = true;
  console.info(
    "[Q-FLEX] grid hydrated —",
    QFLEX_FLEET_SHORT || QFLEX_FLEET_BRAND || "Q-Flex Fleet",
    "cards=",
    grid.querySelectorAll(".t10-card").length
  );
}

export function bootTop10({ force = false } = {}) {
  if (booted && !force) {
    // Photo-only cards — never spin up shared card WebGL on resume
    getSharedTop10Renderer(false)?.dispose?.();
    document.getElementById("t10-shared-webgl")?.remove?.();
    viewers.forEach((v) => v?.resume?.());
    return;
  }
  if (force && booted) {
    try {
      getSharedTop10Renderer(false)?.dispose?.();
    } catch {
      /* ignore */
    }
    document.getElementById("t10-shared-webgl")?.remove?.();
    viewers.clear();
    booted = false;
  }
  renderTop10Sheet();
}

export function pauseTop10() {
  if (modalEl && !modalEl.hidden) {
    closeRefs();
  }
  disposeModalGlb();
  getSharedTop10Renderer(false)?.dispose?.();
  document.getElementById("t10-shared-webgl")?.remove?.();
  viewers.forEach((v) => v?.pause?.());
  document.querySelectorAll(".t10-hover-video, .t10-video-stage video").forEach((vid) => {
    try { vid.pause(); } catch (_) {}
  });
}

window.__TOP10__ = {
  vessels: TOP10_VESSELS,
  boot: bootTop10,
  pause: pauseTop10,
  render: renderTop10Sheet,
  assertManifestUrls,
  openRefs,
  closeRefs,
  /** QA / CDP: live modal GLB viewer instance (or null). */
  getGlbViewer: () => modalGlbViewer,
  /** Stress helper: open Ortho → toggle 3D → close across vessels; returns summary. */
  async stressInspector(cycles = 15) {
    const list = TOP10_VESSELS.slice(0, 10);
    const errors = [];
    const beforeGl = document.querySelectorAll("canvas").length;
    for (let i = 0; i < cycles; i++) {
      const v = list[i % list.length];
      try {
        openRefs(v, { tab: "all" });
        const shown = modalEl?.dataset?.vesselId;
        if (String(shown) !== String(v.imo)) {
          errors.push(`cycle ${i}: expected IMO ${v.imo} got ${shown}`);
        }
        if (modalEl?.dataset?.viewerMode !== "ortho:triplet") {
          errors.push(`cycle ${i}: default mode not ortho:triplet (${modalEl?.dataset?.viewerMode})`);
        }
        const draft = modalEl?.querySelector('[data-telem="draft"] strong')?.textContent;
        const expect = `${Number(v.draft_m).toFixed(1)} m`;
        if (draft && draft !== expect) {
          errors.push(`cycle ${i}: draft leak ${draft} != ${expect}`);
        }
        // Optional 3D View toggle (lazy)
        applyInspectorTab(modalEl, "glb");
        await new Promise((r) => setTimeout(r, 120));
        if (isGlbReady(v)) {
          // either glb or ortho:glb-fail — never blank
          const mode = modalEl?.dataset?.viewerMode || "";
          if (!mode.startsWith("glb") && !mode.startsWith("ortho")) {
            errors.push(`cycle ${i}: unexpected mode after 3D toggle: ${mode}`);
          }
        } else if (modalEl?.dataset?.viewerMode !== "ortho:not-ready") {
          // disabled vessels must stay on ortho messaging
          const mode = modalEl?.dataset?.viewerMode || "";
          if (mode.startsWith("glb") && mode !== "glb:loading") {
            errors.push(`cycle ${i}: ready=false still entered live glb (${mode})`);
          }
        }
        // Return to Ortho Triplet before close
        applyInspectorTab(modalEl, "all");
        await new Promise((r) => setTimeout(r, 40));
        closeRefs();
        if (activeInspectorImo !== null) {
          errors.push(`cycle ${i}: activeInspectorImo not cleared`);
        }
        await new Promise((r) => setTimeout(r, 20));
      } catch (err) {
        errors.push(`cycle ${i}: ${err?.message || err}`);
      }
    }
    const afterGl = document.querySelectorAll("canvas").length;
    const mem = performance.memory
      ? {
          usedJSHeapMB: Math.round(performance.memory.usedJSHeapSize / 1048576),
          totalJSHeapMB: Math.round(performance.memory.totalJSHeapSize / 1048576),
        }
      : null;
    const summary = {
      cycles,
      errors,
      canvasCountBefore: beforeGl,
      canvasCountAfter: afterGl,
      canvasDelta: afterGl - beforeGl,
      memory: mem,
      ok: errors.length === 0 && afterGl <= beforeGl + 1,
    };
    console.info("[Q-FLEX] stressInspector", summary);
    return summary;
  },
};

export default { bootTop10, pauseTop10, TOP10_VESSELS };

// Auto-boot if page loaded directly on top10 sheet
if (typeof document !== "undefined") {
  const checkAndBoot = () => {
    const sheet = document.documentElement.dataset.sheet || (new URLSearchParams(window.location.search)).get("sheet");
    if (sheet === "top10" || window.location.hash.includes("top10") || document.getElementById("sheet-top10")?.classList.contains("active")) {
      bootTop10();
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", checkAndBoot);
  } else {
    checkAndBoot();
  }
}
