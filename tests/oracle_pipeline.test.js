/**
 * End-to-end Oracle pipeline test — Live Korolev INSUFFICIENT @ NOMINAL.
 *
 * Run:
 *   node --test tests/oracle_pipeline.test.js
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  OracleEngine,
  oracle_createEngine,
  oracle_setThresholdsForTest,
  oracle_getThresholds,
} from "../web/js/oracle_engine.js";
import { oracle_buildSheetView } from "../web/js/oracle_sheet.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const HEALTH_JSON = path.join(ROOT, "output", "api", "v1", "health.json");
const HEALTH_BARE = path.join(ROOT, "output", "api", "v1", "health");

/**
 * Dual Gate SoT mirror for offline tests — must match services.dual_gate
 * export_dual_gate_thresholds() (injected, never invented in oracle_engine.js).
 */
const DUAL_GATE_THRESHOLDS = Object.freeze({
  source: "services.dual_gate",
  FLEET_SAMPLE_FULL_MIN: 100,
  FLEET_SAMPLE_LIMITED_MIN: 5,
  FLEET_WIDE_METRIC_MIN_N: 30,
  PIPELINE_LIVE_LAG_SEC: 300,
  DISK_FREE_MIN_PCT: 20.0,
  DISK_FREE_CRITICAL_PCT: 10.0,
  RECONNECT_STORM_MAX: 5,
  RATE_LIMIT_STORM_MAX: 1,
  G3_COVERAGE_PLATEAU_MIN: 3,
  G3_COVERAGE_PLATEAU_MAX: 5,
  G3_UNIQUE_PER_HOUR_TYPICAL: 7,
});

/** Live Korolev fixture — pipeline healthy, terrestrial sample insufficient. */
function korolevLiveFixture() {
  return {
    service: "sentinel_dashboard",
    status: "ok",
    active_node: "korolev",
    operational_status: "NOMINAL",
    pipeline_health_status: "NOMINAL",
    fleet_sample_status: "INSUFFICIENT",
    top500_live_coverage: 2,
    live_ok: true,
    stale: false,
    ais_lag_sec: 7,
    integrity_ok: true,
    port_ok: true,
    disk_free_pct: 36.05,
    failover_in_progress: false,
    thresholds: { ...DUAL_GATE_THRESHOLDS },
    replica: {
      stale: false,
      live_ok: true,
      age_sec: 7,
      status: "FRESH",
      ais_truth: "live_ok",
      integrity_ok: true,
    },
    pipeline_health: {
      pipeline_health_status: "NOMINAL",
      reasons: [],
      ais_lag_sec: 7,
      live_ok: true,
      reconnects: 0,
      rate_limit_hits: 0,
      port_ok: true,
      active_node: "korolev",
      failover_in_progress: false,
      disk_free_pct: 36.05,
    },
    fleet_sample: {
      fleet_sample_status: "INSUFFICIENT",
      top500_live_coverage: 2,
      top500_universe: 500,
      limited_min: DUAL_GATE_THRESHOLDS.FLEET_SAMPLE_LIMITED_MIN,
      full_min: DUAL_GATE_THRESHOLDS.FLEET_SAMPLE_FULL_MIN,
    },
    quant_pipeline: {
      model_cv_accuracy_pct: 53.6,
      model_provenance: "offline_batch",
      model_last_retrained: "2026-09-01T00:00:00Z",
    },
    archive: {
      is_synthetic: true,
      demo_mode: true,
      label: "ARCHIVE REGISTRY: SNAPSHOT / DEMO MODE",
      honest: true,
    },
    is_synthetic: false,
  };
}

/**
 * Persist oracle_state onto disk health.json (mirrors Python sync path).
 * @param {Record<string, unknown>} doc
 * @param {Record<string, unknown>} oracleState
 */
function writeHealthWithOracleState(doc, oracleState) {
  const out = {
    ...doc,
    oracle_state: oracleState,
    oracle_contract_mode: oracleState.contract_mode || "WARN_NOMINAL",
    generated_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
  };
  fs.mkdirSync(path.dirname(HEALTH_JSON), { recursive: true });
  const text = JSON.stringify(out, null, 2) + "\n";
  fs.writeFileSync(HEALTH_JSON, text, "utf8");
  fs.writeFileSync(HEALTH_BARE, text, "utf8");
  return out;
}

test("oracle pipeline: Korolev NOMINAL + coverage=2 INSUFFICIENT → PASS planes", async (t) => {
  const t0 = Date.now();
  oracle_setThresholdsForTest(DUAL_GATE_THRESHOLDS);
  const fixture = korolevLiveFixture();
  const engine = oracle_createEngine();

  await t.test("0) thresholds from Dual Gate blob — no hardcoded JS literals", () => {
    const T = oracle_getThresholds();
    assert.equal(T.FLEET_SAMPLE_LIMITED_MIN, 5);
    assert.equal(T.FLEET_SAMPLE_FULL_MIN, 100);
    const src = fs.readFileSync(
      path.join(ROOT, "web", "js", "oracle_engine.js"),
      "utf8",
    );
    assert.equal(
      /FLEET_SAMPLE_LIMITED_MIN:\s*5/.test(src),
      false,
      "oracle_engine.js must not hardcode FLEET_SAMPLE_LIMITED_MIN: 5",
    );
  });

  await t.test("1) OracleEngine.evaluateThresholds on Live Korolev fixture", () => {
    const ev = engine.evaluateThresholds(fixture);
    assert.equal(ev.pipeline_health_status, "NOMINAL");
    assert.equal(ev.fleet_sample_status, "INSUFFICIENT");
    assert.equal(ev.oracle_state.status, "INSUFFICIENT");
    assert.equal(ev.oracle_state.coverage, 2);
    assert.equal(ev.contract_mode, "WARN_NOMINAL");
    assert.equal(ev.publish_blocked, false, "pipeline NOMINAL must not block publish");
    assert.equal(ev.production_actionable, false, "INSUFFICIENT must not be actionable");
  });

  await t.test("2) Dual Gate / G3 / archive / ML planes return PASS semantics", () => {
    const ev = engine.evaluateThresholds(fixture);
    const ml = engine.predictMLDrift(fixture);

    // Dual Gate — pipeline PASS (NOMINAL); sample informational INSUFFICIENT
    assert.equal(ev.pipeline_health.pipeline_health_status, "NOMINAL");
    assert.ok(
      Array.isArray(ev.pipeline_health.reasons) && ev.pipeline_health.reasons.length === 0,
    );
    assert.equal(
      ev.contract_mode,
      "WARN_NOMINAL",
      "OOB green mode for INSUFFICIENT",
    );

    // G3 — honest terrestrial advisory PASS (no chase-FULL)
    assert.equal(ev.g3.plane, "g3_terrestrial");
    assert.equal(ev.g3.top500_live_coverage, 2);
    assert.equal(ev.g3.chase_full_forbidden, true);
    assert.equal(ev.g3.band, "below_plateau");

    // Archive — honest SNAPSHOT/DEMO PASS
    assert.equal(ev.archive.feeds_fleet_sample, false);
    assert.equal(ev.archive.honest, true);
    assert.equal(ev.archive.is_synthetic, true);
    assert.match(String(ev.archive.label), /SNAPSHOT|DEMO/i);

    // ML — advisory PASS (never actionable)
    assert.equal(ml.advisory_only, true);
    assert.equal(ml.production_actionable, false);
    assert.ok(typeof ml.p_drift === "number" && ml.p_drift >= 0 && ml.p_drift <= 1);
    assert.ok(["LOW", "MODERATE", "HIGH", "CRITICAL", "UNKNOWN"].includes(ml.drift_band));
    assert.notEqual(
      ml.live_inference_confidence,
      undefined,
      "CV must stay separate from live confidence",
    );

    // Aggregate PASS flag for the four planes
    const planePass = {
      dual_gate: ev.pipeline_health_status === "NOMINAL" && ev.contract_mode === "WARN_NOMINAL",
      g3: ev.g3.chase_full_forbidden === true && ev.g3.top500_live_coverage === 2,
      archive: ev.archive.honest === true && ev.archive.feeds_fleet_sample === false,
      ml: ml.advisory_only === true && ml.production_actionable === false,
    };
    assert.deepEqual(planePass, {
      dual_gate: true,
      g3: true,
      archive: true,
      ml: true,
    });
  });

  await t.test("3) health.json rewritten with fresh oracle_state timestamp", () => {
    const ev = engine.evaluateThresholds(fixture);
    const beforeMtime = fs.existsSync(HEALTH_JSON)
      ? fs.statSync(HEALTH_JSON).mtimeMs
      : 0;

    const written = writeHealthWithOracleState(fixture, ev.oracle_state);
    assert.ok(fs.existsSync(HEALTH_JSON), "health.json must exist");

    const disk = JSON.parse(fs.readFileSync(HEALTH_JSON, "utf8"));
    assert.ok(disk.oracle_state, "oracle_state present");
    for (const key of ["status", "confidence", "coverage", "last_eval"]) {
      assert.ok(key in disk.oracle_state, `oracle_state.${key}`);
    }
    assert.equal(disk.oracle_state.status, "INSUFFICIENT");
    assert.equal(disk.oracle_state.coverage, 2);
    assert.equal(disk.oracle_state.confidence, "LOW");

    const lastEval = Date.parse(disk.oracle_state.last_eval);
    assert.ok(Number.isFinite(lastEval), "last_eval must be valid ISO");
    assert.ok(lastEval >= t0 - 1000, "last_eval must be fresh (near test start)");
    assert.ok(lastEval <= Date.now() + 2000, "last_eval not in the far future");

    const gen = Date.parse(disk.generated_at || written.generated_at);
    assert.ok(Number.isFinite(gen), "generated_at valid");

    const afterMtime = fs.statSync(HEALTH_JSON).mtimeMs;
    assert.ok(afterMtime >= beforeMtime, "health.json mtime must advance or stay current");

    // bare health path must match
    const bare = JSON.parse(fs.readFileSync(HEALTH_BARE, "utf8"));
    assert.equal(
      createHash("sha256").update(JSON.stringify(disk.oracle_state)).digest("hex"),
      createHash("sha256").update(JSON.stringify(bare.oracle_state)).digest("hex"),
    );
  });

  await t.test("4) oracle_sheet receives correct render state", () => {
    const disk = JSON.parse(fs.readFileSync(HEALTH_JSON, "utf8"));
    const view = oracle_buildSheetView(disk);

    assert.equal(view.coverage, 2);
    assert.equal(view.fleet_sample_status, "INSUFFICIENT");
    assert.equal(view.predictor_status, "WARN");
    assert.equal(view.confidence, "LOW");
    assert.ok(Array.isArray(view.metrics) && view.metrics.length === 4);

    const ids = view.metrics.map((m) => m.id);
    assert.deepEqual(ids, [
      "dual_gate_margin",
      "g3_health",
      "archive_readiness",
      "ml_drift",
    ]);

    for (const m of view.metrics) {
      assert.ok(m.title, "metric title");
      assert.ok(m.display != null && String(m.display).length > 0, "metric display");
    }

    assert.ok(Array.isArray(view.forecast_series) && view.forecast_series.length === 25);
    assert.ok(
      view.forecast_series.every((x) => typeof x === "number" && x >= 0 && x <= 1),
    );
  });
});
