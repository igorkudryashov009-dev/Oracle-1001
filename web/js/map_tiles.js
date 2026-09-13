/**
 * Sentinel basemap helper — UTF-8 safe, no Mapbox key required.
 * Override via window.__SENTINEL_MAP__ = { tileUrl, maxZoom, subdomains, attribution }
 * Default: OpenStreetMap (avoids "API KEY REQUIRED" Mapbox tiles).
 */
(function (w) {
  "use strict";
  var DEFAULT = {
    tileUrl: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    maxZoom: 12,
    subdomains: "abc",
    attribution: "&copy; OpenStreetMap",
  };

  function cfg() {
    var c = w.__SENTINEL_MAP__ || {};
    return {
      tileUrl: c.tileUrl || DEFAULT.tileUrl,
      maxZoom: c.maxZoom != null ? c.maxZoom : DEFAULT.maxZoom,
      subdomains: c.subdomains || DEFAULT.subdomains,
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
    }
    return L.tileLayer(c.tileUrl, {
      maxZoom: c.maxZoom,
      subdomains: c.subdomains,
      attribution: c.attribution,
    }).addTo(map);
  }

  w.__SENTINEL_MAP_TILES__ = { cfg: cfg, addBasemap: addBasemap, DEFAULT: DEFAULT };
})(window);
