/**
 * Route Analytics Engine — horizon filter, KPI coefficients, selection state
 * Consumes window.__SENTINEL_PAYLOAD__.route_analytics (build-time embed).
 */
export function getRoutePayload() {
  const P = window.__SENTINEL_PAYLOAD__ || {};
  return P.route_analytics || window.__ROUTE_PAYLOAD__ || null;
}

export function listVessels(payload) {
  return (payload && payload.vessels) || [];
}

export function listGroups(payload) {
  return (payload && payload.groups) || ["ALL"];
}

/**
 * Resolve tracks + KPIs for horizon and selection (IMO or group tag).
 */
export function resolveSlice(payload, { horizon = "7d", selection = "ALL" } = {}) {
  const data = payload || getRoutePayload();
  if (!data) {
    return { vessels: [], tracks: {}, kpis: {}, fleetKpi: {}, panels: {}, sts: [], heatmap: [], error: "NO_PAYLOAD" };
  }
  const hz = data.by_horizon?.[horizon] || data.by_horizon?.["7d"] || {};
  const allVessels = listVessels(data);
  let vessels = allVessels;
  if (selection && selection !== "ALL") {
    if (/^\d+$/.test(String(selection))) {
      vessels = allVessels.filter((v) => String(v.imo) === String(selection));
    } else {
      vessels = allVessels.filter((v) => String(v.group_tag) === String(selection) || String(v.tier) === String(selection));
    }
  }
  const imoSet = new Set(vessels.map((v) => String(v.imo)));
  const tracks = {};
  const kpis = {};
  Object.entries(hz.tracks || {}).forEach(([imo, tr]) => {
    if (imoSet.has(String(imo))) tracks[imo] = tr;
  });
  Object.entries(hz.vessel_kpis || {}).forEach(([imo, k]) => {
    if (imoSet.has(String(imo))) kpis[imo] = k;
  });

  // Recompute fleet KPI for selection
  const kpiList = Object.values(kpis);
  const fleetKpi =
    selection === "ALL"
      ? hz.fleet_kpi || {}
      : {
          current_sog: avg(kpiList.map((k) => k.current_sog)),
          total_dwt: sum(kpiList.map((k) => k.total_dwt)),
          voyage_efficiency: avg(kpiList.map((k) => k.voyage_efficiency)),
          anomaly_flags: sum(kpiList.map((k) => k.anomaly_flags)),
          avg_dwt_utilization: avg(kpiList.map((k) => k.dwt_utilization)),
          avg_route_anomaly: avg(kpiList.map((k) => k.route_anomaly_score)),
        };

  // Panels: if single vessel, rebuild lightweight series from its track; else use horizon panels
  let panels = hz.panels || {};
  if (vessels.length === 1) {
    panels = buildPanelsFromTrack(vessels[0], tracks[String(vessels[0].imo)] || [], kpis[String(vessels[0].imo)] || {}, data.aggregates || [], horizon);
  }

  const sts = (hz.sts_zones || []).filter((z) => imoSet.has(String(z.imo)));
  const heatmap = (hz.heatmap || []).filter((h) => imoSet.has(String(h.imo)));

  return {
    vessels,
    tracks,
    kpis,
    fleetKpi,
    panels,
    sts,
    heatmap,
    horizon,
    selection,
    source_mode: data.source_mode,
  };
}

function avg(arr) {
  if (!arr.length) return 0;
  return Math.round((arr.reduce((a, b) => a + Number(b || 0), 0) / arr.length) * 100) / 100;
}
function sum(arr) {
  return Math.round(arr.reduce((a, b) => a + Number(b || 0), 0) * 100) / 100;
}

function buildPanelsFromTrack(vessel, track, kpi, aggregates, horizon) {
  const labels = (track || []).map((p) => (horizon === "1d" ? String(p.t).slice(11, 16) : String(p.t).slice(5, 16)));
  const sog = (track || []).map((p) => p.sog);
  const draft = (track || []).map((p) => p.draft_m);
  return {
    primary_imo: vessel.imo,
    p1_speed: { labels, sog, design_speed: vessel.design_speed_kn || 19.5 },
    p2_draught: { labels, draft_m: draft, design_draft: vessel.design_draft_m || 12 },
    p3_dwt: {
      labels: [vessel.name],
      capacity: [vessel.dwt_tons],
      utilized: [(kpi.dwt_utilization || 0) * (vessel.dwt_tons || 0)],
    },
    p4_engine_fuel: {
      labels: [vessel.name],
      engine_load: [kpi.engine_load_factor || 0],
      fuel_efficiency: [kpi.fuel_efficiency_index || 0],
    },
    p5_dark_gaps: { labels: ["0-2h", "2-6h", "6-12h", "12-24h", "24h+"], counts: [1, 0, 0, 0, Number(kpi.dark_ais_gap_prob > 0.3)] },
    p6_radar: {
      labels: ["Sanction Risk", "Speed Variance", "Route Deviation", "STS Probability", "Dark AIS", "Fuel Drag"],
      values: [
        100 * (kpi.sanction_proximity_index || 0),
        100 * (kpi.speed_degradation_index || 0),
        100 * (kpi.route_anomaly_score || 0),
        35,
        100 * (kpi.dark_ais_gap_prob || 0),
        100 * (1 - (kpi.fuel_efficiency_index || 0)),
      ],
    },
    p7_eta: {
      progress_pct: Math.min(100, 55 + 0.35 * (kpi.voyage_efficiency || 50)),
      eta_variance_h: kpi.eta_variance_h || 0,
      label: `ΔETA ${(kpi.eta_variance_h || 0) >= 0 ? "+" : ""}${(kpi.eta_variance_h || 0).toFixed?.(1) ?? kpi.eta_variance_h}h`,
    },
    p8_weather: {
      labels: labels.filter((_, i) => i % Math.max(1, Math.floor(labels.length / 12)) === 0),
      wave_m: labels.filter((_, i) => i % Math.max(1, Math.floor(labels.length / 12)) === 0).map((_, i) => +(1.2 + 0.6 * Math.sin(i / 3)).toFixed(2)),
      wind_kn: labels.filter((_, i) => i % Math.max(1, Math.floor(labels.length / 12)) === 0).map((_, i) => +(14 + 5 * Math.cos(i / 4)).toFixed(1)),
      current_kn: labels.filter((_, i) => i % Math.max(1, Math.floor(labels.length / 12)) === 0).map((_, i) => +(0.5 + 0.3 * Math.sin(i / 5)).toFixed(2)),
    },
    p9_fleet_density: {
      labels: (aggregates || []).slice(-30).map((a) => String(a.date).slice(5)),
      chokepoint_density: (aggregates || []).slice(-30).map((a) => a.chokepoint_density_index || 0),
      sts_events: (aggregates || []).slice(-30).map((a) => a.sts_event_count || 0),
      active_tankers: (aggregates || []).slice(-30).map((a) => a.total_active_tankers || 0),
    },
  };
}

export function speedColor(sog) {
  const s = Number(sog) || 0;
  if (s < 1) return "#ef4444";
  if (s < 6) return "#f59e0b";
  if (s < 12) return "#eab308";
  if (s < 16) return "#22c55e";
  return "#00e5ff";
}

export function exportSliceCsv(slice) {
  const rows = [["imo", "name", "sog", "avg_sog", "dwt", "dwt_util", "anomaly", "efficiency"]];
  (slice.vessels || []).forEach((v) => {
    const k = slice.kpis[String(v.imo)] || {};
    rows.push([
      v.imo,
      v.name,
      k.current_sog,
      k.avg_sog,
      v.dwt_tons,
      k.dwt_utilization,
      k.route_anomaly_score,
      k.voyage_efficiency,
    ]);
  });
  return rows.map((r) => r.join(",")).join("\n");
}

export default {
  getRoutePayload,
  resolveSlice,
  listVessels,
  listGroups,
  speedColor,
  exportSliceCsv,
};
