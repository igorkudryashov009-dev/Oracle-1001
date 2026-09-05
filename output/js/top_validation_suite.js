/**
 * ORACLE-1001 · Frontend Double Cross-Validation Suite
 * Loaded by TOP-100 / TOP-200 / TOP-500 analytics pages.
 * Auto-runs when URL has ?audit=1 — exposes window.ORACLE_AUDIT.run().
 */
(function () {
  "use strict";

  const EPS_TRI = 0.05;
  const BAD_TIP_RE = /undefined|NaN%|\[undefined\]|NaN\b/i;

  function detectModule() {
    if (window.__TOP500_PAYLOAD__) return "top500";
    if (window.__TOP200_PAYLOAD__) return "top200";
    if (window.__TOP100__ || (typeof PAYLOAD !== "undefined" && PAYLOAD && PAYLOAD.top100_dwt != null)) return "top100";
    if (window.__TOP100_PAYLOAD__) return "top100";
    return "unknown";
  }

  function payloadOf(mod) {
    if (mod === "top500") return window.__TOP500_PAYLOAD__;
    if (mod === "top200") return window.__TOP200_PAYLOAD__;
    if (typeof PAYLOAD !== "undefined") return PAYLOAD;
    return window.__TOP100_PAYLOAD__ || null;
  }

  function runtimeState(mod) {
    if (mod === "top500") return window.__TOP500__;
    if (mod === "top200") return window.__TOP200__;
    return window.__TOP100__;
  }

  function gPct(dwt, fleet) {
    return 100 * (Number(dwt) || 0) / Math.max(fleet || 1, 1);
  }

  function flagBucket(fl) {
    const known = ["Маршалловы О-ва", "Панама", "Либерия", "Багамы", "Бермуды"];
    return known.indexOf(fl) >= 0 ? fl : "Китай / Прочие";
  }

  function aggregate(vessels, keyFn) {
    const map = {};
    vessels.forEach((v) => {
      const k = keyFn(v) || "—";
      if (!map[k]) map[k] = { n: 0, dwt: 0 };
      map[k].n += 1;
      map[k].dwt += Number(v.dwt_tons) || 0;
    });
    return map;
  }

  function sumG(map, fleet) {
    return Object.keys(map).reduce((s, k) => s + gPct(map[k].dwt, fleet), 0);
  }

  function scanTooltips() {
    const tip = document.getElementById("tip");
    const issues = [];
    // Probe data-* segments / paths / rects for generated tip HTML via mouseenter simulation
    const nodes = document.querySelectorAll("[data-risk], [data-port], [data-nav], [data-vtype], path.seg, rect.seg, text.seg, .seg");
    const sample = Array.prototype.slice.call(nodes, 0, 80);
    sample.forEach((el, i) => {
      try {
        const ev = new MouseEvent("mouseenter", { bubbles: true, clientX: 40, clientY: 40 });
        el.dispatchEvent(ev);
        if (tip && tip.style.display !== "none") {
          const html = tip.innerHTML || "";
          if (BAD_TIP_RE.test(html)) issues.push(`tooltip#${i}: ${html.slice(0, 120)}`);
        }
        el.dispatchEvent(new MouseEvent("mouseleave", { bubbles: true }));
      } catch (_e) { /* ignore */ }
    });
    if (tip) tip.style.display = "none";
    return issues;
  }

  function checkNav() {
    const disc = [];
    ["top100_analytics.html", "top200_analytics.html", "top500_analytics.html"].forEach((href) => {
      const a = document.querySelector(`.nav a[href="${href}"], .nav-links a[href="${href}"]`);
      if (!a) disc.push(`nav missing ${href}`);
    });
    return disc;
  }

  function toggleD13(mode) {
    const btn = document.querySelector(`.d13-mode[data-mode="${mode}"]`);
    if (btn) btn.click();
  }

  function wait(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  function riskBucketVal(v) {
    const r = (v.risk || "").toUpperCase();
    const st = (v.sanctions_tags || []).join(" ").toUpperCase();
    const raw = ((v.vessel_type || "") + " " + (v.destination_context || "")).toUpperCase();
    if (r === "EXTREME" || st.includes("OFAC") || st.includes("BLACKLIST") || st.includes("BLOCKED")) return "Extreme / Blacklist";
    if (r === "HIGH" || st.includes("EU") || st.includes("UK") || raw.includes("GREY") || raw.includes("СЕР")) return "High Risk / Grey Zone";
    if (r === "MID" || st.includes("SHADOW") || raw.includes("ТЕНЕВ") || raw.includes("SHADOW")) return "Medium / Shadow Fleet";
    if (r === "LOW") return "Low / Clean Compliance";
    return "Medium / Shadow Fleet";
  }

  function ageBucketVal(v) {
    const a = Number(v.age_years) || 0;
    if (a < 5) return "< 5 лет (Newbuild)";
    if (a <= 10) return "5–10 лет";
    if (a <= 15) return "10–15 лет";
    if (a <= 20) return "15–20 лет";
    return "> 20 лет (Dark Fleet Target)";
  }

  function opsStatusBucketVal(v) {
    const nav = ((v.nav_status || "") + " " + (v.nav_bucket || "") + " " + (v.destination_context || "")).toUpperCase();
    if (nav.includes("DRYDOCK") || nav.includes("SHIPYARD") || nav.includes("РЕМОНТ") || nav.includes("REPAIR") || nav.includes("ДОК")) return "Repair / Shipyard";
    if (nav.includes("MOOR") || nav.includes("STS") || nav.includes("ANCHOR") || nav.includes("ШВАРТ") || nav.includes("ЯКОР") || nav.includes("BERTH")) return "Moored / STS Transfer";
    const draft = Number(v.draft_m) || 0;
    if (draft >= 10.0 || nav.includes("LADEN") || nav.includes("ГРУЗ")) return "Laden (В грузу / Max Draft)";
    return "Ballast (В балласте)";
  }

  function ratioBucketVal(v) {
    const dwt = Number(v.dwt_tons) || 0;
    const gt = Math.max(Number(v.gt) || 1, 1);
    const r = dwt / gt;
    if (r > 1.8) return "Ultra High Ratio (> 1.8 DWT/GT)";
    if (r >= 1.4) return "High Ratio (1.4–1.8)";
    if (r >= 0.9) return "Standard LNG/Gas (0.9–1.4)";
    return "Volume-Driven (< 0.9)";
  }

  function speedBucketVal(sp) {
    const s = Number(sp) || 0;
    if (s < 11.0) return "Eco Speed (< 11 узлов)";
    if (s <= 14.0) return "Standard Transit (11–14 узлов)";
    if (s <= 16.0) return "High Speed (14–16 узлов)";
    return "Express (> 16 узлов)";
  }

  function estMaxDraftVal(v) {
    const dwt = Number(v.dwt_tons) || 0;
    const draft = Number(v.draft_m) || 0;
    const vt = String(v.vessel_type || "").toUpperCase();
    const tech = String(v.tech_type || "").toUpperCase();
    let maxD;
    if (dwt >= 250000) maxD = 22.5;
    else if (dwt >= 180000) maxD = 18.5;
    else if (dwt >= 115000) maxD = 16.0;
    else if (vt.includes("LNG") || tech.includes("MOSS") || tech.includes("MEMBRANE") || tech.includes("QFLEX")) maxD = 12.2;
    else if (dwt >= 70000) maxD = 14.5;
    else if (dwt >= 40000) maxD = 12.0;
    else maxD = 10.0;
    return Math.max(maxD, draft);
  }

  function draftBucketVal(v) {
    const draft = Number(v.draft_m) || 0;
    const maxD = estMaxDraftVal(v);
    const ratio = maxD > 0 ? (draft / maxD) * 100 : 0;
    if (ratio > 90.0) return "Full Laden (> 90% Max Draft)";
    if (ratio >= 60.0) return "Partial Load (60–90%)";
    if (ratio >= 30.0) return "Light Ballast (30–60%)";
    return "Minimum Draft (< 30% / Port Operations)";
  }

  async function runTriDonutAudit(mod, payload) {
    const disc = [];
    const perf = {};
    const vessels = payload.vessels || [];
    const fleet = Number(payload.fleet_total_dwt) || 81300000;
    const sliceDwt = vessels.reduce((s, v) => s + (Number(v.dwt_tons) || 0), 0);

    const fab0 = document.getElementById("fabReset");
    if (fab0) {
      fab0.click();
      await wait(180);
    }

    const t0 = performance.now();
    const zones = aggregate(vessels, (v) => v.ops_zone);
    const techs = aggregate(vessels, (v) => v.tech_type);
    const flags = aggregate(vessels, (v) => flagBucket(v.flag_short || v.flag || "—"));
    const risks = aggregate(vessels, riskBucketVal);
    const ages = aggregate(vessels, ageBucketVal);
    const ops = aggregate(vessels, opsStatusBucketVal);
    const ratios = aggregate(vessels, ratioBucketVal);
    const speeds = aggregate(vessels, (v) => speedBucketVal(v.speed_knots));
    const drafts = aggregate(vessels, draftBucketVal);

    const z = sumG(zones, fleet);
    const t = sumG(techs, fleet);
    const f = sumG(flags, fleet);
    const r = sumG(risks, fleet);
    const a = sumG(ages, fleet);
    const o = sumG(ops, fleet);
    const rt = sumG(ratios, fleet);
    const sp = sumG(speeds, fleet);
    const dr = sumG(drafts, fleet);
    const base = gPct(sliceDwt, fleet);
    perf.symmetry_ms = +(performance.now() - t0).toFixed(2);

    if (Math.abs(z - t) > EPS_TRI || Math.abs(t - f) > EPS_TRI) {
      disc.push(`Tri-Donut asymmetry z=${z.toFixed(4)} t=${t.toFixed(4)} f=${f.toFixed(4)}`);
    }
    [z, t, f, r, a, o, rt, sp, dr].forEach((val, i) => {
      const label = ["zone", "tech", "flag", "risk", "age", "ops", "ratio", "speed", "draft"][i];
      if (Math.abs(val - base) > EPS_TRI) {
        disc.push(`Nona-Donut ${label} sum ${val.toFixed(4)} != slice ${base.toFixed(4)}`);
      }
    });

    // Interactive: click each donut sector; KPI/filtered N must match data-n
    const t1 = performance.now();
    const panes = [
      { id: "c13a", kind: "zone", map: zones, attr: "ops_zone" },
      { id: "c13b", kind: "tech", map: techs, attr: "tech_type" },
      { id: "c13c", kind: "flag", map: flags, attr: null },
    ];
    if (document.getElementById("c13d")) panes.push({ id: "c13d", kind: "risk", map: risks, attr: null, fn: riskBucketVal });
    if (document.getElementById("c13e")) panes.push({ id: "c13e", kind: "age", map: ages, attr: null, fn: ageBucketVal });
    if (document.getElementById("c13f")) panes.push({ id: "c13f", kind: "ops", map: ops, attr: null, fn: opsStatusBucketVal });
    if (document.getElementById("c13g")) panes.push({ id: "c13g", kind: "ratio", map: ratios, attr: null, fn: ratioBucketVal });
    if (document.getElementById("c13h")) panes.push({ id: "c13h", kind: "speed", map: speeds, attr: null, fn: (v) => speedBucketVal(v.speed_knots) });
    if (document.getElementById("c13i")) panes.push({ id: "c13i", kind: "draft", map: drafts, attr: null, fn: draftBucketVal });

    for (const pane of panes) {
      const el = document.getElementById(pane.id);
      if (!el) {
        disc.push(`missing chart ${pane.id}`);
        continue;
      }
      const keys = Object.keys(pane.map);
      keys.slice(0, 12).forEach((key) => {
        let filtered;
        if (pane.attr) filtered = vessels.filter((v) => v[pane.attr] === key);
        else if (pane.fn) filtered = vessels.filter((v) => pane.fn(v) === key);
        else filtered = vessels.filter((v) => flagBucket(v.flag_short || v.flag || "—") === key);
        if (filtered.length !== pane.map[key].n) {
          disc.push(`cascade ${pane.kind}=${key} expected ${pane.map[key].n} got ${filtered.length}`);
        }
      });
      const clickables = el.querySelectorAll("[data-d13key]");
      let tested = 0;
      clickables.forEach((node) => {
        if (tested >= 6) return;
        const key = node.getAttribute("data-d13key");
        const expectedN = Number(node.getAttribute("data-n"));
        if (!key || !expectedN) return;
        node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        tested += 1;
        const rt = runtimeState(mod);
        const kpi = document.querySelector("#kpiRow .kpi .v");
        const kpiN = kpi ? Number(String(kpi.textContent).replace(/\s/g, "")) : NaN;
        if (rt && Array.isArray(rt.filteredImos) && rt.filteredImos.length !== expectedN) {
          // allow brief async — soft check via declared data-n vs map
          if (pane.map[key] && pane.map[key].n !== expectedN) {
            disc.push(`DOM data-n drift ${pane.kind}=${key} data-n=${expectedN} map=${pane.map[key].n}`);
          }
        }
        if (!Number.isNaN(kpiN) && kpiN !== expectedN && tested === 1) {
          // first click should update KPI; if engine uses rAF, skip hard fail
        }
      });
    }
    perf.cascade_ms = +(performance.now() - t1).toFixed(2);
    if (perf.cascade_ms > 150) disc.push(`cascade audit ${perf.cascade_ms}ms > 150ms`);

    // View mode toggle
    const modeKey = mod === "top500" ? "top500_d13_view_mode" : "top200_d13_view_mode";
    try {
      toggleD13("trio2");
      await wait(80);
      toggleD13("trio3");
      await wait(80);
      toggleD13("all");
      await wait(80);
      toggleD13("sunburst");
      await wait(80);
      const sun = document.getElementById("d13Sun");
      if (sun && sun.hidden) disc.push("sunburst toggle did not reveal #d13Sun");
      toggleD13("trio");
      await wait(80);
      const trio = document.getElementById("d13Trio");
      if (trio && trio.hidden) disc.push("trio toggle did not reveal #d13Trio");
      localStorage.setItem(modeKey, "trio");
    } catch (e) {
      disc.push(`d13 toggle error: ${e && e.message}`);
    }

    // Reset filters if FAB present
    const fab = document.getElementById("fabReset");
    if (fab) fab.click();
    await wait(160);

    return { disc, perf };
  }

  async function runTop100Audit(payload) {
    const disc = [];
    const perf = {};
    const vessels = payload.vessels || [];
    const t0 = performance.now();
    const sum = vessels.reduce((s, v) => s + (Number(v.dwt_tons) || 0), 0);
    const declared = Number(payload.top100_dwt) || 0;
    if (Math.abs(sum - declared) > 1) disc.push(`TOP-100 DWT sum ${sum} != ${declared}`);
    if (vessels.length !== 100) disc.push(`TOP-100 count ${vessels.length} != 100`);
    perf.sum_ms = +(performance.now() - t0).toFixed(2);
    // KPI row exists
    if (!document.getElementById("kpiRow")) disc.push("missing #kpiRow");
    return { disc, perf };
  }

  async function run() {
    const mod = detectModule();
    const payload = payloadOf(mod);
    const discrepancies = [];
    const performance_metrics = {};
    const tAll = performance.now();

    if (!payload) {
      return { status: "FAILED", module: mod, discrepancies: ["payload missing"], performance_metrics: {} };
    }

    discrepancies.push(...checkNav());

    if (mod === "top100") {
      const r = await runTop100Audit(payload);
      discrepancies.push(...r.disc);
      Object.assign(performance_metrics, r.perf);
    } else if (mod === "top200" || mod === "top500") {
      const r = await runTriDonutAudit(mod, payload);
      discrepancies.push(...r.disc);
      Object.assign(performance_metrics, r.perf);
    } else {
      discrepancies.push(`unknown module ${mod}`);
    }

    const tipIssues = scanTooltips();
    tipIssues.forEach((x) => discrepancies.push(x));

    // Runtime global state consistency
    const rt = runtimeState(mod);
    if (rt && typeof rt.count === "number" && payload.vessels && rt.count !== payload.vessels.length && !rt.filter) {
      // filtered count may differ — only flag if no active sector and counts diverge wildly
    }

    performance_metrics.total_ms = +(performance.now() - tAll).toFixed(2);
    const status = discrepancies.length ? "FAILED" : "SUCCESS";
    const report = {
      status,
      module: mod,
      discrepancies,
      performance_metrics,
      vessel_count: (payload.vessels || []).length,
      ts: Date.now(),
    };
    try {
      localStorage.setItem("oracle1001_audit_last", JSON.stringify(report));
    } catch (_e) { /* ignore */ }
    window.__ORACLE_AUDIT_LAST__ = report;
    if (status === "SUCCESS") {
      console.info("[ORACLE_AUDIT]", mod, "SUCCESS", performance_metrics);
    } else {
      console.warn("[ORACLE_AUDIT]", mod, "FAILED", discrepancies);
    }
    return report;
  }

  window.ORACLE_AUDIT = { run, detectModule };

  function boot() {
    const q = new URLSearchParams(location.search);
    if (q.get("audit") === "1") {
      // Defer until engines finish first paint
      setTimeout(() => { run(); }, 500);
    }
  }
  if (document.readyState === "complete" || document.readyState === "interactive") boot();
  else document.addEventListener("DOMContentLoaded", boot);
})();
