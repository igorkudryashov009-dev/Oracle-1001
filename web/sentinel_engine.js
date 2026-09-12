/* ORACLE-1001 / Sentinel — 18-chart engine + TTF Forecast sheet */
(function () {
  "use strict";

  const P = window.__SENTINEL_PAYLOAD__ || {};
  const COLORS = P.tier_colors || { ALPHA: "#ef4444", BRAVO: "#f59e0b", CHARLIE: "#3b82f6", DELTA: "#10b981" };
  const charts = [];
  const TTF_CANVAS_IDS = [
    "ttfCone", "ttfKde", "ttfGranger", "ttfImportance", "ttfAllocDonut", "ttfEquityCurves",
  ];

  Chart.defaults.color = "#8aa4bf";
  Chart.defaults.borderColor = "rgba(120,180,220,.15)";
  Chart.defaults.font.family = "Manrope, system-ui, sans-serif";

  function fmt(n) {
    if (n == null || isNaN(n)) return "—";
    return Number(n).toLocaleString("ru-RU");
  }

  function parseSheetFromUrl() {
    try {
      const q = new URLSearchParams(window.location.search || "");
      const sheet = String(q.get("sheet") || "").toLowerCase().trim();
      const hash = String(window.location.hash || "").replace(/^#/, "").toLowerCase().trim();
      if (sheet === "ttf"     || hash === "ttf"     || hash === "tab-ttf-forecast") return "ttf";
      if (sheet === "top10" || sheet === "qflex" || sheet === "q-flex" || hash === "top10" || hash === "qflex" || hash === "q-flex" || hash === "sheet-top10") return "top10";
      if (sheet === "route"   || hash === "route"   || hash === "sheet-route")     return "route";
      if (sheet === "balance" || hash === "balance" || hash === "sheet-balance")   return "balance";
      if (sheet === "archive" || hash === "archive" || hash === "sheet-archive")   return "archive";
      if (sheet === "ais"     || hash === "ais"     || hash === "sheet-ais")       return "ais";
    } catch (_) { /* ignore */ }
    return "ais";
  }

  function destroyChartsByIds(ids) {
    (ids || []).forEach((id) => {
      const el = document.getElementById(id);
      if (!el || typeof Chart === "undefined" || !Chart.getChart) return;
      const existing = Chart.getChart(el);
      if (existing) {
        try { existing.destroy(); } catch (_) { /* ignore */ }
      }
    });
  }

  function forceChartRelayout(root) {
    // Flush layout after display:none → block (Chart.js blank-canvas fix)
    window.dispatchEvent(new Event("resize"));
    const scope = root || document;
    if (typeof Chart !== "undefined" && Chart.getChart) {
      scope.querySelectorAll("canvas").forEach((cv) => {
        const ch = Chart.getChart(cv);
        if (ch) {
          try { ch.resize(); } catch (_) { /* ignore */ }
        }
      });
    }
    if (window.Plotly && window.Plotly.Plots) {
      scope.querySelectorAll(".plotly-graph-div").forEach((p) => {
        try { window.Plotly.Plots.resize(p); } catch (_) { /* ignore */ }
      });
    }
  }

  function afterLayout(fn) {
    requestAnimationFrame(() => {
      requestAnimationFrame(fn);
    });
  }

  function renderKPI() {
    const box = document.getElementById("kpiRow");
    if (!box) return;
    const fs = P.fleet_summary || {};
    const tiers = P.tier_live_counts || {};
    const fr = P.replica_freshness || {};
    const ops = P.operational_status || "NOMINAL";
    box.innerHTML = `
      <div class="kpi"><div class="k">Registry Targets</div><div class="v">${fmt(fs.vessel_count)}</div><div class="s">Alpha–Delta strategic list</div></div>
      <div class="kpi"><div class="k">Live Positions</div><div class="v">${fmt(P.live_vessel_count)}</div><div class="s">mode: ${P.source_mode || "—"}</div></div>
      <div class="kpi"><div class="k">Alpha Online</div><div class="v">${fmt(tiers.ALPHA || 0)}</div><div class="s">B ${tiers.BRAVO || 0} · C ${tiers.CHARLIE || 0} · D ${tiers.DELTA || 0}</div></div>
      <div class="kpi"><div class="k">Ingestion MPS</div><div class="v">${fmt((P.c15_mps || {}).current || 0)}</div><div class="s">latency ${(P.c16_latency || {}).current_ms || 0} ms</div></div>
      <div class="kpi"><div class="k">Replica / OPS</div><div class="v">${fr.status || ops}</div><div class="s">lag ${fr.lag_minutes != null ? fr.lag_minutes + "m" : "—"} · ${ops}</div></div>`;
    const heroSub = document.getElementById("heroSub");
    if (heroSub) {
      heroSub.textContent =
        `Generated ${P.generated_at_utc || "—"} · Fleet DWT ${fmt(fs.total_dwt)} t · ${P.source_mode} · ${ops}`;
    }
    const foot = document.getElementById("footMeta");
    if (foot) {
      foot.textContent =
        `STS: ${(P.c03_sts_clusters || []).length} · Dark: ${(P.c06_dark_timeline || []).length} · replica ${fr.status || "—"}`;
    }

    const banner = document.getElementById("staleBanner");
    const opsEl = document.getElementById("opsStatus");
    if (banner && fr.stale && fr.banner) {
      banner.textContent = fr.banner;
      banner.classList.add("on");
    }
    if (opsEl) {
      const pipe = P.pipeline_health_status || (fr.stale ? "DEGRADED" : "NOMINAL");
      opsEl.textContent = fr.stale
        ? `OPS DEGRADED · LAG ${fr.lag_minutes}m`
        : `OPS ${pipe} · PIPELINE`;
      opsEl.classList.add(fr.stale ? "stale" : "fresh");
    }

    // Persistent fleet-sample banner (all sheets) — never requires a click
    const fsBanner = document.getElementById("fleetSampleBanner");
    if (fsBanner) {
      const fs = String(P.fleet_sample_status || "").toUpperCase();
      const n = P.top500_live_coverage != null ? P.top500_live_coverage : "—";
      if (fs && fs !== "FULL") {
        fsBanner.innerHTML =
          `<strong>Fleet sample: ${fs}</strong> (N=${n} of 500, terrestrial AIS coverage) — ` +
          `quant signals reduced confidence` +
          (P.sample_size_caveat ? ` · ${P.sample_size_caveat}` : "");
        fsBanner.classList.add("on");
      } else {
        fsBanner.classList.remove("on");
        fsBanner.textContent = "";
      }
    }
  }

  function makeMap(elId, points, opts) {
    const el = document.getElementById(elId);
    if (!el || typeof L === "undefined") return null;
    const map = L.map(el, { zoomControl: true, attributionControl: false }).setView([20, 40], 2);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      maxZoom: 10,
    }).addTo(map);
    (points || []).forEach((p) => {
      const color = p.color || COLORS[p.tier] || "#10b981";
      const r = opts && opts.alphaOnly ? 5 : (p.tier === "ALPHA" ? 5 : 3);
      L.circleMarker([p.lat, p.lon], {
        radius: r,
        color,
        fillColor: color,
        fillOpacity: 0.75,
        weight: 1,
      }).bindTooltip(`${p.name || p.imo || ""} · ${p.tier || ""} · SOG ${p.sog ?? "—"}`, { direction: "top" }).addTo(map);
    });
    return map;
  }

  function barChart(canvasId, labels, datasets, stacked) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    destroyChartsByIds([canvasId]);
    charts.push(new Chart(ctx, {
      type: "bar",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { stacked: !!stacked },
          y: { stacked: !!stacked, beginAtZero: true },
        },
        plugins: { legend: { display: datasets.length > 1 } },
      },
    }));
  }

  function lineChart(canvasId, labels, datasets) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    destroyChartsByIds([canvasId]);
    charts.push(new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        elements: { point: { radius: 0 }, line: { tension: 0.35 } },
        plugins: { legend: { display: datasets.length > 1 } },
      },
    }));
  }

  function doughnut(canvasId, labels, data, colors, opts) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    destroyChartsByIds([canvasId]);
    const o = opts || {};
    charts.push(new Chart(ctx, {
      type: "doughnut",
      data: {
        labels,
        datasets: [{ data, backgroundColor: colors, borderWidth: 0 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: o.cutout || "62%",
        plugins: {
          legend: { position: o.legend || "bottom", labels: { boxWidth: 10, font: { size: 10 } } },
          tooltip: {
            callbacks: {
              label: (item) => {
                const v = Number(item.raw) || 0;
                return ` ${item.label}: ${v.toFixed(1)}%`;
              },
            },
          },
        },
      },
    }));
  }

  function radar(canvasId, labels, datasets) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    charts.push(new Chart(ctx, {
      type: "radar",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: { r: { min: 0, max: 100, ticks: { display: false } } },
      },
    }));
  }

  function scatter(canvasId, points) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    const byTier = {};
    points.forEach((p) => {
      const t = p.tier || "DELTA";
      if (!byTier[t]) byTier[t] = [];
      byTier[t].push({ x: p.draft_m, y: p.dwt / 1000 });
    });
    charts.push(new Chart(ctx, {
      type: "scatter",
      data: {
        datasets: Object.keys(byTier).map((t) => ({
          label: t,
          data: byTier[t],
          backgroundColor: COLORS[t] || "#10b981",
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { title: { display: true, text: "Draft m" } },
          y: { title: { display: true, text: "DWT (×1000 t)" } },
        },
      },
    }));
  }

  function renderLists() {
    const sts = document.getElementById("stsList");
    if (sts) {
      const rows = (P.c03_sts_clusters || []).slice(0, 12);
      sts.innerHTML = rows.length
        ? rows.map((c) => {
            const imos = (c.imos || []).filter(Boolean).join("/");
            const tiers = (c.tiers || []).filter(Boolean).join("/");
            const dur = c.duration_min != null ? `${c.duration_min}m` : "";
            const dist = c.min_dist_nm != null ? `${Number(c.min_dist_nm).toFixed(2)}nm` : "";
            const names = (c.vessels || []).slice(0, 2).join(" · ");
            return `<div class="row"><span>${names || "pair"} <small>${imos}</small></span><span>${tiers} · ${dist} · ${dur}<br/>${Number(c.lat).toFixed(2)}, ${Number(c.lon).toFixed(2)}</span></div>`;
          }).join("")
        : `<div class="row"><span>No STS clusters (&lt;0.5nm, SOG&lt;1, &gt;30m) in window</span></div>`;
    }
    const dark = document.getElementById("darkList");
    if (dark) {
      const rows = (P.c06_dark_timeline || []).slice(0, 14);
      dark.innerHTML = rows.length
        ? rows.map((e) => `<div class="row"><span>${e.name || e.imo} <small>${e.imo || ""}</small></span><span>${e.gap_hours}h · ${e.tier || ""} · ${e.zone || ""}</span></div>`).join("")
        : `<div class="row"><span>No dark-AIS gaps &gt; 4h near critical zones</span></div>`;
    }
    const ws = document.getElementById("wsHealth");
    const h = P.c17_ws_health || {};
    if (ws) {
      ws.innerHTML = `
        <div class="badge"><span class="dot ${h.heartbeat_ok ? "on" : "off"}"></span>${h.status || "UNKNOWN"}</div>
        <div class="meta">Uptime: ${fmt(h.uptime_sec)} s<br/>Reconnects: ${fmt(h.reconnects)}<br/>Heartbeat: ${h.heartbeat_ok ? "OK" : "STALE"}</div>`;
    }
  }

  let ttfBooted = false;

  function switchSheet(name, opts) {
    const force = !!(opts && opts.force);
    let sheet = "ais";
    if (name === "ttf") sheet = "ttf";
    else if (name === "top10" || name === "qflex" || name === "q-flex") sheet = "top10";
    else if (name === "route") sheet = "route";
    else if (name === "balance") sheet = "balance";
    else if (name === "archive") sheet = "archive";

    document.documentElement.dataset.sheet = sheet;

    document.querySelectorAll(".sheet-tab").forEach((t) => {
      const ds = t.getAttribute("data-sheet");
      const on =
        ds === sheet ||
        (sheet === "top10" && (ds === "qflex" || ds === "q-flex" || ds === "top10"));
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });

    const ais = document.getElementById("sheet-ais");
    const ttf = document.getElementById("tab-ttf-forecast");
    const top10 = document.getElementById("sheet-top10");
    const route = document.getElementById("sheet-route");
    const balance = document.getElementById("sheet-balance");
    const archive = document.getElementById("sheet-archive");
    const kpi = document.getElementById("kpiRow");

    if (ais) {
      ais.classList.toggle("active", sheet === "ais");
      ais.style.display = sheet === "ais" ? "block" : "none";
    }
    if (ttf) {
      ttf.classList.toggle("active", sheet === "ttf");
      ttf.style.display = sheet === "ttf" ? "block" : "none";
    }
    if (top10) {
      top10.classList.toggle("active", sheet === "top10");
      top10.style.display = sheet === "top10" ? "block" : "none";
    }
    if (route) {
      route.classList.toggle("active", sheet === "route");
      route.style.display = sheet === "route" ? "block" : "none";
    }
    if (balance) {
      balance.classList.toggle("active", sheet === "balance");
      balance.style.display = sheet === "balance" ? "block" : "none";
    }
    if (archive) {
      archive.classList.toggle("active", sheet === "archive");
      archive.style.display = sheet === "archive" ? "block" : "none";
    }
    if (kpi) kpi.style.display = sheet === "ais" ? "" : "none";

    const title = document.getElementById("heroTitle");
    const sub = document.getElementById("heroSub");

    if (window.__TOP10__ && typeof window.__TOP10__.pause === "function" && sheet !== "top10") {
      try { window.__TOP10__.pause(); } catch (_) { /* ignore */ }
    }

    try {
      if (window.__HUD_STATE__ && typeof window.__HUD_STATE__.setSheet === "function") {
        window.__HUD_STATE__.setSheet(sheet);
      }
    } catch (_) { /* ignore */ }

    if (sheet === "ttf") {
      if (title) title.textContent = "ПРОГНОЗ TTF · MARKET FORECAST ENSEMBLE";
      if (sub) {
        const T = P.ttf_forecast || {};
        const spot = T.spot_eur_mwh != null ? Number(T.spot_eur_mwh).toFixed(2) : "—";
        sub.textContent = `Spot ${spot} €/MWh · Meta-ensemble CatBoost/Markov/Spectral/Elliott · Orbitron HUD`;
      }
      afterLayout(() => {
        try {
          if (!ttfBooted || force) {
            destroyChartsByIds(TTF_CANVAS_IDS);
            renderTTF();
            ttfBooted = true;
          }
          forceChartRelayout(ttf);
        } catch (err) {
          console.error("TTF render failed", err);
        }
      });
    } else if (sheet === "top10") {
      if (title) title.textContent = "ТОП 10 LNG ФЛАГМАНОВ · PHOTOGRAMMETRIC 3D";
      if (sub) {
        sub.textContent = "Q-Max / Membrane · orthographic triplets · WebGL ACESFilmic · ACTIVE OSINT TRACK";
      }
      afterLayout(() => {
        const boot = () => {
          if (window.__TOP10__ && typeof window.__TOP10__.boot === "function") {
            window.__TOP10__.boot({ force: !!force });
          } else {
            setTimeout(boot, 40);
          }
        };
        boot();
      });
    } else if (sheet === "route") {
      if (title) title.textContent = "МАРШРУТ · ROUTE ANALYTICS · SPATIOTEMPORAL";
      if (sub) {
        const R = P.route_analytics || {};
        sub.textContent = `${R.source_mode || "route"} · 1D/7D/30D kinematics · DWT · anomaly KPIs · Orbitron HUD`;
      }
      afterLayout(() => {
        const boot = () => {
          if (window.__ROUTE__ && typeof window.__ROUTE__.boot === "function") {
            window.__ROUTE__.boot({ force: !!force });
          } else {
            setTimeout(boot, 40);
          }
        };
        boot();
      });
    } else if (sheet === "balance") {
      if (title) title.textContent = "БАЛАНС · TOP-500 FLEET BALANCE · 6 QUANT METRICS";
      if (sub) {
        const B = P.balance || {};
        const spoofN = B.spoofed_excluded || 0;
        sub.textContent = `Clean fleet ${B.fleet_size || "—"} · spoofed excluded ${spoofN} · What-If LSSI · Apple×NASA HUD`;
      }
      afterLayout(() => {
        document.dispatchEvent(new CustomEvent("sentinelSheetChange", { detail: { sheet: "balance" } }));
        if (window.__BALANCE__ && typeof window.__BALANCE__.boot === "function") {
          window.__BALANCE__.boot();
        }
      });
    } else if (sheet === "archive") {
      if (title) title.textContent = "ARCHIVE · VESSEL DAILY SNAPSHOTS · FULL FLEET";
      if (sub) {
        sub.textContent = "Immutable UTC freeze · 20-parameter registry + AIS overlay · CSV/JSON export";
      }
      afterLayout(() => {
        document.dispatchEvent(new CustomEvent("sentinelSheetChange", { detail: { sheet: "archive" } }));
        if (window.__ARCHIVE__ && typeof window.__ARCHIVE__.boot === "function") {
          window.__ARCHIVE__.boot();
        }
      });
    } else {
      if (title) title.textContent = "SENTINEL LIVE AIS · 18 INFOGRAPHICS";
      if (sub) {
        sub.textContent = "Strategic fleet Alpha–Delta · AISStream ingestion · Real-time kinetics & pipeline health";
      }
      renderKPI();
      afterLayout(() => forceChartRelayout(ais));
    }
  }

  /** Alias for desk / external callers (Plotly-style API from brief). */
  function switchTab(tabId) {
    if (tabId === "tab-ttf-forecast" || tabId === "ttf")   return switchSheet("ttf",     { force: true });
    if (tabId === "sheet-top10" || tabId === "top10" || tabId === "qflex" || tabId === "q-flex") return switchSheet("top10", { force: true });
    if (tabId === "sheet-route"      || tabId === "route") return switchSheet("route",   { force: true });
    if (tabId === "sheet-balance"    || tabId === "balance") return switchSheet("balance", { force: true });
    if (tabId === "sheet-archive"    || tabId === "archive") return switchSheet("archive", { force: true });
    return switchSheet("ais");
  }

  function wireTabs() {
    document.querySelectorAll(".sheet-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.getAttribute("data-sheet") || "ais";
        const needsForce =
          target === "ttf" ||
          target === "top10" ||
          target === "qflex" ||
          target === "q-flex" ||
          target === "route" ||
          target === "balance" ||
          target === "archive";
        switchSheet(target, { force: needsForce });
        // Fire custom event so balance_engine.js can boot lazily
        document.dispatchEvent(new CustomEvent("sentinelSheetChange", { detail: { sheet: target === "qflex" || target === "q-flex" ? "top10" : target } }));
        try {
          const url = new URL(window.location.href);
          const urlSheet = target === "top10" ? "qflex" : target;
          if (urlSheet === "ais") url.searchParams.delete("sheet");
          else url.searchParams.set("sheet", urlSheet);
          window.history.replaceState({}, "", url.pathname + url.search);
        } catch (_) { /* ignore */ }
      });
    });
  }

  function heatColor(v) {
    const x = Math.max(0, Math.min(1, Number(v) || 0));
    const r = Math.round(8 + 40 * (1 - x));
    const g = Math.round(20 + 180 * x);
    const b = Math.round(40 + 200 * x);
    return `rgba(${r},${g},${b},${0.25 + 0.55 * x})`;
  }

  function showTtfSreBanner(msg) {
    const el = document.getElementById("ttfSreBanner");
    if (!el) return;
    el.textContent = msg;
    el.classList.add("on");
  }

  function clearTtfSreBanner() {
    const el = document.getElementById("ttfSreBanner");
    if (!el) return;
    el.textContent = "";
    el.classList.remove("on");
  }

  function skel(msg) {
    return `<div class="ttf-skel">${msg || "DATA UNAVAILABLE"}</div>`;
  }

  function safePanel(name, fn) {
    try {
      fn();
      return true;
    } catch (err) {
      console.error(`TTF panel ${name} failed`, err);
      return false;
    }
  }

  function getTtfData() {
    const fromGlobal = window.SENTINEL_TTF_DATA;
    const fromPayload = (P && P.ttf_forecast) || null;
    const T = fromGlobal && typeof fromGlobal === "object" && !fromGlobal.error
      ? fromGlobal
      : fromPayload;
    return T;
  }

  function renderTTF() {
    const T = getTtfData();
    const stale = !!(P.replica_freshness && P.replica_freshness.stale) || !!(T && T.replica_stale);
    const nullPayload = !T || T.error || T.integrity_status === "FAIL" || T.SENTINEL_TTF_DATA === false;

    if (nullPayload) {
      showTtfSreBanner(
        "[ CRITICAL DATA INTEGRITY ERROR: TTF ENSEMBLE PAYLOAD NULL / REPLICA STALE ]"
      );
      const box = document.getElementById("ttfKpiRow");
      if (box) box.innerHTML = skel("TTF KPI UNAVAILABLE · INTEGRITY GATE");
      ["ttfCone", "ttfKde", "ttfGranger", "ttfImportance", "ttfAllocDonut", "ttfEquityCurves"].forEach((id) => {
        const cv = document.getElementById(id);
        if (cv && cv.parentElement) cv.parentElement.innerHTML = skel(`PANEL ${id} · NO DATA`);
      });
      const opt = document.getElementById("ttfOptBadge");
      if (opt) {
        opt.textContent = "OPT RANGE — INTEGRITY ERROR";
        opt.classList.remove("ok");
        opt.classList.add("err");
      }
      const eb = document.getElementById("ttfElliottBadge");
      if (eb) {
        eb.textContent = "WAVE — INTEGRITY ERROR";
        eb.classList.remove("ok");
        eb.classList.add("err");
      }
      const matrix = document.getElementById("ttfHedgeMatrix");
      if (matrix) matrix.innerHTML = skel("HEDGING STRATEGIES UNAVAILABLE");
      return;
    }

    if (stale) {
      showTtfSreBanner(
        "[ CRITICAL DATA INTEGRITY ERROR: TTF ENSEMBLE PAYLOAD NULL / REPLICA STALE ]"
      );
    } else {
      clearTtfSreBanner();
    }

    // Sync alias for downstream
    window.SENTINEL_TTF_DATA = T;

    const kpi = T.kpi || {};
    safePanel("KPI", () => {
      const box = document.getElementById("ttfKpiRow");
      if (!box) return;
      const p = kpi.ais_causal_p;
      const pTxt = p != null ? Number(p).toExponential(2) : "—";
      box.innerHTML = `
        <div class="kpi ttf-glass"><div class="k">7-Day Forecast</div><div class="v">${fmt(kpi.h7_p50)} €</div><div class="s">P10–P90 ${fmt((kpi.h7_band || {}).p10)}–${fmt((kpi.h7_band || {}).p90)}</div></div>
        <div class="kpi ttf-glass"><div class="k">14-Day Forecast</div><div class="v">${fmt(kpi.h14_p50)} €</div><div class="s">ensemble P50</div></div>
        <div class="kpi ttf-glass"><div class="k">30-Day Forecast</div><div class="v">${fmt(kpi.h30_p50)} €</div><div class="s">ensemble P50</div></div>
        <div class="kpi ttf-glass"><div class="k">Market State Prob</div><div class="v" style="font-size:13px">${kpi.market_state || "—"}</div><div class="s">conf ${fmt(kpi.max_confidence_pct)}% · ${(kpi.optimal_range || "—")}</div></div>
        <div class="kpi ttf-glass"><div class="k">Portfolio ROI $1k</div><div class="v">${kpi.portfolio_roi_pct != null ? ("+" + fmt(kpi.portfolio_roi_pct) + "%") : "—"}</div><div class="s">→ $${kpi.portfolio_terminal_usd != null ? fmt(kpi.portfolio_terminal_usd) : "—"} · Strat B</div></div>
        <div class="kpi ttf-glass"><div class="k">AIS Causal Index</div><div class="v">${fmt(kpi.ais_causal_index)}</div><div class="s">best lag ${fmt(kpi.ais_causal_best_lag)} · p=${pTxt}</div></div>`;
    });

    safePanel("B-Cone", () => {
      const cone = T.cone || { labels: [], history: [], p10: [], p50: [], p90: [] };
      const coneCtx = document.getElementById("ttfCone");
      if (!coneCtx) return;
      if (!(cone.labels || []).length) {
        coneCtx.parentElement.innerHTML = skel("FORECAST CONE · EMPTY SERIES");
        return;
      }
      destroyChartsByIds(["ttfCone"]);
      charts.push(new Chart(coneCtx, {
        type: "line",
        data: {
          labels: cone.labels || [],
          datasets: [
            { label: "History", data: cone.history || [], borderColor: "#8aa4bf", backgroundColor: "transparent", borderWidth: 1.5, pointRadius: 0, spanGaps: false },
            { label: "P90", data: cone.p90 || [], borderColor: "rgba(239,68,68,.45)", backgroundColor: "rgba(239,68,68,.12)", fill: "+1", pointRadius: 0, borderWidth: 1, spanGaps: false },
            { label: "P10", data: cone.p10 || [], borderColor: "rgba(0,229,255,.45)", backgroundColor: "rgba(0,229,255,.08)", fill: false, pointRadius: 0, borderWidth: 1, spanGaps: false },
            { label: "P50", data: cone.p50 || [], borderColor: "#00e5ff", backgroundColor: "transparent", borderWidth: 2.2, pointRadius: 0, borderDash: [5, 4], spanGaps: false },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } } },
          scales: {
            x: { ticks: { maxTicksLimit: 10, maxRotation: 0 } },
            y: { title: { display: true, text: "€/MWh" } },
          },
        },
      }));
    });

    safePanel("C-Opt", () => {
      const opt = document.getElementById("ttfOptBadge");
      if (!opt) return;
      if (!kpi.optimal_range) {
        opt.textContent = "OPT RANGE — DATA PENDING";
        opt.classList.add("err");
        return;
      }
      opt.textContent = `OPTIMAL RANGE ${kpi.optimal_range} · ${fmt(kpi.max_confidence_pct)}%`;
      opt.classList.remove("err");
      opt.classList.add("ok");
    });

    safePanel("C-KDE", () => {
      const kde = T.kde || {};
      const kdeCtx = document.getElementById("ttfKde");
      if (!kdeCtx) return;
      destroyChartsByIds(["ttfKde"]);
      const base = ((kde["7"] || kde["14"] || kde["30"] || { x: [] }).x) || [];
      if (!base.length) {
        kdeCtx.parentElement.innerHTML = skel("KDE · EMPTY DISTRIBUTION");
        return;
      }
      function densDs(key, color, fill) {
        return {
          label: `H${key}`,
          data: (kde[key] || {}).density || [],
          borderColor: color,
          backgroundColor: fill,
          fill: true,
          pointRadius: 0,
          borderWidth: 1.6,
        };
      }
      charts.push(new Chart(kdeCtx, {
        type: "line",
        data: {
          labels: base,
          datasets: [
            densDs("7", "#00e5ff", "rgba(0,229,255,.12)"),
            densDs("14", "#f59e0b", "rgba(245,158,11,.12)"),
            densDs("30", "#10b981", "rgba(16,185,129,.12)"),
          ].filter((d) => (d.data || []).length),
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: { legend: { position: "bottom" } },
          scales: {
            x: { title: { display: true, text: "€/MWh" }, ticks: { maxTicksLimit: 8 } },
            y: { display: false },
          },
        },
      }));
    });

    safePanel("D-Granger", () => {
      const g = T.granger || {};
      if (!(g.labels || []).length) {
        const el = document.getElementById("ttfGranger");
        if (el && el.parentElement) el.parentElement.innerHTML = skel("GRANGER · NO LAGS");
        return;
      }
      barChart("ttfGranger", g.labels || [], [{
        label: "−log10(p)",
        data: g.neg_log10_p || [],
        backgroundColor: (g.significant || []).map((s) => (s ? "#10b981" : "#00e5ff")),
      }]);
    });

    safePanel("D-Elliott", () => {
      const ell = T.elliott || {};
      const eb = document.getElementById("ttfElliottBadge");
      if (eb) {
        eb.textContent = ell.badge || "WAVE — NO STRUCTURE";
        if (!ell.badge) {
          eb.classList.add("err");
          eb.classList.remove("ok");
        } else {
          eb.classList.add("ok");
          eb.classList.remove("err");
        }
      }
      const em = document.getElementById("ttfElliottMeta");
      if (em) {
        em.innerHTML = `
          <div class="row"><span>Status</span><span>${ell.status || "—"}</span></div>
          <div class="row"><span>Direction</span><span>${ell.direction || "—"}</span></div>
          <div class="row"><span>Score</span><span>${ell.score != null ? ell.score : "—"}</span></div>`;
      }
    });

    safePanel("E-Importance", () => {
      const imp = T.importance || { labels: [], values: [] };
      if (!(imp.labels || []).length) {
        const el = document.getElementById("ttfImportance");
        if (el && el.parentElement) el.parentElement.innerHTML = skel("IMPORTANCE · EMPTY");
        return;
      }
      barChart("ttfImportance", imp.labels || [], [{
        label: "Importance",
        data: imp.values || [],
        backgroundColor: "#3b82f6",
      }]);
    });

    safePanel("E-Markov", () => {
      const mh = T.markov_heatmap || { labels: [], matrix: [] };
      const ms = document.getElementById("ttfMarkovState");
      if (ms) ms.textContent = `STATE · ${mh.current_state || "—"}`;
      const table = document.getElementById("ttfMarkovHeat");
      if (!table) return;
      const labs = mh.labels || [];
      const mat = mh.matrix || [];
      const thead = table.querySelector("thead");
      const tbody = table.querySelector("tbody");
      if (thead) {
        thead.innerHTML = `<tr><th>i \\ j</th>${labs.map((l) => `<th>${String(l).replace(/_/g, " ").slice(0, 18)}</th>`).join("")}</tr>`;
      }
      if (tbody) {
        tbody.innerHTML = labs.map((rowLab, i) => {
          const cells = (mat[i] || []).map((v) => `<td style="background:${heatColor(v)}">${Number(v).toFixed(2)}</td>`).join("");
          return `<tr><th>${String(rowLab).replace(/_/g, " ").slice(0, 18)}</th>${cells}</tr>`;
        }).join("") || `<tr><td colspan="4">${skel("MARKOV EMPTY")}</td></tr>`;
      }
    });

    // ── Panel G / H / I · Portfolio & Hedging ──────────────────────────
    safePanel("G-Alloc", () => {
      const H = T.hedging || {};
      const g = H.panel_g || {};
      const center = g.center || {};
      const donut = g.donut || [];
      const centerEl = document.getElementById("ttfAllocCenter");
      if (centerEl) {
        centerEl.innerHTML = center.label
          ? `${center.label}<br/><span style="color:#8aa4bf;font-size:9px">${g.strategy_ref || "Strat B"}</span>`
          : "$1,000 → —";
      }
      if (!donut.length) {
        const el = document.getElementById("ttfAllocDonut");
        if (el && el.parentElement) el.parentElement.innerHTML = skel("ALLOCATION DONUT · EMPTY");
        return;
      }
      doughnut(
        "ttfAllocDonut",
        donut.map((d) => d.label),
        donut.map((d) => d.weight_pct),
        donut.map((d) => d.color),
        { cutout: "68%", legend: "bottom" }
      );
      const am = document.getElementById("ttfAllocMeta");
      if (am) {
        const fx = H.usd_eur_rate != null ? Number(H.usd_eur_rate).toFixed(4) : "—";
        const cap = H.capital_eur != null ? Number(H.capital_eur).toFixed(2) : "—";
        am.innerHTML = `
          <div class="row"><span>Base</span><span>$${fmt(H.base_investment_usd || 1000)} · €${cap}</span></div>
          <div class="row"><span>USD/EUR</span><span>${fx} · ${H.fx_source || "—"}</span></div>
          <div class="row"><span>TTF path P50</span><span>${fmt((H.forecast_path_p50 || {}).d7)} → ${fmt((H.forecast_path_p50 || {}).d14)} → ${fmt((H.forecast_path_p50 || {}).d30)}</span></div>`;
      }
    });

    safePanel("H-Matrix", () => {
      const H = T.hedging || {};
      const matrix = document.getElementById("ttfHedgeMatrix");
      const strategies = ((H.panel_h || {}).strategies) || [];
      const liveBadge = document.getElementById("ttfLivePaperBadge");
      const paper = H.paper_ledger || {};
      const bOrder = paper.strategy_b || (paper.orders || []).find((o) => o.short_id === "B") || {};
      if (liveBadge) {
        const txt = H.live_paper_badge || paper.live_paper_badge || bOrder.badge
          || "LIVE PAPER P&L — MtM: — / Target: +18.4%";
        liveBadge.textContent = `LIVE PAPER P&L · ${txt.replace(/^LIVE PAPER P&L · /, "")}`;
        liveBadge.classList.add("ok");
      }
      if (!matrix) return;
      if (strategies.length < 3) {
        matrix.innerHTML = skel("HEDGING MATRIX · NEED 3 STRATEGIES");
        return;
      }
      matrix.innerHTML = strategies.map((s) => {
        const y = s.expected_yield_pct || {};
        const rec = s.id === (H.recommended_strategy_id || "B");
        const rules = (s.execution_rules || []).slice(0, 4).map((r) => `<li>${r}</li>`).join("");
        const mtm = s.paper_mtm_pnl_usd;
        const mtmTxt = mtm == null ? "—" : `${mtm >= 0 ? "+" : ""}$${Number(mtm).toFixed(2)}`;
        return `<article class="hedge-card${rec ? " rec" : ""}">
          <h4>${s.id} · ${s.name || "—"}</h4>
          <div class="tier">${s.risk_tier || ""}${rec ? " · RECOMMENDED" : ""}</div>
          <div class="metrics">
            <div><span>Yield</span><br/><b>+${fmt(y.lo)}% … +${fmt(y.hi)}%</b></div>
            <div><span>VaR 95%</span><br/><b>${fmt(s.var_95_pct)}%</b></div>
            <div><span>Sharpe</span><br/><b>${fmt(s.expected_sharpe)}</b></div>
            <div><span>R/R</span><br/><b>${fmt(s.risk_reward)}</b></div>
            <div><span>Paper MtM</span><br/><b>${mtmTxt}</b></div>
            <div><span>Status</span><br/><b>${s.paper_status || "—"}</b></div>
          </div>
          <ul>${rules}</ul>
        </article>`;
      }).join("");
    });

    safePanel("I-Equity", () => {
      const H = T.hedging || {};
      const eq = H.panel_i || {};
      const seriesMap = eq.series || {};
      const mtmMap = eq.mtm_series || {};
      if (!(eq.labels || []).length) {
        const el = document.getElementById("ttfEquityCurves");
        if (el && el.parentElement) el.parentElement.innerHTML = skel("EQUITY CURVES · EMPTY");
        return;
      }
      const eqDatasets = [
        {
          label: "Benchmark $1,000",
          data: eq.benchmark || [],
          borderColor: "#64748b",
          backgroundColor: "transparent",
          borderDash: [4, 4],
          borderWidth: 1.4,
          fill: false,
        },
      ];
      ["A", "B", "C"].forEach((id) => {
        const s = seriesMap[id];
        if (!s) return;
        eqDatasets.push({
          label: s.name || `Strategy ${id}`,
          data: s.values || [],
          borderColor: s.color || "#00e5ff",
          backgroundColor: "transparent",
          borderWidth: id === "B" ? 2.4 : 1.6,
          fill: false,
        });
        const m = mtmMap[id];
        if (m && (m.values || []).some((v) => v != null)) {
          eqDatasets.push({
            label: m.name || `MtM ${id}`,
            data: m.values || [],
            borderColor: m.color || "#ffffff",
            backgroundColor: "transparent",
            borderWidth: 2,
            borderDash: [2, 3],
            pointRadius: 3,
            pointHoverRadius: 5,
            spanGaps: true,
            fill: false,
          });
        }
      });
      lineChart("ttfEquityCurves", eq.labels, eqDatasets);
    });

    // Synchronous layout flush for Chart.js / Plotly
    forceChartRelayout(document.getElementById("tab-ttf-forecast"));
    if (document.body) void document.body.offsetHeight;
  }

  function boot() {
    renderKPI();
    wireTabs();
    makeMap("map01", P.c01_heatmap || []);
    makeMap("map04", (P.c04_alpha_tracks || []).map((t) => ({
      lat: t.lat, lon: t.lon, name: t.name, tier: "ALPHA", sog: t.sog, color: COLORS.ALPHA, imo: t.imo,
    })), { alphaOnly: true });

    const cp = P.c02_chokepoints || [];
    barChart("c02", cp.map((x) => x.name.replace("Strait of ", "").replace(" Canal", "")), [{
      label: "Vessels",
      data: cp.map((x) => x.count),
      backgroundColor: "#00e5ff",
    }]);

    const pm = P.c05_port_matrix || [];
    barChart("c05", pm.map((x) => x.port), [
      { label: "Anchor proxy h", data: pm.map((x) => x.anchor_hours_proxy), backgroundColor: "#f59e0b" },
      { label: "Berth proxy h", data: pm.map((x) => x.berth_hours_proxy), backgroundColor: "#3b82f6" },
    ], true);

    scatter("c07", P.c07_draft_scatter || []);
    const sp = P.c08_speed_spectrum || { labels: [], counts: [] };
    barChart("c08", sp.labels, [{ label: "Vessels", data: sp.counts, backgroundColor: "#10b981" }]);

    const dev = (P.c09_course_deviation || {}).score || 0;
    doughnut("c09", ["Deviation", "Aligned"], [dev, Math.max(0, 100 - dev)], ["#ef4444", "#10b981"]);

    const spoof = P.c10_spoofing_radar || {};
    radar("c10", ["Coord jump", "Velocity spike", "Heading incoherence", "Clean"], [{
      label: "Flags",
      data: [
        spoof.coord_jump || 0,
        spoof.velocity_spike || 0,
        spoof.heading_incoherent || 0,
        Math.min(100, (spoof.clean || 0) / 10),
      ],
      backgroundColor: "rgba(239,68,68,.25)",
      borderColor: "#ef4444",
    }]);

    const tt = P.c11_tonnage_transit || { series: [] };
    lineChart("c11", (tt.series || []).map((s) => s.t), [
      { label: "Floating storage", data: (tt.series || []).map((s) => s.floating / 1e6), borderColor: "#f59e0b", backgroundColor: "rgba(245,158,11,.15)", fill: true },
      { label: "Steaming", data: (tt.series || []).map((s) => s.steaming / 1e6), borderColor: "#00e5ff", backgroundColor: "rgba(0,229,255,.1)", fill: true },
    ]);

    const flags = P.c12_flag_tree || [];
    barChart("c12", flags.map((f) => (f.name || "").slice(0, 14)), [{
      label: "DWT",
      data: flags.map((f) => f.value / 1e6),
      backgroundColor: "#3b82f6",
    }]);

    const rr = P.c13_risk_radar || { labels: [] };
    radar("c13", rr.labels || [], ["ALPHA", "BRAVO", "CHARLIE", "DELTA"].map((t) => ({
      label: t,
      data: rr[t] || [],
      borderColor: COLORS[t],
      backgroundColor: (COLORS[t] || "#fff") + "33",
    })));

    const geo = P.c14_geo_exposure || { zones: [], tiers: [], matrix: {} };
    barChart("c14", geo.zones || [], (geo.tiers || []).map((t) => ({
      label: t,
      data: (geo.zones || []).map((z) => (geo.matrix[z] || [])[["ALPHA", "BRAVO", "CHARLIE", "DELTA"].indexOf(t)] || 0),
      backgroundColor: COLORS[t],
    })), true);

    const mps = (P.c15_mps || {}).series || [];
    lineChart("c15", mps.map((_, i) => i + 1), [{
      label: "MPS", data: mps, borderColor: "#00e5ff", fill: false,
    }]);
    const lat = (P.c16_latency || {}).series || [];
    lineChart("c16", lat.map((_, i) => i + 1), [{
      label: "ms", data: lat, borderColor: "#f59e0b", fill: false,
    }]);

    const me = P.c18_match_efficiency || { matched: 0, unmatched: 0 };
    doughnut("c18", ["Matched targets", "Background"], [me.matched, me.unmatched], ["#10b981", "#64748b"]);

    renderLists();

    // Deep-link: ?sheet=ttf|top10|route
    const initialSheet = parseSheetFromUrl();
    if (initialSheet === "ttf") {
      switchTab("tab-ttf-forecast");
    } else if (initialSheet === "top10") {
      switchTab("top10");
    } else if (initialSheet === "route") {
      switchTab("route");
    } else {
      afterLayout(() => forceChartRelayout(document.getElementById("sheet-ais")));
    }

    window.addEventListener("hashchange", () => {
      switchSheet(parseSheetFromUrl(), { force: true });
    });
    window.addEventListener("popstate", () => {
      switchSheet(parseSheetFromUrl(), { force: true });
    });
    window.addEventListener("resize", () => {
      const sheet = document.documentElement.dataset.sheet || parseSheetFromUrl();
      const root = sheet === "ttf"
        ? document.getElementById("tab-ttf-forecast")
        : sheet === "top10"
          ? document.getElementById("sheet-top10")
          : sheet === "route"
            ? document.getElementById("sheet-route")
            : document.getElementById("sheet-ais");
      forceChartRelayout(root);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.__SENTINEL__ = { payload: P, charts, switchSheet, switchTab, parseSheetFromUrl, forceChartRelayout };
  window.switchTab = switchTab;
})();
