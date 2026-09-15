from pathlib import Path
from playwright.sync_api import sync_playwright
import json

OUT = Path("output/assets/arctic/screenshots")
OUT.mkdir(parents=True, exist_ok=True)
url = "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=arctic"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1100})
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1800)
    page.screenshot(path=str(OUT / "arctic_sheet_cards.png"), full_page=False)
    page.locator('[data-open-derived="9737187"]').first.click()
    page.wait_for_timeout(900)
    page.screenshot(path=str(OUT / "arctic_video_derived_disclaimer.png"), full_page=False)
    banner = page.locator("#arkDerivedBanner").inner_text()
    (OUT / "disclaimer_text.txt").write_text(banner, encoding="utf-8")
    gl = page.evaluate(
        """() => {
      const canvases = [...document.querySelectorAll('canvas')];
      return {
        canvas: canvases.length,
        modalMode: document.getElementById('arcticInspectorModal')?.dataset?.viewerMode || null,
        cards: document.querySelectorAll('#arctic-grid-container .ark-card').length,
        draftCells: [...document.querySelectorAll('#arctic-grid-container .t10-telem-cell .lbl')]
          .map(el => el.textContent.trim())
      };
    }"""
    )
    (OUT / "webgl_check.json").write_text(json.dumps(gl, indent=2), encoding="utf-8")
    print("banner:", banner[:240].replace("\n", " | "))
    print("check:", gl)
    print("shots:", [p.name for p in OUT.glob("*.png")])
    browser.close()
print("OK")
