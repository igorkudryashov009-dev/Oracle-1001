/**
 * ROUTE sheet orchestration — controls, map, KPI strip, 9-panel grid
 */
import { getRoutePayload, resolveSlice, listVessels, listGroups, exportSliceCsv } from "./route_analytics_engine.js";
import { createRouteMap } from "./route_map_view.js";
import { renderRouteInfographics, destroyRouteInfographics } from "./route_infographics.js";

let mapApi = null;
let state = { horizon: "7d", selection: "ALL", heat: true, sts: true };
let booted = false;
let wired = false;

function fmt(n, d = 1) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return Number(n).toLocaleString("ru-RU", { maximumFractionDigits: d });
}

function fillSelectors(payload) {
  const sel = document.getElementById("routeVesselSelect");
  if (!sel) return;
  const vessels = listVessels(payload);
  const groups = listGroups(payload);
  const opts = [`<option value="ALL">ALL FLEET CLUSTER</option>`];
  groups.filter((g) => g !== "ALL").forEach((g) => {
    opts.push(`<option value="${g}">GROUP · ${g}</option>`);
  });
  vessels.forEach((v) => {
    opts.push(`<option value="${v.imo}">IMO ${v.imo} · ${v.name}</option>`);
  });
  sel.innerHTML = opts.join("");
  sel.value = state.selection;
}

function renderKpiStrip(fleetKpi) {
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  };
  set("routeKpiSog", `${fmt(fleetKpi.current_sog, 1)} kn`);
  set("routeKpiDwt", fmt(fleetKpi.total_dwt, 0));
  set("routeKpiEff", `${fmt(fleetKpi.voyage_efficiency, 1)}`);
  set("routeKpiAnom", fmt(fleetKpi.anomaly_flags, 0));
}

function apply() {
  const payload = getRoutePayload();
  if (!payload) {
    const banner = document.getElementById("routeSreBanner");
    if (banner) {
      banner.classList.add("on");
      banner.textContent = "[SRE:ROUTE_PAYLOAD_NULL] route_analytics missing from dashboard payload";
    }
    return;
  }
  const slice = resolveSlice(payload, state);
  renderKpiStrip(slice.fleetKpi || {});
  if (!mapApi) mapApi = createRouteMap("routeMap");
  if (mapApi) {
    mapApi.setOverlays({ heat: state.heat, sts: state.sts });
    mapApi.render(slice);
  }
  renderRouteInfographics(slice.panels || {});
  const meta = document.getElementById("routeMeta");
  if (meta) {
    meta.textContent = `${slice.source_mode || "—"} · horizon ${state.horizon} · vessels ${slice.vessels.length} · tracks ${Object.keys(slice.tracks).length}`;
  }
}

function wireControls() {
  if (wired) return;
  wired = true;
  document.querySelectorAll("[data-route-hz]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.horizon = btn.getAttribute("data-route-hz") || "7d";
      document.querySelectorAll("[data-route-hz]").forEach((b) => b.classList.toggle("active", b === btn));
      apply();
    });
  });
  const sel = document.getElementById("routeVesselSelect");
  if (sel) {
    sel.addEventListener("change", () => {
      state.selection = sel.value || "ALL";
      apply();
    });
  }
  const heat = document.getElementById("routeToggleHeat");
  if (heat) {
    heat.addEventListener("change", () => {
      state.heat = !!heat.checked;
      apply();
    });
  }
  const sts = document.getElementById("routeToggleSts");
  if (sts) {
    sts.addEventListener("change", () => {
      state.sts = !!sts.checked;
      apply();
    });
  }
  const scrub = document.getElementById("routeTimeScrub");
  if (scrub) {
    scrub.addEventListener("input", () => {
      // Map scrubber → horizon buckets
      const v = Number(scrub.value) || 50;
      state.horizon = v < 33 ? "1d" : v < 66 ? "7d" : "30d";
      document.querySelectorAll("[data-route-hz]").forEach((b) => {
        b.classList.toggle("active", b.getAttribute("data-route-hz") === state.horizon);
      });
      apply();
    });
  }
  const exp = document.getElementById("routeExportBtn");
  if (exp) {
    exp.addEventListener("click", () => {
      const slice = resolveSlice(getRoutePayload(), state);
      const csv = exportSliceCsv(slice);
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `route_analytics_${state.horizon}_${state.selection}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }
  const expJson = document.getElementById("routeExportJsonBtn");
  if (expJson) {
    expJson.addEventListener("click", () => {
      const slice = resolveSlice(getRoutePayload(), state);
      const blob = new Blob([JSON.stringify(slice, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `route_analytics_${state.horizon}_${state.selection}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }
}

export function bootRoute({ force = false } = {}) {
  const root = document.getElementById("sheet-route");
  if (!root) return;
  const payload = getRoutePayload();
  if (!booted || force) {
    state.horizon = (payload && payload.default_horizon) || "7d";
    state.selection = (payload && payload.default_selection) || "ALL";
    fillSelectors(payload);
    wireControls();
    document.querySelectorAll("[data-route-hz]").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-route-hz") === state.horizon);
    });
    booted = true;
  }
  apply();
  setTimeout(() => {
    if (mapApi && mapApi.map) mapApi.map.invalidateSize();
    window.dispatchEvent(new Event("resize"));
  }, 80);
}

export function pauseRoute() {
  /* map stays; charts remain — no WebGL loop */
}

export function destroyRoute() {
  destroyRouteInfographics();
  if (mapApi) {
    mapApi.destroy();
    mapApi = null;
  }
  booted = false;
}

window.__ROUTE__ = { boot: bootRoute, pause: pauseRoute, destroy: destroyRoute, apply, getState: () => ({ ...state }) };

export default { bootRoute, pauseRoute };
