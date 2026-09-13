/**
 * UAIP Visualizer 2026 — Quant Risk & Regime Chart Visualizer.
 * Dual Truth & Strict Caveat Enforcement:
 *   If is_synthetic === true, applies a prominent, un-dismissible HUD overlay:
 *   "SYNTHETIC DEMO DATA · NOT WIRED TO LIVE MODEL"
 */

export class UAIPQuantVisualizer {
  /**
   * @param {string} coneCanvasId - Canvas ID for TTF Confidence Cone
   * @param {string} regimeCanvasId - Canvas ID for Bayesian Regime Doughnut/Radar
   * @param {string} [containerId] - Wrapper element ID for synthetic overlay injection
   */
  constructor(coneCanvasId, regimeCanvasId, containerId = "uaip-quant-container") {
    this.coneCanvas = document.getElementById(coneCanvasId);
    this.regimeCanvas = document.getElementById(regimeCanvasId);
    this.container = document.getElementById(containerId) || (this.coneCanvas ? this.coneCanvas.parentElement : null);
    this.coneChart = null;
    this.regimeChart = null;
    this.overlayElement = null;
  }

  /**
   * Inject or toggle the mandatory synthetic warning overlay.
   * @param {boolean} isSynthetic
   * @param {Array<string>} [syntheticComponents=[]]
   * @param {string} [caveat=""]
   * @param {boolean} [productionActionable=false]
   * @param {string|null} [blockedReason=null]
   * @param {string|null} [strategyId=null]
   */
  updateSyntheticOverlay(
    isSynthetic,
    syntheticComponents = [],
    caveat = "",
    productionActionable = false,
    blockedReason = null,
    strategyId = null
  ) {
    if (!this.container) return;

    // Ensure relative positioning on the container so overlay locks onto it
    if (getComputedStyle(this.container).position === "static") {
      this.container.style.position = "relative";
    }

    const existingOverlay = this.container.querySelector(".uaip-synthetic-overlay");

    if (!isSynthetic) {
      if (existingOverlay) {
        existingOverlay.remove();
      }
      return;
    }

    if (existingOverlay) {
      this.overlayElement = existingOverlay;
    } else {
      this.overlayElement = document.createElement("div");
      this.overlayElement.className = "uaip-synthetic-overlay";
      this.container.appendChild(this.overlayElement);
    }

    const compList = syntheticComponents.length > 0 ? syntheticComponents.join(", ") : "returns_sharpe_cvar";
    const actionableBadge = productionActionable
      ? `<span class="badge-actionable-ok">ACTIONABLE: OK</span>`
      : `<span class="badge-actionable-blocked">PROD ACTIONABLE: FALSE (BLOCKED)</span>`;

    const strategyBadge = strategyId
      ? `<span style="color:#38bdf8;font-weight:600">RECOMMENDED: ${strategyId}</span>`
      : `<span style="color:#f87171;font-weight:600;letter-spacing:0.5px">NO ACTIONABLE STRATEGY — ${blockedReason || "SAMPLE N < 30"}</span>`;

    this.overlayElement.innerHTML = `
      <div class="uaip-overlay-content">
        <div class="uaip-warning-header">
          <span class="uaip-warning-icon">⚠</span>
          <span class="uaip-warning-title">SYNTHETIC DEMO DATA · NOT WIRED TO LIVE MODEL</span>
        </div>
        <div class="uaip-warning-body">
          <div class="uaip-warning-row">
            <span class="uaip-label">Synthetic Components:</span>
            <span class="uaip-val">[ ${compList} ]</span>
          </div>
          <div class="uaip-warning-row">
            <span class="uaip-label">Dual Gate Status:</span>
            ${actionableBadge}
          </div>
          <div class="uaip-warning-row">
            <span class="uaip-label">Strategy Recommendation:</span>
            <span class="uaip-val">${strategyBadge}</span>
          </div>
          ${caveat ? `<div class="uaip-warning-caveat">${caveat}</div>` : ""}
        </div>
        <div class="uaip-warning-footer">
          DESK NORMATIVE ENVELOPE / PAPER LEDGER N &lt; 30 · STATISTICAL INFERENCE UNQUALIFIED
        </div>
      </div>
    `;

    // Apply inline critical CSS for the overlay in case external stylesheet is pending
    Object.assign(this.overlayElement.style, {
      position: "absolute",
      top: "0",
      left: "0",
      width: "100%",
      height: "100%",
      zIndex: "50",
      background: "rgba(10, 15, 29, 0.82)",
      backdropFilter: "blur(4px)",
      WebkitBackdropFilter: "blur(4px)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      padding: "20px",
      boxSizing: "border-box",
      pointerEvents: "auto",
      border: "1px solid rgba(239, 68, 68, 0.45)",
      borderRadius: "6px",
    });

    const inner = this.overlayElement.querySelector(".uaip-overlay-content");
    if (inner) {
      Object.assign(inner.style, {
        background: "rgba(15, 23, 42, 0.95)",
        border: "1px solid #ef4444",
        boxShadow: "0 0 25px rgba(239, 68, 68, 0.35)",
        borderRadius: "6px",
        padding: "16px 20px",
        maxWidth: "520px",
        width: "90%",
        color: "#f87171",
        fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
        textAlign: "center",
      });
    }
  }

  /**
   * Render or update confidence cone.
   * @param {Array<string>} dates
   * @param {Array<number>} p10
   * @param {Array<number>} p50
   * @param {Array<number>} p90
   * @param {Object} [meta={}]
   */
  renderConfidenceCone(dates, p10, p50, p90, meta = {}) {
    if (!this.coneCanvas) return;
    const ctx = this.coneCanvas.getContext("2d");

    if (this.coneChart) {
      this.coneChart.destroy();
    }

    const fillGradient = ctx.createLinearGradient(0, 0, 0, this.coneCanvas.height || 300);
    fillGradient.addColorStop(0, "rgba(59, 130, 246, 0.28)");
    fillGradient.addColorStop(1, "rgba(59, 130, 246, 0.02)");

    this.coneChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: dates,
        datasets: [
          {
            label: "P90 (Upper Bound)",
            data: p90,
            borderColor: "rgba(59, 130, 246, 0.4)",
            borderDash: [5, 5],
            pointRadius: 0,
            fill: false,
          },
          {
            label: `P50 (Median Trajectory) [${meta.quantiles_source || "CatBoost"}]`,
            data: p50,
            borderColor: "#38bdf8",
            borderWidth: 2.5,
            pointRadius: 3,
            pointBackgroundColor: "#0284c7",
            fill: "-1",
            backgroundColor: fillGradient,
          },
          {
            label: "P10 (Lower Bound)",
            data: p10,
            borderColor: "rgba(59, 130, 246, 0.4)",
            borderDash: [5, 5],
            pointRadius: 0,
            fill: "-1",
            backgroundColor: fillGradient,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 400 },
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { grid: { color: "rgba(255, 255, 255, 0.05)" }, ticks: { color: "#94a3b8" } },
          y: {
            title: { display: true, text: "TTF EUR/MWh", color: "#64748b" },
            grid: { color: "rgba(255, 255, 255, 0.05)" },
            ticks: { color: "#94a3b8" },
          },
        },
        plugins: {
          legend: { labels: { color: "#cbd5e1", font: { family: "Manrope" } } },
          tooltip: {
            backgroundColor: "rgba(15, 23, 42, 0.9)",
            borderColor: "rgba(56, 189, 248, 0.3)",
            borderWidth: 1,
          },
        },
      },
    });

    // Check synthetic state from meta
    if (meta.is_synthetic !== undefined) {
      this.updateSyntheticOverlay(
        Boolean(meta.is_synthetic),
        meta.synthetic_components || [],
        meta.sample_size_caveat || "",
        Boolean(meta.production_actionable),
        meta.blocked_reason || null,
        meta.recommended_strategy_id || null
      );
    }
  }

  /**
   * Render Strategy recommendation banner or element.
   * If strategyId is null/undefined, explicitly renders:
   * "NO ACTIONABLE STRATEGY — {blocked_reason}"
   * @param {HTMLElement|string} containerOrId
   * @param {string|null} [strategyId=null]
   * @param {string|null} [strategyName=null]
   * @param {string|null} [blockedReason=null]
   */
  renderStrategy(containerOrId, strategyId = null, strategyName = null, blockedReason = null) {
    const el = typeof containerOrId === "string" ? document.getElementById(containerOrId) : containerOrId;
    if (!el) return;

    if (!strategyId) {
      const reason = blockedReason || "insufficient_sample";
      el.innerHTML = `
        <div class="uaip-strategy-card uaip-strategy-blocked" style="border:1px solid #ef4444; background:rgba(239,68,68,0.12); padding:10px 14px; border-radius:6px; color:#fca5a5; font-family:'JetBrains Mono', monospace; font-size:12px; margin-bottom:12px;">
          <div style="font-weight:700; letter-spacing:0.5px; display:flex; align-items:center; gap:8px;">
            <span>⛔</span>
            <span>NO ACTIONABLE STRATEGY — ${reason}</span>
          </div>
          <div style="font-size:10px; color:#f87171; margin-top:4px; opacity:0.85;">
            Production execution locked by Dual Deploy Gate (production_actionable = false).
          </div>
        </div>
      `;
    } else {
      el.innerHTML = `
        <div class="uaip-strategy-card uaip-strategy-active" style="border:1px solid #0284c7; background:rgba(14,165,233,0.12); padding:10px 14px; border-radius:6px; color:#7dd3fc; font-family:'JetBrains Mono', monospace; font-size:12px; margin-bottom:12px;">
          <div style="font-weight:700; letter-spacing:0.5px;">
            <span style="background:#0284c7; color:#fff; padding:2px 6px; border-radius:3px; margin-right:6px;">STRATEGY ${strategyId}</span>
            <span>${strategyName || "Active Strategy"}</span>
          </div>
        </div>
      `;
    }
  }

  /**
   * Render Bayesian regime distribution.
   * @param {Object} probabilities - { regime_name: probability }
   */
  renderRegimeRadar(probabilities) {
    if (!this.regimeCanvas) return;
    const ctx = this.regimeCanvas.getContext("2d");

    if (this.regimeChart) {
      this.regimeChart.destroy();
    }

    const labels = Object.keys(probabilities);
    const dataValues = Object.values(probabilities);

    this.regimeChart = new Chart(ctx, {
      type: "doughnut",
      data: {
        labels: labels,
        datasets: [
          {
            data: dataValues,
            backgroundColor: ["#10b981", "#38bdf8", "#ef4444"],
            borderWidth: 2,
            borderColor: "#0f172a",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "bottom", labels: { color: "#cbd5e1" } },
        },
        cutout: "70%",
      },
    });
  }
}
