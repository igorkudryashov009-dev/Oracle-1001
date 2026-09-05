"""Print fleet Fill Rate + Risk Labels + L8 sanctions tags from fleet_database.csv."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pipeline.robust_parser import TZ_COLUMNS, fill_rate  # noqa: E402


def main() -> int:
    csv_path = ROOT / "output" / "fleet_database.csv"
    if not csv_path.is_file():
        print(f"ERROR: missing {csv_path}", file=sys.stderr)
        return 1

    df = pd.read_csv(csv_path, low_memory=False)
    n = len(df)
    rates = [fill_rate({c: row.get(c) for c in TZ_COLUMNS}) for _, row in df.iterrows()]
    avg = sum(rates) / max(n, 1)

    risk_counts = Counter(
        (str(v).upper() if pd.notna(v) and str(v).strip() else "UNLABELED")
        for v in df["compliance_risk_level"].tolist()
    )

    tag_counter: Counter[str] = Counter()
    if "sanctions_tags" in df.columns:
        for raw in df["sanctions_tags"].fillna("").astype(str):
            if not raw.strip() or raw.lower() == "nan":
                continue
            for tag in [t.strip() for t in raw.split(";") if t.strip()]:
                tag_counter[tag] += 1

    print("=" * 64)
    print("ORACLE-1001 FLEET REPORT")
    print("=" * 64)
    print(f"Vessels:           {n}")
    print(f"Fill Rate (avg):   {avg:.2f}%")
    print("Risk Labels:")
    for k, v in sorted(risk_counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {k:<12} {v}")
    print("L8 Sanctions Tags:")
    if tag_counter:
        for k, v in sorted(tag_counter.items(), key=lambda x: (-x[1], x[0])):
            print(f"  {k:<14} {v}")
    else:
        print("  (none)")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
