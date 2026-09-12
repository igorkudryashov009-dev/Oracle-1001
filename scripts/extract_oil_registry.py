#!/usr/bin/env python3
"""Extract Tier-2 oil tanker registry from the full ~5,908 vessel OSINT fleet.

Sources (first hit wins for the FULL fleet):
  1. FLEET_FULL_CSV env
  2. output/fleet_database_full.csv  (cached materialization)
  3. git blob b132925:output/fleet_database.csv  (historical 5,908 SoT)
  4. vessel_daily_archive / vessel_fleet_registry  (fallback, usually gas-only)

Tier-1 gas IMOs are taken from output/fleet_database.csv (~1,253) and excluded.
Writes:
  output/fleet_oil_tankers.csv
  output/fleet_database_full.csv   (when recovered from git)
  output/archive/oil_registry_extract_meta.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
GAS_CSV = ROOT / "output" / "fleet_database.csv"
FULL_CACHE = ROOT / "output" / "fleet_database_full.csv"
OIL_OUT = ROOT / "output" / "fleet_oil_tankers.csv"
META_OUT = ROOT / "output" / "archive" / "oil_registry_extract_meta.json"
GIT_FULL_REF = "b132925:output/fleet_database.csv"

GAS_RE = re.compile(
    r"LNG|LPG|LEG|FLNG|GAS\b|СПГ|СУГ|ГАЗОВ|ETHYLENE|AMMONIA|VLEC|ETHANE",
    re.I,
)

OUT_COLS = [
    "imo",
    "mmsi",
    "vessel_name",
    "vessel_type",
    "dwt_tons",
    "draft_m",
    "flag",
    "vessel_category",
]


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_imo(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, float) and v != v):
        return None
    try:
        return str(int(float(str(v).strip())))
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return ""
    return str(v).strip()


def _f(v: Any) -> Optional[float]:
    if v is None or v == "" or (isinstance(v, float) and v != v):
        return None
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def _load_csv(path: Path) -> list[dict[str, Any]]:
    import pandas as pd

    df = pd.read_csv(path, low_memory=False)
    if "vessel_category" in df.columns:
        cats = df["vessel_category"].astype(str).str.lower()
        if (cats == "vessel").sum() > 100:
            df = df[cats == "vessel"].copy()
    return df.to_dict(orient="records")


def _load_gas_imos(path: Path = GAS_CSV) -> set[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Tier-1 gas CSV missing: {path}")
    rows = _load_csv(path)
    out: set[str] = set()
    for r in rows:
        imo = _norm_imo(r.get("imo"))
        if imo:
            out.add(imo)
    return out


def _try_git_full() -> Optional[tuple[list[dict[str, Any]], str]]:
    try:
        blob = subprocess.check_output(
            ["git", "show", GIT_FULL_REF],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    import pandas as pd

    df = pd.read_csv(io.BytesIO(blob), low_memory=False)
    if "vessel_category" in df.columns:
        df = df[df["vessel_category"].astype(str).str.lower() == "vessel"].copy()
    return df.to_dict(orient="records"), f"git:{GIT_FULL_REF}"


def _try_sqlite_fallback() -> Optional[tuple[list[dict[str, Any]], str]]:
    """Best-effort: vessel_daily_archive + fleet registry (may be gas-only)."""
    rows: list[dict[str, Any]] = []
    sources: list[str] = []

    tel = ROOT / "data" / "archive" / "vessel_telemetry_history.sqlite"
    if tel.is_file():
        conn = sqlite3.connect(str(tel))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='vessel_fleet_registry'"
            )
            if cur.fetchone():
                for r in conn.execute(
                    "SELECT imo, mmsi, vessel_name, vessel_type, dwt, draft_max, flag FROM vessel_fleet_registry"
                ):
                    rows.append(
                        {
                            "imo": r[0],
                            "mmsi": r[1],
                            "vessel_name": r[2],
                            "vessel_type": r[3],
                            "dwt_tons": r[4],
                            "draft_m": r[5],
                            "flag": r[6],
                            "vessel_category": "vessel",
                        }
                    )
                sources.append(str(tel.name) + ":vessel_fleet_registry")
        finally:
            conn.close()

    ais = ROOT / "история1" / "sentinel_ais.db"
    if ais.is_file():
        conn = sqlite3.connect(str(ais))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='vessel_daily_archive'"
            )
            if cur.fetchone():
                for r in conn.execute(
                    """
                    SELECT imo, mmsi, vessel_name, vessel_type, dwt, draft, flag
                    FROM vessel_daily_archive
                    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM vessel_daily_archive)
                    """
                ):
                    rows.append(
                        {
                            "imo": r[0],
                            "mmsi": r[1],
                            "vessel_name": r[2],
                            "vessel_type": r[3],
                            "dwt_tons": r[4],
                            "draft_m": r[5],
                            "flag": r[6],
                            "vessel_category": "vessel",
                        }
                    )
                sources.append(str(ais.name) + ":vessel_daily_archive")
        finally:
            conn.close()

    if not rows:
        return None
    return rows, "+".join(sources)


def load_full_fleet(*, refresh_cache: bool = False) -> tuple[list[dict[str, Any]], str]:
    env = os.getenv("FLEET_FULL_CSV")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return _load_csv(p), f"env:{p}"

    if FULL_CACHE.is_file() and not refresh_cache:
        rows = _load_csv(FULL_CACHE)
        if len(rows) >= 4000:
            return rows, str(FULL_CACHE)

    git_pack = _try_git_full()
    if git_pack:
        rows, src = git_pack
        FULL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _write_full_cache(rows)
        return rows, src

    if FULL_CACHE.is_file():
        return _load_csv(FULL_CACHE), str(FULL_CACHE)

    fb = _try_sqlite_fallback()
    if fb:
        return fb

    raise FileNotFoundError(
        "Full ~5908 fleet not found. Set FLEET_FULL_CSV or restore "
        "output/fleet_database_full.csv / git blob b132925:output/fleet_database.csv"
    )


def _write_full_cache(rows: list[dict[str, Any]]) -> None:
    """Persist recovered full fleet for offline re-runs (subset of useful cols)."""
    import pandas as pd

    df = pd.DataFrame(rows)
    keep = [
        c
        for c in [
            "imo",
            "mmsi",
            "vessel_name",
            "vessel_type",
            "dwt_tons",
            "draft_m",
            "flag",
            "vessel_category",
            "gt",
            "loa_m",
            "beam_m",
            "nav_status",
            "built_year",
            "age_years",
            "compliance_risk_level",
        ]
        if c in df.columns
    ]
    if "vessel_category" not in df.columns:
        df["vessel_category"] = "vessel"
        keep.append("vessel_category")
    df[keep].to_csv(FULL_CACHE, index=False)


def extract_oil_rows(
    full_rows: list[dict[str, Any]],
    gas_imos: set[str],
) -> list[dict[str, Any]]:
    """Exclude Tier-1 gas IMOs; keep unique oil IMOs (largest DWT wins)."""
    best: dict[str, dict[str, Any]] = {}
    gas_like_skipped = 0

    for r in full_rows:
        imo = _norm_imo(r.get("imo"))
        if not imo or imo in gas_imos:
            continue
        vtype = _s(r.get("vessel_type"))
        # Soft skip residual gas-typed hulls not in Tier-1 CSV
        if vtype and GAS_RE.search(vtype):
            gas_like_skipped += 1
            continue
        dwt = _f(r.get("dwt_tons") if r.get("dwt_tons") is not None else r.get("dwt")) or 0.0
        draft = _f(r.get("draft_m") or r.get("draft") or r.get("draft_max"))
        cand = {
            "imo": imo,
            "mmsi": _s(r.get("mmsi")),
            "vessel_name": _s(r.get("vessel_name")),
            "vessel_type": vtype or "Oil Tanker",
            "dwt_tons": dwt if dwt else "",
            "draft_m": draft if draft is not None else "",
            "flag": _s(r.get("flag")),
            "vessel_category": "oil_tanker",
            "_dwt": dwt,
        }
        prev = best.get(imo)
        if prev is None or cand["_dwt"] > prev["_dwt"]:
            best[imo] = cand

    out = sorted(best.values(), key=lambda x: (-x["_dwt"], x["imo"]))
    for row in out:
        row.pop("_dwt", None)
    # stash skip count on function attr for meta
    extract_oil_rows.gas_like_skipped = gas_like_skipped  # type: ignore[attr-defined]
    return out


def write_oil_csv(rows: list[dict[str, Any]], path: Path = OIL_OUT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in OUT_COLS})
    return path


def run(*, refresh_cache: bool = False, rebuild_tiers: bool = True) -> dict[str, Any]:
    gas_imos = _load_gas_imos()
    full_rows, full_src = load_full_fleet(refresh_cache=refresh_cache)
    full_imos = {_norm_imo(r.get("imo")) for r in full_rows}
    full_imos.discard(None)

    oil_rows = extract_oil_rows(full_rows, gas_imos)
    write_oil_csv(oil_rows)

    meta: dict[str, Any] = {
        "generated_at": _utc(),
        "full_source": full_src,
        "full_rows": len(full_rows),
        "full_unique_imos": len(full_imos),
        "tier1_gas_imos": len(gas_imos),
        "tier1_gas_overlap_with_full": len(gas_imos & full_imos),
        "tier2_oil_exported": len(oil_rows),
        "gas_like_skipped_in_residual": int(
            getattr(extract_oil_rows, "gas_like_skipped", 0)
        ),
        "oil_csv": str(OIL_OUT),
        "full_cache": str(FULL_CACHE) if FULL_CACHE.is_file() else None,
        "note": (
            "Tier-2 = full OSINT fleet minus Tier-1 gas IMOs (unique). "
            "Target ~4655 assumes gas⊂full; actual unique residual depends on "
            "overlap between curated gas CSV and historical 5908 registry."
        ),
    }
    META_OUT.parent.mkdir(parents=True, exist_ok=True)
    META_OUT.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if rebuild_tiers:
        sys.path.insert(0, str(ROOT))
        from services.fleet_tiers import rebuild_fleet_tiers

        tiers = rebuild_fleet_tiers()
        meta["fleet_tiers"] = tiers["meta"]

    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Re-pull full fleet from git even if fleet_database_full.csv exists",
    )
    ap.add_argument(
        "--no-rebuild-tiers",
        action="store_true",
        help="Skip services.fleet_tiers.rebuild_fleet_tiers()",
    )
    args = ap.parse_args()
    meta = run(refresh_cache=args.refresh_cache, rebuild_tiers=not args.no_rebuild_tiers)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
