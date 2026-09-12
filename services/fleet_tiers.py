#!/usr/bin/env python3
"""Fleet priority tiers for archive rotation.

Tier 1 — Gas carriers (LNG / LPG / LEG / FLNG) from ``output/fleet_database.csv`` (~1,253).
Tier 2 — Oil tankers (crude / product / chemical) from optional oil registry CSV (~4,655 when supplied).

Oil registry search order:
  FLEET_OIL_CSV env → output/fleet_oil_tankers.csv → data/fleet_oil_tankers.csv
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
GAS_FLEET_CSV = ROOT / "output" / "fleet_database.csv"
OIL_CANDIDATES = (
    Path(os.getenv("FLEET_OIL_CSV") or "").expanduser() if os.getenv("FLEET_OIL_CSV") else None,
    ROOT / "output" / "fleet_oil_tankers.csv",
    ROOT / "data" / "fleet_oil_tankers.csv",
)
REGISTRY_META = ROOT / "output" / "archive" / "fleet_tiers_meta.json"
TELEMETRY_DB = ROOT / "data" / "archive" / "vessel_telemetry_history.sqlite"

GAS_RE = re.compile(
    r"LNG|LPG|LEG|FLNG|GAS\b|СПГ|СУГ|ГАЗОВ|GAZOV|ETHYLENE|AMMONIA",
    re.I,
)
OIL_RE = re.compile(
    r"CRUDE|VLCC|ULCC|SUEZMAX|AFRAMAX|PRODUCT\s*TANKER|CHEMICAL|"
    r"OIL\s*TANKER|НЕФТ|ТАНКЕР.*НЕФТ|BITUMEN|ASPHALT",
    re.I,
)

REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS vessel_fleet_registry (
    imo TEXT PRIMARY KEY,
    mmsi TEXT,
    vessel_name TEXT,
    vessel_type TEXT,
    tier INTEGER NOT NULL,
    dwt REAL,
    draft_max REAL,
    flag TEXT,
    source TEXT,
    updated_at TEXT NOT NULL
)
"""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_vessel_type(vessel_type: Any, *, default_tier: int = 1) -> int:
    """Return 1=gas, 2=oil. Ambiguous / offshore defaults to ``default_tier``."""
    s = str(vessel_type or "")
    if GAS_RE.search(s):
        return 1
    if OIL_RE.search(s):
        return 2
    return int(default_tier)


def _norm_imo(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        return str(int(float(str(v).strip())))
    except (TypeError, ValueError):
        return None


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path or not path.is_file():
        return []
    import pandas as pd

    df = pd.read_csv(path, low_memory=False)
    if "vessel_category" in df.columns:
        cats = df["vessel_category"].astype(str).str.lower()
        allowed = {"vessel", "oil_tanker", "tanker", "gas_carrier"}
        df = df[cats.isin(allowed)].copy()
    if "imo" not in df.columns:
        return []
    if "dwt_tons" in df.columns:
        df["dwt_tons"] = pd.to_numeric(df["dwt_tons"], errors="coerce").fillna(0.0)
        df = df.sort_values("dwt_tons", ascending=False)
    elif "dwt" in df.columns:
        df["dwt"] = pd.to_numeric(df["dwt"], errors="coerce").fillna(0.0)
        df = df.sort_values("dwt", ascending=False)
    df = df.drop_duplicates(subset=["imo"], keep="first")
    return df.to_dict(orient="records")


def resolve_oil_fleet_csv() -> Optional[Path]:
    for p in OIL_CANDIDATES:
        if not p or not p.is_file() or p.stat().st_size <= 32:
            continue
        try:
            # Header-only template is not a real registry
            text = p.read_text(encoding="utf-8", errors="replace")
            data_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            if len(data_lines) <= 1:
                continue
            return p
        except OSError:
            continue
    return None


def load_tiered_fleet(
    *,
    gas_csv: Path = GAS_FLEET_CSV,
    oil_csv: Optional[Path] = None,
) -> dict[str, Any]:
    """Build tiered IMO lists + registry rows."""
    gas_rows = _read_csv_rows(gas_csv)
    oil_path = oil_csv or resolve_oil_fleet_csv()
    oil_rows = _read_csv_rows(oil_path) if oil_path else []

    registry: dict[str, dict[str, Any]] = {}
    gas_imos: list[str] = []
    oil_imos: list[str] = []

    for rec in gas_rows:
        imo = _norm_imo(rec.get("imo"))
        if not imo:
            continue
        vtype = _s(rec.get("vessel_type")) or ""
        # Primary gas CSV is the Tier-1 universe even if a few rows look oil-ish
        tier = 1 if classify_vessel_type(vtype, default_tier=1) == 1 else 1
        # Rare explicit oil rows inside gas CSV → still keep as tier1 per SoT count ~1253
        row = {
            "imo": imo,
            "mmsi": _s(rec.get("mmsi")),
            "vessel_name": _s(rec.get("vessel_name")),
            "vessel_type": vtype,
            "tier": tier,
            "dwt": _f(rec.get("dwt_tons") if rec.get("dwt_tons") is not None else rec.get("dwt")),
            "draft_max": _f(rec.get("draft_m") or rec.get("draft_max")),
            "flag": _s(rec.get("flag")),
            "source": "fleet_database.csv",
            "updated_at": _utc_iso(),
        }
        registry[imo] = row
        gas_imos.append(imo)

    for rec in oil_rows:
        imo = _norm_imo(rec.get("imo"))
        if not imo:
            continue
        if imo in registry and registry[imo]["tier"] == 1:
            # Gas SoT wins — do not demote Tier-1 gas carriers
            continue
        vtype = _s(rec.get("vessel_type")) or "Oil Tanker"
        row = {
            "imo": imo,
            "mmsi": _s(rec.get("mmsi")),
            "vessel_name": _s(rec.get("vessel_name")),
            "vessel_type": vtype,
            "tier": 2,
            "dwt": _f(rec.get("dwt_tons") if rec.get("dwt_tons") is not None else rec.get("dwt")),
            "draft_max": _f(rec.get("draft_m") or rec.get("draft_max")),
            "flag": _s(rec.get("flag")),
            "source": str(oil_path.name) if oil_path else "oil_csv",
            "updated_at": _utc_iso(),
        }
        registry[imo] = row
        oil_imos.append(imo)

    # Dedupe preserve order
    def _uniq(seq: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    gas_imos = _uniq(gas_imos)
    oil_imos = _uniq(oil_imos)

    meta = {
        "generated_at": _utc_iso(),
        "tier1_gas_count": len(gas_imos),
        "tier2_oil_count": len(oil_imos),
        "gas_csv": str(gas_csv),
        "oil_csv": str(oil_path) if oil_path else None,
        "oil_csv_missing": oil_path is None,
        "note": None
        if oil_path
        else "Tier-2 oil registry missing — drop output/fleet_oil_tankers.csv (~4655) to enable oil rotation",
    }
    return {
        "registry": list(registry.values()),
        "gas_imos": gas_imos,
        "oil_imos": oil_imos,
        "meta": meta,
    }


def chunk_imos(imos: list[str], limit: int) -> list[list[str]]:
    limit = max(1, int(limit))
    if not imos:
        return []
    return [imos[i : i + limit] for i in range(0, len(imos), limit)]


def persist_registry_sqlite(
    registry_rows: list[dict[str, Any]],
    *,
    db_path: Path = TELEMETRY_DB,
) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(REGISTRY_DDL)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vfr_tier ON vessel_fleet_registry(tier)")
        conn.executemany(
            """
            INSERT OR REPLACE INTO vessel_fleet_registry
            (imo, mmsi, vessel_name, vessel_type, tier, dwt, draft_max, flag, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["imo"],
                    r.get("mmsi"),
                    r.get("vessel_name"),
                    r.get("vessel_type"),
                    int(r["tier"]),
                    r.get("dwt"),
                    r.get("draft_max"),
                    r.get("flag"),
                    r.get("source"),
                    r.get("updated_at") or _utc_iso(),
                )
                for r in registry_rows
            ],
        )
        conn.commit()
        return len(registry_rows)
    finally:
        conn.close()


def write_tiers_meta(meta: dict[str, Any], *, path: Path = REGISTRY_META) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def rebuild_fleet_tiers(*, slot_limit: int = 500) -> dict[str, Any]:
    packed = load_tiered_fleet()
    n = persist_registry_sqlite(packed["registry"])
    gas_batches = chunk_imos(packed["gas_imos"], slot_limit)
    oil_batches = chunk_imos(packed["oil_imos"], slot_limit) if packed["oil_imos"] else []
    meta = {
        **packed["meta"],
        "registry_rows": n,
        "gas_batches": len(gas_batches),
        "oil_batches": len(oil_batches),
        "slot_limit": slot_limit,
        "gas_full_refresh_hours": max(1, len(gas_batches)) * 1.5,  # 2 gas slots / 3h cycle
        "oil_full_refresh_hours": max(1, len(oil_batches)) * 3 if oil_batches else None,
    }
    write_tiers_meta(meta)
    return {
        "meta": meta,
        "gas_imos": packed["gas_imos"],
        "oil_imos": packed["oil_imos"],
        "gas_batches": gas_batches,
        "oil_batches": oil_batches,
    }


def ensure_oil_csv_template() -> Path:
    """Write header-only oil registry template if missing."""
    path = ROOT / "output" / "fleet_oil_tankers.csv"
    if path.is_file() and path.stat().st_size > 32:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "imo,mmsi,vessel_name,vessel_type,dwt_tons,draft_m,flag,vessel_category\n",
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    ensure_oil_csv_template()
    out = rebuild_fleet_tiers()
    print(json.dumps(out["meta"], ensure_ascii=False, indent=2))
