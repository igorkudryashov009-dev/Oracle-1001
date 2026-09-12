"""Full Apple×NASA UX verification — HTML + CSS + JS gates."""
import os
import urllib.request, sys

PORT = int(os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or "8765")

def fetch(url):
    try:
        r = urllib.request.urlopen(url, timeout=6)
        return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ERROR  Cannot fetch {url}: {e}")
        return ""

html = fetch(f"http://127.0.0.1:{PORT}/output/sentinel_dashboard.html")
css  = fetch(f"http://127.0.0.1:{PORT}/output/css/sentinel_hud.css")
js   = fetch(f"http://127.0.0.1:{PORT}/output/js/top10_sheet.js")
js3d = fetch(f"http://127.0.0.1:{PORT}/output/js/vessel_3d_reconstruction.js")

ROUTING_TOKEN = 'sheet === "balance"'

results = [
    # ── HTML checks ────────────────────────────────────────────────────────
    ("HTML: balance sheet DOM",         "sheet-balance"           in html),
    ("HTML: balance routing JS",        ROUTING_TOKEN             in html),
    ("HTML: balance_engine.js",         "balance_engine.js"       in html),
    ("HTML: sentinel_hud.css link",     "sentinel_hud.css"        in html),
    ("HTML: top10-grid-container",      "top10-grid-container"    in html),
    ("HTML: t10-modal",                 "t10-modal"               in html),
    ("HTML: data-sheet balance rule",   'data-sheet="balance"'    in html),
    ("HTML: JetBrains Mono @import",    "JetBrains Mono"          in html),
    ("HTML: Apple Frost White f5f5f7",  "f5f5f7"                  in html),
    ("HTML: Titanium Cyan 00f0ff",      "00f0ff"                  in html),
    ("HTML: bal-panel CSS inlined",     "bal-panel"               in html),
    ("HTML: balance KPI row",           "bal-kpi-row"             in html),
    # ── CSS checks ────────────────────────────────────────────────────────
    ("CSS: design token --t10-cyan",    "--t10-cyan"              in css),
    ("CSS: design token --t10-glass",   "--t10-glass"             in css),
    ("CSS: design token --t10-space",   "--t10-space"             in css),
    ("CSS: deep space bg #07090e",      "07090e"                  in css),
    ("CSS: GLOBAL BODY section",        "GLOBAL BODY"             in css),
    ("CSS: t10CyanPulse keyframes",     "t10CyanPulse"            in css),
    ("CSS: t10FadeIn animation",        "t10FadeIn"               in css),
    ("CSS: sheet entry animation",      "sheet-balance.active"    in css),
    ("CSS: glassmorphic backdrop-filter","backdrop-filter"        in css),
    ("CSS: bal-panel-hdr",              "bal-panel-hdr"           in css),
    ("CSS: bal-dfs-ring SVG gauge",     "bal-dfs-ring"            in css),
    ("CSS: bal-pil-bar latency",        "bal-pil-bar"             in css),
    ("CSS: webgl-context-lost state",   "webgl-context-lost"      in css),
    ("CSS: bal-quant-card",             "bal-quant-card"          in css),
    ("CSS: bal-truth pill",             "bal-truth"               in css),
    ("CSS: t10-modal-open grid fix",    "t10-modal-open"          in css),
    ("CSS: NEVER-BLACK photo-fallback", "t10-photo-fallback"      in css),
    ("CSS: t10-modal entry anim",       "t10ModalIn"              in css),
    ("CSS: balance display rule",       'html[data-sheet="balance"]' in css),
    # ── JS top10_sheet checks ─────────────────────────────────────────────
    ("JS: lockGridVisibility fn",       "lockGridVisibility"      in js),
    ("JS: idempotent closeRefs guard",  "already closed"          in js),
    ("JS: rAF scroll restore",          "requestAnimationFrame"   in js),
    ("JS: rAF photo re-unlock",         "removeProperty"          in js),
    ("JS: setSharedCanvasModalMode",    "setSharedCanvasModalMode" in js),
    ("JS: escBound ESC handler",        "escBound"                in js),
    # ── JS vessel_3d checks ───────────────────────────────────────────────
    ("3D: contextLost guard flag",      "contextLost"             in js3d),
    ("3D: webglcontextlost handler",    "webglcontextlost"        in js3d),
    ("3D: webglcontextrestored",        "webglcontextrestored"    in js3d),
    ("3D: sheet-aware renderFrame",     "activeSheet"             in js3d),
    ("3D: modal-open early return",     "t10-modal-open"          in js3d),
    ("3D: MeshPhysicalMaterial PBR",    "MeshPhysicalMaterial"    in js3d),
    ("3D: ocean shader buildOcean",     "buildOcean"              in js3d),
    ("3D: SharedTop10Renderer class",   "SharedTop10Renderer"     in js3d),
    ("3D: OrbitControls setFocus",      "OrbitControls"           in js3d),
    ("3D: insertBefore ZERO-BLACK",     "insertBefore"            in js3d),
    ("3D: is-3d-ready class set",       "is-3d-ready"             in js3d),
]

passed = failed = 0
print(f"== Oracle-1001 / Sentinel — Apple×NASA UX Gate (port {PORT}) ==")
print()
for label, ok in results:
    tag = "PASS" if ok else "FAIL"
    if ok: passed += 1
    else:  failed += 1
    print(f"  {tag}  {label}")

print()
print(f"HTML  {len(html):>9,} chars")
print(f"CSS   {len(css):>9,} chars")
print(f"JS    {len(js):>9,} chars")
print(f"JS-3D {len(js3d):>9,} chars")
print()
print(f"RESULT: {passed} PASS / {failed} FAIL")
print()
print(f"  BALANCE  http://127.0.0.1:{PORT}/output/sentinel_dashboard.html?sheet=balance")
print(f"  TOP10    http://127.0.0.1:{PORT}/output/sentinel_dashboard.html?sheet=top10")
print(f"  HEALTH   http://127.0.0.1:{PORT}/output/api/v1/health.json")

sys.exit(0 if failed == 0 else 1)
