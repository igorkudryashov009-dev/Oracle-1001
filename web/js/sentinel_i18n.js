/* Server catalogs only. Switching redraws text nodes and does not touch AIS sockets. */
(function () {
  var LANGS = ["en", "ru", "de", "fr", "es", "pt", "zh", "ja", "ar", "ko"];
  var cache = null;
  var current = "en";

  function readCookie() {
    var parts = String(document.cookie || "").split(";");
    for (var i = 0; i < parts.length; i++) {
      var bit = parts[i].trim();
      if (bit.indexOf("sentinel_lang=") === 0) return bit.split("=")[1];
    }
    return "";
  }

  function writeCookie(lang) {
    document.cookie = "sentinel_lang=" + lang + "; Path=/; Max-Age=31536000; SameSite=Lax";
  }

  function apply(doc) {
    current = doc.lang || "en";
    var root = document.documentElement;
    root.lang = current;
    root.dir = doc.dir === "rtl" ? "rtl" : "ltr";
    var strings = doc.strings || {};
    var nodes = document.querySelectorAll("[data-i18n]");
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].getAttribute("data-i18n-live") === "1") continue;
      var key = nodes[i].getAttribute("data-i18n");
      if (strings[key]) nodes[i].textContent = strings[key];
    }
    var select = document.getElementById("langSwitch");
    if (select && select.value !== current) select.value = current;
    window.__sentinelI18n = { lang: current, strings: strings, t: t };
  }

  function t(key, fallback) {
    var strings = (window.__sentinelI18n && window.__sentinelI18n.strings) || {};
    return strings[key] || fallback || key;
  }

  function load(lang) {
    var url = "/api/v1/i18n/catalog?lang=" + encodeURIComponent(lang || "");
    return fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (res) { return res.json(); })
      .then(function (doc) {
        cache = doc;
        apply(doc);
        return doc;
      });
  }

  function mount() {
    var host = document.querySelector(".nav-links");
    if (!host || document.getElementById("langSwitch")) return;
    var select = document.createElement("select");
    select.id = "langSwitch";
    select.className = "pill";
    select.setAttribute("aria-label", "Language");
    LANGS.forEach(function (code) {
      var opt = document.createElement("option");
      opt.value = code;
      opt.textContent = code;
      select.appendChild(opt);
    });
    select.addEventListener("change", function () {
      writeCookie(select.value);
      load(select.value);
    });
    host.appendChild(select);
    var initial = readCookie() || (new URLSearchParams(location.search).get("lang") || "");
    if (initial) writeCookie(initial);
    load(initial);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
  window.SentinelI18n = { t: t, load: load, langs: LANGS };
})();
