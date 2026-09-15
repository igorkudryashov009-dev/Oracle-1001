from pathlib import Path
from playwright.sync_api import sync_playwright
import json

OUT = Path("output/assets/arctic/screenshots")
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1100})
    page.goto(
        "http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=arctic",
        wait_until="networkidle",
        timeout=60000,
    )
    page.wait_for_timeout(1200)
    page.locator("button[data-open-derived='9737187']").first.click()
    page.wait_for_selector("#arcticInspectorModal:not([hidden])", timeout=5000)
    page.wait_for_timeout(600)
    info = page.evaluate(
        """() => {
      const m = document.getElementById('arcticInspectorModal');
      const panel = m && m.querySelector('.t10-insp-panel');
      const ms = m && getComputedStyle(m);
      const ps = panel && getComputedStyle(panel);
      return {
        hidden: m.hidden,
        display: ms.display,
        zIndex: ms.zIndex,
        opacity: ms.opacity,
        panelDisplay: ps && ps.display,
        panelRect: panel && {
          w: panel.getBoundingClientRect().width,
          h: panel.getBoundingClientRect().height,
          top: panel.getBoundingClientRect().top,
          left: panel.getBoundingClientRect().left,
        },
        banner: document.getElementById('arkDerivedBanner')?.innerText || '',
        missing: document.querySelector('.ark-missing')?.innerText || '',
      };
    }"""
    )
    (OUT / "modal_debug.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(info, indent=2, ensure_ascii=False))
    page.locator("#arcticInspectorModal .t10-modal-panel").screenshot(
        path=str(OUT / "arctic_modal_panel.png")
    )
    page.screenshot(path=str(OUT / "arctic_modal_full.png"), full_page=False)
    browser.close()
print("OK")
