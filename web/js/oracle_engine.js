/**
 * OracleEngine — client-side prognostic evaluator for Dual Gate / G3 / archive / ML.
 *
 * Namespace: oracle_* (isolated). Thresholds are NEVER hardcoded here —
 * they arrive from health.thresholds (services.dual_gate.export_dual_gate_thresholds).
 * Same class of bug as pre-1.6.1 quant SoT drift if JS keeps a numeric twin.
 *
 * Import:
 *   import { OracleEngine, oracle_createEngine, oracle_getThresholds,
 *            oracle_applyThresholdsFromHealth } from "./oracle_engine.js";
 */

/** @typedef {"NOMINAL"|"DEGRADED"|"CRITICAL"|"UNKNOWN"} OraclePipelineStatus */
/** @typedef {"FULL"|"LIMITED"|"INSUFFICIENT"|"UNKNOWN"} OracleFleetSampleStatus */

/** @typedef {Record<string, number|string>} OracleThresholds */

const ORACLE_THRESHOLD_KEYS = Object.freeze([
  "FLEET_SAMPLE_FULL_MIN",
  "FLEET_SAMPLE_LIMITED_MIN",
  "FLEET_WIDE_METRIC_MIN_N",
  "PIPELINE_LIVE_LAG_SEC",
  "DISK_FREE_MIN_PCT",
  "DISK_FREE_CRITICAL_PCT",
  "RECONNECT_STORM_MAX",
  "RATE_LIMIT_STORM_MAX",
  "G3_COVERAGE_PLATEAU_MIN",
  "G3_COVERAGE_PLATEAU_MAX",
  "G3_UNIQUE_PER_HOUR_TYPICAL",
]);

/** @type {OracleThresholds|null} */
let _runtimeThresholds = null;

/**
 * Apply Dual Gate thresholds from health.json / evaluate payload.
 * Only source of numeric cutoffs for the Oracle client.
 * @param {unknown} raw
 * @returns {OracleThresholds|null}
 */
export function oracle_applyThresholdsFromHealth(raw) {
  try {
    let src = raw;
    if (raw && typeof raw === "object" && "thresholds" in /** @type {object} */ (raw)) {
      src = /** @type {Record<string, unknown>} */ (raw).thresholds;
    }
    if (!src || typeof src !== "object") return _runtimeThresholds;
    const obj = /** @type {Record<string, unknown>} */ (src);
    /** @type {Record<string, number|string>} */
    const next = {};
    for (const key of ORACLE_THRESHOLD_KEYS) {
      const v = obj[key];
      if (v == null || v === "") {
        return _runtimeThresholds;
      }
      next[key] = typeof v === "number" ? v : Number(v);
      if (key !== "source" && !Number.isFinite(/** @type {number} */ (next[key]))) {
        return _runtimeThresholds;
      }
    }
    if (typeof obj.source === "string") next.source = obj.source;
    _runtimeThresholds = Object.freeze(next);
    return _runtimeThresholds;
  } catch {
    return _runtimeThresholds;
  }
}

/**
 * @returns {OracleThresholds}
 */
export function oracle_getThresholds() {
  if (!_runtimeThresholds) {
    throw new Error(
      "oracle thresholds unset — load health.thresholds from dual_gate SoT " +
        "(oracle_applyThresholdsFromHealth). Hardcoded ORACLE_THRESHOLDS forbidden.",
    );
  }
  return _runtimeThresholds;
}

/**
 * True once backend thresholds have been applied at least once.
 * @returns {boolean}
 */
export function oracle_thresholdsReady() {
  return _runtimeThresholds != null;
}

/**
 * Test / offline inject — still must supply full Dual Gate blob (no silent defaults).
 * @param {OracleThresholds} thresholds
 */
export function oracle_setThresholdsForTest(thresholds) {
  const applied = oracle_applyThresholdsFromHealth({ thresholds });
  if (!applied) {
    throw new Error("oracle_setThresholdsForTest: incomplete thresholds blob");
  }
  return applied;
}

/**
 * Deprecated name kept as a live getter proxy — reads runtime SoT only.
 * Accessing before apply throws (via oracle_getThresholds).
 */
export const ORACLE_THRESHOLDS = new Proxy(
  /** @type {OracleThresholds} */ ({}),
  {
    get(_t, prop) {
      if (prop === Symbol.toStringTag) return "OracleThresholds";
      if (typeof prop === "symbol") return undefined;
      const T = oracle_getThresholds();
      return T[/** @type {string} */ (prop)];
    },
    ownKeys() {
      return _runtimeThresholds ? Reflect.ownKeys(_runtimeThresholds) : [];
    },
    getOwnPropertyDescriptor(_t, prop) {
      if (!_runtimeThresholds || typeof prop === "symbol") return undefined;
      if (!(prop in _runtimeThresholds)) return undefined;
      return {
        configurable: true,
        enumerable: true,
        value: _runtimeThresholds[/** @type {string} */ (prop)],
      };
    },
  },
);

const ORACLE_MODULE_ID = "oracle_engine";
const ORACLE_VERSION = "1.0.0";

/**
 * Safely coerce unknown input to a plain object.
 * Accepts object, JSON string, or null/undefined — never throws.
 * @param {unknown} raw
 * @returns {{ ok: boolean, data: Record<string, unknown>, error: string|null }}
 */
export function oracle_safeParseMetrics(raw) {
  try {
    if (raw == null) {
      return { ok: true, data: {}, error: null };
    }
    if (typeof raw === "string") {
      const trimmed = raw.trim();
      if (!trimmed) return { ok: true, data: {}, error: null };
      const parsed = JSON.parse(trimmed);
      if (parsed == null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return {
          ok: false,
          data: {},
          error: "oracle_safeParseMetrics: JSON root must be an object",
        };
      }
      return { ok: true, data: /** @type {Record<string, unknown>} */ (parsed), error: null };
    }
    if (typeof raw === "object" && !Array.isArray(raw)) {
      return { ok: true, data: /** @type {Record<string, unknown>} */ (raw), error: null };
    }
    return {
      ok: false,
      data: {},
      error: `oracle_safeParseMetrics: unsupported type ${typeof raw}`,
    };
  } catch (err) {
    return {
      ok: false,
      data: {},
      error: `oracle_safeParseMetrics: ${err && err.message ? err.message : String(err)}`,
    };
  }
}

/**
 * @param {unknown} value
 * @param {number} [fallback=0]
 * @returns {number}
 */
function oracle_num(value, fallback = 0) {
  try {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  } catch {
    return fallback;
  }
}

/**
 * @param {unknown} value
 * @param {boolean} [fallback=false]
 * @returns {boolean}
 */
function oracle_bool(value, fallback = false) {
  try {
    if (typeof value === "boolean") return value;
    if (value == null) return fallback;
    if (typeof value === "string") {
      const s = value.trim().toLowerCase();
      if (["1", "true", "yes", "on"].includes(s)) return true;
      if (["0", "false", "no", "off", ""].includes(s)) return false;
    }
    return Boolean(value);
  } catch {
    return fallback;
  }
}

/**
 * Client mirror of dual_gate.compute_fleet_sample_status.
 * @param {number} coverage
 * @param {number} [universe=500]
 */
function oracle_computeFleetSample(coverage, universe = 500) {
  const n = Math.max(0, Math.floor(oracle_num(coverage, 0)));
  const uni = Math.max(1, Math.floor(oracle_num(universe, 500)));
  const T = oracle_getThresholds();
  /** @type {OracleFleetSampleStatus} */
  let status = "INSUFFICIENT";
  if (n >= T.FLEET_SAMPLE_FULL_MIN) status = "FULL";
  else if (n >= T.FLEET_SAMPLE_LIMITED_MIN) status = "LIMITED";

  let caveat = null;
  if (status !== "FULL") {
    caveat =
      `based on N=${n} vessels (of ${uni}), statistically insufficient ` +
      "for fleet-wide balance inference (terrestrial AIS coverage)";
  }
  return {
    fleet_sample_status: status,
    top500_live_coverage: n,
    top500_universe: uni,
    limited_min: T.FLEET_SAMPLE_LIMITED_MIN,
    full_min: T.FLEET_SAMPLE_FULL_MIN,
    sample_size_caveat: caveat,
    fleet_wide_metrics_eligible: n >= T.FLEET_WIDE_METRIC_MIN_N,
  };
}

/**
 * Client mirror of dual_gate.compute_pipeline_health_status (subset for HUD metrics).
 * @param {Record<string, unknown>} m
 */
function oracle_computePipelineHealth(m) {
  const T = oracle_getThresholds();
  /** @type {string[]} */
  const reasons = [];

  const replica = (m.replica && typeof m.replica === "object" ? m.replica : {}) || {};
  const pipeline = (m.pipeline_health && typeof m.pipeline_health === "object"
    ? m.pipeline_health
    : {}) || {};
  const diskSrc =
    (m.disk && typeof m.disk === "object" ? m.disk : null) ||
    (pipeline.disk && typeof pipeline.disk === "object" ? pipeline.disk : null) ||
    {};

  const age = oracle_num(
    m.ais_lag_sec ?? pipeline.ais_lag_sec ?? replica.age_sec,
    NaN,
  );
  const liveOk = oracle_bool(
    m.live_ok ?? pipeline.live_ok ?? replica.live_ok,
    false,
  );
  const stale = oracle_bool(m.stale ?? replica.stale, !liveOk);
  const integrityOk = oracle_bool(
    m.integrity_ok ?? replica.integrity_ok,
    true,
  );
  const portOk = oracle_bool(m.port_ok ?? pipeline.port_ok, true);
  const portDrift8478 = oracle_bool(m.port_drift_8478, false);
  const failover = oracle_bool(
    m.failover_in_progress ?? pipeline.failover_in_progress,
    false,
  );
  const reconnects = Math.floor(
    oracle_num(m.reconnects ?? pipeline.reconnects, 0),
  );
  const rateLimits = Math.floor(
    oracle_num(m.rate_limit_hits ?? pipeline.rate_limit_hits ?? m.http_429_count, 0),
  );
  const freePctRaw = m.disk_free_pct ?? diskSrc.disk_free_pct ?? pipeline.disk_free_pct;
  const freePct =
    freePctRaw == null || freePctRaw === ""
      ? null
      : oracle_num(freePctRaw, NaN);
  const hasFree = freePct != null && Number.isFinite(freePct);

  const activeNode = String(
    m.active_node || pipeline.active_node || "korolev",
  ).toLowerCase();

  if (failover) reasons.push("failover_cutover_awaiting_first_edge_sync");
  if (portDrift8478) reasons.push("port_drift_8478");
  if (!portOk) reasons.push("dashboard_not_on_8765");
  if (!integrityOk) reasons.push("wal_or_db_integrity_fail");
  if (
    String(replica.status || "") === "MISSING_REPLICA" ||
    String(replica.ais_truth || m.ais_truth || "") === "missing"
  ) {
    reasons.push("missing_replica");
  }
  if (stale) reasons.push("ais_stale");
  if (rateLimits >= T.RATE_LIMIT_STORM_MAX) {
    reasons.push(`rate_limit_hits=${rateLimits}`);
  }
  if (reconnects >= T.RECONNECT_STORM_MAX) {
    reasons.push(`reconnect_storm=${reconnects}`);
  }
  if (hasFree) {
    if (freePct < T.DISK_FREE_CRITICAL_PCT) {
      reasons.push(`disk_free_critical=${freePct.toFixed(1)}pct`);
    } else if (freePct < T.DISK_FREE_MIN_PCT) {
      reasons.push(`disk_free_low=${freePct.toFixed(1)}pct`);
    }
  }

  const hardCritical = reasons.some((r) =>
    /^(port_drift|dashboard_not|wal_|missing_|ais_stale|http_429|reconnect_storm|disk_free_critical)/.test(
      r,
    ),
  );
  const hardDegraded = reasons.some((r) =>
    /^(failover_|disk_free_low)/.test(r),
  );

  /** @type {OraclePipelineStatus} */
  let status = "UNKNOWN";
  const ageOk = !Number.isFinite(age) || age < T.PIPELINE_LIVE_LAG_SEC;

  if (hardCritical) {
    status = "CRITICAL";
  } else if (hardDegraded) {
    status = "DEGRADED";
  } else if (liveOk && ageOk && reasons.length === 0) {
    status = "NOMINAL";
  } else if (liveOk && ageOk) {
    status =
      reconnects < T.RECONNECT_STORM_MAX && rateLimits === 0
        ? "NOMINAL"
        : "DEGRADED";
  } else if (Number.isFinite(age) && age <= 600) {
    status = "DEGRADED";
    if (!reasons.some((r) => r.startsWith("ais_warming"))) {
      reasons.push(`ais_warming_lag=${age.toFixed(0)}s`);
    }
  } else {
    status = "CRITICAL";
    if (!reasons.length) reasons.push("ais_not_live");
  }

  const out = {
    pipeline_health_status: status,
    reasons,
    ais_lag_sec: Number.isFinite(age) ? age : null,
    live_ok: liveOk,
    reconnects,
    rate_limit_hits: rateLimits,
    port_ok: portOk,
    active_node: activeNode === "london" ? "london" : "korolev",
    failover_in_progress: failover,
  };
  if (hasFree) out.disk_free_pct = Math.round(freePct * 100) / 100;
  return out;
}

/**
 * Client mirror of dual_gate.live_inference_confidence.
 * @param {number|null} cvPct
 * @param {string} fleetStatus
 * @param {number} coverage
 */
function oracle_liveInferenceConfidence(cvPct, fleetStatus, coverage) {
  const T = oracle_getThresholds();
  const status = String(fleetStatus || "INSUFFICIENT").toUpperCase();
  const cv =
    cvPct == null || !Number.isFinite(Number(cvPct)) ? null : Number(cvPct);
  let level = "LOW";
  let factor = 0.01;
  if (status === "FULL") {
    level = "HIGH";
    factor = 1.0;
  } else if (status === "LIMITED") {
    level = "LOW";
    factor = Math.min(1.0, Math.max(0.05, coverage / T.FLEET_SAMPLE_FULL_MIN));
  } else {
    level = "LOW";
    factor = Math.min(0.05, Math.max(0.01, coverage / T.FLEET_SAMPLE_FULL_MIN));
  }
  return {
    model_cv_accuracy_pct: cv == null ? null : Math.round(cv * 100) / 100,
    live_inference_confidence: level,
    live_inference_confidence_pct:
      cv == null ? null : Math.round(cv * factor * 100) / 100,
    live_confidence_factor: Math.round(factor * 10000) / 10000,
    fleet_sample_status: status,
    note:
      "model_cv_accuracy is offline purged-CV; live_inference_confidence " +
      "reflects terrestrial AIS sample representativeness and must not be " +
      "substituted by CV alone.",
  };
}

/**
 * Archive plane honesty flags (never feeds fleet_sample_status).
 * @param {Record<string, unknown>} m
 */
function oracle_archivePlane(m) {
  const archive =
    (m.archive && typeof m.archive === "object" ? m.archive : {}) || {};
  const apiStatus =
    (m.api_status && typeof m.api_status === "object" ? m.api_status : {}) || {};
  const label = String(
    archive.label ||
      apiStatus.label ||
      m.archive_label ||
      "ARCHIVE REGISTRY: SNAPSHOT / DEMO MODE",
  );
  const isSynthetic = oracle_bool(
    archive.is_synthetic ?? apiStatus.is_synthetic ?? m.is_synthetic,
    true,
  );
  const premiumFraud = /PREMIUM\s+SATELLITE/i.test(label);
  return {
    plane: "archive",
    feeds_fleet_sample: false,
    is_synthetic: isSynthetic,
    demo_mode: oracle_bool(archive.demo_mode ?? apiStatus.demo_mode, true),
    label,
    honest: !premiumFraud,
    warning: premiumFraud
      ? "fraudulent_premium_satellite_label"
      : null,
  };
}

/**
 * G3 terrestrial ceiling advisory (informational — does not change Dual Gate).
 * @param {number} coverage
 */
function oracle_g3Advisory(coverage) {
  const T = oracle_getThresholds();
  const n = Math.max(0, Math.floor(coverage));
  let band = "below_plateau";
  if (n >= T.G3_COVERAGE_PLATEAU_MIN && n <= T.G3_COVERAGE_PLATEAU_MAX) {
    band = "within_plateau";
  } else if (n > T.G3_COVERAGE_PLATEAU_MAX && n < T.FLEET_SAMPLE_LIMITED_MIN) {
    band = "above_plateau_still_insufficient";
  } else if (n >= T.FLEET_SAMPLE_LIMITED_MIN && n < T.FLEET_SAMPLE_FULL_MIN) {
    band = "limited_sample";
  } else if (n >= T.FLEET_SAMPLE_FULL_MIN) {
    band = "full_sample";
  }
  return {
    plane: "g3_terrestrial",
    top500_live_coverage: n,
    plateau_min: T.G3_COVERAGE_PLATEAU_MIN,
    plateau_max: T.G3_COVERAGE_PLATEAU_MAX,
    unique_per_hour_typical: T.G3_UNIQUE_PER_HOUR_TYPICAL,
    band,
    chase_full_forbidden: true,
    note:
      "Coverage 3–5 is G3 terrestrial ceiling, not a code bug. " +
      "Do not retune MMSI batching / multi-WS to chase FULL≥100.",
  };
}

export class OracleEngine {
  /**
   * @param {{ seed?: Record<string, unknown>|string|null }} [options]
   */
  constructor(options = {}) {
    this.moduleId = ORACLE_MODULE_ID;
    this.version = ORACLE_VERSION;
    this.thresholds = null;
    /** @type {Record<string, unknown>|null} */
    this._lastThresholds = null;
    /** @type {Record<string, unknown>|null} */
    this._lastMlDrift = null;
    /** @type {string[]} */
    this._errors = [];
    /** @type {string|null} */
    this._evaluatedAt = null;

    try {
      if (options && options.seed != null) {
        this.evaluateThresholds(options.seed);
      }
    } catch (err) {
      this._pushError("constructor", err);
    }
  }

  /**
   * @param {string} where
   * @param {unknown} err
   */
  _pushError(where, err) {
    try {
      const msg = err && /** @type {{message?: string}} */ (err).message
        ? /** @type {{message: string}} */ (err).message
        : String(err);
      this._errors.push(`${where}: ${msg}`);
      if (this._errors.length > 32) this._errors.splice(0, this._errors.length - 32);
    } catch {
      /* swallow */
    }
  }

  /**
   * Run metrics through Dual Gate + G3 + archive honesty planes.
   * Never throws; malformed JSON yields UNKNOWN/degraded advisory.
   * @param {unknown} metricsData
   * @returns {Record<string, unknown>}
   */
  evaluateThresholds(metricsData) {
    try {
      const parsed = oracle_safeParseMetrics(metricsData);
      if (!parsed.ok) this._pushError("evaluateThresholds.parse", parsed.error);

      const m = parsed.data;
      // Dual Gate SoT from health — refuse hardcoded client cutoffs.
      oracle_applyThresholdsFromHealth(m);
      if (!oracle_thresholdsReady()) {
        throw new Error(
          "evaluateThresholds: health.thresholds missing — dual_gate SoT required",
        );
      }
      this.thresholds = oracle_getThresholds();
      const quant =
        (m.quant_pipeline && typeof m.quant_pipeline === "object"
          ? m.quant_pipeline
          : {}) || {};
      const fleetBlock =
        (m.fleet_sample && typeof m.fleet_sample === "object"
          ? m.fleet_sample
          : {}) || {};

      const coverage = oracle_num(
        m.top500_live_coverage ??
          fleetBlock.top500_live_coverage ??
          quant.top500_live_coverage ??
          m.coverage,
        0,
      );
      const universe = oracle_num(
        fleetBlock.top500_universe ?? m.top500_universe,
        500,
      );

      const fleet = oracle_computeFleetSample(coverage, universe);
      const pipeline = oracle_computePipelineHealth(m);
      const archive = oracle_archivePlane(m);
      const g3 = oracle_g3Advisory(coverage);

      const cvPct = oracle_num(
        quant.model_cv_accuracy_pct ??
          m.model_cv_accuracy_pct ??
          quant.ensemble_accuracy_pct,
        NaN,
      );
      const liveConf = oracle_liveInferenceConfidence(
        Number.isFinite(cvPct) ? cvPct : null,
        fleet.fleet_sample_status,
        coverage,
      );

      const publishBlocked = pipeline.pipeline_health_status !== "NOMINAL";
      const productionActionable =
        pipeline.pipeline_health_status === "NOMINAL" &&
        fleet.fleet_sample_status === "FULL" &&
        !oracle_bool(m.is_synthetic, false) &&
        archive.honest;

      /** @type {string} */
      let contractMode = "WARN_NOMINAL";
      if (fleet.fleet_sample_status === "INSUFFICIENT") {
        contractMode = "WARN_NOMINAL";
      } else if (
        pipeline.pipeline_health_status === "CRITICAL" ||
        pipeline.pipeline_health_status === "UNKNOWN"
      ) {
        contractMode = "BLOCKED";
      } else if (fleet.fleet_sample_status === "LIMITED") {
        contractMode = "PASS_LIMITED";
      } else if (
        fleet.fleet_sample_status === "FULL" &&
        pipeline.pipeline_health_status === "NOMINAL"
      ) {
        contractMode = "PASS";
      } else if (fleet.fleet_sample_status === "FULL") {
        contractMode = "PASS_LIMITED";
      }

      const evaluatedAt = new Date().toISOString();
      const oracleState = {
        status: fleet.fleet_sample_status,
        confidence: liveConf.live_inference_confidence,
        coverage: coverage,
        last_eval: evaluatedAt,
        contract_mode: contractMode,
        pipeline_health_status: pipeline.pipeline_health_status,
        live_inference_confidence_pct: liveConf.live_inference_confidence_pct,
        module: this.moduleId,
        version: this.version,
      };

      const result = {
        ok: parsed.ok,
        module: this.moduleId,
        version: this.version,
        evaluated_at: evaluatedAt,
        parse_error: parsed.error,
        thresholds: { ...oracle_getThresholds() },
        pipeline_health: pipeline,
        pipeline_health_status: pipeline.pipeline_health_status,
        fleet_sample: fleet,
        fleet_sample_status: fleet.fleet_sample_status,
        g3,
        archive,
        live_inference: liveConf,
        oracle_state: oracleState,
        contract_mode: contractMode,
        publish_blocked: publishBlocked,
        production_actionable: productionActionable,
        blocked_reason: publishBlocked
          ? `pipeline_health_status=${pipeline.pipeline_health_status}`
          : fleet.fleet_sample_status !== "FULL"
            ? `fleet_sample_status=${fleet.fleet_sample_status}`
            : !productionActionable
              ? "synthetic_or_archive_dishonest"
              : null,
      };

      this._lastThresholds = result;
      this._evaluatedAt = result.evaluated_at;
      return result;
    } catch (err) {
      this._pushError("evaluateThresholds", err);
      const fallback = {
        ok: false,
        module: this.moduleId,
        version: this.version,
        evaluated_at: new Date().toISOString(),
        parse_error: err && /** @type {{message?: string}} */ (err).message
          ? /** @type {{message: string}} */ (err).message
          : String(err),
        pipeline_health_status: "UNKNOWN",
        fleet_sample_status: "UNKNOWN",
        contract_mode: "WARN_NOMINAL",
        oracle_state: {
          status: "UNKNOWN",
          confidence: "LOW",
          coverage: 0,
          last_eval: new Date().toISOString(),
          contract_mode: "WARN_NOMINAL",
        },
        publish_blocked: true,
        production_actionable: false,
        blocked_reason: "oracle_evaluateThresholds_exception",
      };
      this._lastThresholds = fallback;
      this._evaluatedAt = fallback.evaluated_at;
      return fallback;
    }
  }

  /**
   * Probabilistic ML drift score — advisory only, not a trading signal.
   * Separates offline CV from live representativeness (AGENTS ML lock).
   * @param {unknown} mlMetrics
   * @returns {Record<string, unknown>}
   */
  predictMLDrift(mlMetrics) {
    try {
      const parsed = oracle_safeParseMetrics(mlMetrics);
      if (!parsed.ok) this._pushError("predictMLDrift.parse", parsed.error);
      const m = parsed.data;
      const quant =
        (m.quant_pipeline && typeof m.quant_pipeline === "object"
          ? m.quant_pipeline
          : m) || {};

      const cv = oracle_num(
        quant.model_cv_accuracy_pct ?? m.model_cv_accuracy_pct,
        NaN,
      );
      const livePct = oracle_num(
        quant.live_inference_confidence_pct ?? m.live_inference_confidence_pct,
        NaN,
      );
      const liveFactor = oracle_num(
        quant.live_confidence_factor ?? m.live_confidence_factor,
        NaN,
      );
      const coverage = oracle_num(
        m.top500_live_coverage ?? quant.top500_live_coverage,
        0,
      );
      const fleetStatus = String(
        m.fleet_sample_status ||
          quant.fleet_sample_status ||
          oracle_computeFleetSample(coverage).fleet_sample_status,
      ).toUpperCase();

      const conf = oracle_liveInferenceConfidence(
        Number.isFinite(cv) ? cv : null,
        fleetStatus,
        coverage,
      );

      const effectiveLive =
        Number.isFinite(livePct)
          ? livePct
          : conf.live_inference_confidence_pct == null
            ? null
            : conf.live_inference_confidence_pct;

      /** Absolute gap between offline CV and live-scaled confidence (pp). */
      let driftPp = null;
      if (Number.isFinite(cv) && effectiveLive != null && Number.isFinite(effectiveLive)) {
        driftPp = Math.round(Math.abs(cv - effectiveLive) * 100) / 100;
      }

      /**
       * Probabilistic drift in [0, 1]:
       *  - base from sample factor (1 - live_confidence_factor)
       *  - plus CV–live gap normalized by 100
       *  - synthetic / stale model bump
       */
      const factor =
        Number.isFinite(liveFactor)
          ? liveFactor
          : conf.live_confidence_factor;
      let pDrift = Math.max(0, Math.min(1, 1 - factor));
      if (driftPp != null) {
        pDrift = Math.max(0, Math.min(1, 0.55 * pDrift + 0.45 * (driftPp / 100)));
      }

      const provenance = String(
        m.model_provenance || quant.model_provenance || "offline_batch",
      );
      const isSynthetic = oracle_bool(
        m.is_synthetic ?? quant.is_synthetic,
        fleetStatus !== "FULL",
      );
      if (isSynthetic) pDrift = Math.min(1, pDrift + 0.15);
      if (provenance !== "offline_batch" && provenance !== "unknown") {
        /* keep as-is; unknown provenance already caveated */
      }

      const lastRetrained = String(
        m.model_last_retrained_utc ||
          m.model_last_retrained ||
          quant.model_last_retrained_utc ||
          quant.model_last_retrained ||
          "",
      ).trim();

      let freshnessDays = null;
      if (lastRetrained) {
        const ts = Date.parse(lastRetrained);
        if (Number.isFinite(ts)) {
          freshnessDays = Math.max(
            0,
            (Date.now() - ts) / (1000 * 60 * 60 * 24),
          );
          if (freshnessDays > 14) pDrift = Math.min(1, pDrift + 0.1);
          if (freshnessDays > 45) pDrift = Math.min(1, pDrift + 0.15);
        }
      }

      pDrift = Math.round(pDrift * 10000) / 10000;

      /** @type {"LOW"|"MODERATE"|"HIGH"|"CRITICAL"|"UNKNOWN"} */
      let band = "UNKNOWN";
      if (!Number.isFinite(cv) && effectiveLive == null) band = "UNKNOWN";
      else if (pDrift < 0.15) band = "LOW";
      else if (pDrift < 0.4) band = "MODERATE";
      else if (pDrift < 0.7) band = "HIGH";
      else band = "CRITICAL";

      const result = {
        ok: parsed.ok,
        module: this.moduleId,
        version: this.version,
        predicted_at: new Date().toISOString(),
        parse_error: parsed.error,
        model_cv_accuracy_pct: conf.model_cv_accuracy_pct,
        live_inference_confidence: conf.live_inference_confidence,
        live_inference_confidence_pct: conf.live_inference_confidence_pct,
        live_confidence_factor: conf.live_confidence_factor,
        fleet_sample_status: fleetStatus,
        drift_pp: driftPp,
        p_drift: pDrift,
        drift_band: band,
        model_provenance: provenance || "offline_batch",
        model_last_retrained: lastRetrained || null,
        model_age_days:
          freshnessDays == null ? null : Math.round(freshnessDays * 10) / 10,
        is_synthetic: isSynthetic,
        production_actionable: false,
        advisory_only: true,
        note:
          "predictMLDrift is advisory. Never treat p_drift or CV% as live " +
          "fleet confidence. Node A/B serve offline_batch inference only.",
      };

      this._lastMlDrift = result;
      return result;
    } catch (err) {
      this._pushError("predictMLDrift", err);
      const fallback = {
        ok: false,
        module: this.moduleId,
        version: this.version,
        predicted_at: new Date().toISOString(),
        parse_error: err && /** @type {{message?: string}} */ (err).message
          ? /** @type {{message: string}} */ (err).message
          : String(err),
        p_drift: null,
        drift_band: "UNKNOWN",
        production_actionable: false,
        advisory_only: true,
        note: "predictMLDrift failed closed",
      };
      this._lastMlDrift = fallback;
      return fallback;
    }
  }

  /**
   * Aggregated health snapshot suitable for merging into health.json consumers.
   * Uses last evaluateThresholds / predictMLDrift; safe defaults if never run.
   * @returns {Record<string, unknown>}
   */
  getHealthStatus() {
    try {
      const thr = this._lastThresholds;
      const ml = this._lastMlDrift;
      const pipelineStatus = /** @type {OraclePipelineStatus} */ (
        (thr && thr.pipeline_health_status) || "UNKNOWN"
      );
      const fleetStatus = /** @type {OracleFleetSampleStatus} */ (
        (thr && thr.fleet_sample_status) || "UNKNOWN"
      );
      const coverage =
        thr && thr.fleet_sample
          ? oracle_num(
              /** @type {Record<string, unknown>} */ (thr.fleet_sample)
                .top500_live_coverage,
              0,
            )
          : 0;

      const operational =
        pipelineStatus === "NOMINAL"
          ? "NOMINAL"
          : pipelineStatus === "DEGRADED"
            ? "DEGRADED"
            : pipelineStatus === "CRITICAL"
              ? "CRITICAL"
              : "UNKNOWN";

      const oracleState =
        thr && thr.oracle_state
          ? thr.oracle_state
          : {
              status: fleetStatus,
              confidence:
                thr && thr.live_inference
                  ? /** @type {Record<string, unknown>} */ (thr.live_inference)
                      .live_inference_confidence
                  : "LOW",
              coverage,
              last_eval: this._evaluatedAt || new Date().toISOString(),
              contract_mode:
                fleetStatus === "INSUFFICIENT" ? "WARN_NOMINAL" : "UNKNOWN",
            };

      return {
        service: "oracle_engine",
        module: this.moduleId,
        version: this.version,
        status: pipelineStatus === "NOMINAL" ? "ok" : "degraded",
        operational_status: operational,
        pipeline_health_status: pipelineStatus,
        fleet_sample_status: fleetStatus,
        top500_live_coverage: coverage,
        oracle_state: oracleState,
        contract_mode:
          (thr && thr.contract_mode) ||
          oracleState.contract_mode ||
          "WARN_NOMINAL",
        publish_blocked: thr ? Boolean(thr.publish_blocked) : true,
        production_actionable: thr ? Boolean(thr.production_actionable) : false,
        blocked_reason: thr ? thr.blocked_reason : "oracle_not_evaluated",
        g3: thr && thr.g3 ? thr.g3 : oracle_g3Advisory(coverage),
        archive:
          thr && thr.archive
            ? thr.archive
            : {
                plane: "archive",
                feeds_fleet_sample: false,
                is_synthetic: true,
                demo_mode: true,
                honest: true,
              },
        ml_drift: ml
          ? {
              p_drift: ml.p_drift,
              drift_band: ml.drift_band,
              live_inference_confidence: ml.live_inference_confidence,
              model_cv_accuracy_pct: ml.model_cv_accuracy_pct,
              model_provenance: ml.model_provenance,
              advisory_only: true,
            }
          : null,
        live_inference: thr && thr.live_inference ? thr.live_inference : null,
        pipeline_health: thr && thr.pipeline_health ? thr.pipeline_health : null,
        fleet_sample: thr && thr.fleet_sample ? thr.fleet_sample : null,
        evaluated_at: this._evaluatedAt,
        generated_at: new Date().toISOString(),
        errors: this._errors.slice(),
        thresholds: { ...oracle_getThresholds() },
        truth_contract: {
          oracle: "advisory_client_mirror",
          dual_gate_sot: "services/dual_gate.py",
          ais: thr && thr.pipeline_health
            ? /** @type {Record<string, unknown>} */ (thr.pipeline_health).live_ok
              ? "live_ok"
              : "stale_or_unknown"
            : "unknown",
        },
      };
    } catch (err) {
      this._pushError("getHealthStatus", err);
      return {
        service: "oracle_engine",
        module: this.moduleId,
        version: this.version,
        status: "degraded",
        operational_status: "UNKNOWN",
        pipeline_health_status: "UNKNOWN",
        fleet_sample_status: "UNKNOWN",
        top500_live_coverage: 0,
        publish_blocked: true,
        production_actionable: false,
        blocked_reason: "oracle_getHealthStatus_exception",
        generated_at: new Date().toISOString(),
        errors: this._errors.slice(),
      };
    }
  }
}

/**
 * Factory — preferred entry for controllers.
 * @param {{ seed?: Record<string, unknown>|string|null }} [options]
 * @returns {OracleEngine}
 */
export function oracle_createEngine(options) {
  try {
    return new OracleEngine(options || {});
  } catch (err) {
    const engine = new OracleEngine();
    engine._pushError("oracle_createEngine", err);
    return engine;
  }
}

/** Process-wide singleton for non-module script tags / HUD glue. */
export const oracle_engine = oracle_createEngine();

try {
  if (typeof globalThis !== "undefined") {
    globalThis.oracle_engine = oracle_engine;
    globalThis.OracleEngine = OracleEngine;
    globalThis.oracle_createEngine = oracle_createEngine;
    globalThis.ORACLE_THRESHOLDS = ORACLE_THRESHOLDS;
  }
} catch {
  /* non-browser / frozen globalThis */
}
