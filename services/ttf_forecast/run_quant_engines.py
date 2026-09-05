"""Run all TTF quant engines against live feature store and append logs."""

from __future__ import annotations

import json
from pathlib import Path

from services.ttf_forecast.causality_wave import run_causality_wave
from services.ttf_forecast.logging_utils import LOG_PATH, get_quant_logger
from services.ttf_forecast.markov_engine import run_markov_engine
from services.ttf_forecast.spectral_engine import run_spectral_analysis

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "ttf_quant_bundle.json"


def main() -> int:
    log = get_quant_logger("ttf.quant.runner")
    log.info("=== QUANT ENGINE BUNDLE START ===")
    bundle = {}
    try:
        bundle["spectral"] = run_spectral_analysis()
        bundle["markov"] = run_markov_engine()
        bundle["causality_wave"] = run_causality_wave()
        OUT.write_text(json.dumps({
            "spectral_top_cycles": bundle["spectral"].get("dominant_cycles", [])[:5],
            "markov_current": {
                "state": bundle["markov"].get("current_state_name"),
                "probs": bundle["markov"].get("current_state_probabilities"),
            },
            "granger_best": bundle["causality_wave"]["granger"].get("best_lag"),
            "elliott_status": bundle["causality_wave"]["elliott"].get("status"),
        }, indent=2), encoding="utf-8")
        log.info("=== QUANT ENGINE BUNDLE OK → %s | log=%s ===", OUT, LOG_PATH)
        print(json.dumps({
            "ok": True,
            "log": str(LOG_PATH),
            "bundle": str(OUT),
            "markov_state": bundle["markov"].get("current_state_name"),
            "granger_best_lag": bundle["causality_wave"]["granger"].get("best_lag"),
            "elliott": bundle["causality_wave"]["elliott"].get("status"),
        }, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("QUANT ENGINE BUNDLE FAILED: %s", exc)
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
