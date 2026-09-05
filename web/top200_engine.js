/**
 * ORACLE-1001 · TOP-200 Analytics Engine
 * All chart metrics use GLOBAL % of fleet DWT (FLEET_DWT ≈ 81.3 млн т).
 * Absolute vessel counts / tons appear only in tooltips.
 */
(function () {
  "use strict";

  const PAYLOAD = window.__TOP200_PAYLOAD__;
  if (!PAYLOAD) {
    console.error("TOP-200: PAYLOAD missing");
    return;
  }

  const ALL = PAYLOAD.vessels;
  const REST = PAYLOAD.rest_sample || [];
  const FLEET_DWT = Math.max(PAYLOAD.fleet_total_dwt || 81300000, 1);
  const TOP200_DWT = PAYLOAD.top200_dwt;
  const TOP200_SHARE = PAYLOAD.top200_share_pct;
  const PROV = PAYLOAD.provenance || { pct: {}, cells: {} };
  const AXIS_Y = "ГЛОБАЛЬНЫЙ % DWT (от 81.3 М т)";

  const COLORS = {
    LNG: "#00f2fe", LPG: "#10b981", CRUDE: "#f59e0b", PRODUCT: "#a78bfa",
    TANKER: "#3b82f6", OTHER: "#8aa4bf",
    EXTREME: "#ef4444", HIGH: "#f59e0b", MID: "#3b82f6", LOW: "#10b981", UNK: "#6b7fa0",
    OSINT: "#10b981", AIS: "#00f2fe", SYNTH: "#ef4444", KNN: "#D4AF37",
    DRYDOCK: "#a78bfa", ANCHOR: "#f59e0b", SEA: "#00f2fe", ETA: "#D4AF37",
  };
  const PAL = ["#00f2fe", "#D4AF37", "#10b981", "#f59e0b", "#3b82f6", "#a78bfa", "#ef4444", "#ec4899", "#14b8a6", "#84cc16"];
  const VTYPE_RU = { LNG: "СПГ-газовозы", LPG: "СУГ-газовозы", CRUDE: "Нефтеналивные", PRODUCT: "Продуктовозы", TANKER: "Танкеры", OTHER: "Прочие" };
  const RISK_RU = { EXTREME: "Крайний", HIGH: "Высокий", MID: "Средний", LOW: "Низкий", UNK: "Без метки" };
  const NAV_RU = { DRYDOCK: "Док / ремонт", ANCHOR: "Якорь / швартовка", SEA: "Морской переход", ETA: "ETA / ожидание" };
  const FILTER_RU = {
    vtype: "Тип", flag: "Флаг", risk: "Риск", region: "Регион", port: "Порт",
    ageBucket: "Возраст", nav: "Навигация", imo: "IMO", preset: "Пресет",
    opsZone: "Зона", techType: "Технотип",
    d13Risk: "Профиль риска", d13Age: "Возрастной срез", d13Ops: "Статус / осадка",
    d13Ratio: "DWT/GT Ratio", d13Speed: "Скоростной режим", d13Draft: "Осадка %",
  };
  const OPS_ZONE_RU = {
    ME_QATAR: "Ближний Восток / Катар",
    ATLANTIC_US: "Атлантика и Мексиканский залив",
    ARCTIC: "Арктика / СМП",
    APAC: "Азиатско-Тихоокеанский регион",
    OIL_OTHER: "Прочие танкерные акватории",
  };
  const TECH_TYPE_RU = {
    QFLEX_QMAX: "Q-Flex / Q-Max (Мега-СПГ)",
    MEMBRANE: "Membrane Conventional (160-174k м³)",
    MOSS: "Moss Rosenberg (Сферические танкеры)",
    FLNG_FSRU: "FLNG / FSRU (Плавучие заводы/терминалы)",
    VLCC_SUEZ: "VLCC / Suezmax (Сверхтяжелая нефть)",
  };
  const ZONE_COLORS = {
    ME_QATAR: "#f59e0b", ATLANTIC_US: "#00f2fe", ARCTIC: "#a78bfa",
    APAC: "#10b981", OIL_OTHER: "#8aa4bf",
  };
  const TECH_COLORS = {
    QFLEX_QMAX: "#D4AF37", MEMBRANE: "#00f2fe", MOSS: "#3b82f6",
    FLNG_FSRU: "#ec4899", VLCC_SUEZ: "#f59e0b",
  };
  const GREY_FLAG_RE = /comor|комор|gabon|габон|palau|палау|cameroon|камерун|eswatini|свазиленд|cook|кука|belize|белиз|mali|мали|togo|того|tuvalu|тувалу/i;

  function fmtTons(n) { return Math.round(n || 0).toLocaleString("ru-RU") + " т"; }
  function fmtMln(n) {
    const m = (Number(n) || 0) / 1e6;
    return (Math.abs(m) >= 10 ? m.toFixed(1) : m.toFixed(2)) + " млн т";
  }
  function fmtPct(n, d) { return (Number(n) || 0).toFixed(d == null ? 2 : d) + "%"; }
  /** Global share of fleet DWT */
  function gPct(dwt) { return 100 * (Number(dwt) || 0) / FLEET_DWT; }
  function localShare(dwt, sliceDwt) { return sliceDwt > 0 ? 100 * (Number(dwt) || 0) / sliceDwt : 0; }
  function shortPort(p) {
    if (!p) return "—";
    const m = String(p).match(/^([^,(]+)/);
    return (m ? m[1] : p).trim().slice(0, 26);
  }
  function wordShips(n) {
    const k = n % 100, m = n % 10;
    if (k > 10 && k < 20) return "судов";
    if (m === 1) return "судно";
    if (m >= 2 && m <= 4) return "судна";
    return "судов";
  }
  function dwtSum(rows) { return rows.reduce((s, v) => s + (v.dwt_tons || 0), 0); }

  function showTip(e, html) {
    const tip = document.getElementById("tip");
    tip.innerHTML = html;
    tip.style.display = "block";
    tip.style.left = Math.min(window.innerWidth - 320, e.clientX + 12) + "px";
    tip.style.top = Math.min(window.innerHeight - 100, e.clientY + 12) + "px";
  }
  function hideTip() { document.getElementById("tip").style.display = "none"; }

  function tipBlock({ title, n, dwt, sliceDwt, extra, risk }) {
    const riskLabel = RISK_RU[risk] || risk || null;
    const topShare = localShare(dwt, TOP200_DWT);
    return `<b>${title}</b><br>` +
      `<span class="gold">Глобальный DWT:</span> <b>${fmtPct(gPct(dwt))}</b> (от 81.3 М т)<br>` +
      `Доля в ТОП-200: <b>${fmtPct(topShare, 1)}</b>` +
      (sliceDwt > 0 ? ` · срез ${fmtPct(localShare(dwt, sliceDwt), 1)}` : "") + `<br>` +
      `Судов: <b>${n != null ? n : "—"}</b>` +
      (riskLabel ? `<br>OSINT Risk: <b>${riskLabel}</b>` : "") +
      (extra ? `<br><span class="muted">${extra}</span>` : "");
  }

  function tipVessel(v, sliceDwt) {
    return tipBlock({
      title: `${v.vessel_name} (IMO ${v.imo})`,
      n: 1,
      dwt: v.dwt_tons,
      sliceDwt: sliceDwt || dwtSum(CF.filtered()),
      risk: v.risk,
      extra: `Тип: ${VTYPE_RU[v.vtype] || v.vtype} · Флаг: ${v.flag_short || v.flag || "—"} · fill ${fmtPct(v.fill_pct, 1)}`,
    });
  }

  function dominantRisk(rows) {
    const c = {};
    (rows || []).forEach((v) => { c[v.risk] = (c[v.risk] || 0) + 1; });
    const top = Object.entries(c).sort((a, b) => b[1] - a[1])[0];
    return top ? top[0] : "UNK";
  }

  function emptyOverlay(el) {
    if (!el) return;
    el.innerHTML = `<div class="empty-osint" role="status"><span>Данные отсутствуют для данного среза OSINT</span></div>`;
  }

  function safeChart(fn) {
    return function (rows, el) {
      if (!el) return;
      try {
        if (!rows || !rows.length) { emptyOverlay(el); return; }
        fn(rows, el);
      } catch (err) {
        console.error("[TOP-200 chart]", err);
        emptyOverlay(el);
      }
    };
  }

  function svgBox(el) {
    const r = el.getBoundingClientRect();
    return { w: Math.max(280, r.width || 400), h: Math.max(220, r.height || 240) };
  }
  function arcPath(cx, cy, rOut, rIn, a0, a1) {
    const large = (a1 - a0) > Math.PI ? 1 : 0;
    const x1 = cx + rOut * Math.cos(a0), y1 = cy + rOut * Math.sin(a0);
    const x2 = cx + rOut * Math.cos(a1), y2 = cy + rOut * Math.sin(a1);
    const ix1 = cx + rIn * Math.cos(a1), iy1 = cy + rIn * Math.sin(a1);
    const ix2 = cx + rIn * Math.cos(a0), iy2 = cy + rIn * Math.sin(a0);
    return `M${x1} ${y1} A${rOut} ${rOut} 0 ${large} 1 ${x2} ${y2} L${ix1} ${iy1} A${rIn} ${rIn} 0 ${large} 0 ${ix2} ${iy2} Z`;
  }
  function axisYLabel(svgW, svgH) {
    return `<text class="axis-caption" x="12" y="${svgH / 2}" transform="rotate(-90 12 ${svgH / 2})" text-anchor="middle">${AXIS_Y}</text>`;
  }

  class CrossFilterManager {
    constructor(vessels) {
      this.all = vessels;
      this.filters = {
        vtype: null, flag: null, risk: null, region: null, port: null,
        ageBucket: null, nav: null, imo: null, preset: null,
        opsZone: null, techType: null,
      };
      this._raf = 0;
      this._presetFn = null;
    }
    set(key, val) {
      if (this.filters[key] === val) this.filters[key] = null;
      else this.filters[key] = val;
      if (key !== "preset") { this.filters.preset = null; this._presetFn = null; syncPresetButtons(); }
      this.schedule();
    }
    setMany(obj) {
      Object.assign(this.filters, obj);
      this.filters.preset = null; this._presetFn = null; syncPresetButtons();
      this.schedule();
    }
    applyPreset(id, fn) {
      if (this.filters.preset === id) { this.clear(); return; }
      Object.keys(this.filters).forEach((k) => { this.filters[k] = null; });
      this.filters.preset = id;
      this._presetFn = fn;
      syncPresetButtons();
      this.schedule();
    }
    clear(delayMs) {
      Object.keys(this.filters).forEach((k) => { this.filters[k] = null; });
      this._presetFn = null;
      syncPresetButtons();
      this.schedule(delayMs == null ? 0 : delayMs);
    }
    /** Публичный API каскада Tri-Donut → Д16–Д27 */
    applyCrossFilter() {
      this.schedule(0);
    }
    active() { return Object.values(this.filters).some(Boolean); }
    filtered() {
      let rows = this.all;
      if (this._presetFn) rows = rows.filter(this._presetFn);
      const f = this.filters;
      return rows.filter((v) => {
        if (f.vtype && v.vtype !== f.vtype) return false;
        if (f.opsZone && v.ops_zone !== f.opsZone) return false;
        if (f.techType && v.tech_type !== f.techType) return false;
        if (f.flag && v.flag_short !== f.flag && !(v.flag || "").toLowerCase().includes(String(f.flag).toLowerCase())) return false;
        if (f.risk && v.risk !== f.risk) return false;
        if (f.d13Risk && riskBucketD13(v) !== f.d13Risk) return false;
        if (f.d13Age && ageBucketD13(v.age_years) !== f.d13Age) return false;
        if (f.d13Ops && opsStatusBucketD13(v) !== f.d13Ops) return false;
        if (f.d13Ratio && ratioBucketD13(v) !== f.d13Ratio) return false;
        if (f.d13Speed && speedBucketD13(v.speed_knots) !== f.d13Speed) return false;
        if (f.d13Draft && draftBucketD13(v) !== f.d13Draft) return false;
        if (f.region && v.region !== f.region) return false;
        if (f.port && shortPort(v.destination_port) !== f.port) return false;
        if (f.ageBucket && v.age_bucket !== f.ageBucket) return false;
        if (f.nav && v.nav_bucket !== f.nav) return false;
        if (f.imo && String(v.imo) !== String(f.imo)) return false;
        return true;
      });
    }
    schedule(delayMs) {
      cancelAnimationFrame(this._raf);
      if (this._delayT) clearTimeout(this._delayT);
      const go = () => {
        this._raf = requestAnimationFrame(() => renderAll(true));
      };
      if (delayMs && delayMs > 0) this._delayT = setTimeout(go, delayMs);
      else go();
    }
    broadcast(rows) {
      const imos = rows.map((v) => String(v.imo));
      const payload = { imos, ts: Date.now(), source: "top200", sector: this.active(), filters: Object.assign({}, this.filters) };
      try { localStorage.setItem("oracle1001_cross_filter", JSON.stringify(payload)); } catch (_e) {}
      window.__TOP200__ = {
        count: this.all.length, fleetDwt: FLEET_DWT, topDwt: TOP200_DWT, share: TOP200_SHARE,
        filteredImos: imos, filter: Object.assign({}, this.filters), provenance: PROV,
        sliceGlobalPct: gPct(dwtSum(rows)),
      };
    }
  }
  const CF = new CrossFilterManager(ALL);

  function syncPresetButtons() {
    document.querySelectorAll(".preset").forEach((b) => {
      b.classList.toggle("on", b.dataset.preset === CF.filters.preset);
    });
  }

  function renderKPI(rows) {
    const dwt = dwtSum(rows);
    const avgAge = rows.length ? rows.reduce((s, v) => s + v.age_years, 0) / rows.length : 0;
    const avgFill = rows.length ? rows.reduce((s, v) => s + v.fill_pct, 0) / rows.length : 0;
    const osintPct = Number(PROV.pct.OSINT || 0);
    const synthPct = Math.max(0, +(100.0 - osintPct).toFixed(1));
    document.getElementById("kpiRow").innerHTML = `
      <div class="kpi"><div class="k">Суда в срезе</div><div class="v">${rows.length}</div><div class="s">из ТОП-200</div></div>
      <div class="kpi"><div class="k">Суммарный DWT</div><div class="v">${fmtMln(dwt)}</div><div class="s">${fmtPct(gPct(dwt))} от ${fmtMln(FLEET_DWT)}</div></div>
      <div class="kpi"><div class="k">Глобальный вклад</div><div class="v">${fmtPct(gPct(dwt))}</div><div class="s">${AXIS_Y}</div></div>
      <div class="kpi"><div class="k">AVG Fill Rate</div><div class="v">${fmtPct(avgFill, 1)}</div><div class="s">20 колонок ТЗ</div></div>
      <div class="kpi"><div class="k">Средний возраст</div><div class="v">${Math.round(avgAge)}</div><div class="s">лет</div></div>
      <div class="kpi"><div class="k">Провенанс OSINT</div><div class="v">${fmtPct(osintPct, 1)}</div><div class="s">Synth OSINT: ${fmtPct(synthPct, 1)}</div></div>`;
    document.querySelectorAll(".kpi").forEach((k) => { k.classList.add("pulse"); setTimeout(() => k.classList.remove("pulse"), 280); });

    const fb = document.getElementById("filterBar");
    const chips = [];
    Object.entries(CF.filters).forEach(([k, v]) => {
      if (!v) return;
      const label = k === "vtype" ? (VTYPE_RU[v] || v)
        : k === "risk" ? (RISK_RU[v] || v)
        : k === "nav" ? (NAV_RU[v] || v)
        : k === "opsZone" ? (ZONE_SHORT[v] || OPS_ZONE_RU[v] || v)
        : k === "techType" ? (TECH_SHORT[v] || TECH_TYPE_RU[v] || v)
        : k === "preset" && v === "d13_flag_other" ? "Китай / Прочие"
        : v;
      const isD13 = k === "opsZone" || k === "techType" || k === "flag" ||
                    k === "d13Risk" || k === "d13Age" || k === "d13Ops" ||
                    k === "d13Ratio" || k === "d13Speed" || k === "d13Draft" ||
                    (k === "preset" && v === "d13_flag_other");
      const prefix = isD13 ? "Активный фильтр" : (FILTER_RU[k] || k);
      chips.push(`<span class="chip-f">${prefix}: ${label}<button data-k="${k}" title="Сбросить">×</button></span>`);
    });
    fb.innerHTML = chips.length
      ? chips.join("")
      : `<span style="font-size:11px;color:var(--muted)">Все метрики — ${AXIS_Y}. Клик по элементу → кросс-фильтр Д13–Д27</span>`;
    fb.querySelectorAll("button[data-k]").forEach((b) => {
      b.onclick = () => {
        const chip = b.closest(".chip-f");
        const isActiveFilterChip = chip && chip.textContent.indexOf("Активный фильтр") >= 0;
        if (b.dataset.k === "preset" || isActiveFilterChip) CF.clear(150);
        else { CF.filters[b.dataset.k] = null; CF.schedule(0); }
      };
    });
    document.getElementById("fabReset").classList.toggle("on", CF.active());
  }


  // ── D13 · Nona-Donut (Д13.1–13.9) + hierarchical Sunburst ──────────────────
  let d13Mode = (() => {
    try {
      const m = localStorage.getItem("top200_d13_view_mode");
      return ["sunburst", "trio", "trio2", "trio3", "all"].includes(m) ? m : "trio";
    } catch (_e) { return "trio"; }
  })();
  let d13LastRows = [];
  const d13CenterBaseline = {};
  const ZONE_SHORT = {
    ME_QATAR: "Ближний Восток", ATLANTIC_US: "Атлантика/США", ARCTIC: "Арктика/СМП",
    APAC: "АТР", OIL_OTHER: "Прочие маршруты",
  };
  const TECH_SHORT = {
    QFLEX_QMAX: "Q-Flex/Q-Max", MEMBRANE: "Membrane", MOSS: "Moss",
    FLNG_FSRU: "FLNG/FSRU", VLCC_SUEZ: "VLCC/Crude",
  };
  const CHART_KIND = {
    c13a: "zone", c13b: "tech", c13c: "flag",
    c13d: "risk", c13e: "age", c13f: "ops",
    c13g: "ratio", c13h: "speed", c13i: "draft",
  };
  const ALL_D13_IDS = ["c13a", "c13b", "c13c", "c13d", "c13e", "c13f", "c13g", "c13h", "c13i"];

  const RISK_D13_COLORS = {
    "Extreme / Blacklist": "#ef4444",
    "High Risk / Grey Zone": "#f97316",
    "Medium / Shadow Fleet": "#eab308",
    "Low / Clean Compliance": "#10b981",
  };
  const AGE_D13_COLORS = {
    "< 5 лет (Newbuild)": "#00f2fe",
    "5–10 лет": "#3b82f6",
    "10–15 лет": "#8b5cf6",
    "15–20 лет": "#ec4899",
    "> 20 лет (Dark Fleet Target)": "#ef4444",
  };
  const OPS_D13_COLORS = {
    "Laden (В грузу / Max Draft)": "#00f2fe",
    "Ballast (В балласте)": "#38bdf8",
    "Moored / STS Transfer": "#f59e0b",
    "Repair / Shipyard": "#a855f7",
  };
  const RATIO_D13_COLORS = {
    "Ultra High Ratio (> 1.8 DWT/GT)": "#ec4899",
    "High Ratio (1.4–1.8)": "#f59e0b",
    "Standard LNG/Gas (0.9–1.4)": "#00f2fe",
    "Volume-Driven (< 0.9)": "#3b82f6",
  };
  const SPEED_D13_COLORS = {
    "Eco Speed (< 11 узлов)": "#10b981",
    "Standard Transit (11–14 узлов)": "#00f2fe",
    "High Speed (14–16 узлов)": "#f59e0b",
    "Express (> 16 узлов)": "#ef4444",
  };
  const DRAFT_D13_COLORS = {
    "Full Laden (> 90% Max Draft)": "#00f2fe",
    "Partial Load (60–90%)": "#38bdf8",
    "Light Ballast (30–60%)": "#a78bfa",
    "Minimum Draft (< 30% / Port Operations)": "#8aa4bf",
  };

  function flagBucketD13(fl) {
    const known = ["Маршалловы О-ва", "Панама", "Либерия", "Багамы", "Бермуды"];
    if (known.includes(fl)) return fl;
    return "Китай / Прочие";
  }

  function riskBucketD13(v) {
    const r = (v.risk || "").toUpperCase();
    const st = (v.sanctions_tags || []).join(" ").toUpperCase();
    const raw = ((v.vessel_type || "") + " " + (v.destination_context || "")).toUpperCase();
    if (r === "EXTREME" || st.includes("OFAC") || st.includes("BLACKLIST") || st.includes("BLOCKED")) {
      return "Extreme / Blacklist";
    }
    if (r === "HIGH" || st.includes("EU") || st.includes("UK") || raw.includes("GREY") || raw.includes("СЕР")) {
      return "High Risk / Grey Zone";
    }
    if (r === "MID" || st.includes("SHADOW") || raw.includes("ТЕНЕВ") || raw.includes("SHADOW")) {
      return "Medium / Shadow Fleet";
    }
    if (r === "LOW") {
      return "Low / Clean Compliance";
    }
    return "Medium / Shadow Fleet";
  }

  function ageBucketD13(age) {
    const a = Number(age) || 0;
    if (a < 5) return "< 5 лет (Newbuild)";
    if (a <= 10) return "5–10 лет";
    if (a <= 15) return "10–15 лет";
    if (a <= 20) return "15–20 лет";
    return "> 20 лет (Dark Fleet Target)";
  }

  function opsStatusBucketD13(v) {
    const nav = ((v.nav_status || "") + " " + (v.nav_bucket || "") + " " + (v.destination_context || "")).toUpperCase();
    if (nav.includes("DRYDOCK") || nav.includes("SHIPYARD") || nav.includes("РЕМОНТ") || nav.includes("REPAIR") || nav.includes("ДОК")) {
      return "Repair / Shipyard";
    }
    if (nav.includes("MOOR") || nav.includes("STS") || nav.includes("ANCHOR") || nav.includes("ШВАРТ") || nav.includes("ЯКОР") || nav.includes("BERTH")) {
      return "Moored / STS Transfer";
    }
    const draft = Number(v.draft_m) || 0;
    if (draft >= 10.0 || nav.includes("LADEN") || nav.includes("ГРУЗ")) {
      return "Laden (В грузу / Max Draft)";
    }
    return "Ballast (В балласте)";
  }

  function ratioBucketD13(v) {
    const dwt = Number(v.dwt_tons) || 0;
    const gt = Math.max(Number(v.gt) || 1, 1);
    const r = dwt / gt;
    if (r > 1.8) return "Ultra High Ratio (> 1.8 DWT/GT)";
    if (r >= 1.4) return "High Ratio (1.4–1.8)";
    if (r >= 0.9) return "Standard LNG/Gas (0.9–1.4)";
    return "Volume-Driven (< 0.9)";
  }

  function speedBucketD13(sp) {
    const s = Number(sp) || 0;
    if (s < 11.0) return "Eco Speed (< 11 узлов)";
    if (s <= 14.0) return "Standard Transit (11–14 узлов)";
    if (s <= 16.0) return "High Speed (14–16 узлов)";
    return "Express (> 16 узлов)";
  }

  function estMaxDraft(v) {
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

  function draftBucketD13(v) {
    const draft = Number(v.draft_m) || 0;
    const maxD = estMaxDraft(v);
    const ratio = maxD > 0 ? (draft / maxD) * 100 : 0;
    if (ratio > 90.0) return "Full Laden (> 90% Max Draft)";
    if (ratio >= 60.0) return "Partial Load (60–90%)";
    if (ratio >= 30.0) return "Light Ballast (30–60%)";
    return "Minimum Draft (< 30% / Port Operations)";
  }

  function fuelTechD13(v) {
    const txt = ((v.vessel_type || "") + " " + (v.destination_context || "") + " " + (v.raw_text || "")).toUpperCase();
    if (txt.includes("DUAL-FUEL") || txt.includes("X-DF") || txt.includes("DFDE") || txt.includes("ME-GI") || txt.includes("ME-GA") || (v.tech_type && v.tech_type.startsWith("Q"))) {
      return "LNG Dual-Fuel";
    }
    if (txt.includes("SCRUBBER") || txt.includes("СКРУББЕР")) {
      return "Scrubber";
    }
    return "Conventional";
  }

  function aggregateD13(rows, keyFn) {
    const map = {};
    rows.forEach((v) => {
      const k = keyFn(v) || "—";
      if (!map[k]) map[k] = { key: k, n: 0, dwt: 0, ages: [], caps: [], risks: [], drafts: [], fuels: {} };
      map[k].n += 1;
      map[k].dwt += v.dwt_tons || 0;
      map[k].ages.push(v.age_years || 0);
      if (v.capacity_m3) map[k].caps.push(v.capacity_m3);
      map[k].risks.push(v.risk || "UNK");
      if (v.draft_m) map[k].drafts.push(Number(v.draft_m) || 0);
      const ft = fuelTechD13(v);
      map[k].fuels[ft] = (map[k].fuels[ft] || 0) + 1;
    });
    return Object.values(map).sort((a, b) => b.dwt - a.dwt);
  }

  function tipTri(catLabel, seg, sliceDwt, extra, kind) {
    const riskTop = {};
    (seg.risks || []).forEach((r) => { riskTop[r] = (riskTop[r] || 0) + 1; });
    const risk = Object.entries(riskTop).sort((a, b) => b[1] - a[1])[0];
    const avgAge = (seg.ages || []).length ? seg.ages.reduce((a, b) => a + b, 0) / seg.ages.length : 0;
    const avgCap = (seg.caps || []).length ? seg.caps.reduce((a, b) => a + b, 0) / seg.caps.length : 0;
    const avgDraft = (seg.drafts || []).length ? seg.drafts.reduce((a, b) => a + b, 0) / seg.drafts.length : 0;
    const cap = avgCap > 0 ? `ср. capacity ${Math.round(avgCap).toLocaleString("ru-RU")} м³` : "";
    const dr = avgDraft > 0 ? `ср. осадка ${avgDraft.toFixed(1)} м` : "";

    let specExtra = extra || "";
    if (kind === "risk") {
      specExtra = `Санкционный профиль · риск-группа ${catLabel}`;
    } else if (kind === "age") {
      const eedi = avgAge < 5 ? "EEDI Phase 3 · CII A" : avgAge < 10 ? "EEDI Phase 2 · CII B" : avgAge < 15 ? "EEDI Phase 1 · CII C" : avgAge < 20 ? "EEXI Compliance · CII D" : "CII Grade E (Dark Fleet Risk)";
      specExtra = `${eedi} · ср. возраст ${avgAge.toFixed(1)} лет`;
    } else if (kind === "ops") {
      const fuels = Object.entries(seg.fuels || {}).map(([f, n]) => `${f}: ${n}`).join(" | ");
      specExtra = [dr, fuels].filter(Boolean).join(" · ");
    } else if (kind === "ratio") {
      specExtra = `Коэффициент дедвейта к валовой вместимости: ${catLabel}`;
    } else if (kind === "speed") {
      specExtra = `Ходовой режим и скоростной профиль: ${catLabel}`;
    } else if (kind === "draft") {
      specExtra = `Гидродинамическое использование осадки (Draft vs Max): ${catLabel}`;
    }

    return tipBlock({
      title: catLabel,
      n: seg.n,
      dwt: seg.dwt,
      sliceDwt,
      risk: risk ? risk[0] : "UNK",
      extra: [specExtra, cap].filter(Boolean).join(" · ") || undefined,
    });
  }

  /** Single donut with global-% labels; returns SVG markup + segment meta. */
  function buildDonutSvg(w, h, segs, colorFn, centerTitle, centerVal, centerPct, dimKey) {
    const cx = w / 2, cy = h / 2 + 4;
    const R = Math.min(w, h) * 0.38, r = R * 0.58;
    const total = segs.reduce((s, x) => s + x.dwt, 0) || 1;
    let a = -Math.PI / 2, paths = "", labels = "";
    segs.forEach((seg, i) => {
      const sw = (seg.dwt / total) * Math.PI * 2;
      const a2 = a + sw;
      const col = colorFn(seg.key, i);
      const active = !dimKey || dimKey.has(seg.key);
      const cls = active ? "seg hl" : "seg dim";
      paths += `<path class="${cls}" data-d13key="${String(seg.key).replace(/"/g, "&quot;")}" data-dwt="${seg.dwt}" data-n="${seg.n}"
        d="${arcPath(cx, cy, R, r, a, a2)}" fill="${col}" opacity="${active ? 0.9 : 0.25}"></path>`;
      if (sw > 0.18) {
        const mid = a + sw / 2, lr = (R + r) / 2;
        labels += `<text x="${cx + lr * Math.cos(mid)}" y="${cy + lr * Math.sin(mid) + 3}" text-anchor="middle"
          fill="#031018" font-size="9" font-weight="700" font-family="Orbitron">${fmtPct(gPct(seg.dwt), 1)}</text>`;
      }
      a = a2;
    });
    return `<svg viewBox="0 0 ${w} ${h}" data-donut="1">${paths}${labels}
      <circle cx="${cx}" cy="${cy}" r="${r - 4}" fill="rgba(3,7,15,.95)" stroke="rgba(212,175,55,.4)" style="pointer-events:none"/>
      <text class="d13-ct" x="${cx}" y="${cy - 10}" text-anchor="middle" fill="#D4AF37" font-size="8" font-family="Orbitron" style="pointer-events:none">${centerTitle}</text>
      <text class="d13-cv" x="${cx}" y="${cy + 6}" text-anchor="middle" fill="#00f2fe" font-size="12" font-family="Orbitron" font-weight="700" style="pointer-events:none">${centerVal}</text>
      <text class="d13-cp" x="${cx}" y="${cy + 20}" text-anchor="middle" fill="#ff8ec8" font-size="8" font-family="JetBrains Mono" style="pointer-events:none">${centerPct}</text>
    </svg>`;
  }

  function paintDonut(el, rows, kind, dimKeys) {
    if (!el) return;
    const { w, h } = svgBox(el);
    const sliceDwt = dwtSum(rows);
    let segs, colorFn, filterKey, labelFn, centerTitle, centerVal, centerPct, extra;

    if (kind === "zone") {
      segs = aggregateD13(rows, (v) => v.ops_zone);
      segs.forEach((s) => { s.label = OPS_ZONE_RU[s.key] || s.key; });
      colorFn = (k) => ZONE_COLORS[k] || PAL[0];
      filterKey = "opsZone";
      labelFn = (k) => OPS_ZONE_RU[k] || k;
      const top = segs[0];
      centerTitle = top ? `ТОП Зона` : "Зоны";
      centerVal = top ? (OPS_ZONE_RU[top.key] || top.key).split("/")[0].trim().slice(0, 16) : "—";
      centerPct = top ? `${fmtPct(gPct(top.dwt))} от флота` : "—";
      extra = "операционная зона";
    } else if (kind === "tech") {
      segs = aggregateD13(rows, (v) => v.tech_type);
      segs.forEach((s) => { s.label = TECH_TYPE_RU[s.key] || s.key; });
      colorFn = (k) => TECH_COLORS[k] || PAL[1];
      filterKey = "techType";
      labelFn = (k) => TECH_TYPE_RU[k] || k;
      const lngDwt = rows.filter((v) => v.tech_type !== "VLCC_SUEZ").reduce((s, v) => s + v.dwt_tons, 0);
      centerTitle = "СПГ / Флот DWT";
      centerVal = fmtMln(lngDwt || sliceDwt).replace(" млн т", " М т");
      centerPct = `${fmtPct(gPct(lngDwt || sliceDwt))} от флота`;
      extra = "технотип";
    } else if (kind === "flag") {
      segs = aggregateD13(rows, (v) => flagBucketD13(v.flag_short || "—"));
      segs.forEach((s) => { s.label = s.key; });
      const FLAG_COL = {
        "Маршалловы О-ва": "#00f2fe", "Панама": "#D4AF37", "Либерия": "#10b981",
        "Багамы": "#a78bfa", "Бермуды": "#f59e0b", "Китай / Прочие": "#8aa4bf",
      };
      colorFn = (k, i) => FLAG_COL[k] || PAL[i % PAL.length];
      filterKey = "flag";
      labelFn = (k) => k;
      const top = segs[0];
      centerTitle = top ? "ТОП Флаг" : "Флаги";
      centerVal = top ? String(top.key).slice(0, 18) : "—";
      centerPct = top ? `${fmtPct(gPct(top.dwt))} от флота` : "—";
      extra = "юрисдикция / флаг";
    } else if (kind === "risk") {
      segs = aggregateD13(rows, riskBucketD13);
      segs.forEach((s) => { s.label = s.key; });
      colorFn = (k) => RISK_D13_COLORS[k] || PAL[0];
      filterKey = "d13Risk";
      labelFn = (k) => k;
      const top = segs[0];
      centerTitle = "Профиль Риска";
      centerVal = top ? top.key.split("/")[0].trim().slice(0, 16) : "—";
      centerPct = top ? `${fmtPct(gPct(top.dwt))} от флота` : "—";
      extra = "комплаенс и санкции";
    } else if (kind === "age") {
      segs = aggregateD13(rows, (v) => ageBucketD13(v.age_years));
      segs.forEach((s) => { s.label = s.key; });
      colorFn = (k) => AGE_D13_COLORS[k] || PAL[1];
      filterKey = "d13Age";
      labelFn = (k) => k;
      const top = segs[0];
      centerTitle = "Возраст / EEDI";
      centerVal = top ? top.key.split("(")[0].trim().slice(0, 16) : "—";
      centerPct = top ? `${fmtPct(gPct(top.dwt))} от флота` : "—";
      extra = "деградация флота и CII";
    } else if (kind === "ops") {
      segs = aggregateD13(rows, opsStatusBucketD13);
      segs.forEach((s) => { s.label = s.key; });
      colorFn = (k) => OPS_D13_COLORS[k] || PAL[2];
      filterKey = "d13Ops";
      labelFn = (k) => k;
      const top = segs[0];
      centerTitle = "Осадка / Режим";
      centerVal = top ? top.key.split("(")[0].trim().slice(0, 16) : "—";
      centerPct = top ? `${fmtPct(gPct(top.dwt))} от флота` : "—";
      extra = "навигационный статус и топливо";
    } else if (kind === "ratio") {
      segs = aggregateD13(rows, ratioBucketD13);
      segs.forEach((s) => { s.label = s.key; });
      colorFn = (k) => RATIO_D13_COLORS[k] || PAL[0];
      filterKey = "d13Ratio";
      labelFn = (k) => k;
      const top = segs[0];
      centerTitle = "DWT / GT";
      centerVal = top ? top.key.split("(")[0].trim().slice(0, 16) : "—";
      centerPct = top ? `${fmtPct(gPct(top.dwt))} от флота` : "—";
      extra = "эффективность тоннажа";
    } else if (kind === "speed") {
      segs = aggregateD13(rows, (v) => speedBucketD13(v.speed_knots));
      segs.forEach((s) => { s.label = s.key; });
      colorFn = (k) => SPEED_D13_COLORS[k] || PAL[1];
      filterKey = "d13Speed";
      labelFn = (k) => k;
      const top = segs[0];
      centerTitle = "Скорость AIS";
      centerVal = top ? top.key.split("(")[0].trim().slice(0, 16) : "—";
      centerPct = top ? `${fmtPct(gPct(top.dwt))} от флота` : "—";
      extra = "скоростной режим флота";
    } else if (kind === "draft") {
      segs = aggregateD13(rows, draftBucketD13);
      segs.forEach((s) => { s.label = s.key; });
      colorFn = (k) => DRAFT_D13_COLORS[k] || PAL[2];
      filterKey = "d13Draft";
      labelFn = (k) => k;
      const top = segs[0];
      centerTitle = "Осадка / Max";
      centerVal = top ? top.key.split("(")[0].trim().slice(0, 16) : "—";
      centerPct = top ? `${fmtPct(gPct(top.dwt))} от флота` : "—";
      extra = "гидродинамическая загрузка";
    }

    const dimSet = dimKeys || null;
    el.innerHTML = buildDonutSvg(w, h, segs, colorFn, centerTitle, centerVal, centerPct, dimSet);
    el.dataset.d13kind = kind;

    el.querySelectorAll("path.seg").forEach((p) => {
      const key = p.dataset.d13key;
      const seg = segs.find((s) => String(s.key) === String(key));
      if (!seg) return;
      const html = tipTri(labelFn(key), seg, sliceDwt, extra, kind);
      p.onmouseenter = (e) => {
        showTip(e, html);
        onTriDonutHover(el.id, key);
      };
      p.onmousemove = (e) => showTip(e, html);
      p.onmouseleave = () => {
        hideTip();
        onTriDonutLeave();
      };
      p.onclick = () => {
        const d13Reset = { opsZone: null, techType: null, flag: null, d13Risk: null, d13Age: null, d13Ops: null, d13Ratio: null, d13Speed: null, d13Draft: null };
        if (filterKey === "flag") {
          if (key === "Китай / Прочие") {
            CF.applyPreset("d13_flag_other", (v) => flagBucketD13(v.flag_short || "—") === "Китай / Прочие");
          } else {
            CF.setMany(Object.assign(d13Reset, { flag: CF.filters.flag === key ? null : key }));
          }
        } else if (filterKey === "opsZone") {
          CF.setMany(Object.assign(d13Reset, { opsZone: CF.filters.opsZone === key ? null : key }));
        } else if (filterKey === "techType") {
          CF.setMany(Object.assign(d13Reset, { techType: CF.filters.techType === key ? null : key }));
        } else if (filterKey === "d13Risk") {
          CF.setMany(Object.assign(d13Reset, { d13Risk: CF.filters.d13Risk === key ? null : key }));
        } else if (filterKey === "d13Age") {
          CF.setMany(Object.assign(d13Reset, { d13Age: CF.filters.d13Age === key ? null : key }));
        } else if (filterKey === "d13Ops") {
          CF.setMany(Object.assign(d13Reset, { d13Ops: CF.filters.d13Ops === key ? null : key }));
        } else if (filterKey === "d13Ratio") {
          CF.setMany(Object.assign(d13Reset, { d13Ratio: CF.filters.d13Ratio === key ? null : key }));
        } else if (filterKey === "d13Speed") {
          CF.setMany(Object.assign(d13Reset, { d13Speed: CF.filters.d13Speed === key ? null : key }));
        } else if (filterKey === "d13Draft") {
          CF.setMany(Object.assign(d13Reset, { d13Draft: CF.filters.d13Draft === key ? null : key }));
        }
      };
    });
  }

  function relatedKeys(rows, kind, key) {
    let subset;
    if (kind === "zone") subset = rows.filter((v) => v.ops_zone === key);
    else if (kind === "tech") subset = rows.filter((v) => v.tech_type === key);
    else if (kind === "flag") subset = rows.filter((v) => flagBucketD13(v.flag_short || "—") === key);
    else if (kind === "risk") subset = rows.filter((v) => riskBucketD13(v) === key);
    else if (kind === "age") subset = rows.filter((v) => ageBucketD13(v.age_years) === key);
    else if (kind === "ops") subset = rows.filter((v) => opsStatusBucketD13(v) === key);
    else if (kind === "ratio") subset = rows.filter((v) => ratioBucketD13(v) === key);
    else if (kind === "speed") subset = rows.filter((v) => speedBucketD13(v.speed_knots) === key);
    else if (kind === "draft") subset = rows.filter((v) => draftBucketD13(v) === key);
    else subset = rows;

    return {
      zones: new Set(subset.map((v) => v.ops_zone)),
      techs: new Set(subset.map((v) => v.tech_type)),
      flags: new Set(subset.map((v) => flagBucketD13(v.flag_short || "—"))),
      risks: new Set(subset.map((v) => riskBucketD13(v))),
      ages: new Set(subset.map((v) => ageBucketD13(v.age_years))),
      ops: new Set(subset.map((v) => opsStatusBucketD13(v))),
      ratios: new Set(subset.map((v) => ratioBucketD13(v))),
      speeds: new Set(subset.map((v) => speedBucketD13(v.speed_knots))),
      drafts: new Set(subset.map((v) => draftBucketD13(v))),
      subset,
    };
  }

  function snapshotDonutCenters() {
    ALL_D13_IDS.forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      d13CenterBaseline[id] = {
        ct: (el.querySelector(".d13-ct") || {}).textContent || "",
        cv: (el.querySelector(".d13-cv") || {}).textContent || "",
        cp: (el.querySelector(".d13-cp") || {}).textContent || "",
      };
    });
  }

  function setDonutCenter(elId, title, val, pct) {
    const el = document.getElementById(elId);
    if (!el) return;
    const ct = el.querySelector(".d13-ct");
    const cv = el.querySelector(".d13-cv");
    const cp = el.querySelector(".d13-cp");
    if (ct) ct.textContent = title;
    if (cv) cv.textContent = val;
    if (cp) cp.textContent = pct;
  }

  function markTriSegments(elId, activeSet) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.querySelectorAll("path.seg").forEach((p) => {
      const on = activeSet.has(p.dataset.d13key);
      p.classList.toggle("dim", !on);
      p.classList.toggle("hl", on);
      p.style.opacity = on ? "1" : "0.25";
      if (on) {
        p.style.filter = "drop-shadow(0 0 10px #00f2fe) drop-shadow(0 0 6px #ff0080)";
      } else {
        p.style.filter = "none";
      }
    });
  }

  /** Unified Nona-Donut cross-highlight + synchronized center micro-metrics. */
  function onTriDonutHover(sourceChartId, segmentKey) {
    const kind = CHART_KIND[sourceChartId];
    if (!kind || !d13LastRows.length) return;
    const rel = relatedKeys(d13LastRows, kind, segmentKey);
    const dwt = dwtSum(rel.subset);
    const n = rel.subset.length;
    const label =
      kind === "zone" ? (ZONE_SHORT[segmentKey] || OPS_ZONE_RU[segmentKey] || segmentKey)
        : kind === "tech" ? (TECH_SHORT[segmentKey] || TECH_TYPE_RU[segmentKey] || segmentKey)
          : segmentKey;

    markTriSegments("c13a", rel.zones);
    markTriSegments("c13b", rel.techs);
    markTriSegments("c13c", rel.flags);
    markTriSegments("c13d", rel.risks);
    markTriSegments("c13e", rel.ages);
    markTriSegments("c13f", rel.ops);
    markTriSegments("c13g", rel.ratios);
    markTriSegments("c13h", rel.speeds);
    markTriSegments("c13i", rel.drafts);

    const mln = fmtMln(dwt).replace(" млн т", " М т");
    const gp = fmtPct(gPct(dwt));
    const ships = `${n} ${wordShips(n)}`;
    // All nine centers show coordinated slice micro-metrics
    setDonutCenter("c13a", label.slice(0, 20), mln, `${gp} · ${ships}`);
    setDonutCenter("c13b", `Срез · ${label.slice(0, 14)}`, mln, `${gp} от 81.3 М т`);
    setDonutCenter("c13c", `Связь · ${ships}`, mln, `${gp} флота`);
    setDonutCenter("c13d", `Риск-срез`, mln, `${gp} · ${ships}`);
    setDonutCenter("c13e", `Возраст-DWT`, mln, `${gp} от 81.3 М т`);
    setDonutCenter("c13f", `Осадка-Статус`, mln, `${gp} флота`);
    setDonutCenter("c13g", `DWT/GT Ratio`, mln, `${gp} · ${ships}`);
    setDonutCenter("c13h", `Скорость-AIS`, mln, `${gp} от 81.3 М т`);
    setDonutCenter("c13i", `Загрузка/Осадка`, mln, `${gp} флота`);
  }

  function onTriDonutLeave() {
    ALL_D13_IDS.forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.querySelectorAll("path.seg").forEach((p) => {
        p.classList.remove("dim", "hl");
        p.style.opacity = "";
        p.style.filter = "";
      });
      const b = d13CenterBaseline[id];
      if (b) setDonutCenter(id, b.ct, b.cv, b.cp);
    });
  }

  function render13Sunburst(rows, el) {
    const { w, h } = svgBox(el);
    const cx = w / 2, cy = h / 2;
    const R0 = 44, R1 = 78, R2 = 112, R3 = Math.min(w, h) * 0.44;
    const MIN_LABEL = (3 * Math.PI) / 180;
    const sliceDwt = dwtSum(rows);
    const tree = {};
    rows.forEach((v) => {
      const z = v.ops_zone || "OIL_OTHER", t = v.tech_type || "VLCC_SUEZ", fl = flagBucketD13(v.flag_short || "—");
      if (!tree[z]) tree[z] = {};
      if (!tree[z][t]) tree[z][t] = {};
      if (!tree[z][t][fl]) tree[z][t][fl] = { n: 0, dwt: 0 };
      tree[z][t][fl].n += 1;
      tree[z][t][fl].dwt += v.dwt_tons || 0;
    });
    const zoneDwt = (z) => Object.values(tree[z] || {}).reduce((s, techs) => s + Object.values(techs).reduce((p, c) => p + c.dwt, 0), 0);
    const techDwt = (z, t) => Object.values((tree[z] || {})[t] || {}).reduce((s, c) => s + c.dwt, 0);
    let a = -Math.PI / 2, paths = "", labels = "";
    Object.keys(tree).sort((x, y) => zoneDwt(y) - zoneDwt(x)).forEach((z, zi) => {
      const dwtZ = zoneDwt(z);
      const swZ = (dwtZ / Math.max(sliceDwt, 1)) * Math.PI * 2, a2 = a + swZ;
      paths += `<path class="seg" data-zone="${z}" d="${arcPath(cx, cy, R1, R0, a, a2)}" fill="${ZONE_COLORS[z] || PAL[zi]}" opacity=".9"></path>`;
      if (swZ > MIN_LABEL) {
        const mid = a + swZ / 2, lr = (R0 + R1) / 2;
        labels += `<text x="${cx + lr * Math.cos(mid)}" y="${cy + lr * Math.sin(mid) + 3}" text-anchor="middle" fill="#031018" font-size="8" font-weight="700">${fmtPct(gPct(dwtZ), 1)}</text>`;
      }
      let ta = a;
      Object.keys(tree[z]).sort((x, y) => techDwt(z, y) - techDwt(z, x)).forEach((t, ti) => {
        const dwtT = techDwt(z, t);
        const swT = (dwtT / Math.max(sliceDwt, 1)) * Math.PI * 2, ta2 = ta + swT;
        paths += `<path class="seg" data-zone="${z}" data-tech="${t}" d="${arcPath(cx, cy, R2, R1, ta, ta2)}" fill="${TECH_COLORS[t] || PAL[ti]}" opacity=".7"></path>`;
        if (swT > MIN_LABEL) {
          const mid = ta + swT / 2, lr = (R1 + R2) / 2;
          labels += `<text x="${cx + lr * Math.cos(mid)}" y="${cy + lr * Math.sin(mid) + 3}" text-anchor="middle" fill="#e8f4ff" font-size="7">${fmtPct(gPct(dwtT), 1)}</text>`;
        }
        let fa = ta;
        Object.entries(tree[z][t]).forEach(([fl, cell], fi) => {
          const swF = (cell.dwt / Math.max(sliceDwt, 1)) * Math.PI * 2, fa2 = fa + swF;
          paths += `<path class="seg" data-zone="${z}" data-tech="${t}" data-flag="${fl}" d="${arcPath(cx, cy, R3, R2, fa, fa2)}" fill="${PAL[(zi + ti + fi) % PAL.length]}" opacity=".48"></path>`;
          fa = fa2;
        });
        ta = ta2;
      });
      a = a2;
    });
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${paths}${labels}
      <circle cx="${cx}" cy="${cy}" r="${R0 - 3}" fill="rgba(3,7,15,.95)" stroke="rgba(212,175,55,.45)"/>
      <text x="${cx}" y="${cy - 6}" text-anchor="middle" fill="#D4AF37" font-size="11" font-family="Orbitron">${fmtMln(sliceDwt).replace(" млн т", " М т")}</text>
      <text x="${cx}" y="${cy + 10}" text-anchor="middle" fill="#ff8ec8" font-size="9">${fmtPct(gPct(sliceDwt))} от флота</text>
      <text x="${cx}" y="${cy + 24}" text-anchor="middle" fill="#8aa4bf" font-size="7">Зона → Технотип → Флаг</text>
    </svg>`;
    el.querySelectorAll("path.seg").forEach((p) => {
      const z = p.dataset.zone, t = p.dataset.tech, fl = p.dataset.flag;
      const subset = rows.filter((v) => v.ops_zone === z && (!t || v.tech_type === t) && (!fl || flagBucketD13(v.flag_short || "—") === fl));
      const seg = { n: subset.length, dwt: dwtSum(subset), ages: subset.map((v) => v.age_years || 0), caps: subset.map((v) => v.capacity_m3 || 0).filter(Boolean), risks: subset.map((v) => v.risk) };
      const title = [OPS_ZONE_RU[z], t && TECH_TYPE_RU[t], fl].filter(Boolean).join(" → ");
      const html = tipTri(title, seg, sliceDwt, "sunburst");
      p.onmouseenter = (e) => showTip(e, html);
      p.onmousemove = (e) => showTip(e, html);
      p.onmouseleave = hideTip;
      p.onclick = () => {
        if (fl) CF.setMany({ opsZone: z, techType: t, flag: fl === "Китай / Прочие" ? null : fl });
        else if (t) CF.setMany({ opsZone: z, techType: t, flag: null });
        else CF.setMany({ opsZone: z, techType: null, flag: null });
      };
    });
  }

  function syncD13ModeUI() {
    const trio = document.getElementById("d13Trio");
    const trio2 = document.getElementById("d13Trio2");
    const trio3 = document.getElementById("d13Trio3");
    const sun = document.getElementById("d13Sun");
    document.querySelectorAll(".d13-mode").forEach((b) => b.classList.toggle("on", b.dataset.mode === d13Mode));
    
    const showTrio1 = d13Mode === "trio" || d13Mode === "all";
    const showTrio2 = d13Mode === "trio2" || d13Mode === "all";
    const showTrio3 = d13Mode === "trio3" || d13Mode === "all";
    const showSun = d13Mode === "sunburst";

    if (trio) {
      trio.hidden = !showTrio1;
      trio.style.opacity = showTrio1 ? "1" : "0";
      trio.style.transition = "opacity .28s ease";
    }
    if (trio2) {
      trio2.hidden = !showTrio2;
      trio2.style.opacity = showTrio2 ? "1" : "0";
      trio2.style.transition = "opacity .28s ease";
    }
    if (trio3) {
      trio3.hidden = !showTrio3;
      trio3.style.opacity = showTrio3 ? "1" : "0";
      trio3.style.transition = "opacity .28s ease";
    }
    if (sun) {
      sun.hidden = !showSun;
      sun.classList.toggle("on", showSun);
      sun.style.opacity = showSun ? "1" : "0";
      sun.style.transition = "opacity .28s ease";
    }
  }

  function render13(rows, el) {
    d13LastRows = rows;
    syncD13ModeUI();
    if (d13Mode === "sunburst") {
      const sun = document.getElementById("d13Sun") || el;
      render13Sunburst(rows, sun);
      return;
    }
    const c13a = document.getElementById("c13a");
    const c13b = document.getElementById("c13b");
    const c13c = document.getElementById("c13c");
    if (c13a) paintDonut(c13a, rows, "zone", null);
    if (c13b) paintDonut(c13b, rows, "tech", null);
    if (c13c) paintDonut(c13c, rows, "flag", null);

    const c13d = document.getElementById("c13d");
    const c13e = document.getElementById("c13e");
    const c13f = document.getElementById("c13f");
    if (c13d) paintDonut(c13d, rows, "risk", null);
    if (c13e) paintDonut(c13e, rows, "age", null);
    if (c13f) paintDonut(c13f, rows, "ops", null);

    const c13g = document.getElementById("c13g");
    const c13h = document.getElementById("c13h");
    const c13i = document.getElementById("c13i");
    if (c13g) paintDonut(c13g, rows, "ratio", null);
    if (c13h) paintDonut(c13h, rows, "speed", null);
    if (c13i) paintDonut(c13i, rows, "draft", null);

    snapshotDonutCenters();
  }

  // ── D14 Scatter: X = Global % DWT, Y = GT ──────────────────────────────────
  function render14(rows, el) {
    const { w, h } = svgBox(el);
    el.innerHTML = `<canvas width="${w}" height="${h}"></canvas>`;
    const c = el.querySelector("canvas"), ctx = c.getContext("2d");
    const pad = { l: 52, r: 14, t: 28, b: 40 };
    const pts = [
      ...rows.map((v) => ({ gp: gPct(v.dwt_tons), y: v.gt, top: true, v })),
      ...REST.map((v) => ({ gp: gPct(v.dwt_tons), y: v.gt, top: false, v: null })),
    ];
    const maxX = Math.max(...pts.map((p) => p.gp), 0.01);
    const maxY = Math.max(...pts.map((p) => p.y), 1);
    const X = (x) => pad.l + (x / maxX) * (w - pad.l - pad.r);
    const Y = (y) => h - pad.b - (y / maxY) * (h - pad.t - pad.b);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#8aa4bf"; ctx.font = "9px JetBrains Mono";
    ctx.fillText(AXIS_Y + " →", pad.l, 16);
    ctx.save(); ctx.translate(14, h / 2); ctx.rotate(-Math.PI / 2); ctx.fillText("GT ↑", 0, 0); ctx.restore();
    ctx.strokeStyle = "rgba(160,220,255,.12)"; ctx.beginPath();
    for (let i = 0; i <= 4; i++) {
      const yy = pad.t + i * (h - pad.t - pad.b) / 4;
      ctx.moveTo(pad.l, yy); ctx.lineTo(w - pad.r, yy);
    }
    ctx.stroke();
    pts.forEach((p) => {
      ctx.beginPath();
      ctx.arc(X(p.gp), Y(p.y), p.top ? 3.4 : 1.5, 0, Math.PI * 2);
      ctx.fillStyle = p.top ? "rgba(0,242,254,.88)" : "rgba(138,164,191,.25)";
      ctx.fill();
    });
    const sliceDwt = dwtSum(rows);
    c.onmousemove = (e) => {
      const r = c.getBoundingClientRect();
      const mx = (e.clientX - r.left) * (w / r.width), my = (e.clientY - r.top) * (h / r.height);
      let hit = null, best = 9;
      rows.forEach((v) => {
        const d = Math.hypot(X(gPct(v.dwt_tons)) - mx, Y(v.gt) - my);
        if (d < best) { best = d; hit = v; }
      });
      if (hit) showTip(e, tipVessel(hit, sliceDwt));
      else hideTip();
    };
    c.onmouseleave = hideTip;
    c.onclick = (e) => {
      const r = c.getBoundingClientRect();
      const mx = (e.clientX - r.left) * (w / r.width), my = (e.clientY - r.top) * (h / r.height);
      let hit = null, best = 8;
      rows.forEach((v) => {
        const d = Math.hypot(X(gPct(v.dwt_tons)) - mx, Y(v.gt) - my);
        if (d < best) { best = d; hit = v; }
      });
      if (hit) CF.set("imo", String(hit.imo));
    };
  }

  // ── D15 Age stacked — height = global % DWT ────────────────────────────────
  function render15(rows, el) {
    const { w, h } = svgBox(el);
    const buckets = ["0-5", "6-10", "11-15", "16-20", "20+"];
    const types = ["LNG", "CRUDE", "PRODUCT", "TANKER", "LPG", "OTHER"];
    const data = {};
    buckets.forEach((b) => { data[b] = {}; types.forEach((t) => { data[b][t] = 0; }); });
    rows.forEach((v) => {
      const b = v.age_bucket || "20+";
      if (data[b]) data[b][v.vtype] = (data[b][v.vtype] || 0) + v.dwt_tons;
    });
    const maxGp = Math.max(...buckets.map((b) => gPct(types.reduce((s, t) => s + (data[b][t] || 0), 0))), 0.01);
    const pad = { l: 52, r: 12, t: 28, b: 36 };
    const bw = (w - pad.l - pad.r) / buckets.length;
    const sliceDwt = dwtSum(rows);
    let g = axisYLabel(w, h);
    g += `<text x="${w / 2}" y="14" text-anchor="middle" class="axis-caption">${AXIS_Y}</text>`;
    buckets.forEach((b, i) => {
      let y = h - pad.b;
      const x = pad.l + i * bw + 6;
      types.forEach((t) => {
        const val = data[b][t] || 0;
        if (!val) return;
        const bh = (gPct(val) / maxGp) * (h - pad.t - pad.b);
        y -= bh;
        g += `<rect class="seg" data-age="${b}" data-vtype="${t}" data-dwt="${val}" x="${x}" y="${y}" width="${bw - 12}" height="${Math.max(bh, 1)}" fill="${COLORS[t]}" opacity=".88"></rect>`;
      });
      const tot = types.reduce((s, t) => s + (data[b][t] || 0), 0);
      g += `<text x="${x + (bw - 12) / 2}" y="${h - 14}" text-anchor="middle" fill="#8aa4bf" font-size="10">${b}</text>`;
      g += `<text x="${x + (bw - 12) / 2}" y="${h - pad.b - 4}" text-anchor="middle" fill="#00f2fe" font-size="8">${fmtPct(gPct(tot), 1)}</text>`;
    });
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
    el.querySelectorAll("rect.seg").forEach((r) => {
      const dwt = Number(r.dataset.dwt);
      const n = rows.filter((v) => v.age_bucket === r.dataset.age && v.vtype === r.dataset.vtype).length;
      const html = tipBlock({
        title: `Возраст ${r.dataset.age} · ${VTYPE_RU[r.dataset.vtype] || r.dataset.vtype}`,
        n, dwt, sliceDwt, extra: "Атрибуты OSINT: возраст / класс",
      });
      r.onmouseenter = (e) => showTip(e, html);
      r.onmousemove = (e) => showTip(e, html);
      r.onmouseleave = hideTip;
      r.onclick = () => CF.set("ageBucket", r.dataset.age);
    });
  }

  // ── D16 Ports TOP-10 — bar length = global % (cascade from D13) ─────────────
  function render16(rows, el) {
    const { w, h } = svgBox(el);
    const dep = {}, dst = {};
    rows.forEach((v) => {
      const a = shortPort(v.departure_port), b = shortPort(v.destination_port);
      if (a && a !== "—") dep[a] = (dep[a] || 0) + v.dwt_tons;
      if (b && b !== "—") dst[b] = (dst[b] || 0) + v.dwt_tons;
    });
    const topDep = Object.entries(dep).sort((a, b) => b[1] - a[1]).slice(0, 10);
    const topDst = Object.entries(dst).sort((a, b) => b[1] - a[1]).slice(0, 10);
    if (!topDep.length && !topDst.length) { emptyOverlay(el); return; }
    const mid = w / 2;
    const labelPad = 118; // увеличенный padding слева/у оси, чтобы порты не слипались с полосами
    const maxGp = Math.max(...topDep.map((x) => gPct(x[1])), ...topDst.map((x) => gPct(x[1])), 0.01);
    const sliceDwt = dwtSum(rows);
    const nRows = Math.max(topDep.length, topDst.length, 1);
    const rowH = Math.min(26, (h - 52) / nRows);
    const mono = "JetBrains Mono, monospace";
    const barMax = Math.max(40, mid - labelPad - 28);
    let g = `<text x="${w / 2}" y="12" text-anchor="middle" class="axis-caption">${AXIS_Y} · ТОП-10 портов среза</text>
      <text x="${mid / 2}" y="28" fill="#00f2fe" font-size="11" text-anchor="middle" font-family="Orbitron,sans-serif">ОТБЫТИЕ</text>
      <text x="${mid + mid / 2}" y="28" fill="#D4AF37" font-size="11" text-anchor="middle" font-family="Orbitron,sans-serif">НАЗНАЧЕНИЕ</text>`;
    topDep.forEach(([k, c], i) => {
      const y = 40 + i * rowH;
      const bw = (gPct(c) / maxGp) * barMax;
      const labelX = mid - labelPad;
      g += `<text class="d16-lbl" x="${labelX}" y="${y + 14}" fill="#E0E6ED" font-size="12" font-family="${mono}" text-anchor="end">${k.slice(0, 16)}</text>`;
      g += `<rect class="seg" data-port="${k}" data-dwt="${c}" x="${mid - 12 - bw}" y="${y + 3}" width="${Math.max(bw, 2)}" height="${rowH - 8}" rx="3" fill="#00f2fe" opacity=".85"/>`;
      g += `<text x="${mid - 16 - bw}" y="${y + 14}" fill="#00f2fe" font-size="10" font-family="${mono}" text-anchor="end">${fmtPct(gPct(c), 2)}</text>`;
    });
    topDst.forEach(([k, c], i) => {
      const y = 40 + i * rowH;
      const bw = (gPct(c) / maxGp) * barMax;
      const labelX = mid + labelPad;
      g += `<text class="d16-lbl" x="${labelX}" y="${y + 14}" fill="#E0E6ED" font-size="12" font-family="${mono}">${k.slice(0, 16)}</text>`;
      g += `<rect class="seg" data-port="${k}" data-dwt="${c}" x="${mid + 12}" y="${y + 3}" width="${Math.max(bw, 2)}" height="${rowH - 8}" rx="3" fill="#D4AF37" opacity=".88"/>`;
      g += `<text x="${mid + 16 + bw}" y="${y + 14}" fill="#D4AF37" font-size="10" font-family="${mono}">${fmtPct(gPct(c), 2)}</text>`;
    });
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
    el.querySelectorAll("[data-port]").forEach((r) => {
      const dwt = Number(r.dataset.dwt);
      const subset = rows.filter((v) => shortPort(v.destination_port) === r.dataset.port || shortPort(v.departure_port) === r.dataset.port);
      const html = tipBlock({ title: `Порт: ${r.dataset.port}`, n: subset.length, dwt, sliceDwt, risk: dominantRisk(subset) });
      r.style.cursor = "pointer";
      r.onmouseenter = (e) => showTip(e, html);
      r.onmousemove = (e) => showTip(e, html);
      r.onmouseleave = hideTip;
      r.onclick = () => CF.set("port", r.dataset.port);
    });
  }

  // ── D17 Radar: Extreme/High/Mid/Low/Flag Risk — global % DWT ───────────────
  function render17(rows, el) {
    const { w, h } = svgBox(el);
    const cx = w / 2, cy = h / 2 + 8, R = Math.min(w, h) * 0.34;
    const keys = ["EXTREME", "HIGH", "MID", "LOW", "FLAG"];
    const labels = { EXTREME: "Extreme", HIGH: "High", MID: "Medium", LOW: "Low", FLAG: "Flag Risk" };
    const dwtBy = {}; keys.forEach((k) => { dwtBy[k] = 0; });
    rows.forEach((v) => {
      if (dwtBy[v.risk] != null) dwtBy[v.risk] += v.dwt_tons;
      if (GREY_FLAG_RE.test(v.flag || "") || (v.sanctions_tags || []).length) dwtBy.FLAG += v.dwt_tons;
    });
    const maxGp = Math.max(...keys.map((k) => gPct(dwtBy[k])), 0.01);
    const sliceDwt = dwtSum(rows);
    let g = `<text x="${w / 2}" y="14" text-anchor="middle" class="axis-caption">${AXIS_Y}</text>`;
    for (let ring = 1; ring <= 4; ring++) {
      const rr = R * ring / 4;
      let d = "";
      keys.forEach((_, i) => {
        const a = -Math.PI / 2 + i * 2 * Math.PI / keys.length;
        d += (i ? "L" : "M") + (cx + rr * Math.cos(a)) + " " + (cy + rr * Math.sin(a)) + " ";
      });
      g += `<path d="${d}Z" fill="none" stroke="rgba(0,242,254,.15)"/>`;
    }
    let poly = "";
    const cols = { EXTREME: "#ef4444", HIGH: "#f59e0b", MID: "#3b82f6", LOW: "#10b981", FLAG: "#ff0080" };
    keys.forEach((k, i) => {
      const a = -Math.PI / 2 + i * 2 * Math.PI / keys.length;
      const rr = R * (gPct(dwtBy[k]) / maxGp);
      poly += (i ? "L" : "M") + (cx + rr * Math.cos(a)) + " " + (cy + rr * Math.sin(a)) + " ";
      const lx = cx + (R + 28) * Math.cos(a), ly = cy + (R + 28) * Math.sin(a);
      g += `<text class="seg d17-lbl" data-risk="${k}" x="${lx}" y="${ly}" text-anchor="middle" fill="#00f2fe" font-size="11" font-family="JetBrains Mono, monospace" style="cursor:pointer;paint-order:stroke fill;stroke:rgba(0,20,40,.85);stroke-width:2.5px;filter:drop-shadow(0 0 4px rgba(0,242,254,.55))">${labels[k]} · ${fmtPct(gPct(dwtBy[k]), 1)}</text>`;
    });
    g += `<path class="d17-poly" d="${poly}Z" fill="rgba(0,242,254,.2)" stroke="#00f2fe" stroke-width="2" style="transition:d .25s ease"/>`;
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
    el.querySelectorAll("[data-risk]").forEach((t) => {
      const k = t.dataset.risk;
      const n = k === "FLAG"
        ? rows.filter((v) => GREY_FLAG_RE.test(v.flag || "") || (v.sanctions_tags || []).length).length
        : rows.filter((v) => v.risk === k).length;
      const html = tipBlock({ title: `Комплаенс: ${labels[k]}`, n, dwt: dwtBy[k], sliceDwt, risk: k === "FLAG" ? "HIGH" : k });
      t.onmouseenter = (e) => showTip(e, html);
      t.onmouseleave = hideTip;
      t.onclick = () => {
        if (k === "FLAG") CF.applyPreset("shadow", (v) => GREY_FLAG_RE.test(v.flag || "") || (v.sanctions_tags || []).length > 0 || ["EXTREME", "HIGH"].includes(v.risk));
        else CF.set("risk", k);
      };
    });
  }

  // ── D18 Treemap Origin → Destination (risk-colored) ────────────────────────
  function render18(rows, el) {
    const { w, h } = svgBox(el);
    const map = {};
    rows.forEach((v) => {
      const o = shortPort(v.departure_port) || "—";
      const d = shortPort(v.destination_port) || "—";
      const k = `${o} → ${d}`;
      if (!map[k]) map[k] = { n: 0, dwt: 0, risk: v.risk, origin: o, dest: d };
      map[k].n++; map[k].dwt += v.dwt_tons;
      // keep highest risk in node
      const rank = { EXTREME: 4, HIGH: 3, MID: 2, LOW: 1, UNK: 0 };
      if ((rank[v.risk] || 0) > (rank[map[k].risk] || 0)) map[k].risk = v.risk;
    });
    const items = Object.values(map).sort((a, b) => b.dwt - a.dwt).slice(0, 18);
    if (!items.length) { emptyOverlay(el); return; }
    const total = items.reduce((s, x) => s + x.dwt, 0) || 1;
    const sliceDwt = dwtSum(rows);
    let x = 0, y = 0, rowH = h / 2, g = "", row = 0;
    g += `<text x="${w / 2}" y="12" text-anchor="middle" class="axis-caption">${AXIS_Y} · Origin → Destination</text>`;
    items.forEach((it) => {
      const ww = Math.max(36, (it.dwt / total) * w);
      if (x + ww > w + 2 && row === 0) { row = 1; x = 0; y = rowH; }
      g += `<rect class="seg" data-origin="${it.origin}" data-dest="${it.dest}" data-dwt="${it.dwt}" data-n="${it.n}" data-risk="${it.risk}"
        x="${x}" y="${y + (row ? 0 : 14)}" width="${ww - 2}" height="${rowH - (row ? 2 : 16)}" fill="${COLORS[it.risk]}" opacity=".75" rx="4"/>`;
      if (ww > 64) {
        g += `<text x="${x + 5}" y="${y + (row ? 16 : 30)}" fill="#fff" font-size="8">${it.origin.slice(0, 12)} →</text>
          <text x="${x + 5}" y="${y + (row ? 28 : 42)}" fill="#e8f4ff" font-size="8">${it.dest.slice(0, 14)}</text>
          <text x="${x + 5}" y="${y + (row ? 42 : 56)}" fill="#031018" font-size="9" font-weight="700">${fmtPct(gPct(it.dwt), 2)}</text>`;
      }
      x += ww;
    });
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
    el.querySelectorAll("rect.seg").forEach((r) => {
      const html = tipBlock({
        title: `${r.dataset.origin} → ${r.dataset.dest}`,
        n: Number(r.dataset.n), dwt: Number(r.dataset.dwt), sliceDwt, risk: r.dataset.risk,
      });
      r.onmouseenter = (e) => showTip(e, html);
      r.onmousemove = (e) => showTip(e, html);
      r.onmouseleave = hideTip;
      r.onclick = () => CF.set("port", r.dataset.dest);
    });
  }

  // ── D19 Nav bars — width = global % DWT ────────────────────────────────────
  function render19(rows, el) {
    const { w, h } = svgBox(el);
    const keys = ["SEA", "ANCHOR", "ETA", "DRYDOCK"];
    const dwtBy = {}; keys.forEach((k) => { dwtBy[k] = 0; });
    rows.forEach((v) => { dwtBy[v.nav_bucket] = (dwtBy[v.nav_bucket] || 0) + v.dwt_tons; });
    const maxGp = Math.max(...keys.map((k) => gPct(dwtBy[k])), 0.01);
    const pad = { l: 130, r: 48, t: 28, b: 16 };
    const rowH = (h - pad.t - pad.b) / keys.length;
    const sliceDwt = dwtSum(rows);
    let g = `<text x="${w / 2}" y="14" text-anchor="middle" class="axis-caption">${AXIS_Y}</text>`;
    keys.forEach((k, i) => {
      const y = pad.t + i * rowH;
      const bw = (gPct(dwtBy[k]) / maxGp) * (w - pad.l - pad.r);
      g += `<text x="${pad.l - 8}" y="${y + rowH / 2 + 4}" fill="#cfe" font-size="11" text-anchor="end">${NAV_RU[k]}</text>`;
      g += `<rect class="seg" data-nav="${k}" x="${pad.l}" y="${y + 6}" width="${Math.max(bw, 2)}" height="${rowH - 12}" rx="5" fill="${COLORS[k]}" opacity=".88"/>`;
      g += `<text x="${pad.l + bw + 8}" y="${y + rowH / 2 + 4}" fill="#00f2fe" font-size="11">${fmtPct(gPct(dwtBy[k]), 2)}</text>`;
    });
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
    el.querySelectorAll("[data-nav]").forEach((r) => {
      const k = r.dataset.nav;
      const n = rows.filter((v) => v.nav_bucket === k).length;
      const html = tipBlock({ title: NAV_RU[k], n, dwt: dwtBy[k], sliceDwt, extra: "Атрибуты OSINT: nav_status" });
      r.style.cursor = "pointer";
      r.onmouseenter = (e) => showTip(e, html);
      r.onmouseleave = hideTip;
      r.onclick = () => CF.set("nav", k);
    });
  }

  // ── D20 Provenance — bar still cell audit, caption notes global context ────
  function render20(rows, el) {
    const { w, h } = svgBox(el);
    const fields = ["dwt_tons", "gt", "built_year", "speed_knots", "loa_m", "draft_m", "mmsi", "call_sign", "departure_port"];
    function tok(s) { return new Set(String(s || "").split(";").map((x) => x.trim().toLowerCase()).filter(Boolean)); }
    const tot = { OSINT: 0, AIS: 0, SYNTH: 0, KNN: 0 };
    rows.forEach((v) => {
      const sf = tok(v.synthetic_fields), kf = tok(v.imputed_fields), rf = tok(v.registry_mock_fields);
      fields.forEach((f) => {
        if (rf.has(f)) tot.AIS++;
        else if (kf.has(f)) tot.KNN++;
        else if (sf.has(f)) tot.SYNTH++;
        else tot.OSINT++;
      });
    });
    if (!CF.active() && PROV.cells) {
      tot.OSINT = PROV.cells.OSINT || tot.OSINT;
      tot.AIS = PROV.cells.AIS || tot.AIS;
      tot.SYNTH = PROV.cells.SYNTH || 0;
      tot.KNN = PROV.cells.KNN || 0;
    }
    const order = ["OSINT", "AIS", "KNN", "SYNTH"];
    const sum = order.reduce((s, k) => s + tot[k], 0) || 1;
    const sliceDwt = dwtSum(rows);
    let x = 20;
    let g = `<text x="${w / 2}" y="16" text-anchor="middle" fill="#D4AF37" font-size="11" font-family="Orbitron">ПРОВЕНАНС · срез ${fmtPct(gPct(sliceDwt))} флота</text>
      <text x="${w / 2}" y="30" text-anchor="middle" class="axis-caption">Доля ячеек аудита (Synth → 0%)</text>`;
    order.forEach((k, i) => {
      const ww = (tot[k] / sum) * (w - 40);
      g += `<rect x="${x}" y="${h / 2 - 14}" width="${Math.max(ww, 0)}" height="32" fill="${COLORS[k]}" opacity=".92"/>`;
      if (ww > 48) g += `<text x="${x + ww / 2}" y="${h / 2 + 6}" text-anchor="middle" fill="#031018" font-size="11" font-weight="700">${k} ${fmtPct(100 * tot[k] / sum, 1)}</text>`;
      g += `<text x="${40 + i * ((w - 60) / 4)}" y="${h - 22}" fill="${COLORS[k]}" font-size="10">${k}: ${tot[k]}</text>`;
      x += ww;
    });
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
  }

  // ── D21 KPI cards with global % ────────────────────────────────────────────
  function render21(rows, el) {
    const sliceDwt = dwtSum(rows);
    const avgAge = rows.length ? rows.reduce((s, v) => s + v.age_years, 0) / rows.length : 0;
    const ratios = rows.filter((v) => v.dwt_tons > 0).map((v) => v.gt / v.dwt_tons);
    const avgRatio = ratios.length ? ratios.reduce((a, b) => a + b, 0) / ratios.length : 0;
    const loas = rows.map((v) => v.loa_m).filter((x) => x > 0);
    const drafts = rows.map((v) => v.draft_m).filter((x) => x > 0);
    el.innerHTML = `<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;height:100%;padding:6px">
      <div class="kpi" style="margin:0"><div class="k">Глобальный вклад среза</div><div class="v">${fmtPct(gPct(sliceDwt))}</div><div class="s">${AXIS_Y}</div></div>
      <div class="kpi" style="margin:0"><div class="k">Средний возраст</div><div class="v">${avgAge.toFixed(1)}</div><div class="s">лет</div></div>
      <div class="kpi" style="margin:0"><div class="k">GT / DWT Ratio</div><div class="v">${avgRatio.toFixed(2)}</div><div class="s">среднее</div></div>
      <div class="kpi" style="margin:0"><div class="k">LOA / Осадка</div><div class="v">${loas.length ? Math.min(...loas).toFixed(0) : 0}–${loas.length ? Math.max(...loas).toFixed(0) : 0}</div><div class="s">осадка ${(drafts.reduce((a, b) => a + b, 0) / Math.max(drafts.length, 1)).toFixed(1)} м</div></div>
    </div>`;
  }

  // ── D22 Speed donut — arcs weighted by DWT global % ────────────────────────
  function render22(rows, el) {
    const { w, h } = svgBox(el);
    const cx = w / 2, cy = h / 2 + 6, R = Math.min(w, h) * 0.34, r = R * 0.62;
    const buckets = [["0–8 уз", 0, 8], ["8–12 уз", 8, 12], ["12–16 уз", 12, 16], ["16+ уз", 16, 99]];
    const dwtB = buckets.map(() => 0);
    const nB = buckets.map(() => 0);
    rows.forEach((v) => {
      const s = v.speed_knots || 0;
      buckets.forEach((b, i) => { if (s >= b[1] && s < b[2]) { dwtB[i] += v.dwt_tons; nB[i]++; } });
    });
    const sliceDwt = dwtSum(rows) || 1;
    let a = -Math.PI / 2, g = `<text x="${w / 2}" y="12" text-anchor="middle" class="axis-caption">${AXIS_Y}</text>`;
    dwtB.forEach((dwt, i) => {
      const sw = (dwt / sliceDwt) * Math.PI * 2;
      const a2 = a + sw;
      g += `<path class="seg" data-i="${i}" d="${arcPath(cx, cy, R, r, a, a2)}" fill="${PAL[i]}" opacity=".88"></path>`;
      if (sw > 0.18) {
        const mid = a + sw / 2, lr = (R + r) / 2;
        g += `<text x="${cx + lr * Math.cos(mid)}" y="${cy + lr * Math.sin(mid) + 3}" text-anchor="middle" fill="#031018" font-size="9" font-weight="700">${fmtPct(gPct(dwt), 1)}</text>`;
      }
      a = a2;
    });
    g += `<text x="${cx}" y="${cy + 4}" text-anchor="middle" fill="#00f2fe" font-size="12" font-family="Orbitron">${fmtPct(gPct(sliceDwt), 1)}</text>`;
    g += buckets.map((b, i) => `<text x="10" y="${20 + i * 15}" fill="${PAL[i]}" font-size="9">${b[0]} · ${fmtPct(gPct(dwtB[i]), 2)}</text>`).join("");
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
    el.querySelectorAll("path.seg").forEach((p) => {
      const i = Number(p.dataset.i);
      const html = tipBlock({ title: buckets[i][0], n: nB[i], dwt: dwtB[i], sliceDwt, extra: "Атрибуты OSINT: speed_knots" });
      p.onmouseenter = (e) => showTip(e, html);
      p.onmouseleave = hideTip;
    });
  }

  // ── D23 Heatmap — color by anomaly, tip with global % ──────────────────────
  function render23(rows, el) {
    const { w, h } = svgBox(el);
    el.innerHTML = `<canvas width="${w}" height="${h}"></canvas>`;
    const ctx = el.querySelector("canvas").getContext("2d");
    const cols = 20, rowsN = Math.ceil(Math.min(rows.length, 200) / cols);
    const cw = (w - 16) / cols, rh = (h - 32) / Math.max(rowsN, 1);
    const sliceDwt = dwtSum(rows);
    ctx.fillStyle = "#8aa4bf"; ctx.font = "9px JetBrains Mono";
    ctx.fillText(`${AXIS_Y} · ячейка = судно (клик → фильтр IMO)`, 8, 14);
    rows.slice(0, 200).forEach((v, i) => {
      const col = i % cols, row = Math.floor(i / cols);
      const imoOk = /^\d{7}$/.test(String(v.imo || ""));
      const mmsiOk = /^\d{9}$/.test(String(v.mmsi || ""));
      let score = 0;
      if (!imoOk) score += 2;
      if (!mmsiOk) score += 2;
      if (["EXTREME", "HIGH"].includes(v.risk)) score += 1;
      const colors = ["#10b981", "#D4AF37", "#f59e0b", "#ef4444", "#ff0080"];
      ctx.fillStyle = colors[Math.min(score, colors.length - 1)];
      ctx.globalAlpha = 0.2 + Math.min(0.75, gPct(v.dwt_tons) * 8);
      ctx.fillRect(8 + col * cw, 24 + row * rh, cw - 2, rh - 2);
    });
    ctx.globalAlpha = 1;
    const c = el.querySelector("canvas");
    c.onmousemove = (e) => {
      const r = c.getBoundingClientRect();
      const mx = (e.clientX - r.left) * (w / r.width), my = (e.clientY - r.top) * (h / r.height);
      const col = Math.floor((mx - 8) / cw), row = Math.floor((my - 24) / rh);
      const idx = row * cols + col;
      if (rows[idx]) showTip(e, tipVessel(rows[idx], sliceDwt));
      else hideTip();
    };
    c.onmouseleave = hideTip;
    c.onclick = (e) => {
      const r = c.getBoundingClientRect();
      const mx = (e.clientX - r.left) * (w / r.width), my = (e.clientY - r.top) * (h / r.height);
      const col = Math.floor((mx - 8) / cw), row = Math.floor((my - 24) / rh);
      const idx = row * cols + col;
      if (rows[idx]) CF.set("imo", String(rows[idx].imo));
    };
  }

  // ── D24 Gauge — needle = global DWT risk-weighted index ────────────────────
  function render24(rows, el) {
    const { w, h } = svgBox(el);
    const cx = w / 2, cy = h * 0.62, R = Math.min(w, h) * 0.42, r = R * 0.68;
    const scoreMap = { EXTREME: 95, HIGH: 75, MID: 45, LOW: 18, UNK: 35 };
    const sliceDwt = dwtSum(rows) || 1;
    const avg = rows.length
      ? rows.reduce((s, v) => s + (scoreMap[v.risk] || 35) * v.dwt_tons, 0) / sliceDwt
      : 0;
    const start = -Math.PI * 0.75, span = Math.PI * 1.5, end = start + span * (avg / 100);
    function band(a0, a1, col) { return `<path d="${arcPath(cx, cy, R, r, a0, a1)}" fill="${col}"/>`; }
    let g = band(start, start + span, "rgba(160,220,255,.12)");
    g += band(start, start + span * 0.35, "#10b981");
    g += band(start + span * 0.35, start + span * 0.55, "#3b82f6");
    g += band(start + span * 0.55, start + span * 0.75, "#f59e0b");
    g += band(start + span * 0.75, start + span, "#ef4444");
    g += `<path d="${arcPath(cx, cy, R + 6, R - 2, start, end)}" fill="#00f2fe" opacity=".95"/>`;
    g += `<text x="${cx}" y="${cy + 4}" text-anchor="middle" fill="#D4AF37" font-size="22" font-family="Orbitron">${Math.round(avg)}</text>
      <text x="${cx}" y="${cy + 24}" text-anchor="middle" fill="#8aa4bf" font-size="9">DWT-взвешенный риск</text>
      <text x="${cx}" y="${cy + 38}" text-anchor="middle" fill="#ff8ec8" font-size="9">срез ${fmtPct(gPct(sliceDwt))} флота</text>`;
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
  }

  // ── D25 Matrix — cell = global % DWT ───────────────────────────────────────
  function render25(rows, el) {
    const { w, h } = svgBox(el);
    const flagCount = {};
    rows.forEach((v) => { flagCount[v.flag_short] = (flagCount[v.flag_short] || 0) + v.dwt_tons; });
    const flags = Object.entries(flagCount).sort((a, b) => b[1] - a[1]).slice(0, 10).map((x) => x[0]);
    const types = ["LNG", "CRUDE", "PRODUCT", "TANKER", "LPG", "OTHER"];
    const mat = {};
    flags.forEach((f) => { mat[f] = {}; types.forEach((t) => { mat[f][t] = 0; }); });
    rows.forEach((v) => { if (mat[v.flag_short]) mat[v.flag_short][v.vtype] = (mat[v.flag_short][v.vtype] || 0) + v.dwt_tons; });
    const maxGp = Math.max(0.01, ...flags.flatMap((f) => types.map((t) => gPct(mat[f][t]))));
    const pad = { l: 108, r: 8, t: 36, b: 8 };
    const cw = (w - pad.l - pad.r) / types.length, rh = (h - pad.t - pad.b) / Math.max(flags.length, 1);
    const sliceDwt = dwtSum(rows);
    let g = `<text x="${w / 2}" y="14" text-anchor="middle" class="axis-caption">${AXIS_Y}</text>`;
    g += types.map((t, i) => `<text x="${pad.l + i * cw + cw / 2}" y="28" text-anchor="middle" fill="#D4AF37" font-size="8">${VTYPE_RU[t] || t}</text>`).join("");
    flags.forEach((f, fi) => {
      g += `<text x="${pad.l - 6}" y="${pad.t + fi * rh + rh / 2 + 3}" text-anchor="end" fill="#cfe" font-size="9">${f}</text>`;
      types.forEach((t, ti) => {
        const dwt = mat[f][t];
        const a = dwt ? 0.18 + 0.82 * (gPct(dwt) / maxGp) : 0.05;
        g += `<rect class="seg" data-flag="${f}" data-vtype="${t}" data-dwt="${dwt}" x="${pad.l + ti * cw + 2}" y="${pad.t + fi * rh + 2}" width="${cw - 4}" height="${rh - 4}" rx="3"
          fill="#00f2fe" fill-opacity="${a}" stroke="rgba(160,220,255,.15)"/>`;
        if (dwt) g += `<text x="${pad.l + ti * cw + cw / 2}" y="${pad.t + fi * rh + rh / 2 + 3}" text-anchor="middle" fill="#031018" font-size="8" font-weight="700">${fmtPct(gPct(dwt), 1)}</text>`;
      });
    });
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
    el.querySelectorAll("rect.seg").forEach((r) => {
      const dwt = Number(r.dataset.dwt);
      const n = rows.filter((v) => v.flag_short === r.dataset.flag && v.vtype === r.dataset.vtype).length;
      const html = tipBlock({
        title: `${r.dataset.flag} × ${VTYPE_RU[r.dataset.vtype] || r.dataset.vtype}`,
        n, dwt, sliceDwt, extra: "Атрибуты OSINT: флаг / тип",
      });
      r.style.cursor = "pointer";
      r.onmouseenter = (e) => showTip(e, html);
      r.onmousemove = (e) => showTip(e, html);
      r.onmouseleave = hideTip;
      r.onclick = () => CF.setMany({ flag: r.dataset.flag, vtype: r.dataset.vtype });
    });
  }

  // ── D26 Fuel lines — Y = global % of fuel-proxy DWT-weighted burn ──────────
  function render26(rows, el) {
    const { w, h } = svgBox(el);
    const pad = { l: 48, r: 12, t: 28, b: 36 };
    // Aggregate fuel_tpd * weight → represent as share of fleet via proxy: vessel dwt share scaled
    const years = [...new Set(rows.map((v) => v.built_year).filter(Boolean))].sort();
    const types = ["LNG", "CRUDE", "TANKER", "PRODUCT"];
    const series = {};
    types.forEach((t) => { series[t] = {}; });
    rows.forEach((v) => {
      if (!v.built_year) return;
      const t = types.includes(v.vtype) ? v.vtype : "TANKER";
      if (!series[t][v.built_year]) series[t][v.built_year] = [];
      series[t][v.built_year].push(v.dwt_tons);
    });
    const allY = years.length ? years : [2000, 2020];
    const vals = types.flatMap((t) => Object.values(series[t]).map((a) => gPct(a.reduce((s, x) => s + x, 0) / a.length * a.length)));
    const maxY = Math.max(...vals, 0.01);
    const X = (i) => pad.l + (i / Math.max(allY.length - 1, 1)) * (w - pad.l - pad.r);
    const Y = (v) => h - pad.b - (v / maxY) * (h - pad.t - pad.b);
    let g = `<text x="${w / 2}" y="14" text-anchor="middle" class="axis-caption">${AXIS_Y} · DWT по году постройки</text>${axisYLabel(w, h)}`;
    types.forEach((t) => {
      const pts = allY.map((y, i) => {
        const arr = series[t][y];
        if (!arr || !arr.length) return null;
        return [X(i), Y(gPct(arr.reduce((s, x) => s + x, 0)))];
      }).filter(Boolean);
      if (pts.length < 2) return;
      g += `<path d="M${pts.map((p) => p.join(",")).join(" L")}" fill="none" stroke="${COLORS[t]}" stroke-width="2.2"/>`;
      pts.forEach((p) => { g += `<circle cx="${p[0]}" cy="${p[1]}" r="2.8" fill="${COLORS[t]}"/>`; });
    });
    g += types.map((t, i) => `<text x="${pad.l + i * 72}" y="${h - 10}" fill="${COLORS[t]}" font-size="9">${VTYPE_RU[t]}</text>`).join("");
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
  }

  // ── D27 Sunburst — labels = global % ───────────────────────────────────────
  function render27(rows, el) {
    const { w, h } = svgBox(el);
    const cx = w / 2, cy = h / 2, R0 = 40, R1 = 74, R2 = 106, R3 = Math.min(w, h) * 0.44;
    const tree = {};
    rows.forEach((v) => {
      const a = v.vtype || "OTHER", b = v.region || "Прочее", c = shortPort(v.destination_port) || "—";
      if (!tree[a]) tree[a] = {};
      if (!tree[a][b]) tree[a][b] = {};
      if (!tree[a][b][c]) tree[a][b][c] = { n: 0, dwt: 0 };
      tree[a][b][c].n++; tree[a][b][c].dwt += v.dwt_tons;
    });
    const sliceDwt = dwtSum(rows) || 1;
    let a = -Math.PI / 2, g = "";
    Object.keys(tree).forEach((t, ti) => {
      const dwtT = Object.values(tree[t]).reduce((s, reg) => s + Object.values(reg).reduce((p, q) => p + q.dwt, 0), 0);
      const sw = (dwtT / sliceDwt) * Math.PI * 2, a2 = a + sw;
      g += `<path class="seg" data-vtype="${t}" d="${arcPath(cx, cy, R1, R0, a, a2)}" fill="${COLORS[t] || PAL[ti]}" opacity=".9"></path>`;
      if (sw > 0.22) {
        const mid = a + sw / 2, lr = (R0 + R1) / 2;
        g += `<text x="${cx + lr * Math.cos(mid)}" y="${cy + lr * Math.sin(mid) + 3}" text-anchor="middle" fill="#031018" font-size="8" font-weight="700">${fmtPct(gPct(dwtT), 1)}</text>`;
      }
      let ra = a;
      Object.entries(tree[t]).forEach(([reg, ports], ri) => {
        const dwtR = Object.values(ports).reduce((s, x) => s + x.dwt, 0);
        const rsw = (dwtR / sliceDwt) * Math.PI * 2, ra2 = ra + rsw;
        g += `<path class="seg" data-vtype="${t}" data-region="${reg}" d="${arcPath(cx, cy, R2, R1, ra, ra2)}" fill="${PAL[(ti + ri) % PAL.length]}" opacity=".6"></path>`;
        let pa = ra;
        Object.entries(ports).forEach(([port, cell], pi) => {
          const pw = (cell.dwt / sliceDwt) * Math.PI * 2, pa2 = pa + pw;
          g += `<path class="seg" data-vtype="${t}" data-region="${reg}" data-port="${port.replace(/"/g, "&quot;")}" data-dwt="${cell.dwt}" data-n="${cell.n}"
            d="${arcPath(cx, cy, R3, R2, pa, pa2)}" fill="${PAL[(ti + ri + pi + 2) % PAL.length]}" opacity=".48"></path>`;
          pa = pa2;
        });
        ra = ra2;
      });
      a = a2;
    });
    g += `<circle cx="${cx}" cy="${cy}" r="${R0 - 3}" fill="rgba(3,7,15,.95)" stroke="rgba(212,175,55,.45)"/>
      <text x="${cx}" y="${cy - 6}" text-anchor="middle" fill="#D4AF37" font-size="13" font-family="Orbitron">${fmtPct(gPct(sliceDwt))}</text>
      <text x="${cx}" y="${cy + 10}" text-anchor="middle" fill="#8aa4bf" font-size="8">${rows.length} · ${fmtMln(sliceDwt)}</text>
      <text x="${cx}" y="${cy + 22}" text-anchor="middle" fill="#ff8ec8" font-size="7">от ${fmtMln(FLEET_DWT)}</text>`;
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
    el.querySelectorAll("path.seg").forEach((p) => {
      const vt = p.dataset.vtype, reg = p.dataset.region, port = p.dataset.port;
      const subset = rows.filter((v) => v.vtype === vt && (!reg || v.region === reg) && (!port || shortPort(v.destination_port) === port));
      const dwt = dwtSum(subset);
      const title = port ? `Порт назначения: ${port}` : reg ? reg : (VTYPE_RU[vt] || vt);
      const html = tipBlock({ title, n: subset.length, dwt, sliceDwt, extra: "Атрибуты OSINT: тип → регион → порт" });
      p.onmouseenter = (e) => showTip(e, html);
      p.onmousemove = (e) => showTip(e, html);
      p.onmouseleave = hideTip;
      p.onclick = () => {
        if (port) CF.setMany({ vtype: vt, region: reg, port });
        else if (reg) CF.setMany({ vtype: vt, region: reg, port: null });
        else CF.set("vtype", vt);
      };
    });
  }

  /** Soft morph для Д18–Д27: opacity pulse ≈ chart.update('active') без полного thrash DOM */
  const SOFT_CHART_IDS = new Set(["c18", "c19", "c20", "c21", "c22", "c23", "c24", "c25", "c26", "c27"]);

  function softPaint(el, paintFn) {
    if (!el) return;
    el.classList.add("chart-soft-out");
    // двойной rAF — кадр на fade-out, затем перерисовка + fade-in (~60 FPS)
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        paintFn();
        el.classList.remove("chart-soft-out");
        el.classList.add("chart-soft-in");
        setTimeout(() => el.classList.remove("chart-soft-in"), 220);
      });
    });
  }

  function applyCrossFilter() {
    CF.applyCrossFilter();
  }
  window.applyCrossFilter = applyCrossFilter;

  function renderAll(soft) {
    const rows = CF.filtered();
    renderKPI(rows);
    const raw = {
      c13: render13, c14: render14, c15: render15, c16: render16, c17: render17,
      c18: render18, c19: render19, c20: render20, c21: render21, c22: render22,
      c23: render23, c24: render24, c25: render25, c26: render26, c27: render27,
    };
    Object.entries(raw).forEach(([id, fn]) => {
      const el = document.getElementById(id);
      if (!el) return;
      const paint = () => {
        if (id === "c13") fn(rows, el);
        else safeChart(fn)(rows, el);
      };
      if (soft && SOFT_CHART_IDS.has(id)) softPaint(el, paint);
      else paint();
    });
    CF.broadcast(rows);
  }

  // D13 mode toggle (persist view mode)
  document.querySelectorAll(".d13-mode").forEach((b) => {
    b.onclick = () => {
      d13Mode = b.dataset.mode;
      try { localStorage.setItem("top200_d13_view_mode", d13Mode); } catch (_e) {}
      syncD13ModeUI();
      renderAll();
    };
  });

  // Boot UI bindings
  document.getElementById("tabs").querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("on"));
      document.querySelectorAll(".panel").forEach((x) => x.classList.remove("on"));
      t.classList.add("on");
      document.getElementById(t.dataset.tab).classList.add("on");
      setTimeout(() => renderAll(), 40);
    };
  });

  const PRESETS = {
    shadow: (v) => GREY_FLAG_RE.test(v.flag || "") || (v.sanctions_tags || []).length > 0 || ["EXTREME", "HIGH"].includes(v.risk),
    age20: (v) => (v.age_years || 0) > 20,
    lng: (v) => v.vtype === "LNG",
    extreme: (v) => v.risk === "EXTREME" || v.risk === "HIGH",
    vlcc: (v) => v.vtype === "CRUDE" || /vlcc|ulcc/i.test(v.vessel_type || ""),
  };
  document.querySelectorAll(".preset").forEach((b) => {
    b.onclick = () => CF.applyPreset(b.dataset.preset, PRESETS[b.dataset.preset]);
  });

  document.getElementById("fabReset").onclick = () => CF.clear();
  document.getElementById("footCopy").textContent = PAYLOAD.copyright || "© ORACLE-1001";
  window.addEventListener("resize", () => {
    cancelAnimationFrame(CF._raf);
    CF._raf = requestAnimationFrame(() => renderAll());
  });

  renderAll();
})();
