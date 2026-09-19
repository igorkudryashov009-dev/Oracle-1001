/**
 * Oracle Engine Control & Forecast Sheet (sheet=oracle).
 * Prognostic Dual Gate / G3 / archive / ML control surface.
 * Autonomous: valid skeleton loaders until health/oracle_state arrives.
 */
import {
  OracleEngine,
  oracle_createEngine,
  oracle_getThresholds,
  oracle_applyThresholdsFromHealth,
} from "./oracle_engine.js";
import {
  ORACLE_BUS_EVENTS,
  oracle_busOn,
} from "./oracle_event_bus.js";

const SHEET_ID = "sheet-oracle";
const ROOT_ID = "oracle-sheet-root";
const HEALTH_URLS = [
  "/output/api/v1/health",
  "/api/v1/health",
  "./api/v1/health.json",
];

/** @type {OracleEngine} */
const engine = oracle_createEngine();

let booted = false;
let pollTimer = null;
/** @type {Record<string, unknown>|null} */
let lastPayload = null;
let skeletonMode = true;

/** Cross-sheet fleet snapshots from top10 / arctic via Event Bus. */
/** @type {Record<string, Record<string, unknown>>} */
const fleetBusState = { top10: {}, arctic: {} };

/** @type {AbortController|null} */
let lifecycleAbort = null;
/** @type {AbortController|null} */
let activeSheetAbort = null;

/**
 * @param {unknown} v
 * @param {number} [fb=0]
 */
function num(v, fb = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fb;
}

/**
 * Map Dual Gate + contract_mode → header predictor status.
 * @param {Record<string, unknown>} state
 * @returns {"NOMINAL"|"WARN"|"CRITICAL"}
 */
function resolvePredictorStatus(state) {
  const pipe = String(
    state.pipeline_health_status ||
      state.operational_status ||
      "",
  ).toUpperCase();
  const fleet = String(
    state.fleet_sample_status ||
      (state.oracle_state && /** @type {Record<string, unknown>} */ (state.oracle_state).status) ||
      "",
  ).toUpperCase();
  const mode = String(
    state.oracle_contract_mode ||
      (state.oracle_state && /** @type {Record<string, unknown>} */ (state.oracle_state).contract_mode) ||
      "",
  ).toUpperCase();

  if (pipe === "CRITICAL" || mode === "BLOCKED") return "CRITICAL";
  if (
    pipe === "DEGRADED" ||
    fleet === "INSUFFICIENT" ||
    fleet === "LIMITED" ||
    mode === "WARN_NOMINAL" ||
    mode === "PASS_LIMITED"
  ) {
    return "WARN";
  }
  if (pipe === "NOMINAL" && fleet === "FULL") return "NOMINAL";
  if (pipe === "NOMINAL") return "WARN";
  return "WARN";
}

/**
 * Dual Gate margin: headroom from LIMITED→FULL as 0–100, caveated by pipeline.
 * @param {number} coverage
 * @param {string} pipe
 */
function dualGateMargin(coverage, pipe) {
  const T = oracle_getThresholds();
  const raw = Math.max(0, Math.min(100, (coverage / T.FLEET_SAMPLE_FULL_MIN) * 100));
  if (pipe === "CRITICAL") return { value: 0, label: "GATE BLOCKED", unit: "%" };
  if (pipe === "DEGRADED") {
    return { value: Math.round(raw * 0.5), label: "DEGRADED MARGIN", unit: "%" };
  }
  return { value: Math.round(raw * 10) / 10, label: "FULL HEADROOM", unit: "%" };
}

/**
 * G3 health index — plateau awareness (3–5 = healthy terrestrial ceiling).
 * @param {number} coverage
 */
function g3HealthIndex(coverage) {
  const T = oracle_getThresholds();
  let score = 0;
  let label = "BELOW PLATEAU";
  if (coverage <= 0) {
    score = 8;
    label = "NO LIVE SAMPLE";
  } else if (coverage < T.G3_COVERAGE_PLATEAU_MIN) {
    score = Math.round(25 + (coverage / T.G3_COVERAGE_PLATEAU_MIN) * 35);
    label = "WARMING";
  } else if (coverage <= T.G3_COVERAGE_PLATEAU_MAX) {
    score = Math.round(70 + ((coverage - T.G3_COVERAGE_PLATEAU_MIN) /
      Math.max(1, T.G3_COVERAGE_PLATEAU_MAX - T.G3_COVERAGE_PLATEAU_MIN)) * 25);
    label = "WITHIN G3 CEILING";
  } else if (coverage < T.FLEET_SAMPLE_LIMITED_MIN) {
    score = 88;
    label = "ABOVE PLATEAU";
  } else if (coverage < T.FLEET_SAMPLE_FULL_MIN) {
    score = 92;
    label = "LIMITED SAMPLE";
  } else {
    score = 100;
    label = "FULL SAMPLE";
  }
  return { value: score, label, unit: "/100" };
}

/**
 * @param {Record<string, unknown>} state
 */
function archiveReadiness(state) {
  const arch =
    (state.archive && typeof state.archive === "object" ? state.archive : null) ||
    (state.oracle_state && typeof state.oracle_state === "object"
      ? /** @type {Record<string, unknown>} */ (state.oracle_state)
      : null);
  const honest = arch && "honest" in /** @type {object} */ (arch)
    ? Boolean(/** @type {Record<string, unknown>} */ (arch).honest)
    : true;
  const synthetic = Boolean(
    state.is_synthetic ??
      (arch && /** @type {Record<string, unknown>} */ (arch).is_synthetic) ??
      true,
  );
  let value = 55;
  let label = "SNAPSHOT / DEMO";
  if (honest && synthetic) {
    value = 62;
    label = "HONEST DEMO";
  }
  if (honest && !synthetic) {
    value = 95;
    label = "LIVE REGISTRY";
  }
  if (!honest) {
    value = 12;
    label = "LABEL FRAUD RISK";
  }
  return { value, label, unit: "%" };
}

/**
 * Build 24h ML weight forecast polyline (advisory, deterministic from seed metrics).
 * @param {number} pDrift
 * @param {number} cvPct
 * @returns {number[]}
 */
function forecastWeights24h(pDrift, cvPct) {
  const points = [];
  const base = Number.isFinite(cvPct) ? cvPct / 100 : 0.5;
  const drift = Number.isFinite(pDrift) ? pDrift : 0.5;
  for (let h = 0; h <= 24; h++) {
    const t = h / 24;
    const wobble = Math.sin(t * Math.PI * 2) * 0.04 * (1 + drift);
    const decay = drift * 0.12 * t;
    const w = Math.max(0.05, Math.min(0.98, base * (1 - decay) + wobble));
    points.push(Math.round(w * 1000) / 1000);
  }
  return points;
}

/**
 * @param {number[]} series
 * @param {number} width
 * @param {number} height
 */
function svgForecastPath(series, width, height) {
  if (!series.length) return "";
  const pad = 8;
  const w = width - pad * 2;
  const h = height - pad * 2;
  const step = w / Math.max(1, series.length - 1);
  return series
    .map((v, i) => {
      const x = pad + i * step;
      const y = pad + (1 - v) * h;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

function skeletonHtml() {
  return `
  <div class="orc-wrap" id="${ROOT_ID}" data-oracle-sheet="1" aria-busy="true">
    <header class="orc-header oracle-card orc-skeleton-block">
      <div class="orc-header-left">
        <div class="orc-kicker sk-line sk-w40"></div>
        <div class="sk-line sk-w70 sk-h28"></div>
        <div class="sk-line sk-w50"></div>
      </div>
      <div class="orc-status-pill orc-status--warn oracle-status--insufficient">
        <span class="orc-pulse" aria-hidden="true"></span>
        <span class="orc-status-label">LOADING</span>
      </div>
    </header>
    <section class="orc-metrics" aria-label="Oracle metrics loading">
      ${[0, 1, 2, 3]
        .map(
          () => `
        <article class="orc-metric-card oracle-card orc-skeleton-block">
          <div class="sk-line sk-w40"></div>
          <div class="sk-line sk-w55 sk-h32"></div>
          <div class="sk-line sk-w60"></div>
        </article>`,
        )
        .join("")}
    </section>
    <section class="orc-gauge-panel oracle-card orc-skeleton-block">
      <div class="sk-line sk-w35"></div>
      <div class="orc-gauge-track sk-bar"></div>
      <div class="sk-line sk-w45"></div>
    </section>
    <section class="orc-forecast-panel oracle-card orc-skeleton-block">
      <div class="sk-line sk-w50"></div>
      <div class="orc-forecast-sk"></div>
    </section>
  </div>`;
}

/**
 * @param {Record<string, unknown>} view
 */
function renderHtml(view) {
  const status = /** @type {string} */ (view.predictor_status || "WARN");
  const fleet = String(view.fleet_sample_status || "INSUFFICIENT");
  const insufficient = fleet === "INSUFFICIENT";
  let statusClass =
    status === "NOMINAL"
      ? "orc-status--nominal"
      : status === "CRITICAL"
        ? "orc-status--critical"
        : "orc-status--warn";
  if (insufficient) statusClass += " oracle-status--insufficient";
  const coverage = num(view.coverage, 0);
  const T = oracle_getThresholds();
  const fullMin = Number(T.FLEET_SAMPLE_FULL_MIN);
  const limitedMin = Number(T.FLEET_SAMPLE_LIMITED_MIN);
  const g3Min = Number(T.G3_COVERAGE_PLATEAU_MIN);
  const g3Max = Number(T.G3_COVERAGE_PLATEAU_MAX);
  const pctFull = Math.max(0, Math.min(100, (coverage / fullMin) * 100));
  const pctLimited = Math.max(0, Math.min(100, (coverage / limitedMin) * 100));
  const series = /** @type {number[]} */ (view.forecast_series || []);
  const path = svgForecastPath(series, 640, 180);
  const areaPath = path
    ? `${path} L${(640 - 8).toFixed(1)},172 L8,172 Z`
    : "";

  const metrics = /** @type {Array<Record<string, unknown>>} */ (view.metrics || []);

  return `
  <div class="orc-wrap" id="${ROOT_ID}" data-oracle-sheet="1" aria-busy="false">
    <header class="orc-header oracle-card">
      <div class="orc-header-left">
        <div class="orc-kicker">ORACLE-1001 · PREDICTOR CONTROL</div>
        <h2 class="orc-title">Oracle Engine Control &amp; Forecast Sheet</h2>
        <p class="orc-sub">
          Dual Gate · G3 terrestrial · Archive honesty · ML drift (advisory only) ·
          last eval ${view.last_eval || "—"}
        </p>
      </div>
      <div class="orc-status-pill ${statusClass}" title="Predictor status from OracleEngine">
        <span class="orc-pulse" aria-hidden="true"></span>
        <span class="orc-status-label">${insufficient ? "INSUFFICIENT" : status}</span>
      </div>
    </header>

    <section class="orc-metrics" aria-label="Oracle metrics grid">
      ${metrics
        .map(
          (m) => `
        <article class="orc-metric-card oracle-card" data-metric="${m.id}">
          <div class="orc-metric-lbl">${m.title}</div>
          <div class="orc-metric-val">${m.display}<span class="orc-metric-unit">${m.unit || ""}</span></div>
          <div class="orc-metric-hint">${m.hint || ""}</div>
        </article>`,
        )
        .join("")}
    </section>

    <section class="orc-gauge-panel oracle-card" aria-label="Coverage gauge">
      <div class="orc-section-head">
        <h3>Coverage Gauge</h3>
        <span class="orc-chip">${fleet} · N=${coverage} / ${fullMin}</span>
      </div>
      <div class="orc-gauge-meta">
        <span>G3 plateau ${g3Min}–${g3Max}</span>
        <span>LIMITED ≥ ${limitedMin}</span>
        <span>FULL ≥ ${fullMin}</span>
      </div>
      <div class="orc-gauge-track" role="progressbar"
        aria-valuemin="0" aria-valuemax="${fullMin}" aria-valuenow="${coverage}"
        aria-label="top500 live coverage">
        <div class="orc-gauge-fill oracle-gauge-bar" style="width:${pctFull}%"></div>
        <div class="orc-gauge-mark orc-gauge-mark--limited" style="left:${(limitedMin / fullMin) * 100}%" title="LIMITED"></div>
        <div class="orc-gauge-mark orc-gauge-mark--plateau" style="left:${(g3Max / fullMin) * 100}%" title="G3 plateau max"></div>
      </div>
      <div class="orc-gauge-caption">
        Current coverage ≈ <strong>${coverage}</strong> · sample <strong>${fleet}</strong>
        · limited-scale fill ${pctLimited.toFixed(0)}% of LIMITED threshold
        · chase FULL on terrestrial AIS is locked (G3)
      </div>
    </section>

    <section class="orc-forecast-panel oracle-card" aria-label="ML weight forecast 24h">
      <div class="orc-section-head">
        <h3>Forecast Timeline · ML Weights 24h</h3>
        <span class="orc-chip orc-chip--muted">ADVISORY · NOT ACTIONABLE</span>
      </div>
      <svg class="orc-forecast-svg" viewBox="0 0 640 180" role="img"
        aria-label="24-hour ML ensemble weight forecast">
        <defs>
          <linearGradient id="orcForecastFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="rgba(56,189,248,0.35)"/>
            <stop offset="100%" stop-color="rgba(56,189,248,0.02)"/>
          </linearGradient>
        </defs>
        <g class="orc-forecast-grid" stroke="rgba(148,163,184,0.15)" stroke-width="1">
          <line x1="8" y1="45" x2="632" y2="45"/>
          <line x1="8" y1="90" x2="632" y2="90"/>
          <line x1="8" y1="135" x2="632" y2="135"/>
        </g>
        ${areaPath ? `<path d="${areaPath}" fill="url(#orcForecastFill)" stroke="none"/>` : ""}
        ${path ? `<path d="${path}" fill="none" stroke="#38bdf8" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>` : ""}
        <text x="8" y="16" class="orc-svg-label">weight</text>
        <text x="600" y="172" class="orc-svg-label">+24h</text>
        <text x="8" y="172" class="orc-svg-label">now</text>
      </svg>
      <p class="orc-forecast-note">
        Seeded from model_cv=${view.cv_pct ?? "—"}% · p_drift=${view.p_drift ?? "—"} ·
        live confidence=${view.confidence || "—"}.
        Does not collapse CV into live fleet confidence (AGENTS lock).
      </p>
    </section>
  </div>`;
}

/**
 * @param {Record<string, unknown>} health
 */
function buildView(health) {
  oracle_applyThresholdsFromHealth(health);
  const evaluated = engine.evaluateThresholds(health);
  const mlResult = engine.predictMLDrift(health);

  const ost =
    (health.oracle_state && typeof health.oracle_state === "object"
      ? /** @type {Record<string, unknown>} */ (health.oracle_state)
      : null) ||
    /** @type {Record<string, unknown>} */ (evaluated.oracle_state || {});

  const coverage = num(
    ost.coverage ??
      health.top500_live_coverage ??
      (evaluated.fleet_sample &&
        /** @type {Record<string, unknown>} */ (evaluated.fleet_sample)
          .top500_live_coverage),
    0,
  );
  const pipe = String(
    evaluated.pipeline_health_status || health.pipeline_health_status || "UNKNOWN",
  );
  const fleet = String(
    ost.status || evaluated.fleet_sample_status || health.fleet_sample_status || "INSUFFICIENT",
  );
  const confidence = String(
    ost.confidence ||
      (evaluated.live_inference &&
        /** @type {Record<string, unknown>} */ (evaluated.live_inference)
          .live_inference_confidence) ||
      "LOW",
  );
  const merged = {
    ...health,
    ...evaluated,
    oracle_state: ost,
    fleet_sample_status: fleet,
    pipeline_health_status: pipe,
  };
  const predictor = resolvePredictorStatus(merged);
  const dgm = dualGateMargin(coverage, pipe);
  const g3 = g3HealthIndex(coverage);
  const arch = archiveReadiness({
    ...health,
    archive: evaluated.archive,
    is_synthetic:
      evaluated.archive &&
      /** @type {Record<string, unknown>} */ (evaluated.archive).is_synthetic,
  });
  const pDrift = num(mlResult.p_drift, 0.5);
  const quant =
    health.quant_pipeline && typeof health.quant_pipeline === "object"
      ? /** @type {Record<string, unknown>} */ (health.quant_pipeline)
      : {};
  const cvPct = num(
    mlResult.model_cv_accuracy_pct ?? quant.model_cv_accuracy_pct,
    NaN,
  );
  const series = forecastWeights24h(pDrift, Number.isFinite(cvPct) ? cvPct : 50);

  // Visual enrichment from top10/arctic bus (does NOT mutate Dual Gate coverage).
  const top10Ctx = fleetBusState.top10 || {};
  const arcticCtx = fleetBusState.arctic || {};
  const busCards =
    num(top10Ctx.vessel_count, 0) + num(arcticCtx.vessel_count, 0);
  const g3Display = { ...g3 };
  if (busCards > 0 && g3Display.value < 100) {
    g3Display.hint = `${g3.label} · HUD cards ${busCards} (top10+arctic)`;
  }
  const dgmDisplay = { ...dgm };
  if (top10Ctx.avg_speed_kn != null || arcticCtx.avg_speed_kn != null) {
    const speeds = [top10Ctx.avg_speed_kn, arcticCtx.avg_speed_kn]
      .filter((x) => x != null)
      .join("/");
    dgmDisplay.label = `${dgm.label} · SOG ${speeds} kn`;
  }

  return {
    predictor_status: predictor,
    coverage,
    fleet_sample_status: fleet,
    confidence,
    last_eval: ost.last_eval || evaluated.evaluated_at || null,
    cv_pct: Number.isFinite(cvPct) ? cvPct : null,
    p_drift: pDrift,
    forecast_series: series,
    fleet_bus: { top10: top10Ctx, arctic: arcticCtx },
    metrics: [
      {
        id: "dual_gate_margin",
        title: "Dual Gate Margin",
        display: String(dgmDisplay.value),
        unit: dgmDisplay.unit,
        hint: dgmDisplay.label,
      },
      {
        id: "g3_health",
        title: "G3 Health Index",
        display: String(g3Display.value),
        unit: g3Display.unit,
        hint: g3Display.hint || g3Display.label,
      },
      {
        id: "archive_readiness",
        title: "Archive Readiness",
        display: String(arch.value),
        unit: arch.unit,
        hint: arch.label,
      },
      {
        id: "ml_drift",
        title: "ML Drift Probability",
        display: (pDrift * 100).toFixed(1),
        unit: "%",
        hint: String(mlResult.drift_band || "UNKNOWN"),
      },
    ],
  };
}

function ensureHost() {
  let mount = document.getElementById("oracle-sheet-mount");
  if (mount) return mount;
  let host = document.getElementById(SHEET_ID);
  if (!host) {
    host = document.createElement("div");
    host.id = SHEET_ID;
    host.className = "sheet";
    const main = document.querySelector("main") || document.body;
    main.appendChild(host);
  }
  mount = document.createElement("div");
  mount.id = "oracle-sheet-mount";
  host.appendChild(mount);
  return mount;
}

function paintSkeleton() {
  const host = ensureHost();
  host.innerHTML = skeletonHtml();
  skeletonMode = true;
}

/**
 * @param {Record<string, unknown>} health
 */
function paintLive(health) {
  try {
    const view = buildView(health);
    const host = ensureHost();
    host.innerHTML = renderHtml(view);
    skeletonMode = false;
    lastPayload = health;
  } catch (err) {
    console.warn("[oracle_sheet] paintLive failed — keeping skeleton", err);
    if (!document.getElementById(ROOT_ID)) paintSkeleton();
  }
}

async function fetchHealth() {
  const payload =
    (typeof window !== "undefined" && window.__SENTINEL_PAYLOAD__) || null;
  if (payload && typeof payload === "object") {
    return /** @type {Record<string, unknown>} */ (payload);
  }
  for (const url of HEALTH_URLS) {
    try {
      const res = await fetch(url, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) continue;
      const data = await res.json();
      if (data && typeof data === "object") {
        return /** @type {Record<string, unknown>} */ (data);
      }
    } catch {
      /* try next */
    }
  }
  return null;
}

async function refresh() {
  const health = await fetchHealth();
  if (!health) {
    if (!document.getElementById(ROOT_ID)) paintSkeleton();
    return;
  }
  paintLive(health);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function teardownActiveListeners() {
  stopPolling();
  if (activeSheetAbort) {
    try {
      activeSheetAbort.abort();
    } catch (_) {
      /* ignore */
    }
    activeSheetAbort = null;
  }
}

function attachActiveListeners() {
  teardownActiveListeners();
  activeSheetAbort = new AbortController();
  const { signal } = activeSheetAbort;

  oracle_busOn(
    ORACLE_BUS_EVENTS.FLEET_METRICS,
    (ev) => {
      const d = (ev && ev.detail) || {};
      const source = String(d.source || "");
      if (source === "top10" || source === "arctic") {
        fleetBusState[source] = { ...d };
      }
      if (document.documentElement.getAttribute("data-sheet") === "oracle") {
        refresh();
      }
    },
    { signal },
  );

  oracle_busOn(
    ORACLE_BUS_EVENTS.METRICS_REFRESH,
    () => {
      if (document.documentElement.getAttribute("data-sheet") === "oracle") {
        refresh();
      }
    },
    { signal },
  );

  pollTimer = setInterval(() => {
    if (document.documentElement.getAttribute("data-sheet") === "oracle") {
      refresh();
    }
  }, 20000);
}

/**
 * @param {{ force?: boolean }} [opts]
 */
function boot(opts = {}) {
  try {
    const force = !!(opts && opts.force);
    if (booted && !force) {
      attachActiveListeners();
      refresh();
      return;
    }
    booted = true;
    paintSkeleton();
    attachActiveListeners();
    refresh();
  } catch (err) {
    console.warn("[oracle_sheet] boot error", err);
    paintSkeleton();
  }
}

function pause() {
  teardownActiveListeners();
}

function destroy() {
  pause();
  booted = false;
  if (lifecycleAbort) {
    try {
      lifecycleAbort.abort();
    } catch (_) {
      /* ignore */
    }
    lifecycleAbort = null;
  }
  try {
    const mount = document.getElementById("oracle-sheet-mount");
    if (mount) mount.innerHTML = "";
  } catch (_) {
    /* ignore */
  }
}

function bindOracleSheetLifecycle() {
  if (lifecycleAbort) {
    try {
      lifecycleAbort.abort();
    } catch (_) {
      /* ignore */
    }
  }
  lifecycleAbort = new AbortController();
  const { signal } = lifecycleAbort;

  const maybeBoot = () => {
    const sheet = document.documentElement.getAttribute("data-sheet");
    if (sheet === "oracle" || window.location.search.includes("sheet=oracle")) {
      boot({ force: true });
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", maybeBoot, { signal });
  } else {
    maybeBoot();
  }

  document.addEventListener(
    "sentinelSheetChange",
    (ev) => {
      const sheet = ev?.detail?.sheet;
      if (sheet === "oracle") boot({ force: true });
      else pause();
    },
    { signal },
  );
}

if (typeof window !== "undefined") {
  window.__ORACLE_SHEET__ = {
    boot,
    pause,
    destroy,
    refresh,
    engine,
    getLastPayload: () => lastPayload,
    getFleetBusState: () => ({ ...fleetBusState }),
    isSkeleton: () => skeletonMode,
  };
  bindOracleSheetLifecycle();
}

export { boot, pause, destroy, refresh, engine, buildView as oracle_buildSheetView };
