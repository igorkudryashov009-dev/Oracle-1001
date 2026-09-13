/**
 * Route Map View — Leaflet dark cyberpunk / gold track renderer
 */
import { speedColor } from "./route_analytics_engine.js";

const GOLD = "#d4af37";
const CYAN = "#00e5ff";

export function createRouteMap(containerId) {
  const el = document.getElementById(containerId);
  if (!el || typeof L === "undefined") return null;

  if (el._routeMap) {
    try {
      el._routeMap.remove();
    } catch (_) {
      /* ignore */
    }
    el._routeMap = null;
  }
  el.innerHTML = "";

  const map = L.map(el, {
    zoomControl: true,
    attributionControl: false,
    preferCanvas: true,
  }).setView([18, 55], 3);

  if (window.__SENTINEL_MAP_TILES__ && window.__SENTINEL_MAP_TILES__.addBasemap) {
    window.__SENTINEL_MAP_TILES__.addBasemap(map, { maxZoom: 12 });
  } else {
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 12,
      subdomains: "abc",
      attribution: "&copy; OpenStreetMap",
    }).addTo(map);
  }

  // Subtle gold grid overlay via pane tint
  el.style.background = "#0b0e14";

  const layers = {
    tracks: L.layerGroup().addTo(map),
    sts: L.layerGroup().addTo(map),
    heat: L.layerGroup().addTo(map),
    heads: L.layerGroup().addTo(map),
  };

  const api = {
    map,
    layers,
    showHeat: true,
    showSts: true,
    mode: "fleet", // fleet | single
    render(slice) {
      layers.tracks.clearLayers();
      layers.sts.clearLayers();
      layers.heat.clearLayers();
      layers.heads.clearLayers();

      const bounds = [];
      const vesselsByImo = Object.fromEntries((slice.vessels || []).map((v) => [String(v.imo), v]));

      Object.entries(slice.tracks || {}).forEach(([imo, track]) => {
        if (!track || track.length < 2) return;
        const v = vesselsByImo[imo] || { name: imo, imo };
        // Multi-segment color by SOG
        for (let i = 1; i < track.length; i++) {
          const a = track[i - 1];
          const b = track[i];
          const col = speedColor(b.sog);
          const seg = L.polyline(
            [
              [a.lat, a.lon],
              [b.lat, b.lon],
            ],
            { color: col, weight: slice.mode === "single" || Object.keys(slice.tracks).length === 1 ? 4 : 2.5, opacity: 0.85 }
          );
          seg.bindTooltip(
            `<b>${v.name}</b><br/>IMO ${imo}<br/>SOG ${b.sog} kn · Draft ${b.draft_m} m<br/>${b.t}`,
            { sticky: true }
          );
          layers.tracks.addLayer(seg);
          bounds.push([b.lat, b.lon]);
        }
        // Head + direction vector
        const last = track[track.length - 1];
        const prev = track[Math.max(0, track.length - 4)];
        const head = L.circleMarker([last.lat, last.lon], {
          radius: 7,
          color: GOLD,
          fillColor: CYAN,
          fillOpacity: 0.9,
          weight: 2,
        }).bindTooltip(
          `<b>${v.name}</b><br/>IMO ${imo}<br/>SOG ${last.sog} kn · COG ${last.cog ?? "—"}°`,
          { direction: "top" }
        );
        layers.heads.addLayer(head);
        // Direction tick
        const arrow = L.polyline(
          [
            [prev.lat, prev.lon],
            [last.lat, last.lon],
          ],
          { color: GOLD, weight: 2, dashArray: "2 6", opacity: 0.9 }
        );
        layers.heads.addLayer(arrow);
        // Draught change alert near last if delta large
        if (track.length > 5) {
          const d0 = Number(track[0].draft_m);
          const d1 = Number(last.draft_m);
          if (Math.abs(d1 - d0) > 1.5) {
            const alert = L.marker([last.lat, last.lon], {
              icon: L.divIcon({
                className: "route-draft-alert",
                html: `<span>ΔDRAFT ${d1 > d0 ? "+" : ""}${(d1 - d0).toFixed(1)}m</span>`,
                iconSize: [110, 18],
              }),
            });
            layers.heads.addLayer(alert);
          }
        }
      });

      if (api.showSts) {
        (slice.sts || []).forEach((z) => {
          const c = L.circle([z.lat, z.lon], {
            radius: 12000,
            color: "#f59e0b",
            fillColor: "#f59e0b",
            fillOpacity: 0.12,
            weight: 1,
            dashArray: "4 4",
          }).bindTooltip(`STS / Anchorage proxy · IMO ${z.imo}<br/>${z.t}`);
          layers.sts.addLayer(c);
        });
      }

      if (api.showHeat) {
        (slice.heatmap || []).forEach((h) => {
          const r = 18000 + 40000 * (h.weight || 0.3);
          const c = L.circle([h.lat, h.lon], {
            radius: r,
            color: "#7f1d1d",
            fillColor: "#ef4444",
            fillOpacity: 0.08 + 0.2 * (h.weight || 0),
            weight: 0,
          });
          layers.heat.addLayer(c);
        });
      }

      if (bounds.length) {
        try {
          map.fitBounds(bounds, { padding: [28, 28], maxZoom: 7 });
        } catch (_) {
          /* ignore */
        }
      }
      setTimeout(() => map.invalidateSize(), 60);
    },
    setOverlays({ heat, sts } = {}) {
      if (typeof heat === "boolean") api.showHeat = heat;
      if (typeof sts === "boolean") api.showSts = sts;
    },
    destroy() {
      try {
        map.remove();
      } catch (_) {
        /* ignore */
      }
      el._routeMap = null;
    },
  };

  el._routeMap = map;
  return api;
}

export default { createRouteMap };
