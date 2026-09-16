#!/usr/bin/env python3
"""Post-bake regression screenshots against Node A (ARCTIC / Route / Q-Flex)."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logs" / "ship_delta_screens"
OUT.mkdir(parents=True, exist_ok=True)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://45.8.230.214:8765"
REPORT: dict = {"base": BASE, "checks": {}}


def fetch_json(path: str) -> dict:
    url = BASE.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def main() -> int:
    health = fetch_json("/output/api/v1/health")
    REPORT["health"] = {
        "pipeline_health_status": health.get("pipeline_health_status"),
        "fleet_sample_status": health.get("fleet_sample_status"),
        "top500_live_coverage": health.get("top500_live_coverage"),
        "maptiles": health.get("maptiles"),
    }

    try:
        risk = fetch_json("/api/v1/quant/risk")
    except Exception:
        risk = fetch_json("/output/api/v1/quant/risk")
    REPORT["quant"] = {
        "is_synthetic": risk.get("is_synthetic"),
        "production_actionable": risk.get("production_actionable"),
        "recommended_strategy_id": risk.get("recommended_strategy_id"),
        "recommended_strategy_name": risk.get("recommended_strategy_name"),
        "blocked_reason": risk.get("blocked_reason"),
    }

    try:
        qflex = fetch_json("/output/qflex_fleet_cargo.json")
        REPORT["qflex"] = {
            "notional_fallback_count": (qflex.get("fleet") or {}).get("notional_fallback_count"),
            "live_draft_count": (qflex.get("fleet") or {}).get("live_draft_count"),
            "sources": sorted(
                {
                    str(v.get("data_source"))
                    for v in (qflex.get("vessels") or [])
                    if isinstance(v, dict)
                }
            ),
        }
    except Exception as exc:
        REPORT["qflex"] = {"error": str(exc)}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1100})

        # 1) ARCTIC
        page.goto(f"{BASE}/output/sentinel_dashboard.html?sheet=arctic", wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(2500)
        cards = page.locator("#arctic-grid-container .ark-card").count()
        body = page.inner_text("body")
        REPORT["checks"]["arctic_cards"] = cards
        REPORT["checks"]["arctic_has_luma"] = ("AI-GENERATED" in body) or ("LUMA" in body)
        REPORT["checks"]["arctic_has_video_derived"] = "VIDEO-DERIVED" in body or "EXTRACTED FROM AI-GENERATED" in body
        REPORT["checks"]["arctic_top_policy"] = (
            ("TOP VIEW UNAVAILABLE" in body)
            or ("TOP / OVERHEAD" in body)
            or ("VIDEO-DERIVED OVERHEAD" in body)
        )
        page.screenshot(path=str(OUT / "01_arctic_sheet.png"), full_page=False)
        derived = page.locator('[data-open-derived]')
        if derived.count() > 0:
            derived.first.click()
            page.wait_for_timeout(1000)
            page.screenshot(path=str(OUT / "01b_arctic_video_derived.png"), full_page=False)
            REPORT["checks"]["arctic_derived_open"] = True
            banner = page.locator("#arkDerivedBanner")
            if banner.count():
                REPORT["checks"]["arctic_derived_banner"] = banner.inner_text()[:280]
        else:
            REPORT["checks"]["arctic_derived_open"] = False

        # 2) Route / maptiles Esri fallback
        page.goto(f"{BASE}/output/sentinel_dashboard.html?sheet=route", wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(3500)
        route_text = page.inner_text("body")
        REPORT["checks"]["route_api_key_required"] = "API KEY REQUIRED" in route_text
        # Probe a tile via proxy (Esri fallback path)
        tile_url = f"{BASE}/api/tiles/esri/2/1/1.png"
        try:
            with urllib.request.urlopen(tile_url, timeout=30) as resp:
                tile_bytes = resp.read()
                REPORT["checks"]["esri_tile_status"] = getattr(resp, "status", 200)
                REPORT["checks"]["esri_tile_bytes"] = len(tile_bytes)
        except Exception as exc:
            # mapbox provider may remap; try generic
            REPORT["checks"]["esri_tile_error"] = str(exc)
            try:
                with urllib.request.urlopen(f"{BASE}/api/tiles/mapbox/2/1/1.png", timeout=30) as resp:
                    tile_bytes = resp.read()
                    REPORT["checks"]["mapbox_proxy_status"] = getattr(resp, "status", 200)
                    REPORT["checks"]["mapbox_proxy_bytes"] = len(tile_bytes)
                    REPORT["checks"]["mapbox_proxy_hdr_fallback"] = resp.headers.get("X-Sentinel-Tiles-Fallback") or resp.headers.get("x-sentinel-tiles-fallback")
            except Exception as exc2:
                REPORT["checks"]["tile_proxy_error"] = str(exc2)
        page.screenshot(path=str(OUT / "02_route_map.png"), full_page=False)

        # 3) Q-Flex / top10 notional honesty (cargo JSON already fetched)
        page.goto(f"{BASE}/output/sentinel_dashboard.html?sheet=top10", wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(2500)
        top_text = page.inner_text("body")
        REPORT["checks"]["top10_mentions_notional"] = bool(
            re.search(r"notional|NOTIONAL|full-capacity|FULL CAPACITY", top_text, re.I)
        )
        page.screenshot(path=str(OUT / "03_top10_qflex.png"), full_page=False)

        browser.close()

    (OUT / "ship_delta_report.json").write_text(json.dumps(REPORT, indent=2), encoding="utf-8")
    print(json.dumps(REPORT, indent=2))
    print("shots:", sorted(p.name for p in OUT.glob("*.png")))

    ok = (
        REPORT["checks"].get("arctic_cards", 0) >= 2
        and REPORT["health"].get("pipeline_health_status")
        and REPORT["quant"].get("recommended_strategy_id") is None
        and not REPORT["checks"].get("route_api_key_required")
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
