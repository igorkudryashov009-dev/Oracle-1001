/**
 * Route Analytics — 9-panel Chart.js infographic grid
 */
const CHART_IDS = [
  "routeP1",
  "routeP2",
  "routeP3",
  "routeP4",
  "routeP5",
  "routeP6",
  "routeP7",
  "routeP8",
  "routeP9",
];

const GOLD = "#d4af37";
const CYAN = "#00e5ff";
const MUTED = "#8aa4bf";

function destroyAll() {
  if (typeof Chart === "undefined" || !Chart.getChart) return;
  CHART_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const ch = Chart.getChart(el);
    if (ch) {
      try {
        ch.destroy();
      } catch (_) {
        /* ignore */
      }
    }
  });
}

function baseOpts(extra = {}) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: MUTED, boxWidth: 10, font: { size: 10 } } },
      tooltip: { mode: "index", intersect: false },
    },
    scales: {
      x: { ticks: { color: MUTED, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { color: "rgba(120,180,220,.08)" } },
      y: { ticks: { color: MUTED }, grid: { color: "rgba(120,180,220,.08)" } },
    },
    ...extra,
  };
}

function line(id, labels, datasets, opts) {
  const el = document.getElementById(id);
  if (!el || typeof Chart === "undefined") return;
  new Chart(el, {
    type: "line",
    data: { labels, datasets },
    options: baseOpts(opts),
  });
}

function bar(id, labels, datasets, opts) {
  const el = document.getElementById(id);
  if (!el || typeof Chart === "undefined") return;
  new Chart(el, {
    type: "bar",
    data: { labels, datasets },
    options: baseOpts(opts),
  });
}

export function renderRouteInfographics(panels) {
  destroyAll();
  const P = panels || {};

  // P1 Speed Dynamics
  const p1 = P.p1_speed || { labels: [], sog: [], design_speed: 19.5 };
  line("routeP1", p1.labels || [], [
    {
      label: "SOG kn",
      data: p1.sog || [],
      borderColor: CYAN,
      backgroundColor: "rgba(0,229,255,.12)",
      fill: true,
      tension: 0.25,
      pointRadius: 0,
    },
    {
      label: "Design",
      data: (p1.labels || []).map(() => p1.design_speed || 19.5),
      borderColor: GOLD,
      borderDash: [6, 4],
      pointRadius: 0,
      fill: false,
    },
  ]);

  // P2 Draught
  const p2 = P.p2_draught || { labels: [], draft_m: [] };
  line("routeP2", p2.labels || [], [
    {
      label: "Draft m",
      data: p2.draft_m || [],
      borderColor: "#f59e0b",
      backgroundColor: "rgba(245,158,11,.12)",
      fill: true,
      tension: 0.2,
      pointRadius: 0,
    },
  ]);

  // P3 DWT
  const p3 = P.p3_dwt || { labels: [], capacity: [], utilized: [] };
  bar("routeP3", p3.labels || [], [
    { label: "Capacity DWT", data: p3.capacity || [], backgroundColor: "rgba(212,175,55,.35)" },
    { label: "Utilized", data: p3.utilized || [], backgroundColor: CYAN },
  ]);

  // P4 Engine / Fuel
  const p4 = P.p4_engine_fuel || { labels: [], engine_load: [], fuel_efficiency: [] };
  bar("routeP4", p4.labels || [], [
    { label: "Engine Load", data: (p4.engine_load || []).map((x) => +(100 * x).toFixed(1)), backgroundColor: "#ef4444" },
    { label: "Fuel Eff %", data: (p4.fuel_efficiency || []).map((x) => +(100 * x).toFixed(1)), backgroundColor: "#22c55e" },
  ]);

  // P5 Dark gaps
  const p5 = P.p5_dark_gaps || { labels: [], counts: [] };
  bar("routeP5", p5.labels || [], [
    { label: "AIS Gaps", data: p5.counts || [], backgroundColor: "#a855f7" },
  ]);

  // P6 Radar
  const p6 = P.p6_radar || { labels: [], values: [] };
  const el6 = document.getElementById("routeP6");
  if (el6 && typeof Chart !== "undefined") {
    new Chart(el6, {
      type: "radar",
      data: {
        labels: p6.labels || [],
        datasets: [
          {
            label: "Anomaly Index",
            data: p6.values || [],
            borderColor: GOLD,
            backgroundColor: "rgba(212,175,55,.2)",
            pointBackgroundColor: CYAN,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          r: {
            min: 0,
            max: 100,
            ticks: { display: false },
            grid: { color: "rgba(120,180,220,.15)" },
            pointLabels: { color: MUTED, font: { size: 9 } },
          },
        },
        plugins: { legend: { display: false } },
      },
    });
  }

  // P7 ETA doughnut progress
  const p7 = P.p7_eta || { progress_pct: 0, label: "—" };
  const el7 = document.getElementById("routeP7");
  const badge = document.getElementById("routeEtaBadge");
  if (badge) badge.textContent = p7.label || `Progress ${p7.progress_pct}%`;
  if (el7 && typeof Chart !== "undefined") {
    const pct = Math.max(0, Math.min(100, Number(p7.progress_pct) || 0));
    new Chart(el7, {
      type: "doughnut",
      data: {
        labels: ["Progress", "Remaining"],
        datasets: [
          {
            data: [pct, 100 - pct],
            backgroundColor: [GOLD, "rgba(120,180,220,.15)"],
            borderWidth: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "72%",
        plugins: { legend: { display: false } },
      },
    });
  }
  const center = document.getElementById("routeEtaCenter");
  if (center) center.textContent = `${Math.round(p7.progress_pct || 0)}%`;

  // P8 Weather
  const p8 = P.p8_weather || { labels: [], wave_m: [], wind_kn: [], current_kn: [] };
  line("routeP8", p8.labels || [], [
    { label: "Wave m", data: p8.wave_m || [], borderColor: CYAN, pointRadius: 0, tension: 0.3 },
    { label: "Wind kn", data: p8.wind_kn || [], borderColor: GOLD, pointRadius: 0, tension: 0.3 },
    { label: "Current kn", data: p8.current_kn || [], borderColor: "#22c55e", pointRadius: 0, tension: 0.3 },
  ]);

  // P9 Fleet density
  const p9 = P.p9_fleet_density || { labels: [], chokepoint_density: [], sts_events: [], active_tankers: [] };
  line("routeP9", p9.labels || [], [
    {
      label: "Chokepoint Density",
      data: p9.chokepoint_density || [],
      borderColor: CYAN,
      yAxisID: "y",
      pointRadius: 0,
      tension: 0.25,
    },
    {
      label: "STS Events",
      data: p9.sts_events || [],
      borderColor: "#f59e0b",
      yAxisID: "y1",
      pointRadius: 0,
      tension: 0.25,
    },
  ], {
    scales: {
      x: { ticks: { color: MUTED, maxTicksLimit: 8 }, grid: { color: "rgba(120,180,220,.08)" } },
      y: { ticks: { color: MUTED }, grid: { color: "rgba(120,180,220,.08)" }, position: "left" },
      y1: { ticks: { color: MUTED }, grid: { drawOnChartArea: false }, position: "right" },
    },
  });
}

export function destroyRouteInfographics() {
  destroyAll();
}

export default { renderRouteInfographics, destroyRouteInfographics };
