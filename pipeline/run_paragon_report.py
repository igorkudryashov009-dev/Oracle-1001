"""CLI: parse one vessel (default G. PARAGON) and print 20-column Fill Rate report."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paths import resolve_fleet_source
from pipeline.robust_parser import (  # noqa: E402
    TZ_KEYS_UPPER,
    fill_rate,
    format_report_table,
    parse_osint_narrative,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--imo", default="9656888")
    ap.add_argument("--xlsx", default="", help="Override source xlsx (optional)")
    args = ap.parse_args()

    import pandas as pd

    xlsx = Path(args.xlsx) if args.xlsx else resolve_fleet_source()
    df = pd.read_excel(xlsx, sheet_name="Sheet1")
    want = re.sub(r"\D", "", args.imo)
    hit = df[df.iloc[:, 0].astype(str).str.replace(r"\D", "", regex=True) == want]
    if hit.empty:
        print(f"ERROR: IMO {args.imo} not found in {xlsx}", file=sys.stderr)
        return 1

    raw = str(hit.iloc[0, 1])
    declared = str(hit.iloc[0, 0])
    upper = parse_osint_narrative(raw, declared_imo=declared, as_snake=False)
    print("=" * 72)
    print(f"ROBUST PARSER REPORT · IMO {want} · {upper.get('VESSEL_NAME')}")
    print(f"Source: {xlsx}")
    print("=" * 72)
    print(format_report_table(upper))
    tags = upper.get("SANCTIONS_TAGS")
    print(f"L8 SANCTIONS_TAGS: {tags or '(none)'}")
    rate = fill_rate(upper, uppercase=True)
    if rate < 100.0:
        missing = [k for k in TZ_KEYS_UPPER if upper.get(k) in (None, "")]
        print("MISSING:", missing)
        return 2
    print("STATUS: 100% Fill Rate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
