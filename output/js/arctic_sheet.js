/**
 * ARCTIC sheet (sheet=arctic) — Arc7 Yamalmax pair.
 * Reuses Q-Flex card chrome; NO Ortho Triplet; NO WebGL Digital Twin on this sheet
 * (avoids WebGL 0/1/0 regression). REAL VIDEO trust = LUMA track.
 */
import {
  ARCTIC_VESSELS,
  ARCTIC_FLEET_BRAND,
  ARCTIC_FLEET_SHORT,
  ARCTIC_LUMA_VIDEO_BADGE,
  ARCTIC_VIDEO_DERIVED_BADGE,
  ARCTIC_TOP_VIEW_NOTE,
  ARCTIC_TOP_VIEW_DERIVED_NOTE,
} from "./arctic_vessels_manifest.js";

let booted = false;
let activeVessel = null;

function fmtDraft(v) {
  if (v?.draft_status === "unconfirmed" || v?.draft_m == null || Number.isNaN(Number(v.draft_m))) {
    return `<span class="ark-unconfirmed" title="Summer draft not independently confirmed">${v?.draft_display || "н/п"}</span>`;
  }
  return `${Number(v.draft_m).toFixed(1)}<span>m</span>`;
}

function fmtDraftPlain(v) {
  if (v?.draft_status === "unconfirmed" || v?.draft_m == null) {
    return v?.draft_display || "не подтверждено";
  }
  return `${Number(v.draft_m).toFixed(1)} m (${v.draft_kind || "summer"})`;
}

function resolveVideo(vessel) {
  const flight = vessel?.flight_video;
  if (flight?.ready && flight?.url) {
    return {
      url: String(flight.url),
      badge: String(flight.fidelity_badge || ARCTIC_LUMA_VIDEO_BADGE),
      bytes: Number(flight.bytes || 0),
    };
  }
  return null;
}

function cardHtml(v) {
  const imo = String(v.imo);
  const rankLabel = String(v.rank).padStart(2, "0");
  const photo = v.refs?.side?.url || "";
  const fb = v.refs?.side?.fallback_url || photo;
  const clip = resolveVideo(v);
  const videoOk = !!clip;
  const videoUrl = clip?.url ? String(clip.url).replace(/"/g, "&quot;") : "";
  const videoDisabled = videoOk ? "" : ' disabled aria-disabled="true"';
  const hoverVideo = videoOk
    ? `<video class="t10-hover-video" muted loop playsinline preload="metadata"
        poster="${photo}" data-src="${videoUrl}" data-imo="${imo}"
        aria-label="Hover preview REAL VIDEO IMO ${imo}"></video>`
    : "";
  const mb = clip ? (clip.bytes / (1024 * 1024)).toFixed(1) : "—";
  return `
  <article class="t10-card vessel-card ark-card" data-imo="${imo}" data-rank="${v.rank}" data-vessel-id="${imo}"${videoOk ? ` data-video-src="${videoUrl}"` : ""}>
    <div class="t10-media t10-viewport t10-parallax" data-viewport data-open-inspector="${imo}" data-parallax="1"${videoOk ? ` data-hover-video="1"` : ""}>
      <div class="t10-parallax-inner">
        <img class="t10-photo-fallback" src="${photo}" alt="${v.name || imo} video-derived side-oblique"
          decoding="async" loading="eager" data-fallback="${fb}"
          onerror="if(!this.dataset.fb){this.dataset.fb=1;this.src=this.dataset.fallback}"/>
        ${hoverVideo}
      </div>
      <div class="t10-media-fade" aria-hidden="true"></div>
      <div class="t10-seg" role="group" aria-label="Media mode">
        <button type="button" class="t10-seg-btn is-primary" data-open-video="${imo}" data-vessel-id="${imo}" title="Open REAL VIDEO"${videoDisabled}>▶ REAL VIDEO</button>
        <button type="button" class="t10-seg-btn" data-open-derived="${imo}" data-vessel-id="${imo}" title="Video-derived views">▣ VIDEO-DERIVED</button>
      </div>
    </div>
    <header class="t10-card-head">
      <div class="t10-title-row">
        <span class="t10-rank">#${rankLabel}</span>
        <h3 class="t10-name">${v.name || "—"}</h3>
      </div>
      <div class="t10-meta-row">
        <span class="t10-pill">IMO ${imo}</span>
        <span class="t10-pill">${String(v.flag || "—").toUpperCase()}</span>
        <span class="t10-pill t10-pill--ok">${v.ice_class || "Arc7"}</span>
        <span class="t10-pill">${v.class || "Yamalmax"}</span>
      </div>
    </header>
    <div class="t10-telem" aria-label="Vessel telemetry from confirmed public particulars">
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
        <div class="val">${fmtDraft(v)}</div>
      </div>
      <div class="t10-telem-cell">
        <div class="lbl">DWT</div>
        <div class="val">${Math.round(Number(v.dwt_tons || 0) / 1000)}<span>kt</span></div>
      </div>
    </div>
    <footer class="t10-card-foot">
      <button type="button" class="t10-foot-btn t10-ref-btn" data-open-derived="${imo}" data-vessel-id="${imo}">VIDEO-DERIVED VIEWS</button>
      <span class="t10-vid-badge" title="${clip?.badge || ARCTIC_LUMA_VIDEO_BADGE}"><span class="dot"></span>LUMA · ${mb} MB</span>
    </footer>
  </article>`;
}

function ensureModal() {
  let modal = document.getElementById("arcticInspectorModal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "arcticInspectorModal";
  modal.className = "t10-modal sentinel-modal-overlay ark-insp-modal";
  modal.hidden = true;
  modal.innerHTML = `
    <div class="t10-modal-backdrop" data-ark-close="1"></div>
    <div class="t10-modal-panel" role="dialog" aria-modal="true" aria-labelledby="arkInspTitle">
      <header class="t10-insp-head">
        <div>
          <div class="t10-insp-hud-label">ORACLE-1001 · ARCTIC · Arc7 · REAL VIDEO + VIDEO-DERIVED VIEWS</div>
          <h2 id="arkInspTitle" class="t10-insp-name">—</h2>
          <div id="arkInspMeta" class="t10-insp-sub">—</div>
        </div>
        <button type="button" class="t10-modal-x" data-ark-close="1" aria-label="Close">×</button>
      </header>
      <nav class="t10-insp-tabs" role="tablist">
        <button type="button" class="t10-insp-tab" data-ark-tab="video" role="tab">
          <span class="tab-icon">▶</span> REAL VIDEO
        </button>
        <button type="button" class="t10-insp-tab active" data-ark-tab="derived" role="tab">
          <span class="tab-icon">▣</span> VIDEO-DERIVED VIEWS
        </button>
      </nav>
      <div class="t10-modal-grid ark-insp-body" style="display:block;padding:16px;overflow:auto;max-height:min(72vh,820px)">
        <div id="arkVideoStage" class="ark-video-stage" hidden>
          <div id="arkVideoHud" class="t10-glb-fidelity t10-video-fidelity"></div>
          <video id="arkModalVideo" class="t10-luma-video t10-real-video" controls playsinline loop muted></video>
        </div>
        <div id="arkDerivedStage" class="ark-derived-stage">
          <div class="ark-derived-banner" id="arkDerivedBanner"></div>
          <div class="ark-derived-grid" id="arkDerivedGrid"></div>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t?.closest?.("[data-ark-close]")) closeInspector();
    const tab = t?.closest?.("[data-ark-tab]");
    if (tab) setTab(tab.getAttribute("data-ark-tab"));
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !modal.hidden) closeInspector();
  });
  return modal;
}

function setTab(mode) {
  const modal = ensureModal();
  const videoStage = modal.querySelector("#arkVideoStage");
  const derivedStage = modal.querySelector("#arkDerivedStage");
  modal.querySelectorAll("[data-ark-tab]").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-ark-tab") === mode);
  });
  if (mode === "video") {
    videoStage.hidden = false;
    derivedStage.hidden = true;
    const vid = modal.querySelector("#arkModalVideo");
    if (vid && activeVessel) {
      const clip = resolveVideo(activeVessel);
      if (clip?.url && vid.dataset.src !== clip.url) {
        vid.dataset.src = clip.url;
        vid.src = clip.url;
        vid.load();
      }
      vid.play?.().catch(() => {});
    }
  } else {
    videoStage.hidden = true;
    derivedStage.hidden = false;
    const vid = modal.querySelector("#arkModalVideo");
    if (vid) {
      try { vid.pause(); } catch (_) { /* ignore */ }
    }
  }
  modal.dataset.viewerMode = mode === "video" ? "video:luma" : "derived:views";
}

function openInspector(vessel, { tab = "derived" } = {}) {
  activeVessel = vessel;
  const modal = ensureModal();
  const title = modal.querySelector("#arkInspTitle");
  const meta = modal.querySelector("#arkInspMeta");
  const hud = modal.querySelector("#arkVideoHud");
  const banner = modal.querySelector("#arkDerivedBanner");
  const grid = modal.querySelector("#arkDerivedGrid");
  const clip = resolveVideo(vessel);

  title.textContent = vessel.name || `IMO ${vessel.imo}`;
  meta.textContent =
    `IMO ${vessel.imo} · FLAG ${vessel.flag || "—"} · LOA ${Number(vessel.loa_m).toFixed(1)} m · ` +
    `BEAM ${Number(vessel.beam_m).toFixed(1)} m · DRAFT ${fmtDraftPlain(vessel)} · ` +
    `DWT ${Number(vessel.dwt_tons || 0).toLocaleString("en-US")} t`;

  hud.innerHTML = `REAL VIDEO · ${clip?.badge || ARCTIC_LUMA_VIDEO_BADGE}`;
  banner.innerHTML = `
    <div class="ark-badge">${vessel.video_derived?.badge || ARCTIC_VIDEO_DERIVED_BADGE}</div>
    <div class="ark-badge ark-badge--warn">NOT ORTHO TRIPLET · DIMENSIONS FROM PUBLIC REGISTRY / Q88 ONLY · NEVER FROM THESE FRAMES</div>`;

  const vd = vessel.video_derived || {};
  const cells = [];
  for (const key of ["side", "bow"]) {
    const ref = vd[key];
    if (!ref?.url) continue;
    const capExtra =
      ref.t_sec != null && ref.t_sec !== ""
        ? ` · t=${ref.t_sec}s`
        : ref.user_source
          ? " · user-curated"
          : "";
    cells.push(`
      <figure class="ark-derived-cell">
        <figcaption>${ref.label || key.toUpperCase()}${capExtra}</figcaption>
        <img src="${ref.url}" alt="${ref.label || key}" loading="eager"
          onerror="if(!this.dataset.fb){this.dataset.fb=1;this.src='${ref.fallback_url || ref.url}'}"/>
        <div class="ark-note">${ref.note || ""}</div>
      </figure>`);
  }
  if (vd.top_available && vd.top?.url) {
    const ref = vd.top;
    cells.push(`
      <figure class="ark-derived-cell">
        <figcaption>${ref.label || "TOP / OVERHEAD"} · user-curated</figcaption>
        <img src="${ref.url}" alt="${ref.label || "top"}" loading="eager"
          onerror="if(!this.dataset.fb){this.dataset.fb=1;this.src='${ref.fallback_url || ref.url}'}"/>
        <div class="ark-note">${ref.note || ARCTIC_TOP_VIEW_DERIVED_NOTE || ""}</div>
      </figure>`);
  } else {
    cells.push(`
      <figure class="ark-derived-cell ark-derived-cell--missing">
        <figcaption>TOP / OVERHEAD</figcaption>
        <div class="ark-missing">${vd.top_note || ARCTIC_TOP_VIEW_NOTE}</div>
      </figure>`);
  }
  grid.innerHTML = cells.join("");

  modal.hidden = false;
  document.body.classList.add("t10-modal-open");
  setTab(tab === "video" ? "video" : "derived");
}

function closeInspector() {
  const modal = document.getElementById("arcticInspectorModal");
  if (!modal) return;
  const vid = modal.querySelector("#arkModalVideo");
  if (vid) {
    try {
      vid.pause();
      vid.removeAttribute("src");
      vid.load();
      delete vid.dataset.src;
    } catch (_) { /* ignore */ }
  }
  modal.hidden = true;
  document.body.classList.remove("t10-modal-open");
  activeVessel = null;
}

function bindParallaxHover(grid) {
  if (!grid || grid.dataset.parallaxBound === "1") return;
  grid.dataset.parallaxBound = "1";
  const FRAME0 = 0.0001;

  grid.addEventListener("pointerenter", (ev) => {
    const card = ev.target.closest?.(".ark-card");
    if (!card) return;
    const vid = card.querySelector(".t10-hover-video");
    if (!vid) return;
    if (!vid.src && vid.dataset.src) {
      vid.src = vid.dataset.src;
      vid.load();
    }
    const onMeta = () => {
      try { vid.currentTime = FRAME0; } catch (_) { /* ignore */ }
      vid.play?.().catch(() => {});
    };
    if (vid.readyState >= 1) onMeta();
    else vid.addEventListener("loadedmetadata", onMeta, { once: true });
    card.classList.add("is-video-active");
  }, true);

  grid.addEventListener("pointerleave", (ev) => {
    const card = ev.target.closest?.(".ark-card");
    if (!card) return;
    const vid = card.querySelector(".t10-hover-video");
    if (vid) {
      try { vid.pause(); vid.currentTime = FRAME0; } catch (_) { /* ignore */ }
    }
    card.classList.remove("is-video-active");
  }, true);
}

function bindGrid(grid) {
  if (!grid || grid.dataset.arkBound === "1") return;
  grid.dataset.arkBound = "1";
  grid.addEventListener("click", (ev) => {
    const btn = ev.target.closest?.("[data-open-video],[data-open-derived],[data-open-inspector]");
    if (!btn) return;
    ev.preventDefault();
    const imo = btn.getAttribute("data-open-video")
      || btn.getAttribute("data-open-derived")
      || btn.getAttribute("data-open-inspector");
    const vessel = ARCTIC_VESSELS.find((x) => String(x.imo) === String(imo));
    if (!vessel) return;
    const tab = btn.hasAttribute("data-open-video") ? "video" : "derived";
    openInspector(vessel, { tab });
  });
  bindParallaxHover(grid);
}

function render() {
  const root = document.getElementById("sheet-arctic");
  if (!root) return;
  const grid = root.querySelector("#arctic-grid-container");
  const intro = root.querySelector(".ark-intro");
  if (intro) {
    intro.innerHTML = `
      <strong>${ARCTIC_FLEET_BRAND}</strong> — 2× Arc7 Yamalmax.
      REAL VIDEO = same trust as Q-Flex LUMA track (${ARCTIC_LUMA_VIDEO_BADGE.split("·")[0].trim()}).
      Frames = <em>VIDEO-DERIVED VIEWS</em>, not Ortho Triplet. LOA/BEAM/DRAFT/DWT from public particulars only.`;
  }
  if (grid) {
    grid.innerHTML = ARCTIC_VESSELS.map(cardHtml).join("");
    bindGrid(grid);
  }
}

function boot(opts = {}) {
  const force = !!(opts && opts.force);
  if (booted && !force) return;
  render();
  booted = true;
}

function pause() {
  closeInspector();
  document.querySelectorAll("#arctic-grid-container .t10-hover-video").forEach((vid) => {
    try { vid.pause(); } catch (_) { /* ignore */ }
  });
}

window.__ARCTIC__ = {
  boot,
  pause,
  close: closeInspector,
  vessels: ARCTIC_VESSELS,
  brand: ARCTIC_FLEET_SHORT,
};

document.addEventListener("DOMContentLoaded", () => {
  const sheet = document.documentElement.getAttribute("data-sheet");
  if (sheet === "arctic" || window.location.search.includes("sheet=arctic")) {
    boot();
  }
});

document.addEventListener("sentinelSheetChange", (ev) => {
  const sheet = ev?.detail?.sheet;
  if (sheet === "arctic") boot({ force: true });
  else pause();
});
