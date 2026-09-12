/**
 * Oracle-1001 / Sentinel — Balance Sheet Engine
 * TOP-500 Fleet Aggregation · 6 Quant Metrics · Apple×NASA HUD
 *
 * Panels:
 *   A — TOP-500 VOLUMETRIC CAPACITY MATRIX   (Cargo Load %, M³ distribution)
 *   B — DATA FIDELITY & SRE TRUST            (DFS circular HUD + PIL latency)
 *   C — OSINT ANOMALY & STS RISK RANKING     (DAR + ΔV composite table)
 *   D — TTF ELASTICITY & SUPPLY CURVE        (LSSI elasticity + price bands)
 */

(function () {
  "use strict";

  // ── Data Sources ─────────────────────────────────────────────────────────────
  const P   = window.__SENTINEL_PAYLOAD__ || {};
  const BAL = P.balance || {};                     // computed by balance_analytics.py
  const TTF = P.ttf_forecast || {};

  const cargo  = BAL.cargo_load || {};
  const dar    = BAL.dar        || {};
  const dv     = BAL.delta_v    || {};
  const dfs    = BAL.dfs        || {};
  const pil    = BAL.pil        || {};
  const lssi   = BAL.lssi       || {};
  const table  = BAL.anomaly_table || [];

  const CYAN    = "#00f0ff";
  const AMBER   = "#ffb300";
  const CRIMSON = "#ff3b30";
  const GREEN   = "#32d74b";
  const MUTED   = "rgba(245,245,247,0.42)";
  const FROST   = "#f5f5f7";
  const GLASS   = "rgba(18,24,38,0.65)";

  const balCharts = [];

  function metricInsufficient(m) {
    const st = String((m && m.signal_status) || BAL.signal_status || "").toLowerCase();
    const sig = String((m && m.signal) || "").toUpperCase();
    const fs = String(P.fleet_sample_status || BAL.fleet_sample_status || "").toUpperCase();
    if (st === "insufficient_sample" || sig === "INSUFFICIENT_SAMPLE") return true;
    if (fs && fs !== "FULL") return true;
    if (P.production_actionable === false) return true;
    return false;
  }

  function displaySignal(m) {
    if (metricInsufficient(m)) return "INSUFFICIENT_SAMPLE";
    return (m && (m.signal || m.dfs_grade)) || "—";
  }

  // ── What-If Scenario State ───────────────────────────────────────────────────
  const scenario = {
    blockageDays: 0,   // Strait Blockage Delay [0..30]
    tempAnomalyC: 0,   // European Temperature Anomaly [-10..+10]
  };

  /** Apply What-If stress to cargo / LSSI metrics (pure, non-destructive). */
  function applyScenario(baseCargo, baseLssi) {
    const delay = Number(scenario.blockageDays) || 0;
    const temp = Number(scenario.tempAnomalyC) || 0;

    // Blockage: each day delays ~1.8% of EU-bound laden volume (proxy)
    const delayFrac = Math.min(0.55, delay * 0.018);
    const ladenM3 = Number(baseCargo.total_m3_laden || 0);
    const ballastM3 = Number(baseCargo.total_m3_ballast || 0);
    const delayedM3 = ladenM3 * delayFrac;
    const effectiveLaden = ladenM3 - delayedM3;

    // Cold anomaly (neg °C) → heating demand ↑ → TTF ↑; warm → reverse
    // Empirical: ±1°C ≈ ±0.35 €/MWh on short-term TTF (proxy)
    const tempImpact = -temp * 0.35;
    const supplyShockPct = -delayFrac * 100; // negative supply to Europe
    // LSSI elasticity base ≈ -0.45 € per 1% supply change (from backend curve)
    const elasticity = Number(baseLssi.elasticity_eur_per_pct) || -0.45;
    const blockageImpact = supplyShockPct * Math.abs(elasticity) * 0.12;
    const spot = Number(baseLssi.ttf_spot || 0);
    const impliedSpot = spot + tempImpact + blockageImpact;

    const curve = (baseLssi.supply_curve || []).map((p) => {
      const base = Number(p.implied_ttf || spot);
      return {
        ...p,
        implied_ttf: +(base + tempImpact + blockageImpact).toFixed(3),
      };
    });

    const cargoAdj = {
      ...baseCargo,
      total_m3_laden: Math.round(effectiveLaden),
      total_m3_ballast: Math.round(ballastM3 + delayedM3 * 0.35),
      total_m3_transit: Math.round(effectiveLaden + ballastM3 + delayedM3 * 0.35),
      delayed_m3: Math.round(delayedM3),
      laden_pct: baseCargo.laden_pct,
      scenario_active: delay > 0 || Math.abs(temp) > 0.01,
    };

    const lssiAdj = {
      ...baseLssi,
      ttf_spot: +impliedSpot.toFixed(3),
      supply_curve: curve,
      scenario: {
        blockage_days: delay,
        temp_anomaly_c: temp,
        delayed_m3: Math.round(delayedM3),
        temp_impact_eur: +tempImpact.toFixed(3),
        blockage_impact_eur: +blockageImpact.toFixed(3),
        implied_ttf: +impliedSpot.toFixed(3),
      },
    };

    return { cargo: cargoAdj, lssi: lssiAdj };
  }

  function getActiveCargo() {
    return applyScenario(cargo, lssi).cargo;
  }
  function getActiveLssi() {
    return applyScenario(cargo, lssi).lssi;
  }
  function fmt(n, decimals) {
    if (n == null || isNaN(n)) return "—";
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(decimals ?? 2) + "M";
    if (n >= 1_000)     return (n / 1_000).toFixed(decimals ?? 1) + "K";
    return Number(n).toFixed(decimals ?? 0);
  }

  function fmtNum(n) {
    if (n == null || isNaN(n)) return "—";
    return Number(n).toLocaleString("en-US");
  }

  function severityColor(s) {
    const l = String(s || "").toUpperCase();
    if (l === "HIGH"    || l === "CRITICAL" || l === "EXTREME") return CRIMSON;
    if (l === "MEDIUM"  || l === "DEGRADED")                   return AMBER;
    return GREEN;
  }

  function killChart(id) {
    const el = document.getElementById(id);
    if (!el || typeof Chart === "undefined") return;
    const existing = typeof Chart.getChart === "function" ? Chart.getChart(el) : null;
    if (existing) { try { existing.destroy(); } catch (_) {} }
  }

  function drawChart(id, config) {
    killChart(id);
    const el = document.getElementById(id);
    if (!el) return;
    try {
      const ch = new Chart(el, config);
      balCharts.push(ch);
      return ch;
    } catch (e) {
      console.warn("[BALANCE] chart error", id, e);
    }
  }

  // ── KPI Banner ────────────────────────────────────────────────────────────────
  function renderBalanceKPI() {
    const el = document.getElementById("bal-kpi-row");
    if (!el) return;
    const c = getActiveCargo();
    const L = getActiveLssi();
    const grade = dfs.grade || "—";
    const gradeColor = grade === "A" ? GREEN : grade === "B" ? CYAN : grade === "C" ? AMBER : CRIMSON;
    const status = BAL.status || "—";
    const spoofN = Number(BAL.spoofed_excluded || (BAL.ais_spoofing || {}).spoofed_count || 0);
    const alerts = (P.alerts || {}).alerts || [];

    el.innerHTML = `
      <div class="bal-kpi-card">
        <div class="bkc-label">FLEET SIZE (CLEAN)</div>
        <div class="bkc-value">${fmtNum(BAL.fleet_size || P.live_vessel_count || 0)}</div>
        <div class="bkc-sub">TOP-500 · spoofed excluded ${spoofN}</div>
      </div>
      <div class="bal-kpi-card">
        <div class="bkc-label">M³ IN TRANSIT</div>
        <div class="bkc-value" style="color:${CYAN}">${fmt(L.total_m3_transit || c.total_m3_transit || 0)}</div>
        <div class="bkc-sub">${c.delayed_m3 ? `delayed ${fmt(c.delayed_m3)} m³` : "Laden + Floating Storage"}</div>
      </div>
      <div class="bal-kpi-card">
        <div class="bkc-label">LADEN RATIO</div>
        <div class="bkc-value" style="color:${GREEN}">${(c.laden_pct ?? 0).toFixed(1)}%</div>
        <div class="bkc-sub">${c.laden_count ?? "—"} laden · ${c.ballast_count ?? "—"} ballast</div>
      </div>
      <div class="bal-kpi-card">
        <div class="bkc-label">DATA FIDELITY</div>
        <div class="bkc-value" style="color:${metricInsufficient(dfs) ? AMBER : gradeColor}">${metricInsufficient(dfs) ? "INSUFFICIENT_SAMPLE" : `Grade ${grade}`}</div>
        <div class="bkc-sub">DFS ${(dfs.dfs_score ?? 0).toFixed(1)}% · not actionable unless FULL</div>
      </div>
      <div class="bal-kpi-card">
        <div class="bkc-label">SPOOF / GHOST</div>
        <div class="bkc-value" style="color:${spoofN > 0 ? CRIMSON : GREEN}">${spoofN}</div>
        <div class="bkc-sub">bound ${(BAL.ais_spoofing || {}).bound_kn || 21} kn</div>
      </div>
      <div class="bal-kpi-card">
        <div class="bkc-label">LSSI · WHAT-IF TTF</div>
        <div class="bkc-value" style="color:${L.signal === "BULLISH" ? GREEN : L.signal === "BEARISH" ? CRIMSON : AMBER}">
          ${(L.ttf_spot ?? 0).toFixed(2)}
        </div>
        <div class="bkc-sub">€/MWh · alerts ${alerts.length}</div>
      </div>`;
  }

  // ── Panel A: Volumetric Capacity Matrix ───────────────────────────────────────
  function renderPanelA() {
    const c = getActiveCargo();
    // A1: Laden vs Ballast doughnut
    drawChart("balA1", {
      type: "doughnut",
      data: {
        labels: ["Laden (Cargo)", "Ballast", "Unknown"],
        datasets: [{
          data: [c.laden_count || 0, c.ballast_count || 0, c.unknown_count || 0],
          backgroundColor: [CYAN, AMBER, "rgba(255,255,255,0.15)"],
          borderWidth: 0,
          hoverOffset: 6,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        cutout: "68%",
        plugins: {
          legend: { position: "bottom", labels: { color: MUTED, boxWidth: 10, font: { size: 10 } } },
          tooltip: {
            callbacks: {
              label: (item) => ` ${item.label}: ${item.raw} vessels (${((item.raw / Math.max(BAL.fleet_size || 1, 1)) * 100).toFixed(1)}%)`,
            },
          },
        },
      },
    });

    // Centre overlay (injected via CSS absolute positioning — updated dynamically)
    const donutCenter = document.getElementById("balA1-center");
    if (donutCenter) {
      const delayNote = c.delayed_m3 ? `<br/><span style="font-size:9px;color:${AMBER}">Δ −${fmt(c.delayed_m3)}</span>` : "";
      donutCenter.innerHTML = `<span style="font-size:18px;color:${CYAN}">${(c.laden_pct ?? 0).toFixed(1)}%</span><br/><span style="font-size:10px;color:${MUTED}">LADEN</span>${delayNote}`;
    }

    // A2: Tier-breakdown stacked bar
    const tb = c.tier_breakdown || {};
    const tierLabels = ["ALPHA", "BRAVO", "CHARLIE", "DELTA"];
    drawChart("balA2", {
      type: "bar",
      data: {
        labels: tierLabels,
        datasets: [
          {
            label: "Laden",
            data: tierLabels.map((t) => (tb[t] || {}).laden || 0),
            backgroundColor: CYAN,
            borderWidth: 0,
          },
          {
            label: "Ballast",
            data: tierLabels.map((t) => (tb[t] || {}).ballast || 0),
            backgroundColor: AMBER,
            borderWidth: 0,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          x: { stacked: true, grid: { color: "rgba(255,255,255,0.05)" }, ticks: { color: MUTED, font: { size: 10 } } },
          y: { stacked: true, grid: { color: "rgba(255,255,255,0.05)" }, ticks: { color: MUTED, font: { size: 10 } }, beginAtZero: true },
        },
        plugins: {
          legend: { labels: { color: MUTED, boxWidth: 10, font: { size: 10 } } },
        },
      },
    });

    // A3: M³ in transit gauge (horizontal bar) — scenario-aware
    const L = getActiveLssi();
    const totalM3 = c.total_m3_transit || L.total_m3_transit || 0;
    const qBaseM3 = L.q_base_m3 || lssi.q_base_m3 || 1;
    const pct = Math.min(100, Math.round((totalM3 / qBaseM3) * 100));
    const el = document.getElementById("balA3-gauge");
    if (el) {
      el.innerHTML = `
        <div class="bal-gauge-label">TOTAL M³ IN TRANSIT ${c.scenario_active ? "(SCENARIO)" : ""}</div>
        <div class="bal-gauge-value" style="color:${CYAN}">${fmt(totalM3)} m³</div>
        <div class="bal-gauge-bar-wrap">
          <div class="bal-gauge-bar" style="width:${pct}%;background:linear-gradient(90deg,${CYAN},#0080ff)"></div>
        </div>
        <div class="bal-gauge-meta">
          ${pct}% of fleet baseline (${fmt(qBaseM3)} m³) ·
          Steaming: ${fmt(L.steaming_m3 || lssi.steaming_m3 || 0)} m³ ·
          Floating: ${fmt(L.floating_m3 || lssi.floating_m3 || 0)} m³
          ${c.delayed_m3 ? ` · Delayed: ${fmt(c.delayed_m3)} m³` : ""}
        </div>`;
    }
  }

  // ── Panel B: Data Fidelity & SRE Trust ────────────────────────────────────────
  function renderPanelB() {
    // B1: DFS circular progress (arc chart)
    const dfsScore = Number(dfs.dfs_score ?? 0);
    const pilStatus = pil.status || "NOMINAL";
    const pilColor = pilStatus === "CRITICAL" ? CRIMSON : pilStatus === "DEGRADED" ? AMBER : GREEN;

    drawChart("balB1", {
      type: "doughnut",
      data: {
        labels: ["Physical Messages", "Synthetic / Unmatched"],
        datasets: [{
          data: [dfs.physical_pct || 0, dfs.synthetic_pct || 0],
          backgroundColor: [GREEN, "rgba(255,59,48,0.35)"],
          borderWidth: 0,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        cutout: "72%",
        rotation: -90, circumference: 180,   // half-circle HUD arc
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (i) => ` ${i.label}: ${Number(i.raw).toFixed(1)}%`,
            },
          },
        },
      },
    });

    const b1center = document.getElementById("balB1-center");
    if (b1center) {
      b1center.innerHTML = `
        <div style="font-size:24px;font-weight:700;color:${GREEN}">${dfsScore.toFixed(1)}%</div>
        <div style="font-size:10px;color:${MUTED};letter-spacing:.08em">DATA FIDELITY</div>
        <div style="font-size:11px;color:${GREEN};margin-top:4px">Grade&nbsp;${dfs.grade || "—"}</div>`;
    }

    // B2: PIL latency sparkline
    const latSpark = pil.lat_spark || [];
    const labels = latSpark.map((_, i) => i.toString());
    drawChart("balB2", {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Insert Latency (ms)",
          data: latSpark,
          borderColor: pilColor,
          backgroundColor: "transparent",
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.3,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          x: { display: false },
          y: { grid: { color: "rgba(255,255,255,0.05)" }, ticks: { color: MUTED, font: { size: 9 } }, beginAtZero: true },
        },
        plugins: { legend: { display: false } },
      },
    });

    // B3: MPS spark
    const mpsSpark = pil.mps_spark || [];
    drawChart("balB3", {
      type: "line",
      data: {
        labels: mpsSpark.map((_, i) => i.toString()),
        datasets: [{
          label: "MPS",
          data: mpsSpark,
          borderColor: CYAN,
          backgroundColor: "rgba(0,240,255,0.08)",
          fill: true,
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.3,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          x: { display: false },
          y: { grid: { color: "rgba(255,255,255,0.05)" }, ticks: { color: MUTED, font: { size: 9 } }, beginAtZero: true },
        },
        plugins: { legend: { display: false } },
      },
    });

    // B4: SRE status tiles
    const b4el = document.getElementById("balB4-status");
    if (b4el) {
      const pilDisplay = pil.status_display || pilStatus;
      const caveat = pil.sample_size_caveat || BAL.sample_size_caveat || "";
      const tiles = [
        { label: "DB LAG",    value: `${pil.lag_minutes ?? "—"}m`,       color: pil.is_stale ? CRIMSON : GREEN },
        { label: "LATENCY P95", value: `${pil.latency_p95_ms ?? "—"}ms`, color: pil.latency_p95_ms > 200 ? AMBER : GREEN },
        { label: "MPS",       value: `${pil.mps_current ?? "—"}`,        color: CYAN },
        { label: "FIDELITY",  value: dfs.status || "—",                  color: dfs.is_stale ? AMBER : dfs.is_live ? GREEN : AMBER },
        { label: "SOURCE",    value: (dfs.source_mode || "—").replace("+STALE", ""), color: MUTED },
        { label: "PIL STATUS",value: pilDisplay,                         color: pilColor },
      ];
      b4el.innerHTML = tiles.map((t) => `
        <div class="bal-sre-tile">
          <div class="bst-label">${t.label}</div>
          <div class="bst-value" style="color:${t.color}">${t.value}</div>
        </div>`).join("") +
        (caveat
          ? `<div class="bal-sre-tile" style="grid-column:1/-1">
               <div class="bst-label">SAMPLE CAVEAT</div>
               <div class="bst-value" style="color:${AMBER};font-size:10px;line-height:1.35;white-space:normal">${caveat}</div>
             </div>`
          : "");
    }
  }

  // ── Panel C: OSINT Anomaly & STS Risk Ranking ─────────────────────────────────
  function renderPanelC() {
    // C1: DAR vs ΔV comparison chart
    const darVal = dar.dar_pct || 0;
    const dvIdx  = dv.delta_v_index || 0;
    drawChart("balC1", {
      type: "bar",
      data: {
        labels: ["DAR %", "ΔV Index", "STS Clusters", "Anch Halts"],
        datasets: [{
          label: "Fleet Anomaly Metrics",
          data: [darVal, dvIdx, dv.sts_cluster_count || 0, dv.anomalous_halt_count || 0],
          backgroundColor: [CRIMSON, AMBER, "#ff6b35", "#a855f7"],
          borderWidth: 0,
          borderRadius: 4,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          x: { grid: { color: "rgba(255,255,255,0.05)" }, ticks: { color: MUTED, font: { size: 10 } } },
          y: { grid: { color: "rgba(255,255,255,0.05)" }, ticks: { color: MUTED, font: { size: 10 } }, beginAtZero: true },
        },
        plugins: { legend: { display: false } },
      },
    });

    // C2: SOG distribution
    const sog = dv.sog_distribution || {};
    drawChart("balC2", {
      type: "doughnut",
      data: {
        labels: ["STS/Dark (<0.5kn)", "Anchored (<3kn)", "Slow (<10kn)", "Transit (<16kn)", "Fast (16+kn)"],
        datasets: [{
          data: [sog.sts_zone || 0, sog.anchored || 0, sog.slow || 0, sog.transit || 0, sog.fast || 0],
          backgroundColor: [CRIMSON, AMBER, "#3b82f6", CYAN, GREEN],
          borderWidth: 0,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        cutout: "60%",
        plugins: {
          legend: { position: "right", labels: { color: MUTED, boxWidth: 8, font: { size: 9 } } },
        },
      },
    });

    // C3: Risk table
    const tblEl = document.getElementById("balC-table");
    if (!tblEl) return;
    if (!table.length) {
      tblEl.innerHTML = `<tr><td colspan="7" style="text-align:center;color:${GREEN};padding:24px">
        ✓ NO HIGH-RISK ANOMALIES DETECTED · FLEET NOMINAL</td></tr>`;
      return;
    }
    const riskBadge = (r) => {
      const c = r === "EXTREME" || r === "HIGH" ? CRIMSON : r === "MEDIUM" ? AMBER : GREEN;
      return `<span class="bal-risk-badge" style="color:${c};border-color:${c}40;background:${c}12">${r}</span>`;
    };
    const tierBadge = (t) => {
      const colors = { ALPHA: "#ef4444", BRAVO: "#f59e0b", CHARLIE: "#3b82f6", DELTA: "#10b981" };
      const c = colors[t] || MUTED;
      return `<span class="bal-tier-badge" style="color:${c}">${t}</span>`;
    };
    tblEl.innerHTML = table.map((row, i) => `
      <tr class="bal-table-row">
        <td class="bal-td-rank">${i + 1}</td>
        <td class="bal-td-name">${row.name || row.imo}</td>
        <td>${tierBadge(row.tier)}</td>
        <td>${riskBadge(row.risk)}</td>
        <td class="bal-td-num" style="color:${row.dark_hours > 24 ? CRIMSON : row.dark_hours > 8 ? AMBER : MUTED}">
          ${row.dark_hours.toFixed(1)}h
        </td>
        <td class="bal-td-flag">
          ${row.is_sts ? `<span class="bal-flag-tag" style="color:${CRIMSON}">STS</span>` : ""}
          ${row.is_halt ? `<span class="bal-flag-tag" style="color:${AMBER}">HALT</span>` : ""}
          ${row.is_spoofed ? `<span class="bal-flag-tag" style="color:${CRIMSON}">SPOOFED TRACK DETECTED</span>` : ""}
        </td>
        <td class="bal-td-score" style="color:${row.composite_score > 50 ? CRIMSON : row.composite_score > 25 ? AMBER : MUTED}">
          ${row.composite_score.toFixed(0)}
        </td>
      </tr>`).join("");
  }

  // ── Panel D: TTF Elasticity & Supply Curve ───────────────────────────────────
  function renderPanelD() {
    const L = getActiveLssi();
    const curve = L.supply_curve || [];
    const bands = L.ttf_bands || lssi.ttf_bands || {};
    const spot  = L.ttf_spot || 0;

    // D1: Supply elasticity curve (line + scatter for current position)
    const curveLabels = curve.map((p) => `${p.supply_pct_of_base > 0 ? "+" : ""}${p.supply_pct_of_base}%`);
    const curveData   = curve.map((p) => p.implied_ttf);
    const currentIdx  = curve.findIndex((p) => p.supply_pct_of_base === 0);

    drawChart("balD1", {
      type: "line",
      data: {
        labels: curveLabels,
        datasets: [
          {
            label: "TTF Elasticity Curve",
            data: curveData,
            borderColor: AMBER,
            backgroundColor: "rgba(255,179,0,0.07)",
            fill: true,
            borderWidth: 2,
            pointRadius: (ctx) => ctx.dataIndex === currentIdx ? 8 : 3,
            pointBackgroundColor: (ctx) => ctx.dataIndex === currentIdx ? CYAN : AMBER,
            tension: 0.4,
          },
          {
            label: "TTF Spot",
            data: curveLabels.map(() => spot),
            borderColor: "rgba(0,240,255,0.45)",
            borderDash: [5, 4],
            borderWidth: 1.5,
            pointRadius: 0,
            fill: false,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          x: { grid: { color: "rgba(255,255,255,0.05)" }, ticks: { color: MUTED, font: { size: 9 } } },
          y: {
            grid: { color: "rgba(255,255,255,0.05)" },
            ticks: { color: MUTED, font: { size: 9 }, callback: (v) => `€${v.toFixed(1)}` },
          },
        },
        plugins: {
          legend: { labels: { color: MUTED, boxWidth: 10, font: { size: 10 } } },
          tooltip: {
            callbacks: {
              label: (i) => ` ${i.dataset.label}: €${Number(i.raw).toFixed(2)}/MWh`,
            },
          },
        },
      },
    });

    // D2: H7 & H30 price band comparison
    const h7  = bands.h7  || {};
    const h30 = bands.h30 || {};
    drawChart("balD2", {
      type: "bar",
      data: {
        labels: ["H7 P10", "H7 P50", "H7 P90", "H30 P10", "H30 P50", "H30 P90"],
        datasets: [{
          label: "TTF Price Band (€/MWh)",
          data: [h7.p10, h7.p50, h7.p90, h30.p10, h30.p50, h30.p90],
          backgroundColor: [
            "rgba(0,240,255,0.35)", CYAN, "rgba(0,240,255,0.35)",
            "rgba(255,179,0,0.35)", AMBER, "rgba(255,179,0,0.35)",
          ],
          borderWidth: 0,
          borderRadius: 4,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          x: { grid: { color: "rgba(255,255,255,0.05)" }, ticks: { color: MUTED, font: { size: 9 } } },
          y: {
            grid: { color: "rgba(255,255,255,0.05)" },
            ticks: { color: MUTED, font: { size: 9 }, callback: (v) => `€${v.toFixed(1)}` },
            beginAtZero: false,
          },
        },
        plugins: { legend: { display: false } },
      },
    });

    // D3: LSSI index + impact text (scenario-aware)
    const d3el = document.getElementById("balD3-meta");
    if (d3el) {
      const sc = L.scenario || {};
      const impactSign = (sc.blockage_impact_eur || 0) + (sc.temp_impact_eur || 0) > 0 ? "+" : "";
      const totalImpact = (sc.temp_impact_eur || 0) + (sc.blockage_impact_eur || 0);
      d3el.innerHTML = `
        <div class="bal-lssi-row">
          <span class="bal-lssi-label">LSSI INDEX</span>
          <span class="bal-lssi-value" style="color:${L.signal === "BULLISH" ? GREEN : L.signal === "BEARISH" ? CRIMSON : AMBER}">
            ${L.lssi_index ?? lssi.lssi_index ?? "—"}
          </span>
        </div>
        <div class="bal-lssi-row">
          <span class="bal-lssi-label">TTF ${sc.blockage_days || sc.temp_anomaly_c ? "WHAT-IF" : "SPOT"}</span>
          <span class="bal-lssi-value" style="color:${AMBER}">€${(spot ?? 0).toFixed(2)}/MWh</span>
        </div>
        <div class="bal-lssi-row">
          <span class="bal-lssi-label">SCENARIO Δ</span>
          <span class="bal-lssi-value" style="color:${totalImpact > 0 ? CRIMSON : totalImpact < 0 ? GREEN : MUTED}">
            ${impactSign}€${totalImpact.toFixed(2)}/MWh
          </span>
        </div>
        <div class="bal-lssi-row">
          <span class="bal-lssi-label">SIGNAL</span>
          <span class="bal-lssi-value" style="color:${displaySignal(L) === "INSUFFICIENT_SAMPLE" ? AMBER : (L.signal === "BULLISH" ? GREEN : L.signal === "BEARISH" ? CRIMSON : AMBER)}">
            ${displaySignal({ ...lssi, ...L })}&nbsp;&nbsp;ε=${L.elasticity ?? lssi.elasticity ?? 0.25}
          </span>
        </div>`;
    }
  }

  // ── Panel E: Quant Pipeline Dashboard ────────────────────────────────────────
  function renderPanelE() {
    const el = document.getElementById("balE-quant");
    if (!el) return;
    const QP = P.quant_pipeline || {};
    const bog = QP.bog     || {};
    const rot = QP.routing || {};
    const eta = QP.eta     || {};
    const tbi = QP.tbi     || {};
    const ice = QP.ice     || {};
    const feat = ice.features || {};

    const dirColor = ice.direction === "LONG" ? GREEN : ice.direction === "SHORT" ? CRIMSON : AMBER;
    const accPct   = Number(QP.ensemble_accuracy_pct || 0);
    const accBasis = String(QP.accuracy_basis || "confidence_proxy");
    const proxyPct = Number(QP.ensemble_confidence_proxy_pct ?? (Number(QP.confidence || 0) * 100));
    const accColor = accBasis === "purged_cv_directional"
      ? (accPct >= 80 ? GREEN : accPct >= 70 ? AMBER : CRIMSON)
      : AMBER;
    const comps = QP.component_accuracy_pct || {};
    const gov = QP.weight_governance || {};
    const downN = (gov.downweighted || []).length;

    const cards = [
      {
        icon: "🔥", title: "BOG DECAY ENGINE",
        value: `${(bog.fleet_bog_loss_pct || 0).toFixed(2)}%`,
        valueColor: bog.fleet_bog_loss_pct > 1 ? AMBER : GREEN,
        rows: [
          { k: "Active Voyages",   v: bog.active_voyages ?? "—" },
          { k: "Init M³",          v: fmt(bog.fleet_total_init_m3) },
          { k: "Delivered M³",     v: fmt(bog.fleet_total_deliv_m3) },
          { k: "Value Lost €",     v: "€" + fmt(bog.fleet_value_lost_eur) },
          { k: "Energy Lost MWh",  v: fmt(bog.fleet_energy_lost_mwh) },
        ],
      },
      {
        icon: "🧭", title: "HMM DESTINATION PREDICTOR",
        value: `${((rot.fleet_p_eu_avg || 0) * 100).toFixed(1)}% EU`,
        valueColor: rot.fleet_p_eu_avg > 0.55 ? GREEN : AMBER,
        rows: [
          { k: "EU-Bound",          v: rot.eu_bound_count ?? "—" },
          { k: "Asia-Bound",        v: rot.asia_bound_count ?? "—" },
          { k: "Rerouting",         v: rot.uncertain_count ?? "—" },
          { k: "TTF−JKM Spread",    v: `€${(rot.spread_ttf_jkm || 0).toFixed(2)}` },
        ],
      },
      {
        icon: "🌊", title: "NOAA HYDRODYNAMICS ETA",
        value: `${eta.avg_eta_correction_h || 0}h`,
        valueColor: Math.abs(eta.avg_eta_correction_h || 0) > 24 ? CRIMSON : AMBER,
        rows: [
          { k: "Vessels Analysed",  v: eta.vessels_analysed ?? "—" },
          { k: "Delayed >12h",      v: eta.delayed_more_than_12h ?? "—" },
          { k: "Avg Correction",    v: `${eta.avg_eta_correction_h || 0}h` },
          { k: "Month",             v: eta.month ?? "—" },
        ],
      },
      {
        icon: "🏭", title: "TERMINAL BOTTLENECK",
        value: (tbi.tbi_score || 0).toFixed(3),
        valueColor: (tbi.tbi_score || 0) > 0.6 ? CRIMSON : (tbi.tbi_score || 0) > 0.3 ? AMBER : GREEN,
        rows: [
          { k: "TBI Score",         v: (tbi.tbi_score || 0).toFixed(3) },
          { k: "Signal",            v: tbi.injection_delay_signal || "—" },
          { k: "Delayed MWh",       v: fmt(tbi.total_delayed_mwh) },
          { k: "Terminals",         v: (tbi.terminals || []).length },
        ],
      },
      {
        icon: "📊", title: "ICE MICROSTRUCTURE ENSEMBLE",
        value: ice.direction || "—",
        valueColor: dirColor,
        rows: [
          { k: "P(Bull)",           v: `${((ice.p_bull || 0) * 100).toFixed(1)}%` },
          { k: "Confidence",        v: `${((ice.confidence || 0) * 100).toFixed(1)}%` },
          { k: "Target H7",         v: `€${(ice.target_h7 || 0).toFixed(2)}` },
          { k: "Flow Signal",       v: (feat.f_flow || 0).toFixed(3) },
          { k: "COT Proxy",         v: (feat.f_cot_proxy || 0).toFixed(3) },
        ],
      },
      {
        icon: "🎯", title: "ENSEMBLE ACCURACY",
        value: `${accPct.toFixed(1)}%`,
        valueColor: accColor,
        rows: [
          { k: "Accuracy Basis",    v: accBasis },
          { k: "Conf. Proxy (diag)", v: `${proxyPct.toFixed(1)}%` },
          { k: "CatBoost DirAcc",   v: comps.catboost != null ? `${Number(comps.catboost).toFixed(1)}%` : `${(QP.catboost_accuracy_pct || 0).toFixed(1)}%` },
          { k: "Markov / Spectral", v: `${comps.markov != null ? Number(comps.markov).toFixed(1) : "—"}% / ${comps.spectral != null ? Number(comps.spectral).toFixed(1) : "—"}%` },
          { k: "Elliott DirAcc",    v: comps.elliott != null ? `${Number(comps.elliott).toFixed(1)}%` : "—" },
          { k: "Governance",        v: downN ? `downweight×${downN}` : "baseline" },
          { k: "Direction",         v: ice.direction || "—" },
          { k: "Markov State",      v: QP.markov_state || "—" },
        ],
      },
    ];

    el.innerHTML = cards.map((c) => `
      <div class="bal-qp-card">
        <div class="bal-qp-card-title">
          <span class="bal-qp-icon">${c.icon}</span>
          ${c.title}
        </div>
        <div class="bal-qp-value" style="color:${c.valueColor}">${c.value}</div>
        <div class="bal-qp-rows">
          ${c.rows.map((r) => `
            <div class="bal-qp-row">
              <span class="bal-qp-key">${r.k}</span>
              <span class="bal-qp-val" style="color:${FROST}">${r.v}</span>
            </div>`).join("")}
        </div>
      </div>`).join("");
  }

  // ── What-If Slider Wiring ────────────────────────────────────────────────────
  function wireWhatIfSliders() {
    const blockage = document.getElementById("bal-slider-blockage");
    const temp = document.getElementById("bal-slider-temp");
    const valB = document.getElementById("bal-val-blockage");
    const valT = document.getElementById("bal-val-temp");
    const badge = document.getElementById("bal-whatif-badge");
    const impact = document.getElementById("bal-whatif-impact");
    if (!blockage || !temp) return;

    // Restore from HUD state if available
    try {
      const saved = (window.__HUD_STATE__ && window.__HUD_STATE__.getWhatIf)
        ? window.__HUD_STATE__.getWhatIf()
        : null;
      if (saved) {
        if (saved.blockageDays != null) blockage.value = saved.blockageDays;
        if (saved.tempAnomalyC != null) temp.value = saved.tempAnomalyC;
      }
    } catch (_) { /* ignore */ }

    function sync() {
      scenario.blockageDays = Number(blockage.value) || 0;
      scenario.tempAnomalyC = Number(temp.value) || 0;
      if (valB) valB.textContent = String(scenario.blockageDays);
      if (valT) valT.textContent = scenario.tempAnomalyC.toFixed(1);
      const active = scenario.blockageDays > 0 || Math.abs(scenario.tempAnomalyC) > 0.01;
      if (badge) {
        badge.textContent = active ? "SCENARIO ACTIVE" : "BASELINE";
        badge.style.borderColor = active ? "rgba(255,179,0,0.45)" : "";
        badge.style.color = active ? AMBER : "";
      }
      const L = getActiveLssi();
      const sc = L.scenario || {};
      if (impact) {
        impact.innerHTML = active
          ? `Blockage <strong>${scenario.blockageDays}d</strong> · Temp <strong>${scenario.tempAnomalyC >= 0 ? "+" : ""}${scenario.tempAnomalyC.toFixed(1)}°C</strong> → `
            + `TTF <strong style="color:${AMBER}">€${(sc.implied_ttf || L.ttf_spot || 0).toFixed(2)}</strong> `
            + `(Δ €${((sc.temp_impact_eur || 0) + (sc.blockage_impact_eur || 0)).toFixed(2)}) · `
            + `delayed <strong>${fmt(sc.delayed_m3 || 0)}</strong> m³`
          : "Adjust sliders to stress-test LSSI elasticity &amp; volumetric capacity.";
      }
      try {
        if (window.__HUD_STATE__ && typeof window.__HUD_STATE__.setWhatIf === "function") {
          window.__HUD_STATE__.setWhatIf({
            blockageDays: scenario.blockageDays,
            tempAnomalyC: scenario.tempAnomalyC,
          });
        }
      } catch (_) { /* ignore */ }
      // Re-render affected panels only
      renderBalanceKPI();
      renderPanelA();
      renderPanelD();
    }

    blockage.addEventListener("input", sync);
    temp.addEventListener("input", sync);
    sync();
  }

  // ── Main Entry Point ──────────────────────────────────────────────────────────
  function bootBalance() {
    if (!document.getElementById("sheet-balance")) return;
    if (!window.__SENTINEL_PAYLOAD__) {
      console.warn("[BALANCE] payload not yet available — deferring");
      return;
    }
    try {
      wireWhatIfSliders();
      renderBalanceKPI();
      renderPanelA();
      renderPanelB();
      renderPanelC();
      renderPanelD();
      renderPanelE();
      console.info("[BALANCE] sheet rendered OK · fleet=" + (BAL.fleet_size || 0) + " · spoofed=" + (BAL.spoofed_excluded || 0));
    } catch (e) {
      console.error("[BALANCE] render error", e);
    }
  }

  function resizeAllCharts() {
    balCharts.forEach((ch) => { try { ch.resize(); } catch (_) {} });
  }

  // Boot: on DOMContentLoaded or when sheet becomes active
  document.addEventListener("DOMContentLoaded", function () {
    const sheet = document.documentElement.getAttribute("data-sheet") || "ais";
    if (sheet === "balance") {
      setTimeout(bootBalance, 60);
    }
  });

  // Also wire to switchTab event from sentinel_engine.js
  document.addEventListener("sentinelSheetChange", function (e) {
    if ((e.detail || {}).sheet === "balance") {
      setTimeout(function () {
        bootBalance();
        resizeAllCharts();
      }, 80);
    }
  });

  // Expose for manual reload / debugging
  window.__BALANCE__ = {
    boot: bootBalance,
    resize: resizeAllCharts,
    payload: BAL,
    scenario,
    applyScenario,
  };

})();
