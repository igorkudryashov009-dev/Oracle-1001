"""UX verification — Apple×NASA design system gate."""
import os
import urllib.request, sys

PORT = int(os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or "8765")
URL = f"http://127.0.0.1:{PORT}/output/sentinel_dashboard.html"

try:
    r = urllib.request.urlopen(URL, timeout=6)
    c = r.read().decode("utf-8", errors="replace")
except Exception as e:
    print(f"FAIL Cannot reach {URL}: {e}")
    sys.exit(1)

checks = [
    ("Balance sheet DOM element",       "sheet-balance"),
    ("Balance routing JS",              'sheet === "balance"'),
    ("balance_engine.js module",        "balance_engine.js"),
    ("sentinel_hud.css linked",         "sentinel_hud.css"),
    ("Glassmorphic backdrop-filter",    "backdrop-filter"),
    ("Top10 grid container",            "top10-grid-container"),
    ("t10-card glassmorphic class",     "t10-card"),
    ("NASA inspector modal",            "t10-modal"),
    ("JetBrains Mono font",             "JetBrains Mono"),
    ("Design token --t10-cyan",         "--t10-cyan"),
    ("Design token --t10-glass",        "--t10-glass"),
    ("Design token --t10-space",        "--t10-space"),
    ("Balance panel CSS .bal-panel",    "bal-panel"),
    ("Balance panel header CSS",        "bal-panel-hdr"),
    ("top10_sheet.js ES module",        "top10_sheet.js"),
    ("Deep space bg #07090e",           "07090e"),
    ("Apple Frost White #f5f5f7",       "f5f5f7"),
    ("Titanium Cyan #00f0ff",           "00f0ff"),
    ("Muted Amber #ffb300",             "ffb300"),
    ("Balance KPI row",                 "bal-kpi-row"),
    ("Anomaly table CSS",               "bal-anomaly-table"),
    ("DFS ring gauge CSS",              "bal-dfs-ring"),
    ("PIL latency bar CSS",             "bal-pil-bar"),
    ("WebGL context-lost CSS rule",     "webgl-context-lost"),
    ("Global body bg comment",          "GLOBAL BODY"),
    ("Ambient micro-glow animation",    "t10CyanPulse"),
    ("Modal entry animation",           "t10ModalIn"),
    ("Balance sheet display rule",      'html[data-sheet="balance"]'),
    ("Quant panel CSS",                 "bal-quant-card"),
    ("SRE truth pill",                  "bal-truth"),
]

passed = failed = 0
for label, token in checks:
    ok = token in c
    tag = "PASS" if ok else "FAIL"
    if ok: passed += 1
    else:  failed += 1
    print(f"  {tag}  {label}")

print()
print(f"HTML size: {len(c):,} chars")
print(f"Result: {passed} PASS / {failed} FAIL")
sys.exit(0 if failed == 0 else 1)
