from pathlib import Path
from services.satellite_ais_adapter import SatelliteAISAdapter

html = Path("output/sentinel_dashboard.html").read_text(encoding="utf-8")
css = Path("output/css/sentinel_hud.css").read_text(encoding="utf-8")
print("top10_2col", "repeat(2, 1fr)" in css)
print("banner", "fleetSampleBanner" in html and "fleet-sample-banner" in html)
print("spoof21", "bound_kn" in html)
print("whatif", "blockage_days" in html)
print("sat", SatelliteAISAdapter().coverage_report()["status"])
