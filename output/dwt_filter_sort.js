/**
 * DWT filter + sort for Fleet Dashboard.
 * Expects <tr data-dwt="N"> (N = numeric DWT, or 0 when absent in source).
 * Honest degradation: missing/invalid DWT never invents tonnage; when a numeric
 * range is active such rows fail the filter; with empty from/to they stay visible.
 *
 * Works with jQuery DataTables (#fleet-table) when present; otherwise falls back
 * to DOM show/hide on tbody rows.
 */
(function (global) {
  "use strict";

  var DEFAULTS = {
    tableSelector: "#fleet-table",
    fromId: "dwt-from",
    toId: "dwt-to",
    sortBtnId: "dwt-sort-toggle",
    dwtColumnData: "dwt_tons",
  };

  function attrValue(dwt) {
    if (dwt === null || dwt === undefined || dwt === "") return "0";
    var n = Number(dwt);
    if (!Number.isFinite(n)) return "0";
    return Object.is(n, -0) ? "0" : String(n);
  }

  function parseOptionalNumber(raw) {
    if (raw === null || raw === undefined) return null;
    var s = String(raw).trim();
    if (s === "") return null;
    var n = Number(s);
    return Number.isFinite(n) ? n : null;
  }

  /** Numeric DWT for filtering; null = absent/invalid (do not treat data-dwt=0 as real). */
  function rowDwtNumeric(rowData, tr) {
    if (rowData && Object.prototype.hasOwnProperty.call(rowData, "dwt_tons")) {
      var v = rowData.dwt_tons;
      if (v === null || v === undefined || v === "") return null;
      var n = Number(v);
      return Number.isFinite(n) ? n : null;
    }
    if (tr) {
      if (tr.getAttribute("data-dwt-missing") === "1") return null;
      var raw = tr.getAttribute("data-dwt");
      if (raw === null || raw === "") return null;
      var n2 = Number(raw);
      if (!Number.isFinite(n2)) return null;
      // Sentinel 0 from generator means missing when source had no DWT
      if (n2 === 0 && tr.getAttribute("data-dwt-missing") === "1") return null;
      return n2;
    }
    return null;
  }

  function readRange(cfg) {
    var fromEl = document.getElementById(cfg.fromId);
    var toEl = document.getElementById(cfg.toId);
    return {
      from: fromEl ? parseOptionalNumber(fromEl.value) : null,
      to: toEl ? parseOptionalNumber(toEl.value) : null,
    };
  }

  function passesRange(dwt, from, to) {
    if (from === null && to === null) return true;
    if (dwt === null) return false;
    if (from !== null && dwt < from) return false;
    if (to !== null && dwt > to) return false;
    return true;
  }

  function findDwtColIndex(api, dwtColumnData) {
    var cols = api.settings()[0].aoColumns || [];
    for (var i = 0; i < cols.length; i++) {
      if (cols[i].mData === dwtColumnData || cols[i].data === dwtColumnData) return i;
    }
    return -1;
  }

  function applyDomFallback(cfg, from, to, sortDir) {
    var table = document.querySelector(cfg.tableSelector);
    if (!table || !table.tBodies.length) return;
    var rows = Array.prototype.slice.call(table.tBodies[0].rows);
    rows.forEach(function (tr) {
      var missing = tr.getAttribute("data-dwt-missing") === "1";
      var dwt = missing ? null : rowDwtNumeric(null, tr);
      tr.style.display = passesRange(dwt, from, to) ? "" : "none";
    });
    if (sortDir === "asc" || sortDir === "desc") {
      var visible = rows.filter(function (tr) {
        return tr.style.display !== "none";
      });
      visible.sort(function (a, b) {
        var da = rowDwtNumeric(null, a);
        var db = rowDwtNumeric(null, b);
        var na = da === null ? -Infinity : da;
        var nb = db === null ? -Infinity : db;
        return sortDir === "asc" ? na - nb : nb - na;
      });
      var tbody = table.tBodies[0];
      visible.forEach(function (tr) {
        tbody.appendChild(tr);
      });
    }
  }

  function updateSortButton(btn, dir) {
    if (!btn) return;
    btn.setAttribute("data-dir", dir);
    btn.textContent = dir === "asc" ? "DWT ↑ возр." : "DWT ↓ убыв.";
    btn.setAttribute("aria-label", dir === "asc" ? "Сортировка DWT по возрастанию" : "Сортировка DWT по убыванию");
  }

  function init(options) {
    var cfg = Object.assign({}, DEFAULTS, options || {});
    var fromEl = document.getElementById(cfg.fromId);
    var toEl = document.getElementById(cfg.toId);
    var sortBtn = document.getElementById(cfg.sortBtnId);
    var sortDir = (sortBtn && sortBtn.getAttribute("data-dir")) || "desc";
    updateSortButton(sortBtn, sortDir);

    var dt = cfg.dataTable || null;
    if (!dt && global.jQuery && global.jQuery.fn && global.jQuery.fn.dataTable) {
      var $ = global.jQuery;
      if ($.fn.dataTable.isDataTable(cfg.tableSelector)) {
        dt = $(cfg.tableSelector).DataTable();
      }
    }

    function apply() {
      var range = readRange(cfg);
      if (dt) {
        // Drop previous DWT search fn, keep other fleet filters
        global.jQuery.fn.dataTable.ext.search = global.jQuery.fn.dataTable.ext.search.filter(function (fn) {
          return !fn._dwtFilter;
        });
        var filterFn = function (settings, _data, dataIndex) {
          if (settings.nTable.id !== "fleet-table") return true;
          var row = dt.row(dataIndex).data();
          var dwt = rowDwtNumeric(row, null);
          return passesRange(dwt, range.from, range.to);
        };
        filterFn._dwtFilter = true;
        global.jQuery.fn.dataTable.ext.search.push(filterFn);
        dt.draw();
      } else {
        applyDomFallback(cfg, range.from, range.to, null);
      }
    }

    function applySort() {
      if (dt) {
        var idx = findDwtColIndex(dt, cfg.dwtColumnData);
        if (idx >= 0) {
          dt.order([[idx, sortDir]]).draw();
        }
      } else {
        var range = readRange(cfg);
        applyDomFallback(cfg, range.from, range.to, sortDir);
      }
    }

    function onSortClick() {
      sortDir = sortDir === "asc" ? "desc" : "asc";
      updateSortButton(sortBtn, sortDir);
      applySort();
    }

    if (fromEl) fromEl.addEventListener("input", apply);
    if (toEl) toEl.addEventListener("input", apply);
    if (fromEl) fromEl.addEventListener("change", apply);
    if (toEl) toEl.addEventListener("change", apply);
    if (sortBtn) sortBtn.addEventListener("click", onSortClick);

    return {
      apply: apply,
      applySort: applySort,
      attrValue: attrValue,
      getSortDir: function () {
        return sortDir;
      },
    };
  }

  function stampRow(tr, dwtTons) {
    var missing =
      dwtTons === null ||
      dwtTons === undefined ||
      dwtTons === "" ||
      !Number.isFinite(Number(dwtTons));
    tr.setAttribute("data-dwt", attrValue(missing ? null : dwtTons));
    if (missing) tr.setAttribute("data-dwt-missing", "1");
    else tr.removeAttribute("data-dwt-missing");
  }

  global.DwtFilterSort = {
    init: init,
    attrValue: attrValue,
    stampRow: stampRow,
    passesRange: passesRange,
    parseOptionalNumber: parseOptionalNumber,
  };
})(typeof window !== "undefined" ? window : globalThis);
