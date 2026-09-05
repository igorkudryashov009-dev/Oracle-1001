"""Strategic fleet registry with Alpha / Bravo / Charlie / Delta tiers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FLEET = ROOT / "output" / "fleet_database.csv"

# Strategic tier cutoffs by DWT rank within the vessel universe (1-indexed).
TIER_RULES = {
    "ALPHA": (1, 100),      # flagship / highest DWT
    "BRAVO": (101, 300),
    "CHARLIE": (301, 600),
    "DELTA": (601, 10_000),
}

TIER_COLORS = {
    "ALPHA": "#ef4444",
    "BRAVO": "#f59e0b",
    "CHARLIE": "#3b82f6",
    "DELTA": "#10b981",
}


def assign_strategic_tier(rank: int, risk: str = "LOW") -> str:
    """Assign NATO-style strategic tier from DWT rank + risk escalation."""
    risk_u = str(risk or "LOW").upper()
    for tier, (lo, hi) in TIER_RULES.items():
        if lo <= rank <= hi:
            # Escalate EXTREME / HIGH risk vessels one tier up when possible
            if risk_u == "EXTREME" and tier == "BRAVO":
                return "ALPHA"
            if risk_u == "EXTREME" and tier == "CHARLIE":
                return "BRAVO"
            if risk_u == "HIGH" and tier == "DELTA":
                return "CHARLIE"
            return tier
    return "DELTA"


@dataclass
class TargetVessel:
    imo: str
    mmsi: str
    vessel_name: str
    dwt_tons: float
    gt: float
    flag: str
    vessel_type: str
    draft_m: float
    speed_knots: float
    nav_status: str
    destination_port: str
    departure_port: str
    compliance_risk_level: str
    sanctions_tags: str
    rank: int
    tier: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    age_years: float = 0.0
    built_year: Optional[int] = None
    call_sign: str = ""
    loa_m: float = 0.0
    beam_m: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FleetRegistry:
    """In-memory index of the target fleet (≈1,001 vessels) with tier tags."""

    vessels: list[TargetVessel] = field(default_factory=list)
    by_mmsi: dict[str, TargetVessel] = field(default_factory=dict)
    by_imo: dict[str, TargetVessel] = field(default_factory=dict)

    @classmethod
    def from_csv(cls, path: Path | None = None, top_n: int = 1001) -> "FleetRegistry":
        csv_path = path or DEFAULT_FLEET
        if not csv_path.exists():
            raise FileNotFoundError(f"Fleet database not found: {csv_path}")

        df = pd.read_csv(csv_path, low_memory=False)
        if "vessel_category" in df.columns:
            df = df[df["vessel_category"].astype(str).str.lower() == "vessel"].copy()
        df["dwt_tons"] = pd.to_numeric(df.get("dwt_tons"), errors="coerce").fillna(0.0)
        df = df.sort_values("dwt_tons", ascending=False).head(top_n).reset_index(drop=True)

        reg = cls()
        for i, row in df.iterrows():
            rank = int(i) + 1
            risk = str(row.get("compliance_risk_level") or "LOW")
            tier = assign_strategic_tier(rank, risk)
            mmsi = str(row.get("mmsi") or "").strip()
            imo = str(row.get("imo") or "").strip()
            if not mmsi or mmsi.lower() == "nan":
                continue
            v = TargetVessel(
                imo=imo,
                mmsi=mmsi,
                vessel_name=str(row.get("vessel_name") or f"IMO {imo}"),
                dwt_tons=float(row.get("dwt_tons") or 0.0),
                gt=float(row.get("gt") or 0.0),
                flag=str(row.get("flag") or ""),
                vessel_type=str(row.get("vessel_type") or ""),
                draft_m=float(row.get("draft_m") or 0.0),
                speed_knots=float(row.get("speed_knots") or 0.0),
                nav_status=str(row.get("nav_status") or ""),
                destination_port=str(row.get("destination_port") or ""),
                departure_port=str(row.get("departure_port") or ""),
                compliance_risk_level=risk,
                sanctions_tags=str(row.get("sanctions_tags") or ""),
                rank=rank,
                tier=tier,
                age_years=float(row.get("age_years") or 0.0),
                built_year=int(row["built_year"]) if pd.notna(row.get("built_year")) and row.get("built_year") else None,
                call_sign=str(row.get("call_sign") or ""),
                loa_m=float(row.get("loa_m") or 0.0),
                beam_m=float(row.get("beam_m") or 0.0),
            )
            reg.vessels.append(v)
            reg.by_mmsi[mmsi] = v
            if imo:
                reg.by_imo[imo] = v
        return reg

    def match(self, mmsi: str | None = None, imo: str | None = None) -> Optional[TargetVessel]:
        if mmsi and str(mmsi) in self.by_mmsi:
            return self.by_mmsi[str(mmsi)]
        if imo and str(imo) in self.by_imo:
            return self.by_imo[str(imo)]
        return None

    def tier_counts(self) -> dict[str, int]:
        out = {"ALPHA": 0, "BRAVO": 0, "CHARLIE": 0, "DELTA": 0}
        for v in self.vessels:
            out[v.tier] = out.get(v.tier, 0) + 1
        return out

    def export_targets_json(self, path: Path) -> None:
        payload = {
            "generated_for": "sentinel_aisstream",
            "count": len(self.vessels),
            "tier_counts": self.tier_counts(),
            "targets": [
                {
                    "imo": v.imo,
                    "mmsi": v.mmsi,
                    "name": v.vessel_name,
                    "tier": v.tier,
                    "rank": v.rank,
                    "dwt_tons": v.dwt_tons,
                    "risk": v.compliance_risk_level,
                }
                for v in self.vessels
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def summary(self) -> dict[str, Any]:
        return {
            "vessel_count": len(self.vessels),
            "tier_counts": self.tier_counts(),
            "total_dwt": round(sum(v.dwt_tons for v in self.vessels), 0),
        }
