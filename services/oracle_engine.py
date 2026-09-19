"""
OracleEngine — server-side prognostic evaluator for Dual Gate / G3 / archive / ML.

Python SoT twin of web/js/oracle_engine.js. Thresholds are imported from
services.dual_gate (never duplicated). Fleet-sample INSUFFICIENT maps to OOB
contract mode WARN_NOMINAL (logged warning, does not fail the contract).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.dual_gate import (
    FLEET_SAMPLE_FULL_MIN,
    FLEET_SAMPLE_LIMITED_MIN,
    G3_COVERAGE_PLATEAU_MAX,
    G3_COVERAGE_PLATEAU_MIN,
    compute_fleet_sample_status,
    compute_pipeline_health_status,
    export_dual_gate_thresholds,
    live_inference_confidence,
)

logger = logging.getLogger("oracle_engine")

ORACLE_MODULE_ID = "oracle_engine"
ORACLE_VERSION = "1.0.0"

CONTRACT_MODE_WARN_NOMINAL = "WARN_NOMINAL"
CONTRACT_MODE_PASS = "PASS"
CONTRACT_MODE_PASS_LIMITED = "PASS_LIMITED"
CONTRACT_MODE_BLOCKED = "BLOCKED"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def oracle_safe_parse_metrics(raw: Any) -> tuple[bool, dict[str, Any], Optional[str]]:
    """Parse object / JSON string without raising. Returns (ok, data, error)."""
    try:
        if raw is None:
            return True, {}, None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return True, {}, None
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                return False, {}, "oracle_safe_parse_metrics: JSON root must be an object"
            return True, parsed, None
        if isinstance(raw, dict):
            return True, raw, None
        return False, {}, f"oracle_safe_parse_metrics: unsupported type {type(raw).__name__}"
    except (json.JSONDecodeError, TypeError, UnicodeError, ValueError) as exc:
        return False, {}, f"oracle_safe_parse_metrics: {exc}"


def resolve_oob_contract_mode(
    *,
    fleet_sample_status: str,
    pipeline_health_status: str,
) -> str:
    """Map Dual Gate planes → OOB contract mode.

    INSUFFICIENT never fails the OOB contract: mode WARN_NOMINAL (green + warn).
    Publish blocking remains pipeline_health-only (AGENTS Dual Gate).
    """
    fleet = str(fleet_sample_status or "UNKNOWN").upper()
    pipe = str(pipeline_health_status or "UNKNOWN").upper()

    if fleet == "INSUFFICIENT":
        return CONTRACT_MODE_WARN_NOMINAL
    if pipe in {"CRITICAL", "UNKNOWN"}:
        return CONTRACT_MODE_BLOCKED
    if fleet == "LIMITED":
        return CONTRACT_MODE_PASS_LIMITED
    if fleet == "FULL" and pipe == "NOMINAL":
        return CONTRACT_MODE_PASS
    if fleet == "FULL":
        return CONTRACT_MODE_PASS_LIMITED
    return CONTRACT_MODE_WARN_NOMINAL


def build_oracle_state(
    *,
    status: str,
    confidence: Any,
    coverage: int | float,
    last_eval: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical health.json oracle_state object."""
    out: dict[str, Any] = {
        "status": str(status or "UNKNOWN").upper(),
        "confidence": confidence,
        "coverage": int(coverage or 0),
        "last_eval": last_eval or _utc_now(),
    }
    if extra:
        for k, v in extra.items():
            if k not in out:
                out[k] = v
    return out


class OracleEngine:
    """Prognostic Dual Gate / G3 / archive / ML evaluator."""

    def __init__(self, seed: Any = None) -> None:
        self.module_id = ORACLE_MODULE_ID
        self.version = ORACLE_VERSION
        self._last_thresholds: dict[str, Any] | None = None
        self._last_ml_drift: dict[str, Any] | None = None
        self._errors: list[str] = []
        self._evaluated_at: str | None = None
        if seed is not None:
            try:
                self.evaluate_thresholds(seed)
            except Exception as exc:  # noqa: BLE001
                self._push_error("constructor", exc)

    def _push_error(self, where: str, err: Any) -> None:
        try:
            msg = getattr(err, "message", None) or str(err)
            self._errors.append(f"{where}: {msg}")
            if len(self._errors) > 32:
                self._errors = self._errors[-32:]
        except Exception:  # noqa: BLE001
            pass

    def evaluate_thresholds(self, metrics_data: Any) -> dict[str, Any]:
        """Run metrics through Dual Gate + G3 advisory + archive honesty."""
        try:
            ok, data, parse_error = oracle_safe_parse_metrics(metrics_data)
            if not ok and parse_error:
                self._push_error("evaluate_thresholds.parse", parse_error)

            quant = data.get("quant_pipeline") if isinstance(data.get("quant_pipeline"), dict) else {}
            fleet_block = data.get("fleet_sample") if isinstance(data.get("fleet_sample"), dict) else {}
            replica = data.get("replica") if isinstance(data.get("replica"), dict) else {}
            pipeline_in = (
                data.get("pipeline_health") if isinstance(data.get("pipeline_health"), dict) else {}
            )
            disk = data.get("disk") if isinstance(data.get("disk"), dict) else {}

            coverage = int(
                data.get("top500_live_coverage")
                or fleet_block.get("top500_live_coverage")
                or quant.get("top500_live_coverage")
                or data.get("coverage")
                or 0
            )
            universe = int(fleet_block.get("top500_universe") or data.get("top500_universe") or 500)
            sample = compute_fleet_sample_status(coverage, universe=universe)

            age = data.get("ais_lag_sec", pipeline_in.get("ais_lag_sec", replica.get("age_sec")))
            freshness = {
                "age_sec": age,
                "live_ok": bool(
                    data.get("live_ok", pipeline_in.get("live_ok", replica.get("live_ok", False)))
                ),
                "stale": bool(data.get("stale", replica.get("stale", False))),
                "integrity_ok": replica.get("integrity_ok", data.get("integrity_ok", True)),
                "status": replica.get("status"),
                "ais_truth": replica.get("ais_truth") or data.get("ais_truth"),
            }
            connector = {
                "reconnects": int(data.get("reconnects") or pipeline_in.get("reconnects") or 0),
                "rate_limit_hits": int(
                    data.get("rate_limit_hits")
                    or pipeline_in.get("rate_limit_hits")
                    or data.get("http_429_count")
                    or 0
                ),
                "http_429_count": int(data.get("http_429_count") or 0),
            }
            if data.get("disk_free_pct") is not None and "disk_free_pct" not in disk:
                disk = {**disk, "disk_free_pct": data.get("disk_free_pct")}

            pipe = compute_pipeline_health_status(
                freshness=freshness,
                connector=connector,
                port_ok=bool(data.get("port_ok", pipeline_in.get("port_ok", True))),
                port_drift_8478=bool(data.get("port_drift_8478", False)),
                active_node=str(data.get("active_node") or pipeline_in.get("active_node") or "korolev"),
                failover_in_progress=bool(
                    data.get("failover_in_progress", pipeline_in.get("failover_in_progress", False))
                ),
                disk=disk or None,
            )

            cv_raw = quant.get("model_cv_accuracy_pct", data.get("model_cv_accuracy_pct"))
            try:
                cv_pct = float(cv_raw) if cv_raw is not None else None
            except (TypeError, ValueError):
                cv_pct = None
            live_conf = live_inference_confidence(
                model_cv_accuracy_pct=cv_pct,
                fleet_sample_status=str(sample["fleet_sample_status"]),
                coverage=coverage,
            )

            archive = data.get("archive") if isinstance(data.get("archive"), dict) else {}
            api_status = data.get("api_status") if isinstance(data.get("api_status"), dict) else {}
            label = str(
                archive.get("label")
                or api_status.get("label")
                or data.get("archive_label")
                or "ARCHIVE REGISTRY: SNAPSHOT / DEMO MODE"
            )
            archive_plane = {
                "plane": "archive",
                "feeds_fleet_sample": False,
                "is_synthetic": bool(
                    archive.get("is_synthetic", api_status.get("is_synthetic", data.get("is_synthetic", True)))
                ),
                "demo_mode": bool(archive.get("demo_mode", api_status.get("demo_mode", True))),
                "label": label,
                "honest": "PREMIUM SATELLITE" not in label.upper(),
            }

            g3_band = "below_plateau"
            if G3_COVERAGE_PLATEAU_MIN <= coverage <= G3_COVERAGE_PLATEAU_MAX:
                g3_band = "within_plateau"
            elif coverage > G3_COVERAGE_PLATEAU_MAX and coverage < FLEET_SAMPLE_LIMITED_MIN:
                g3_band = "above_plateau_still_insufficient"
            elif FLEET_SAMPLE_LIMITED_MIN <= coverage < FLEET_SAMPLE_FULL_MIN:
                g3_band = "limited_sample"
            elif coverage >= FLEET_SAMPLE_FULL_MIN:
                g3_band = "full_sample"

            contract_mode = resolve_oob_contract_mode(
                fleet_sample_status=str(sample["fleet_sample_status"]),
                pipeline_health_status=str(pipe["pipeline_health_status"]),
            )
            last_eval = _utc_now()
            oracle_state = build_oracle_state(
                status=str(sample["fleet_sample_status"]),
                confidence=live_conf.get("live_inference_confidence"),
                coverage=coverage,
                last_eval=last_eval,
                extra={
                    "contract_mode": contract_mode,
                    "pipeline_health_status": pipe["pipeline_health_status"],
                    "live_inference_confidence_pct": live_conf.get("live_inference_confidence_pct"),
                    "module": self.module_id,
                    "version": self.version,
                },
            )

            if contract_mode == CONTRACT_MODE_WARN_NOMINAL:
                logger.info(
                    "OracleEngine WARN_NOMINAL: fleet_sample_status=INSUFFICIENT "
                    "coverage=%s pipeline=%s (OOB stays green)",
                    coverage,
                    pipe.get("pipeline_health_status"),
                )

            result: dict[str, Any] = {
                "ok": ok,
                "module": self.module_id,
                "version": self.version,
                "evaluated_at": last_eval,
                "parse_error": parse_error,
                "thresholds": export_dual_gate_thresholds(),
                "pipeline_health": pipe,
                "pipeline_health_status": pipe["pipeline_health_status"],
                "fleet_sample": sample,
                "fleet_sample_status": sample["fleet_sample_status"],
                "g3": {
                    "plane": "g3_terrestrial",
                    "top500_live_coverage": coverage,
                    "plateau_min": G3_COVERAGE_PLATEAU_MIN,
                    "plateau_max": G3_COVERAGE_PLATEAU_MAX,
                    "band": g3_band,
                    "chase_full_forbidden": True,
                },
                "archive": archive_plane,
                "live_inference": live_conf,
                "oracle_state": oracle_state,
                "contract_mode": contract_mode,
                "publish_blocked": pipe["pipeline_health_status"] != "NOMINAL",
                "production_actionable": (
                    pipe["pipeline_health_status"] == "NOMINAL"
                    and sample["fleet_sample_status"] == "FULL"
                    and archive_plane.get("honest", True)
                    and not bool(data.get("is_synthetic", False))
                ),
            }
            self._last_thresholds = result
            self._evaluated_at = last_eval
            return result
        except Exception as exc:  # noqa: BLE001
            self._push_error("evaluate_thresholds", exc)
            last_eval = _utc_now()
            fallback = {
                "ok": False,
                "module": self.module_id,
                "version": self.version,
                "evaluated_at": last_eval,
                "parse_error": str(exc),
                "pipeline_health_status": "UNKNOWN",
                "fleet_sample_status": "UNKNOWN",
                "contract_mode": CONTRACT_MODE_WARN_NOMINAL,
                "oracle_state": build_oracle_state(
                    status="UNKNOWN",
                    confidence="LOW",
                    coverage=0,
                    last_eval=last_eval,
                    extra={"contract_mode": CONTRACT_MODE_WARN_NOMINAL},
                ),
                "publish_blocked": True,
                "production_actionable": False,
            }
            self._last_thresholds = fallback
            self._evaluated_at = last_eval
            return fallback

    def predict_ml_drift(self, ml_metrics: Any) -> dict[str, Any]:
        """Advisory ML drift probability — never a trading signal."""
        try:
            ok, data, parse_error = oracle_safe_parse_metrics(ml_metrics)
            if not ok and parse_error:
                self._push_error("predict_ml_drift.parse", parse_error)
            quant = data.get("quant_pipeline") if isinstance(data.get("quant_pipeline"), dict) else data

            def _f(key: str, default: float | None = None) -> float | None:
                raw = quant.get(key, data.get(key))
                if raw is None:
                    return default
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return default

            coverage = int(data.get("top500_live_coverage") or quant.get("top500_live_coverage") or 0)
            fleet_status = str(
                data.get("fleet_sample_status")
                or quant.get("fleet_sample_status")
                or compute_fleet_sample_status(coverage)["fleet_sample_status"]
            ).upper()
            cv = _f("model_cv_accuracy_pct")
            conf = live_inference_confidence(
                model_cv_accuracy_pct=cv,
                fleet_sample_status=fleet_status,
                coverage=coverage,
            )
            live_pct = _f("live_inference_confidence_pct", conf.get("live_inference_confidence_pct"))
            factor = _f("live_confidence_factor", float(conf.get("live_confidence_factor") or 0.01))
            assert factor is not None

            drift_pp = None
            if cv is not None and live_pct is not None:
                drift_pp = round(abs(cv - live_pct), 2)

            p_drift = max(0.0, min(1.0, 1.0 - float(factor)))
            if drift_pp is not None:
                p_drift = max(0.0, min(1.0, 0.55 * p_drift + 0.45 * (drift_pp / 100.0)))
            is_synthetic = bool(data.get("is_synthetic", quant.get("is_synthetic", fleet_status != "FULL")))
            if is_synthetic:
                p_drift = min(1.0, p_drift + 0.15)
            p_drift = round(p_drift, 4)

            if p_drift < 0.15:
                band = "LOW"
            elif p_drift < 0.4:
                band = "MODERATE"
            elif p_drift < 0.7:
                band = "HIGH"
            else:
                band = "CRITICAL"

            result = {
                "ok": ok,
                "module": self.module_id,
                "version": self.version,
                "predicted_at": _utc_now(),
                "parse_error": parse_error,
                "model_cv_accuracy_pct": conf.get("model_cv_accuracy_pct"),
                "live_inference_confidence": conf.get("live_inference_confidence"),
                "live_inference_confidence_pct": conf.get("live_inference_confidence_pct"),
                "live_confidence_factor": conf.get("live_confidence_factor"),
                "fleet_sample_status": fleet_status,
                "drift_pp": drift_pp,
                "p_drift": p_drift,
                "drift_band": band,
                "model_provenance": str(
                    data.get("model_provenance") or quant.get("model_provenance") or "offline_batch"
                ),
                "is_synthetic": is_synthetic,
                "production_actionable": False,
                "advisory_only": True,
            }
            self._last_ml_drift = result
            return result
        except Exception as exc:  # noqa: BLE001
            self._push_error("predict_ml_drift", exc)
            fallback = {
                "ok": False,
                "module": self.module_id,
                "version": self.version,
                "predicted_at": _utc_now(),
                "parse_error": str(exc),
                "p_drift": None,
                "drift_band": "UNKNOWN",
                "production_actionable": False,
                "advisory_only": True,
            }
            self._last_ml_drift = fallback
            return fallback

    def get_health_status(self) -> dict[str, Any]:
        """Aggregate last evaluation into a health-shaped payload + oracle_state."""
        try:
            thr = self._last_thresholds or {}
            ml = self._last_ml_drift
            oracle_state = thr.get("oracle_state") or build_oracle_state(
                status="UNKNOWN",
                confidence="LOW",
                coverage=0,
                last_eval=self._evaluated_at,
                extra={"contract_mode": CONTRACT_MODE_WARN_NOMINAL},
            )
            return {
                "service": "oracle_engine",
                "module": self.module_id,
                "version": self.version,
                "status": "ok" if thr.get("pipeline_health_status") == "NOMINAL" else "degraded",
                "operational_status": thr.get("pipeline_health_status") or "UNKNOWN",
                "pipeline_health_status": thr.get("pipeline_health_status") or "UNKNOWN",
                "fleet_sample_status": thr.get("fleet_sample_status") or "UNKNOWN",
                "oracle_state": oracle_state,
                "contract_mode": thr.get("contract_mode") or oracle_state.get("contract_mode"),
                "ml_drift": (
                    {
                        "p_drift": ml.get("p_drift"),
                        "drift_band": ml.get("drift_band"),
                        "advisory_only": True,
                    }
                    if isinstance(ml, dict)
                    else None
                ),
                "evaluated_at": self._evaluated_at,
                "generated_at": _utc_now(),
                "errors": list(self._errors),
            }
        except Exception as exc:  # noqa: BLE001
            self._push_error("get_health_status", exc)
            last_eval = _utc_now()
            return {
                "service": "oracle_engine",
                "module": self.module_id,
                "version": self.version,
                "status": "degraded",
                "oracle_state": build_oracle_state(
                    status="UNKNOWN",
                    confidence="LOW",
                    coverage=0,
                    last_eval=last_eval,
                    extra={"contract_mode": CONTRACT_MODE_WARN_NOMINAL},
                ),
                "contract_mode": CONTRACT_MODE_WARN_NOMINAL,
                "generated_at": last_eval,
                "errors": list(self._errors),
            }

    def attach_to_health_document(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Evaluate doc and inject oracle_state (mutates and returns doc)."""
        try:
            result = self.evaluate_thresholds(doc)
            quant = doc.get("quant_pipeline") if isinstance(doc.get("quant_pipeline"), dict) else {}
            self.predict_ml_drift({**doc, **quant})
            state = result.get("oracle_state") or build_oracle_state(
                status=str(doc.get("fleet_sample_status") or "UNKNOWN"),
                confidence="LOW",
                coverage=int(doc.get("top500_live_coverage") or 0),
            )
            doc["oracle_state"] = state
            doc["oracle_contract_mode"] = result.get("contract_mode") or state.get("contract_mode")
            # Dual Gate thresholds SoT for HUD — JS must not hardcode ORACLE_THRESHOLDS.
            doc["thresholds"] = result.get("thresholds") or export_dual_gate_thresholds()
            return doc
        except Exception as exc:  # noqa: BLE001
            self._push_error("attach_to_health_document", exc)
            doc["oracle_state"] = build_oracle_state(
                status=str(doc.get("fleet_sample_status") or "UNKNOWN"),
                confidence="LOW",
                coverage=int(doc.get("top500_live_coverage") or 0),
                extra={"contract_mode": CONTRACT_MODE_WARN_NOMINAL, "error": str(exc)},
            )
            doc["oracle_contract_mode"] = CONTRACT_MODE_WARN_NOMINAL
            doc["thresholds"] = export_dual_gate_thresholds()
            return doc


def oracle_create_engine(seed: Any = None) -> OracleEngine:
    return OracleEngine(seed=seed)


def apply_oracle_state_to_health_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Public helper for ais_health / write_health_files."""
    return OracleEngine().attach_to_health_document(doc)


def sync_oracle_state_health_files(root: Path | None = None) -> dict[str, Any]:
    """Re-read health.json, attach oracle_state, write back. Safe no-op on errors."""
    root = root or Path(__file__).resolve().parents[1]
    health_path = root / "output" / "api" / "v1" / "health.json"
    bare_path = root / "output" / "api" / "v1" / "health"
    try:
        if health_path.is_file():
            doc = json.loads(health_path.read_text(encoding="utf-8"))
        elif bare_path.is_file():
            doc = json.loads(bare_path.read_text(encoding="utf-8"))
        else:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        apply_oracle_state_to_health_doc(doc)
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text(text, encoding="utf-8")
        bare_path.write_text(text, encoding="utf-8")
        return doc.get("oracle_state") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("sync_oracle_state_health_files failed: %s", exc)
        return build_oracle_state(
            status="UNKNOWN",
            confidence="LOW",
            coverage=0,
            extra={"contract_mode": CONTRACT_MODE_WARN_NOMINAL, "error": str(exc)},
        )
