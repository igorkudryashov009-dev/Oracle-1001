"""
UAIP Visualizer 2026 / Sentinel — Canonical Micro-API Server.
Internal micro-service providing FastAPI schemas & endpoints for quant risk.

Network & Port Topology:
  - Canonical Public Edge: port 8765 (sentinel-web / serve_dashboard.py)
  - Internal Micro-API: port 8766 (api_server.py, loopback 127.0.0.1:8766)
  - Reverse Proxy: sentinel-web forwards /api/v1/quant/* -> http://127.0.0.1:8766/api/v1/quant/*

Strict Table & Schema Invariants:
  - Table name: ais_positions (NEVER positions)
  - Columns: received_at, timestamp_utc, mmsi
  - Database: история1/sentinel_ais.db
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from services.quant_risk_service import compute_quant_risk_payload, resolve_db_path
from services.ais_health import build_health_document
from services.dual_gate import resolve_active_node, compute_pipeline_health_status, compute_fleet_sample_status

ROOT = Path(__file__).resolve().parent
CANONICAL_EDGE_PORT = 8765
INTERNAL_API_PORT = int(os.environ.get("API_SERVER_PORT", "8766"))


class GateStatus(BaseModel):
    pipeline_health_status: str = Field(..., description="NOMINAL | DEGRADED | CRITICAL")
    fleet_sample_status: str = Field(..., description="FULL | LIMITED | INSUFFICIENT")
    top500_live_coverage: int
    ais_lag_sec: float
    canonical_port: int
    operational_status: str = Field("NOMINAL", description="High-level operational health")
    active_node: str = Field("korolev", description="Active physical node: 'korolev' | 'london'")


class QuantRiskMetrics(BaseModel):
    horizon_days: int = Field(..., description="Forecast horizon in days (7, 14, 30)")
    active_node: str = Field("korolev", description="Active physical node: 'korolev' | 'london'")
    is_synthetic: bool = Field(
        ...,
        description="Mandatory flag: True if ANY component relies on synthetic or statistically insufficient sample data",
    )
    synthetic_components: List[str] = Field(
        ...,
        description="List of components not yet backed by statistically sufficient production data",
    )
    pipeline_health_status: str = Field(..., description="NOMINAL | DEGRADED | CRITICAL from Dual Gate")
    fleet_sample_status: str = Field(..., description="FULL | LIMITED | INSUFFICIENT from Dual Gate")
    signal_status: str = Field(..., description="production_signal | insufficient_sample | pipeline_degraded")
    production_actionable: bool = Field(..., description="False if pipeline != NOMINAL or fleet != FULL or is_synthetic")
    sample_size_caveat: Optional[str] = Field(None, description="Detailed statistical caveat")

    spot_eur_mwh: float = Field(..., description="Live TTF spot price in EUR/MWh")
    p10: float = Field(..., description="Lower quantile bound (EUR/MWh)")
    p50: float = Field(..., description="Median ensemble forecast (EUR/MWh)")
    p90: float = Field(..., description="Upper quantile bound (EUR/MWh)")
    quantiles_source: str = Field(..., description="Source model identifier")
    model_cv_accuracy_pct: float = Field(..., description="Directional cross-validation accuracy")
    model_last_retrained: Optional[str] = Field(
        None,
        description="UTC ISO timestamp of last offline CatBoost/HMM artifact retrain (honest freshness caveat)",
    )
    model_last_retrained_utc: Optional[str] = Field(
        None,
        description="Alias of model_last_retrained (explicit UTC naming for consumers)",
    )
    model_provenance: str = Field(
        "offline_batch",
        description="Training locus: offline_batch (Node A/B serve inference only; never edge retrain)",
    )
    live_inference_confidence: str = Field(..., description="Confidence level under G3 terrestrial ceiling")

    active_regime: str = Field(..., description="Most probable current regime from Gaussian HMM")
    regime_probabilities: Dict[str, float] = Field(..., description="Posterior regime probabilities")
    regime_source: str = Field(..., description="Source identifier")

    sharpe_ratio: float = Field(..., description="Sharpe ratio (desk-normative target or empirical)")
    cvar_95_pct: float = Field(..., description="Conditional Value-at-Risk at 95% confidence (%)")
    var_95_pct: float = Field(..., description="Value-at-Risk at 95% confidence (%)")
    recommended_strategy_id: Optional[str] = Field(
        None,
        description="Optimal strategy from hedging book (e.g. 'B'), or None if production_actionable is False",
    )
    recommended_strategy_name: Optional[str] = Field(
        None,
        description="Strategy name, or None if production_actionable is False",
    )
    blocked_reason: Optional[str] = Field(
        None,
        description="Specific reason why strategy recommendation is gated/blocked when production_actionable is False",
    )
    ledger_sample_n: int = Field(..., description="Number of daily marks in paper ledger")
    ledger_sample_status: str = Field(..., description="insufficient_sample (N < 30) | sufficient")
    desk_normative_envelope: Dict[str, Any] = Field(..., description="Desk published targets from playbook")
    ledger_empirical_summary: Optional[Dict[str, Any]] = Field(None, description="Empirical metrics from ledger marks")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[STARTUP] UAIP Visualizer 2026 Internal API running on port {INTERNAL_API_PORT}")
    print(f"[STARTUP] SQLite DB candidate: {resolve_db_path()}")
    yield


app = FastAPI(
    title="UAIP Visualizer 2026 Internal Engine",
    version="1.0.0-prod",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/output/api/v1/health", response_model=GateStatus)
@app.get("/api/v1/health", response_model=GateStatus)
async def get_health_status() -> GateStatus:
    """Canonical Dual-Deploy Gate probe matching AGENTS.md contract."""
    doc = build_health_document()
    pipeline_status = doc.get("pipeline_health_status", "CRITICAL")
    fleet_status = doc.get("fleet_sample_status", "INSUFFICIENT")
    coverage = int(doc.get("top500_live_coverage") or 0)
    ais_lag = float((doc.get("replica") or {}).get("age_sec") or (doc.get("pipeline_health") or {}).get("ais_lag_sec") or 0.0)
    active_node = str(doc.get("active_node") or resolve_active_node())

    return GateStatus(
        pipeline_health_status=pipeline_status,
        fleet_sample_status=fleet_status,
        top500_live_coverage=coverage,
        ais_lag_sec=round(ais_lag, 1),
        canonical_port=CANONICAL_EDGE_PORT,
        operational_status=doc.get("operational_status", "NOMINAL" if pipeline_status == "NOMINAL" else "DEGRADED"),
        active_node=active_node,
    )


@app.get("/api/v1/quant/risk", response_model=QuantRiskMetrics)
@app.get("/output/api/v1/quant/risk", response_model=QuantRiskMetrics)
async def get_quant_risk(
    horizon: int = Query(7, ge=1, le=30, description="Forecast horizon in days (7, 14, 30)")
) -> QuantRiskMetrics:
    """Deterministic Quant Risk Payload with Dual-Gate Consumer Contract."""
    payload = compute_quant_risk_payload(horizon=horizon)
    return QuantRiskMetrics(**payload)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="127.0.0.1", port=INTERNAL_API_PORT, reload=False)
