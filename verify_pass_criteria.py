"""PASS criteria gate — Apple×NASA UX on DASHBOARD_PORT."""
import os
import urllib.request
import sys

PORT = int(os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or "8765")
BASE = f"http://127.0.0.1:{PORT}"

def fetch(path):
    r = urllib.request.urlopen(BASE + path, timeout=8)
    return r.status, r.read().decode("utf-8", errors="replace")

status, html = fetch("/output/sentinel_dashboard.html?sheet=balance")
_, css = fetch("/output/css/sentinel_hud.css")
_, js = fetch("/output/js/top10_sheet.js")
_, js3d = fetch("/output/js/vessel_3d_reconstruction.js")
_, baljs = fetch("/output/js/balance_engine.js")

checks = [
    ("HTTP 200 balance sheet", status == 200),
    ("Nav tab БАЛАНС / TOP-500 BALANCE", "БАЛАНС / TOP-500 BALANCE" in html),
    ("#balance-sheet-container present", 'id="balance-sheet-container"' in html),
    ("Panel A VOLUMETRIC", "VOLUMETRIC CAPACITY MATRIX" in html),
    ("Panel B DATA FIDELITY", "DATA FIDELITY" in html and "SRE TRUST" in html),
    ("Panel C OSINT ANOMALY", "OSINT ANOMALY" in html),
    ("Panel D TTF ELASTICITY", "TTF ELASTICITY" in html),
    ("balance_engine.js loaded", "balance_engine.js" in html),
    ("Grid minmax(360px) balance inline", "minmax(360px,1fr)" in html or "minmax(360px, 1fr)" in html),
    ("CSS #balance-sheet-container grid 360", "minmax(360px, 1fr)" in css),
    ("CSS #top10-grid-container 2-col", "repeat(2, 1fr)" in css and "top10-grid-container" in css),
    ("CSS top10 media ≤1024 1-col", "max-width: 1024px" in css or "max-width:1024px" in css),
    ("CSS glass rgba(18, 24, 38, 0.65)", "rgba(18, 24, 38, 0.65)" in css or "rgba(18,24,38,.65)" in html),
    ("CSS deep space #07090e", "07090e" in css),
    ("CSS Titanium Cyan #00f0ff", "00f0ff" in css),
    ("CSS JetBrains Mono", "JetBrains Mono" in css or "JetBrains Mono" in html),
    ("CSS backdrop-filter blur(20px)", "blur(20px)" in css or "blur(20px)" in html),
    ("CSS never-black photo-fallback", "t10-photo-fallback" in css),
    ("JS viewport aspect / height fallback", "aspect-ratio" in css or "height:220px" in css or "height: 220px" in css),
    ("JS MeshPhysicalMaterial", "MeshPhysicalMaterial" in js3d),
    ("JS SharedTop10Renderer", "SharedTop10Renderer" in js3d),
    ("JS setScissor", "setScissor" in js3d),
    ("JS ocean shader", "buildOcean" in js3d),
    ("JS non-destructive closeRefs", "already closed" in js and "lockGridVisibility" in js),
    ("JS ESC handler", "Escape" in js),
    ("JS top10 data-vessel-id delegation", "data-vessel-id" in js and "refsDelegated" in js),
    ("JS balance renderPanelA-D", all(x in baljs for x in ("renderPanelA", "renderPanelB", "renderPanelC", "renderPanelD"))),
    ("Balance payload embedded", '"balance"' in html or "__BALANCE_PAYLOAD__" in html),
]

# Asset probes
for asset in ("/assets/7000/1-1.jpg", "/assets/7000/10-3.jpg", "/output/js/vessel_3d_reconstruction.js"):
    try:
        st, _ = fetch(asset)
        checks.append((f"Asset 200 {asset}", st == 200))
    except Exception as e:
        checks.append((f"Asset 200 {asset}", False))

passed = failed = 0
print(f"== Oracle-1001 PASS Criteria Gate :{PORT} ==")
print()
for label, ok in checks:
    tag = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {tag}  {label}")

print()
print(f"RESULT: {passed} PASS / {failed} FAIL")
print()
print(f"  BALANCE  {BASE}/output/sentinel_dashboard.html?sheet=balance")
print(f"  TOP10    {BASE}/output/sentinel_dashboard.html?sheet=top10")
sys.exit(0 if failed == 0 else 1)
