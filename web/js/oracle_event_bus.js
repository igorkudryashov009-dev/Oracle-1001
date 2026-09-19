/**
 * Oracle Event Bus — CustomEvent hub for Sentinel HUD sheets.
 * Namespace: oracle_* · isolates Dual Gate predictor from sheet DOM churn.
 *
 * Events:
 *   oracle:fleet-metrics  — top10 / arctic emitted vessel telemetry snapshots
 *   oracle:metrics-refresh — request Oracle sheet to recompute visuals
 *   oracle:sheet-change    — mirror of sheet navigation (optional)
 */
export const ORACLE_BUS_EVENTS = Object.freeze({
  FLEET_METRICS: "oracle:fleet-metrics",
  METRICS_REFRESH: "oracle:metrics-refresh",
  SHEET_CHANGE: "oracle:sheet-change",
});

const TARGET = typeof document !== "undefined" ? document : null;

/** @type {Map<string, Set<Function>>} */
const oracle_listenerRegistry = new Map();

/**
 * @param {string} type
 * @param {Record<string, unknown>} [detail]
 */
export function oracle_busEmit(type, detail = {}) {
  try {
    if (!TARGET) return false;
    const payload = {
      ...detail,
      emitted_at: new Date().toISOString(),
      bus: "oracle_event_bus",
    };
    TARGET.dispatchEvent(
      new CustomEvent(type, {
        detail: payload,
        bubbles: true,
        cancelable: false,
      }),
    );
    return true;
  } catch (err) {
    console.warn("[oracle_bus] emit failed", type, err);
    return false;
  }
}

/**
 * Subscribe; returns unsubscribe fn. Prefer AbortSignal for sheet lifecycle.
 * @param {string} type
 * @param {(ev: CustomEvent) => void} handler
 * @param {{ signal?: AbortSignal }} [opts]
 * @returns {() => void}
 */
export function oracle_busOn(type, handler, opts = {}) {
  if (!TARGET || typeof handler !== "function") {
    return () => {};
  }

  /** @param {Event} ev */
  const wrapped = (ev) => {
    try {
      handler(/** @type {CustomEvent} */ (ev));
    } catch (err) {
      console.warn("[oracle_bus] handler error", type, err);
    }
  };

  TARGET.addEventListener(type, wrapped);
  if (!oracle_listenerRegistry.has(type)) {
    oracle_listenerRegistry.set(type, new Set());
  }
  oracle_listenerRegistry.get(type).add(wrapped);

  const unsub = () => {
    try {
      TARGET.removeEventListener(type, wrapped);
      oracle_listenerRegistry.get(type)?.delete(wrapped);
    } catch {
      /* ignore */
    }
  };

  if (opts.signal) {
    if (opts.signal.aborted) {
      unsub();
    } else {
      opts.signal.addEventListener("abort", unsub, { once: true });
    }
  }

  return unsub;
}

/** Tear down all bus listeners registered through oracle_busOn (emergency). */
export function oracle_busDestroyAll() {
  if (!TARGET) return;
  for (const [type, set] of oracle_listenerRegistry.entries()) {
    for (const fn of set) {
      try {
        TARGET.removeEventListener(type, fn);
      } catch {
        /* ignore */
      }
    }
    set.clear();
  }
  oracle_listenerRegistry.clear();
}

/**
 * Summarize a vessel list for cross-sheet metrics (no Dual Gate mutation).
 * @param {unknown[]} vessels
 * @param {string} source
 */
export function oracle_summarizeFleet(vessels, source) {
  const list = Array.isArray(vessels) ? vessels : [];
  let speedSum = 0;
  let speedN = 0;
  let dwtSum = 0;
  for (const v of list) {
    if (!v || typeof v !== "object") continue;
    const row = /** @type {Record<string, unknown>} */ (v);
    const sog = Number(row.speed_knots ?? row.sog ?? row.ais_sog);
    if (Number.isFinite(sog) && sog >= 0) {
      speedSum += sog;
      speedN += 1;
    }
    const dwt = Number(row.dwt_tons);
    if (Number.isFinite(dwt) && dwt > 0) dwtSum += dwt;
  }
  return {
    source: String(source || "unknown"),
    vessel_count: list.length,
    avg_speed_kn: speedN ? Math.round((speedSum / speedN) * 10) / 10 : null,
    total_dwt_kt: dwtSum ? Math.round(dwtSum / 1000) : null,
    sample_imos: list
      .slice(0, 8)
      .map((v) =>
        v && typeof v === "object"
          ? String(/** @type {Record<string, unknown>} */ (v).imo || "")
          : "",
      )
      .filter(Boolean),
  };
}

/**
 * Emit fleet metrics + refresh request (top10 / arctic call site).
 * @param {string} source
 * @param {unknown[]} vessels
 * @param {Record<string, unknown>} [extra]
 */
export function oracle_publishFleetUpdate(source, vessels, extra = {}) {
  const summary = oracle_summarizeFleet(vessels, source);
  const detail = { ...summary, ...extra };
  oracle_busEmit(ORACLE_BUS_EVENTS.FLEET_METRICS, detail);
  oracle_busEmit(ORACLE_BUS_EVENTS.METRICS_REFRESH, {
    reason: "fleet_update",
    source,
    vessel_count: summary.vessel_count,
  });
  return detail;
}

if (typeof globalThis !== "undefined") {
  try {
    globalThis.oracle_busEmit = oracle_busEmit;
    globalThis.oracle_busOn = oracle_busOn;
    globalThis.ORACLE_BUS_EVENTS = ORACLE_BUS_EVENTS;
  } catch {
    /* ignore */
  }
}
