/**
 * Shared vessel-card telemetry helpers (SPEED · TTF VAL).
 * Aligns MWh/m³ with services/ttf_forecast/quant_pipeline.py (BOGDecayEngine).
 */

/** ≈ 21.1 MJ/kg × 450 kg/m³ / 3600 → MWh thermal per m³ LNG */
export const MWH_PER_M3_LNG = 2.6375;

/** USD per EUR — inverse of hedging_engine FALLBACK_USD_EUR (0.92) */
export const EUR_USD_RATE = 1.087;

export const QMAX_CARGO_M3 = 266_000;
export const QFLEX_CARGO_M3 = 216_000;
export const YAMALMAX_CARGO_M3 = 172_600;
export const CONVENTIONAL_CARGO_M3 = 145_000;

/**
 * Live / bake-time TTF spot (€/MWh) from Sentinel payload.
 * @returns {number}
 */
export function resolveTtfSpotEurMwh() {
  const P = window.__SENTINEL_PAYLOAD__ || {};
  const candidates = [
    P.ttf_spot,
    P.ttf_forecast?.spot_eur_mwh,
    P.quant_pipeline?.ttf_spot,
  ];
  for (const c of candidates) {
    const n = Number(c);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return 0;
}

/**
 * Nominal cargo capacity m³ (not DWT tonnes).
 * @param {Record<string, unknown>} vessel
 * @returns {number}
 */
export function resolveCargoM3(vessel) {
  const direct = Number(
    vessel?.cargo_m3 ?? vessel?.capacity_m3 ?? vessel?.ttf_cargo_m3 ?? NaN,
  );
  if (Number.isFinite(direct) && direct > 0) return direct;

  const cls = String(vessel?.class || "").toLowerCase();
  if (cls.includes("q-max") || cls.includes("qmax")) return QMAX_CARGO_M3;
  if (cls.includes("q-flex") || cls.includes("qflex")) return QFLEX_CARGO_M3;
  if (cls.includes("yamal") || cls.includes("arc7")) return YAMALMAX_CARGO_M3;

  const dwt = Number(vessel?.dwt_tons || 0);
  if (dwt >= 150_000) return QMAX_CARGO_M3;
  if (dwt >= 120_000) return QFLEX_CARGO_M3;
  if (dwt >= 90_000) return YAMALMAX_CARGO_M3;
  return CONVENTIONAL_CARGO_M3;
}

/**
 * Current speed knots — payload field only; absent → 0.0.
 * @param {Record<string, unknown>} vessel
 * @returns {number}
 */
export function resolveSpeedKnots(vessel) {
  const n = Number(
    vessel?.speed_knots ?? vessel?.sog ?? vessel?.ais_sog ?? NaN,
  );
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

/**
 * Notional full-capacity LNG cargo value in USD.
 * Value = m³ × €/MWh × MWh/m³ × EUR→USD
 * Prefer vessel.ttf_cargo_value_usd when provided by backend.
 * @param {Record<string, unknown>} vessel
 * @param {number} [ttfSpot]
 * @returns {{ usd: number, source: string, capacity_m3: number, ttf_spot: number }}
 */
export function resolveTtfCargoValueUsd(vessel, ttfSpot) {
  const pre = Number(vessel?.ttf_cargo_value_usd);
  if (Number.isFinite(pre) && pre > 0) {
    return {
      usd: pre,
      source: "manifest_ttf_cargo_value_usd",
      capacity_m3: resolveCargoM3(vessel),
      ttf_spot: Number(ttfSpot) || resolveTtfSpotEurMwh(),
    };
  }
  const spot = Number(ttfSpot);
  const ttf = Number.isFinite(spot) && spot > 0 ? spot : resolveTtfSpotEurMwh();
  const m3 = resolveCargoM3(vessel);
  const usd = m3 * ttf * MWH_PER_M3_LNG * EUR_USD_RATE;
  return {
    usd: Number.isFinite(usd) && usd > 0 ? usd : 0,
    source: "notional_full_capacity_x_ttf",
    capacity_m3: m3,
    ttf_spot: ttf,
  };
}

/** Format `$42.8` + unit span `M` for .t10-telem-cell .val */
export function formatSpeedHtml(knots) {
  const k = Number.isFinite(Number(knots)) ? Number(knots) : 0;
  return `${k.toFixed(1)}<span>kn</span>`;
}

export function formatTtfValHtml(usd) {
  const n = Number(usd);
  if (!Number.isFinite(n) || n <= 0) return `$0.0<span>M</span>`;
  return `$${(n / 1e6).toFixed(1)}<span>M</span>`;
}

/**
 * Secondary telem row markup (SPEED + TTF VAL) under LOA/BEAM/DRAFT/DWT.
 * @param {Record<string, unknown>} vessel
 */
export function renderSpeedTtfTelemRow(vessel) {
  const speed = resolveSpeedKnots(vessel);
  const { usd, source, capacity_m3, ttf_spot } = resolveTtfCargoValueUsd(vessel);
  const title =
    source === "manifest_ttf_cargo_value_usd"
      ? "Pre-calculated ttf_cargo_value_usd from manifest/API"
      : `Notional full-capacity proxy · ${Math.round(capacity_m3).toLocaleString("en-US")} m³ × €${ttf_spot.toFixed(2)}/MWh × ${MWH_PER_M3_LNG} MWh/m³ × ${EUR_USD_RATE} USD/EUR · NOT bill-of-lading`;
  return `
    <div class="t10-telem t10-telem--ext" aria-label="Speed and notional TTF cargo value">
      <div class="t10-telem-cell" data-metric="speed">
        <div class="lbl">SPEED</div>
        <div class="val">${formatSpeedHtml(speed)}</div>
      </div>
      <div class="t10-telem-cell" data-metric="ttf-val" title="${title.replace(/"/g, "&quot;")}">
        <div class="lbl">TTF VAL</div>
        <div class="val">${formatTtfValHtml(usd)}</div>
      </div>
    </div>`;
}
