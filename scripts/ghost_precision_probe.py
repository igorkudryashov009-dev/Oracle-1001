#!/usr/bin/env python3
"""Ghost-detector precision probe — measure only, do NOT change 21kn threshold.

Writes output/ghost_precision.md from last spoof flags / live detect when available.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOUND_KN = 21.0
OUT = ROOT / "output" / "ghost_precision.md"


def main() -> int:
    health = ROOT / "output" / "api" / "v1" / "health.json"
    spoof = {}
    if health.is_file():
        try:
            doc = json.loads(health.read_text(encoding="utf-8"))
            spoof = doc.get("ais_spoofing") or {}
        except (OSError, json.JSONDecodeError):
            spoof = {}

    imos = list(spoof.get("spoofed_imos") or [])
    # Prefer vessel-level details if present
    details = spoof.get("details") or spoof.get("vessels") or []
    speeds: list[float] = []
    if isinstance(details, list):
        for d in details:
            if not isinstance(d, dict):
                continue
            for k in ("spoof_speed_kn", "speed_kn", "sog", "speed"):
                if d.get(k) is not None:
                    try:
                        speeds.append(float(d[k]))
                    except (TypeError, ValueError):
                        pass
                    break

    n = max(len(imos), len(speeds), int(spoof.get("spoofed_count") or 0))
    # If we have fewer than 44, note sample size honestly
    over_21 = sum(1 for s in speeds if s > BOUND_KN)
    share = (over_21 / len(speeds)) if speeds else None

    lines = [
        "# Ghost detector precision probe",
        "",
        f"- generated_at_utc: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- bound_kn (unchanged): {BOUND_KN}",
        f"- spoofed_count (health): {spoof.get('spoofed_count')}",
        f"- spoofed_imos_n: {len(imos)}",
        f"- speed_samples_n: {len(speeds)}",
        f"- speed_gt_21kn_n: {over_21 if speeds else 'n/a'}",
        f"- share_speed_gt_21kn: {round(share, 4) if share is not None else 'n/a'}",
        "",
        "## Interpretation",
        "",
        "Laden VLCC / Q-Max physical ceiling is locked at 21 kn in",
        "`services/sentinel_analytics.py` (`LNG_PHYSICAL_SPEED_KN`).",
        "This report **does not change** the threshold — it measures how often",
        "flagged vessels exceed 21 kn (false-positive candidates among laden hulls",
        "would require cargo/draught context not available in terrestrial AIS alone).",
        "",
        f"Requested sample window: last 44 flags — available n={n}.",
        "",
        "## Note",
        "",
        "If speed_samples_n=0, health snapshot lacks per-vessel speeds;",
        "re-run after a release that embeds spoof detail, or extend detector",
        "export in a follow-up sprint. Threshold remains 21 kn.",
        "",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} n={n} share={share}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
