/**
 * Sentinel HUD — Archive sheet (daily vessel_daily_archive snapshots).
 * No-scroll fixed grid + pagination; VesselFinder / hybrid API status.
 */
const MANIFEST_URL = "/output/archive/manifest.json";
const API_STATUS_URL = "/output/archive/api_status.json";
const DAILY_BALANCE_URLS = [
  "/output/daily_balance.json",
  "/output/archive/daily_balance.json",
];
const PAGE_SIZE = 18;

let dates = [];
let rows = [];
let current = "";
let page = 0;
let booted = false;

function el(id) {
  return document.getElementById(id);
}

function fmt(n, d = 0) {
  const x = Number(n);
  return Number.isFinite(x) ? x.toFixed(d) : "—";
}

function fmtInt(n) {
  const x = Number(n);
  return Number.isFinite(x) ? x.toLocaleString("en-US") : "—";
}

function esc(s) {
  return String(s ?? "—")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/"/g, "&quot;");
}

async function loadApiStatus() {
  try {
    const s = await fetch(API_STATUS_URL, { cache: "no-store" }).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    });

    let liveCoverage = "—";
    try {
      const h = await fetch("/output/api/v1/health", { cache: "no-store" }).then((r) => r.json());
      const n = h.top500_live_coverage ?? (h.fleet_sample || {}).top500_live_coverage;
      if (n != null) liveCoverage = String(n);
    } catch {
      /* health fetch optional */
    }

    const wrap = el("arch-api-status");
    const isCommercial = s.ingest_mode === "commercial_rest" && s.status === "OK";
    const tone = String(s.ui_tone || (isCommercial ? "ok" : "hybrid"));
    if (wrap) {
      wrap.classList.remove("is-ok", "is-hybrid", "is-degraded");
      wrap.classList.add(
        tone === "ok" ? "is-ok" : tone === "degraded" ? "is-degraded" : "is-hybrid"
      );
    }
    if (el("arch-api-plan")) {
      el("arch-api-plan").textContent = isCommercial
        ? "COMMERCIAL REST API"
        : "HYBRID LOCAL (OSINT SNAPSHOT)";
    }
    if (el("arch-api-key")) {
      el("arch-api-key").textContent = String(
        s.ui_key ||
          (s.key_masked ? `AUTO-RESOLVED (${s.key_masked})` : "—")
      );
    }
    if (el("arch-api-status-text")) {
      el("arch-api-status-text").textContent = String(s.ui_status || s.status || "NOMINAL");
    }

    const t1 = Number(s.tier1_gas_count) || 1253;
    const t2 = Number(s.tier2_oil_count) || 0;
    const knownTotal = t2 > 0 ? `${fmtInt(t1)}+${fmtInt(t2)}` : fmtInt(s.total_monitored || t1);

    if (el("arch-known-fleet-kpi")) {
      el("arch-known-fleet-kpi").textContent = `${knownTotal} vessels`;
    }
    if (el("arch-live-g3-kpi")) {
      el("arch-live-g3-kpi").textContent = `N=${liveCoverage} live`;
    }
    if (el("arch-known-fleet-desc")) {
      const snapDate = s.last_sync_at ? s.last_sync_at.slice(0, 10) : "2026-09-12";
      el("arch-known-fleet-desc").textContent = `${knownTotal} known vessels`;
    }
    if (el("arch-live-g3-desc")) {
      el("arch-live-g3-desc").textContent = `N=${liveCoverage} live AIS-tracked`;
    }

    if (el("arch-api-slots")) {
      const slots =
        s.slots_label ||
        `${s.slots_used ?? "—"}/${s.slots_limit ?? 500} (ROTATING)`;
      el("arch-api-slots").textContent = slots;
    }
    if (el("arch-bal-tier")) {
      const label = s.rotation_label || s.active_tier_label || "";
      const tier = s.active_tier != null ? `TIER ${s.active_tier}` : "—";
      el("arch-bal-tier").textContent = label || tier;
    }
    if (el("arch-bal-rot-sub")) {
      const phase = s.slot_phase != null ? `phase ${s.slot_phase}/2` : "slot phase";
      const oil = Number(s.tier2_oil_count);
      el("arch-bal-rot-sub").textContent = Number.isFinite(oil)
        ? `${phase} · oil ${fmtInt(oil)}`
        : phase;
    }
  } catch {
    /* keep HTML defaults */
  }
}

async function fetchDailyBalance() {
  let lastErr = null;
  for (const url of DAILY_BALANCE_URLS) {
    try {
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr || new Error("daily_balance unavailable");
}

async function loadDailyBalance() {
  try {
    const b = await fetchDailyBalance();
    if (el("arch-bal-oil")) {
      el("arch-bal-oil").textContent = fmt(b.oil_transit_ktons ?? b.oil_in_transit_ktons, 1);
    }
    if (el("arch-bal-gas")) {
      el("arch-bal-gas").textContent = fmt(b.gas_transit_ktons ?? b.gas_in_transit_ktons, 1);
    }
    if (el("arch-bal-date")) {
      const cov = b.coverage_status ? ` · ${b.coverage_status}` : "";
      el("arch-bal-date").textContent = `${String(b.date || b.balance_date || "—")}${cov}`;
    }
    if (el("arch-bal-oil-sub")) {
      const n = b.oil_vessels;
      el("arch-bal-oil-sub").textContent =
        n != null ? `Tier 2 · ${fmtInt(n)} laden samples` : "Tier 2 · tankers";
    }
    if (el("arch-bal-gas-sub")) {
      const n = b.gas_vessels;
      el("arch-bal-gas-sub").textContent =
        n != null ? `Tier 1 · ${fmtInt(n)} laden samples` : "Tier 1 · LNG/LPG";
    }
    if (el("arch-bal-sample")) {
      const hrs = b.lookback_hours != null ? `${b.lookback_hours}h` : "7d";
      el("arch-bal-sample").textContent = `sample n=${fmtInt(b.sample_n ?? b.n ?? 0)} · ${hrs}`;
    }
    const strip = el("arch-balance-strip");
    if (strip) {
      strip.classList.remove("is-adequate", "is-limited", "is-insufficient");
      const cov = String(b.coverage_status || "").toUpperCase();
      if (cov === "ADEQUATE") strip.classList.add("is-adequate");
      else if (cov === "LIMITED") strip.classList.add("is-limited");
      else if (cov === "INSUFFICIENT") strip.classList.add("is-insufficient");
    }
  } catch {
    /* keep HTML defaults */
  }
}

async function loadManifest() {
  const m = await fetch(MANIFEST_URL, { cache: "no-store" }).then((r) => r.json());
  dates = (m.dates || []).slice().sort();
  current = m.latest || dates[dates.length - 1] || "";
  const pick = el("arch-date");
  const scrub = el("arch-scrub");
  if (pick) {
    pick.innerHTML = dates.map((d) => `<option value="${d}">${d}</option>`).join("");
    pick.value = current;
  }
  if (scrub) {
    scrub.max = Math.max(0, dates.length - 1);
    scrub.value = String(Math.max(0, dates.indexOf(current)));
  }
  const open = el("arch-open-full");
  if (open) open.href = "/output/archive_dashboard.html";
  return m;
}

async function loadDate(day) {
  if (!day) return;
  const st = el("arch-status");
  if (st) st.textContent = `Loading ${day}…`;
  const data = await fetch(`/output/archive/snapshots/${day}.json`, { cache: "no-store" }).then(
    (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    }
  );
  rows = data.vessels || [];
  current = day;
  page = 0;
  if (el("arch-date")) el("arch-date").value = day;
  fillFilters();
  render();
  if (st) st.textContent = `${fmtInt(rows.length)} vessels · ${day} · no-scroll page ${PAGE_SIZE}`;
}

function fillFilters() {
  const flags = [...new Set(rows.map((r) => r.flag).filter(Boolean))].sort();
  const risks = [...new Set(rows.map((r) => r.risk_level).filter(Boolean))].sort();
  const f = el("arch-flag");
  const rk = el("arch-risk");
  if (f) {
    const v = f.value;
    f.innerHTML = `<option value="">ALL FLAGS</option>` + flags.map((x) => `<option>${x}</option>`).join("");
    f.value = v;
  }
  if (rk) {
    const v = rk.value;
    rk.innerHTML = `<option value="">ALL RISK</option>` + risks.map((x) => `<option>${x}</option>`).join("");
    rk.value = v;
  }
}

function filtered() {
  const q = (el("arch-q")?.value || "").trim().toLowerCase();
  const flag = el("arch-flag")?.value || "";
  const risk = el("arch-risk")?.value || "";
  const amin = Number(el("arch-ain")?.value || 0);
  return rows.filter((r) => {
    if (flag && r.flag !== flag) return false;
    if (risk && r.risk_level !== risk) return false;
    if (Number(r.ais_integrity || 0) < amin) return false;
    if (!q) return true;
    return `${r.imo} ${r.vessel_name || ""} ${r.mmsi || ""}`.toLowerCase().includes(q);
  });
}

function render() {
  const list = filtered();
  const live = list.filter((r) => Number(r.ais_integrity || 0) >= 0.5).length;
  const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
  if (page >= pages) page = pages - 1;
  if (page < 0) page = 0;
  const slice = list.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);

  if (el("arch-k-n")) el("arch-k-n").textContent = fmtInt(list.length);
  if (el("arch-k-live")) el("arch-k-live").textContent = fmtInt(live);
  if (el("arch-k-date")) el("arch-k-date").textContent = current || "—";

  const tb = el("arch-tbody");
  if (tb) {
    tb.innerHTML = slice
      .map(
        (r) => `<tr>
      <td title="${esc(r.imo)}">${esc(r.imo)}</td>
      <td title="${esc(r.vessel_name)}">${esc(String(r.vessel_name || "—").slice(0, 28))}</td>
      <td title="${esc(r.mmsi)}">${esc(r.mmsi)}</td>
      <td title="${esc(r.flag)}">${esc(r.flag)}</td>
      <td>${fmt(r.dwt, 0)}</td>
      <td>${fmt(r.speed, 1)}</td>
      <td title="${esc(r.risk_level)}">${esc(r.risk_level)}</td>
      <td title="${esc(r.destination_port)}">${esc(String(r.destination_port || "—").slice(0, 24))}</td>
      <td>${fmt(r.ais_integrity, 2)}</td>
    </tr>`
      )
      .join("");
  }

  const label = el("arch-page-label");
  if (label) {
    label.textContent = `Page ${page + 1} / ${pages} · showing ${slice.length} of ${fmtInt(list.length)}`;
  }
  const prev = el("arch-prev");
  const next = el("arch-next");
  if (prev) prev.disabled = page <= 0;
  if (next) next.disabled = page >= pages - 1;
}

function setKeyMsg(text, kind) {
  const msg = el("arch-key-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.classList.remove("is-ok", "is-err");
  if (kind === "ok") msg.classList.add("is-ok");
  if (kind === "err") msg.classList.add("is-err");
}

function openKeyModal() {
  const modal = el("arch-key-modal");
  if (!modal) return;
  modal.hidden = false;
  setKeyMsg("", null);
  const input = el("arch-key-input");
  if (input) {
    input.value = "";
    setTimeout(() => input.focus(), 30);
  }
}

function closeKeyModal() {
  const modal = el("arch-key-modal");
  if (modal) modal.hidden = true;
}

async function submitUserkey() {
  const input = el("arch-key-input");
  const submit = el("arch-key-submit");
  const userkey = (input?.value || "").trim();
  if (!userkey) {
    setKeyMsg("Empty key provided", "err");
    return;
  }
  const runSync = !!el("arch-key-sync")?.checked;
  if (submit) submit.disabled = true;
  setKeyMsg("Validating…", null);
  try {
    const res = await fetch("/api/config/update-key", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ userkey, run_sync: runSync }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      setKeyMsg(data.message || `Applied ${data.key_masked || ""}`, "ok");
      await loadApiStatus();
      setTimeout(closeKeyModal, 900);
    } else {
      setKeyMsg(data.error || `HTTP ${res.status}`, "err");
      await loadApiStatus();
    }
  } catch (err) {
    setKeyMsg(String(err?.message || err), "err");
  } finally {
    if (submit) submit.disabled = false;
  }
}

function wire() {
  el("arch-date")?.addEventListener("change", () => loadDate(el("arch-date").value));
  el("arch-scrub")?.addEventListener("input", () => {
    const i = Number(el("arch-scrub").value || 0);
    if (dates[i]) loadDate(dates[i]);
  });
  const onFilter = () => {
    page = 0;
    render();
  };
  el("arch-q")?.addEventListener("input", onFilter);
  el("arch-flag")?.addEventListener("change", onFilter);
  el("arch-risk")?.addEventListener("change", onFilter);
  el("arch-ain")?.addEventListener("input", onFilter);
  el("arch-prev")?.addEventListener("click", () => {
    page -= 1;
    render();
  });
  el("arch-next")?.addEventListener("click", () => {
    page += 1;
    render();
  });
  el("arch-csv")?.addEventListener("click", () => {
    if (current) window.open(`/output/archive/snapshots/${current}.csv`, "_blank");
  });
  el("arch-json")?.addEventListener("click", () => {
    if (current) window.open(`/output/archive/snapshots/${current}.json`, "_blank");
  });
  el("arch-open-key-modal")?.addEventListener("click", openKeyModal);
  el("arch-key-cancel")?.addEventListener("click", closeKeyModal);
  el("arch-key-backdrop")?.addEventListener("click", closeKeyModal);
  el("arch-key-submit")?.addEventListener("click", submitUserkey);
  el("arch-key-input")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") submitUserkey();
    if (ev.key === "Escape") closeKeyModal();
  });
}

export async function bootArchiveSheet() {
  if (booted) {
    if (current) render();
    loadApiStatus();
    loadDailyBalance();
    return;
  }
  booted = true;
  wire();
  try {
    await Promise.all([loadApiStatus(), loadDailyBalance(), loadManifest()]);
    if (current) await loadDate(current);
  } catch (err) {
    const st = el("arch-status");
    if (st) st.textContent = `Archive unavailable: ${err?.message || err}`;
  }
}

document.addEventListener("sentinelSheetChange", (ev) => {
  if (ev?.detail?.sheet === "archive") bootArchiveSheet();
});

if (typeof window !== "undefined") {
  window.bootArchiveSheet = bootArchiveSheet;
}
