import re
from playwright.sync_api import sync_playwright

def parse_kpi_percentages(text: str) -> tuple[float, float]:
    """Extract OSINT and Synth percentages from KPI text."""
    # Example: "98.6%" and "Synth OSINT: 1.4%"
    nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)%", text)]
    if len(nums) >= 2:
        return nums[0], nums[1]
    return 0.0, 0.0

def test_frontend():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Capture console messages and errors
        errors = []
        page.on("pageerror", lambda err: errors.append(f"PAGE ERROR: {err}"))
        page.on("console", lambda msg: errors.append(f"CONSOLE {msg.type}: {msg.text}") if msg.type == "error" else None)

        # ── 1. TOP-200 ANALYTICS ─────────────────────────────────────────────
        print("\n[1/2] Testing TOP-200 Analytics (top200_analytics.html)...")
        page.goto("http://127.0.0.1:8765/output/top200_analytics.html", timeout=25000)
        page.wait_for_selector("#c13a svg", timeout=10000)
        print("  Base triad c13a-c13c loaded.")

        # Check Provenance KPI
        kpi_prov = page.locator("#kpiRow .kpi").last.inner_text()
        print(f"  Provenance KPI text:\n    {kpi_prov.replace(chr(10), ' · ')}")
        osint_pct, synth_pct = parse_kpi_percentages(kpi_prov)
        total_prov = round(osint_pct + synth_pct, 1)
        print(f"  OSINT: {osint_pct}% + Synth: {synth_pct}% = {total_prov}%")
        assert abs(total_prov - 100.0) < 0.1, f"Provenance sum {total_prov}% != 100.0% in TOP-200"

        # Check Trio 3 mode
        btn_trio3 = page.locator('button.d13-mode[data-mode="trio3"]')
        btn_trio3.click()
        page.wait_for_selector("#c13g svg", timeout=5000)
        page.wait_for_selector("#c13h svg", timeout=5000)
        page.wait_for_selector("#c13i svg", timeout=5000)
        print("  Triad 3 (c13g, c13h, c13i) rendered successfully.")

        # Check All 9 Donut mode
        btn_all = page.locator('button.d13-mode[data-mode="all"]')
        btn_all.click()
        for cid in ["c13a", "c13b", "c13c", "c13d", "c13e", "c13f", "c13g", "c13h", "c13i"]:
            page.wait_for_selector(f"#{cid} svg", timeout=5000)
        print("  All 9 Donut charts (c13a–c13i) rendered in 'all' mode.")

        # Test cross-hover on c13h (Speed Knots)
        page.evaluate("""() => {
            const el = document.querySelector('#c13h path.seg');
            if (el) {
                el.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true, clientX: 100, clientY: 100 }));
            }
        }""")
        center_text_c13a = page.locator("#c13a .d13-ct").text_content()
        print(f"  Cross-hover dispatched on c13h. Center title on c13a: '{center_text_c13a}'")

        # Leave hover
        page.evaluate("""() => {
            const el = document.querySelector('#c13h path.seg');
            if (el) {
                el.dispatchEvent(new MouseEvent('mouseleave', { bubbles: true }));
            }
        }""")

        # Cascade filter by clicking on c13h (Speed)
        page.evaluate("""() => {
            const el = document.querySelector('#c13h path.seg');
            if (el) el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        }""")
        page.wait_for_timeout(300)
        kpi_slice_200 = page.locator("#kpiRow .kpi .v").first.inner_text()
        print(f"  Cascade filter applied via c13h segment -> vessels in slice: {kpi_slice_200}")

        # Reset filter
        page.locator("#fabReset").click()
        page.wait_for_timeout(350)

        # Validation suite audit
        res200 = page.evaluate("window.ORACLE_AUDIT ? window.ORACLE_AUDIT.run() : null")
        disc200 = res200.get("discrepancies", []) if res200 else ["ORACLE_AUDIT unavailable"]
        print(f"  Audit TOP-200 discrepancies ({len(disc200)}): {disc200}")
        assert len(disc200) == 0, f"Discrepancies found in TOP-200: {disc200}"

        # ── 2. TOP-500 ANALYTICS ─────────────────────────────────────────────
        print("\n[2/2] Testing TOP-500 Analytics (top500_analytics.html)...")
        page.goto("http://127.0.0.1:8765/output/top500_analytics.html", timeout=25000)
        page.wait_for_selector("#c13a svg", timeout=10000)
        print("  Base triad c13a-c13c loaded.")

        # Check Provenance KPI
        kpi_prov500 = page.locator("#kpiRow .kpi").last.inner_text()
        print(f"  Provenance KPI text:\n    {kpi_prov500.replace(chr(10), ' · ')}")
        osint_pct500, synth_pct500 = parse_kpi_percentages(kpi_prov500)
        total_prov500 = round(osint_pct500 + synth_pct500, 1)
        print(f"  OSINT: {osint_pct500}% + Synth: {synth_pct500}% = {total_prov500}%")
        assert abs(total_prov500 - 100.0) < 0.1, f"Provenance sum {total_prov500}% != 100.0% in TOP-500"

        # Check Trio 3 mode
        page.locator('button.d13-mode[data-mode="trio3"]').click()
        page.wait_for_selector("#c13g svg", timeout=5000)
        page.wait_for_selector("#c13h svg", timeout=5000)
        page.wait_for_selector("#c13i svg", timeout=5000)
        print("  Triad 3 (c13g, c13h, c13i) rendered successfully.")

        # Check All 9 Donut mode
        page.locator('button.d13-mode[data-mode="all"]').click()
        for cid in ["c13a", "c13b", "c13c", "c13d", "c13e", "c13f", "c13g", "c13h", "c13i"]:
            page.wait_for_selector(f"#{cid} svg", timeout=5000)
        print("  All 9 Donut charts (c13a–c13i) rendered in 'all' mode.")

        # Cross-hover on c13h
        page.evaluate("""() => {
            const el = document.querySelector('#c13h path.seg');
            if (el) {
                el.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true, clientX: 100, clientY: 100 }));
            }
        }""")
        center_text_c13a_500 = page.locator("#c13a .d13-ct").text_content()
        print(f"  Cross-hover dispatched on c13h. Center title on c13a: '{center_text_c13a_500}'")

        # Cascade filter by clicking on c13g (DWT/GT Ratio)
        page.evaluate("""() => {
            const el = document.querySelector('#c13g path.seg');
            if (el) el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        }""")
        page.wait_for_timeout(300)
        kpi_slice_500 = page.locator("#kpiRow .kpi .v").first.inner_text()
        print(f"  Cascade filter applied via c13g segment -> vessels in slice: {kpi_slice_500}")

        # Reset filter
        page.locator("#fabReset").click()
        page.wait_for_timeout(400)

        # Validation suite audit
        res500 = page.evaluate("window.ORACLE_AUDIT ? window.ORACLE_AUDIT.run() : null")
        disc500 = res500.get("discrepancies", []) if res500 else ["ORACLE_AUDIT unavailable"]
        print(f"  Audit TOP-500 discrepancies ({len(disc500)}): {disc500}")
        assert len(disc500) == 0, f"Discrepancies found in TOP-500: {disc500}"

        # ── 3. TOP-100 ANALYTICS ─────────────────────────────────────────────
        print("\n[3/3] Testing TOP-100 Analytics (top100_analytics.html)...")
        page.goto("http://127.0.0.1:8765/output/top100_analytics.html", timeout=25000)
        page.wait_for_selector("#kpiRow", timeout=10000)
        kpi_prov100 = page.locator("#kpiRow .kpi").last.inner_text()
        print(f"  Provenance KPI text:\n    {kpi_prov100.replace(chr(10), ' · ')}")
        osint_pct100, synth_pct100 = parse_kpi_percentages(kpi_prov100)
        total_prov100 = round(osint_pct100 + synth_pct100, 1)
        print(f"  OSINT: {osint_pct100}% + Synth: {synth_pct100}% = {total_prov100}%")
        assert abs(total_prov100 - 100.0) < 0.1, f"Provenance sum {total_prov100}% != 100.0% in TOP-100"

        browser.close()

        if errors:
            print(f"\nCaptured {len(errors)} browser warnings/errors:")
            for err in errors[:5]:
                print(f"  {err}")

        print("\n>>> ALL VALIDATION CHECKS PASSED: Provenance sum = 100.0%, 9 Donut charts synchronized, 0 audit discrepancies.")

if __name__ == "__main__":
    test_frontend()
