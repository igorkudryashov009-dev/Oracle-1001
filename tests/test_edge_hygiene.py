"""Edge hygiene: aborted clients, shutdown, Dual Gate import, route shape."""
from __future__ import annotations

import pytest

from services.api_auth import sanitize_health
from services.dual_gate import FLEET_WIDE_METRIC_MIN_N
from services.quant_risk_service import MIN_STATISTICAL_SAMPLE_N


def test_write_body_swallows_broken_pipe() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "serve_dashboard.py"
    spec = importlib.util.spec_from_file_location("serve_dashboard_hygiene", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    DashboardHandler = mod.DashboardHandler

    class _Pipe:
        def write(self, data: bytes) -> None:
            raise BrokenPipeError()

    handler = DashboardHandler.__new__(DashboardHandler)
    handler.wfile = _Pipe()
    handler._write_body(b"{}")


def test_quant_sample_floor_is_the_dual_gate_constant() -> None:
    assert MIN_STATISTICAL_SAMPLE_N is FLEET_WIDE_METRIC_MIN_N
    assert MIN_STATISTICAL_SAMPLE_N == 30


def test_shutdown_line_is_printed_once(capsys: pytest.CaptureFixture[str]) -> None:
    import services.process_shutdown as ps

    ps._DONE = False
    ps.note_shutdown()
    ps.note_shutdown()
    out = capsys.readouterr().out
    assert out.count("graceful shutdown complete") == 1
    ps._DONE = False


_GATE_KEYS = {
    "pipeline_health_status",
    "fleet_sample_status",
    "top500_live_coverage",
    "ais_lag_sec",
    "canonical_port",
    "operational_status",
    "active_node",
    "oob",
    "disk_free_pct",
    "contract_version",
}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Public edge health is the slim contract from sanitize_health "
        "(commit 95da5ff). api_server /api/v1/health returns GateStatus. "
        "Key sets differ; this prompt does not unify the two handlers."
    ),
)
def test_edge_and_microapi_health_key_sets_match() -> None:
    doc = {
        "pipeline_health_status": "NOMINAL",
        "fleet_sample_status": "INSUFFICIENT",
        "top500_live_coverage": 2,
        "acceptance": {"status": "DEGRADED", "fully_commissioned_at": "2026-09-25T11:40:20Z"},
        "active_node": "korolev",
        "contract_version": "1.8.0-ops-gis-sot",
    }
    slim = sanitize_health(doc, tier="public")
    assert set(slim) == _GATE_KEYS
