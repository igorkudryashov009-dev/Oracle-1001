"""Prepare AIS subscription targets from fleet_database.csv.

AISstream filters by MMSI only (max 50 per WebSocket). IMO without MMSI
cannot be subscribed — listed in targets_missing_mmsi.csv.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
FLEET = ROOT / "output" / "fleet_database.csv"
OUT_TARGETS = ROOT / "targets.json"
OUT_MISSING = ROOT / "targets_missing_mmsi.csv"
OUT_BATCHES = ROOT / "targets_batches.json"

# Official AISstream limit (https://aisstream.io/documentation)
MMSI_BATCH_SIZE = 50


def _norm_mmsi(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    if s.endswith(".0"):
        s = s[:-2]
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) < 5:
        return None
    return digits


def _norm_imo(v) -> str:
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def main() -> int:
    if not FLEET.exists():
        print(f"ERROR: {FLEET} not found. Run run_all.py first.")
        return 1

    df = pd.read_csv(FLEET)
    vessels = df[df["vessel_category"] == "vessel"].copy()
    print(f"Vessel rows in fleet_database: {len(vessels)}")
    print(f"Unique IMO (vessel): {vessels['imo'].nunique()}")

    vessels["imo_n"] = vessels["imo"].map(_norm_imo)
    vessels["mmsi_n"] = vessels["mmsi"].map(_norm_mmsi)

    # Prefer row with MMSI if duplicates
    vessels["_has_mmsi"] = vessels["mmsi_n"].notna().astype(int)
    vessels = vessels.sort_values(["imo_n", "_has_mmsi"], ascending=[True, False])
    unique = vessels.drop_duplicates("imo_n", keep="first")

    with_mmsi = unique[unique["mmsi_n"].notna()].copy()
    without = unique[unique["mmsi_n"].isna()].copy()

    # Deduplicate by MMSI for subscription (one stream target per MMSI)
    # Keep first IMO mapping for each MMSI
    mmsi_unique = with_mmsi.drop_duplicates("mmsi_n", keep="first")

    targets = [
        {
            "imo": row.imo_n,
            "mmsi": row.mmsi_n,
            "vessel_name": None if pd.isna(row.vessel_name) else str(row.vessel_name),
        }
        for row in with_mmsi.itertuples()
    ]

    # Batches of unique MMSIs (subscription units)
    mmsi_list = mmsi_unique["mmsi_n"].tolist()
    batches = [
        mmsi_list[i : i + MMSI_BATCH_SIZE]
        for i in range(0, len(mmsi_list), MMSI_BATCH_SIZE)
    ]

    OUT_TARGETS.write_text(
        json.dumps(
            {
                "generated_from": str(FLEET),
                "aisstream_mmsi_batch_limit": MMSI_BATCH_SIZE,
                "stats": {
                    "vessel_rows": int(len(vessels)),
                    "unique_imo_vessel": int(unique["imo_n"].nunique()),
                    "unique_imo_with_mmsi": int(len(with_mmsi)),
                    "unique_imo_without_mmsi": int(len(without)),
                    "unique_mmsi_for_subscription": int(len(mmsi_list)),
                    "websocket_batches_needed": int(len(batches)),
                },
                "targets": targets,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    OUT_BATCHES.write_text(
        json.dumps({"batch_size": MMSI_BATCH_SIZE, "batches": batches}, indent=2),
        encoding="utf-8",
    )

    missing_df = without[["imo_n", "vessel_name"]].rename(columns={"imo_n": "imo"})
    missing_df.to_csv(OUT_MISSING, index=False, encoding="utf-8-sig")

    print()
    print("=== TARGETS SUMMARY (honest counts) ===")
    print(f"  Unique IMO with valid MMSI:     {len(with_mmsi)}")
    print(f"  Unique IMO WITHOUT MMSI:        {len(without)}")
    print(f"  Unique MMSI for subscription:   {len(mmsi_list)}")
    print(f"  AISstream limit per WS:         {MMSI_BATCH_SIZE}")
    print(f"  Parallel WebSocket batches:     {len(batches)}")
    print()
    print(f"  Wrote {OUT_TARGETS}")
    print(f"  Wrote {OUT_BATCHES}")
    print(f"  Wrote {OUT_MISSING} ({len(without)} rows)")
    print()
    print(
        "NOTE: ~5900 vessel *rows* in fleet != vessels with MMSI. "
        "AISstream requires MMSI; IMO without MMSI cannot be subscribed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
