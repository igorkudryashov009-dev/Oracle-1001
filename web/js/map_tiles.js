/**
 * Sentinel basemap helper — UTF-8 safe.
 *
 * Default: same-origin tile proxy `/api/tiles/{provider}/{z}/{x}/{y}.png`
 *   → server injects paid provider key (never in browser URL / Network tab)
 *   → on miss/error/no-key the proxy serves Esri Dark Gray (Never-Black maps)
 *
 * Override via window.__SENTINEL_MAP__ = { tileUrl, maxZoom, subdomains, attribution }
 * or TILE_SERVER at dashboard build time.
 *
 * Direct Esri URL remains available as ESRI_FALLBACK for hard client-side last resort
 * (proxy down entirely). Do NOT put Mapbox/CARTO tokens in this file.
 */
(function (w) {
  "use strict";

  var ESRI_FALLBACK =
    "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}";

  var DEFAULT = {
    tileUrl: "/api/tiles/mapbox/{z}/{x}/{y}.png",
    maxZoom: 16,
    subdomains: "",
    attribution: "Tiles via Sentinel proxy · Esri fallback",
  };

  function cfg() {
    var c = w.__SENTINEL_MAP__ || {};
    return {
      tileUrl: c.tileUrl || DEFAULT.tileUrl,
      maxZoom: c.maxZoom != null ? c.maxZoom : DEFAULT.maxZoom,
      subdomains: c.subdomains != null ? c.subdomains : DEFAULT.subdomains,
      attribution: c.attribution != null ? c.attribution : DEFAULT.attribution,
    };
  }

  /** Add Leaflet tile layer; returns the layer. */
  function addBasemap(map, overrides) {
    if (!map || typeof L === "undefined") return null;
    var c = cfg();
    if (overrides) {
      if (overrides.tileUrl) c.tileUrl = overrides.tileUrl;
      if (overrides.maxZoom != null) c.maxZoom = overrides.maxZoom;
      if (overrides.subdomains != null) c.subdomains = overrides.subdomains;
    }
    var opts = {
      maxZoom: c.maxZoom,
      attribution: c.attribution,
      crossOrigin: true,
      // Hard last-resort if proxy process is completely unreachable
      errorTileUrl: "",
    };
    if (c.subdomains) opts.subdomains = c.subdomains;
    var layer = L.tileLayer(c.tileUrl, opts);
    var esriArmed = false;
    layer.on("tileerror", function () {
      if (esriArmed) return;
      if (String(c.tileUrl).indexOf("/api/tiles/") !== 0) return;
      esriArmed = true;
      try {
        L.tileLayer(ESRI_FALLBACK, {
          maxZoom: c.maxZoom,
          attribution: "Tiles &copy; Esri (client fallback)",
          crossOrigin: true,
        }).addTo(map);
      } catch (_) { /* ignore */ }
    });
    return layer.addTo(map);
  }

  w.__SENTINEL_MAP_TILES__ = {
    cfg: cfg,
    addBasemap: addBasemap,
    DEFAULT: DEFAULT,
    ESRI_FALLBACK: ESRI_FALLBACK,
  };
})(window);
