/**
 * Oracle-1001 / Sentinel — HUD State Persistence (localStorage)
 * Persists: active sheet, HUD filters, map overlays, What-If scenario.
 * Auto-retry: soft rehydrate on DOMContentLoaded if storage is readable.
 */
(function () {
  "use strict";

  const KEY = "oracle1001.sentinel.hud.v1";
  const RETRY_MS = 400;
  const MAX_RETRIES = 5;

  const defaults = {
    sheet: null,
    filters: {
      tiers: ["ALPHA", "BRAVO", "CHARLIE", "DELTA"],
      showSpoofed: true,
      showDark: true,
      showSts: true,
    },
    mapOverlays: {
      heatmap: true,
      chokepoints: true,
      alphaTracks: true,
      stsClusters: true,
    },
    whatIf: {
      blockageDays: 0,
      tempAnomalyC: 0,
    },
    updatedAt: null,
  };

  function safeParse(raw) {
    try {
      const obj = JSON.parse(raw);
      return obj && typeof obj === "object" ? obj : null;
    } catch (_) {
      return null;
    }
  }

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (!raw) return { ...defaults };
      const parsed = safeParse(raw);
      if (!parsed) return { ...defaults };
      return {
        ...defaults,
        ...parsed,
        filters: { ...defaults.filters, ...(parsed.filters || {}) },
        mapOverlays: { ...defaults.mapOverlays, ...(parsed.mapOverlays || {}) },
        whatIf: { ...defaults.whatIf, ...(parsed.whatIf || {}) },
      };
    } catch (e) {
      console.warn("[HUD_STATE] load failed", e);
      return { ...defaults };
    }
  }

  function save(partial) {
    try {
      const next = {
        ...load(),
        ...partial,
        filters: { ...load().filters, ...((partial && partial.filters) || {}) },
        mapOverlays: { ...load().mapOverlays, ...((partial && partial.mapOverlays) || {}) },
        whatIf: { ...load().whatIf, ...((partial && partial.whatIf) || {}) },
        updatedAt: new Date().toISOString(),
      };
      // Avoid nested load races — rebuild cleanly
      const base = load();
      const merged = {
        sheet: partial && partial.sheet !== undefined ? partial.sheet : base.sheet,
        filters: { ...base.filters, ...((partial && partial.filters) || {}) },
        mapOverlays: { ...base.mapOverlays, ...((partial && partial.mapOverlays) || {}) },
        whatIf: { ...base.whatIf, ...((partial && partial.whatIf) || {}) },
        updatedAt: new Date().toISOString(),
      };
      localStorage.setItem(KEY, JSON.stringify(merged));
      return merged;
    } catch (e) {
      console.warn("[HUD_STATE] save failed", e);
      return null;
    }
  }

  function setSheet(sheet) {
    return save({ sheet: String(sheet || "ais") });
  }

  function setFilters(filters) {
    return save({ filters: filters || {} });
  }

  function setMapOverlays(overlays) {
    return save({ mapOverlays: overlays || {} });
  }

  function setWhatIf(whatIf) {
    return save({ whatIf: whatIf || {} });
  }

  function getWhatIf() {
    return load().whatIf;
  }

  function getFilters() {
    return load().filters;
  }

  function getMapOverlays() {
    return load().mapOverlays;
  }

  /** Restore sheet preference if URL has no ?sheet= */
  function restoreSheetPreference(attempt) {
    const n = attempt || 0;
    try {
      const url = new URL(window.location.href);
      if (url.searchParams.has("sheet")) {
        // URL wins — mirror into storage
        setSheet(url.searchParams.get("sheet"));
        return;
      }
      const state = load();
      if (state.sheet && state.sheet !== "ais") {
        url.searchParams.set("sheet", state.sheet);
        window.history.replaceState({}, "", url.pathname + url.search);
        document.documentElement.setAttribute("data-sheet", state.sheet);
        if (typeof window.switchTab === "function") {
          window.switchTab(state.sheet);
        } else if (window.__SENTINEL__ && typeof window.__SENTINEL__.switchSheet === "function") {
          window.__SENTINEL__.switchSheet(state.sheet, { force: true });
        }
      }
    } catch (e) {
      if (n < MAX_RETRIES) {
        setTimeout(() => restoreSheetPreference(n + 1), RETRY_MS);
      } else {
        console.warn("[HUD_STATE] restore retries exhausted", e);
      }
    }
  }

  // Persist sheet changes from tab clicks
  document.addEventListener("sentinelSheetChange", function (e) {
    const sheet = (e.detail || {}).sheet;
    if (sheet) setSheet(sheet);
  });

  document.addEventListener("DOMContentLoaded", function () {
    restoreSheetPreference(0);
  });

  window.__HUD_STATE__ = {
    key: KEY,
    load,
    save,
    setSheet,
    setFilters,
    setMapOverlays,
    setWhatIf,
    getWhatIf,
    getFilters,
    getMapOverlays,
    defaults,
  };
})();
