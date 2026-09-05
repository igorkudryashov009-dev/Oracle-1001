"""
Build output/mission_control.html — portal over analytical contours.

Honest degradation only:
  Module 1 Fleet     → LIVE if fleet_database present with vessels
  Module 2 AIS       → STANDBY / ACCUMULATING / LIVE by days_accumulated
  Module 3 Forecast  → NO FORECAST if issued=false; LIVE if issued=true
  Module 4 Causal    → AWAITING / INSUFFICIENT OVERLAP / LIVE by price file + overlap
  Module 5 Gas weekly → AWAITING / BUDGET_TRIMMED / PARTIAL / LIVE from
                        features/gas_carrier_weekly/mission_control_module.json

Never paints LIVE without real backing data.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
FEATURES = ROOT / "features"
HISTORY = ROOT / "история1"
DAILY = HISTORY / "daily"
CONFIG = ROOT / "config.yaml"
OUT_HTML = OUTPUT / "mission_control.html"
PUBLISH_PAYLOAD_JSON = OUTPUT / "mission_control_payload.json"
PUBLISH_MANIFEST_JSON = OUTPUT / "publish_manifest.json"

FLEET_CSV = OUTPUT / "fleet_database.csv"
IDENTITY_CSV = OUTPUT / "identity_conflicts.csv"
MISMATCH_CSV = OUTPUT / "imo_mismatch.csv"
REVIEW_CSV = OUTPUT / "needs_review.csv"
NONVESSEL_CSV = OUTPUT / "non_vessel_entities.csv"
INVALID_CSV = OUTPUT / "invalid_imo_checksum.csv"
FORECAST_JSON = FEATURES / "forecast_ensemble_report.json"
CAUSAL_JSON = FEATURES / "causal_report.json"
TARGETS_JSON = ROOT / "targets.json"
GAS_WEEKLY_MODULE_JSON = FEATURES / "gas_carrier_weekly" / "mission_control_module.json"
GAS_WEEKLY_SUMMARY_JSON = FEATURES / "gas_carrier_weekly" / "summary.json"

AIS_TARGET_DAYS = 30  # days needed for stable analytics banner
TOTAL_MODULES = 5

# Patterns that must never appear in LD8 static publish artifacts (values, not doc mentions).
FORBIDDEN_PUBLISH_PATTERNS = (
    re.compile(r"AISSTREAM_API_KEY\s*=\s*\S+", re.I),
    re.compile(r"PROVIDER_API_KEY\s*=\s*\S+", re.I),
    re.compile(r"""userkey['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{12,}""", re.I),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY"),
)


def _load_cfg() -> dict[str, Any]:
    if not CONFIG.exists():
        return {}
    with CONFIG.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _rel_path(path: Path | str | None) -> str | None:
    """Publish-safe relative path from project root (no absolute Windows paths)."""
    if path is None:
        return None
    p = Path(path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.name


def _sanitize_for_publish(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sanitize_for_publish(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_publish(v) for v in obj]
    if isinstance(obj, str):
        if re.match(r"^[A-Za-z]:\\", obj) or obj.replace("\\", "/").startswith(str(ROOT).replace("\\", "/")):
            return _rel_path(obj)
    return obj


def assert_publish_safe(text: str, *, context: str = "artifact") -> None:
    for pat in FORBIDDEN_PUBLISH_PATTERNS:
        if pat.search(text):
            raise SystemExit(
                f"Publish safety check failed ({context}): matched {pat.pattern!r}"
            )


def _gas_top_n(cfg: dict) -> int:
    try:
        return int((cfg.get("gas_weekly") or {}).get("top_n_target", 500))
    except (TypeError, ValueError):
        return 500


def _fleet_csv_path(cfg: dict) -> Path:
    rel = (cfg.get("paths") or {}).get("fleet_database", "output/fleet_database.csv")
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def _attach_gas_universe_table(module: dict[str, Any], cfg: dict) -> dict[str, Any]:
    """Embed TOP-N gas universe for Mission Control filters (no provider secrets)."""
    from weekly_gas_carrier_monitor import load_gas_carriers

    top_n = _gas_top_n(cfg)
    fleet_path = _fleet_csv_path(cfg)
    module.setdefault("metrics", {})["top_n_target"] = top_n
    if not fleet_path.exists():
        module.setdefault("tables", {})["gas_universe_top500"] = []
        return module

    try:
        df = load_gas_carriers(fleet_path).iloc[:top_n]
    except Exception:
        module.setdefault("tables", {})["gas_universe_top500"] = []
        return module

    polled: set[str] = set()
    for row in (module.get("tables") or {}).get("poll_sample") or []:
        imo = row.get("imo")
        if imo is not None:
            polled.add(str(int(float(imo))) if str(imo).replace(".", "", 1).isdigit() else str(imo))

    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        imo = r.get("imo")
        imo_key = str(imo) if imo is not None else ""
        rows.append(
            {
                "imo": imo,
                "vessel_name": None if pd.isna(r.get("vessel_name")) else r.get("vessel_name"),
                "vessel_type": None if pd.isna(r.get("vessel_type")) else r.get("vessel_type"),
                "dwt_tons": None if pd.isna(r.get("dwt_tons")) else r.get("dwt_tons"),
                "risk_tier": r.get("risk_tier"),
                "compliance_risk_level": None
                if pd.isna(r.get("compliance_risk_level"))
                else r.get("compliance_risk_level"),
                "poll_status": "polled" if imo_key in polled else "pending",
            }
        )

    module.setdefault("tables", {})["gas_universe_top500"] = rows
    module["metrics"]["gas_universe_count"] = len(rows)
    module["metrics"]["gas_carriers_matched_in_fleet"] = int(len(load_gas_carriers(fleet_path)))
    return module


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _csv_rows(path: Path, limit: Optional[int] = None) -> list[dict]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    if limit is not None:
        df = df.head(limit)
    # keep lean columns for portal tables
    keep = [
        c
        for c in (
            "imo",
            "vessel_name",
            "vessel_type",
            "mmsi",
            "flag",
            "dwt_tons",
            "source_confidence",
            "identity_spoofing_suspected_imo",
            "identity_spoofing_note",
            "imo_from_text",
        )
        if c in df.columns
    ]
    if not keep:
        keep = list(df.columns)[:8]
    records = []
    for row in df[keep].to_dict(orient="records"):
        clean = {}
        for k, v in row.items():
            if pd.isna(v):
                clean[k] = None
            elif hasattr(v, "item"):
                clean[k] = v.item()
            else:
                clean[k] = v
        records.append(clean)
    return records


def _resolve_price_path(cfg: dict) -> Optional[Path]:
    rel = (cfg.get("paths") or {}).get("price_source")
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def collect_module1_fleet() -> dict[str, Any]:
    if not FLEET_CSV.exists():
        return {
            "id": "fleet",
            "name": "FLEET REGISTRY",
            "status": "STANDBY",
            "status_label": "STANDBY",
            "tone": "red",
            "banner": "fleet_database.csv отсутствует — запустите run_all.py",
            "live": False,
            "metrics": {},
            "tables": {},
            "links": {"full_dashboard": "dashboard.html"},
        }

    fleet = pd.read_csv(FLEET_CSV, usecols=lambda c: c in ("vessel_category", "imo", "imo_valid"))
    vessel_count = int((fleet["vessel_category"] == "vessel").sum()) if "vessel_category" in fleet.columns else int(len(fleet))
    identity_n = len(pd.read_csv(IDENTITY_CSV)) if IDENTITY_CSV.exists() else 0
    mismatch_n = len(pd.read_csv(MISMATCH_CSV)) if MISMATCH_CSV.exists() else 0
    review_n = len(pd.read_csv(REVIEW_CSV)) if REVIEW_CSV.exists() else 0
    non_n = len(pd.read_csv(NONVESSEL_CSV)) if NONVESSEL_CSV.exists() else 0
    invalid_n = len(pd.read_csv(INVALID_CSV)) if INVALID_CSV.exists() else 0

    # Prefer dashboard-embedded METRICS if present (authoritative run_all snapshot)
    metrics = {
        "vessel_count": vessel_count,
        "non_vessel": non_n,
        "imo_mismatch": mismatch_n,
        "identity_conflicts": identity_n,
        "needs_review": review_n,
        "invalid_imo": invalid_n,
    }
    dash = OUTPUT / "dashboard.html"
    if dash.exists():
        m = re.search(r"const METRICS = (\{.*?\});", dash.read_text(encoding="utf-8"), re.S)
        if m:
            try:
                embedded = json.loads(m.group(1))
                metrics = {
                    "vessel_count": int(embedded.get("vessel_count", vessel_count)),
                    "non_vessel": int(embedded.get("non_vessel", non_n)),
                    "imo_mismatch": int(embedded.get("imo_mismatch", mismatch_n)),
                    "identity_conflicts": int(embedded.get("identity_conflicts", identity_n)),
                    "needs_review": int(embedded.get("needs_review", review_n)),
                    "invalid_imo": int(embedded.get("invalid_imo", invalid_n)),
                    "avg_fill": embedded.get("avg_fill"),
                    "source_rows": embedded.get("source_rows"),
                    "valid_imo": embedded.get("valid_imo"),
                }
            except Exception:
                pass

    live = metrics["vessel_count"] > 0
    return {
        "id": "fleet",
        "name": "FLEET REGISTRY",
        "status": "LIVE" if live else "STANDBY",
        "status_label": "LIVE" if live else "STANDBY",
        "tone": "live" if live else "red",
        "banner": (
            f"Данные статичны с момента последнего run_all.py · vessels={metrics['vessel_count']}"
            if live
            else "Нет vessel-записей в fleet_database"
        ),
        "live": live,
        "metrics": metrics,
        "tables": {
            "identity_conflicts": _csv_rows(IDENTITY_CSV),
            "imo_mismatch": _csv_rows(MISMATCH_CSV),
            "needs_review_sample": _csv_rows(REVIEW_CSV, limit=50),
        },
        "links": {
            "full_dashboard": "dashboard.html",
            "note": "Полная таблица 5908 судов — в dashboard.html (iframe ниже при LIVE)",
        },
    }


def collect_module2_ais() -> dict[str, Any]:
    days = sorted(p.stem for p in DAILY.glob("*.csv")) if DAILY.exists() else []
    n_days = len(days)
    targets = _read_json(TARGETS_JSON) or {}
    stats = targets.get("stats") or {}
    n_tracked = int(stats.get("unique_mmsi_for_subscription") or 0)

    if n_days == 0:
        status, tone, label = "STANDBY", "red", "STANDBY"
        banner = (
            "Ожидание активации коллектора — архив пуст (0 дней). "
            "Нужны .env с AISSTREAM_API_KEY и запуск collector.py"
        )
    elif n_days < AIS_TARGET_DAYS:
        status, tone, label = "ACCUMULATING", "amber", "ACCUMULATING"
        banner = (
            f"Архив растёт: {n_days} дней из необходимых ~{AIS_TARGET_DAYS} "
            "для устойчивой аналитики"
        )
    else:
        status, tone, label = "LIVE", "live", "LIVE"
        banner = f"Архив активен: {n_days} дней накоплено · last={days[-1]}"

    return {
        "id": "ais",
        "name": "AIS TRACKING ARCHIVE",
        "status": status,
        "status_label": label,
        "tone": tone,
        "banner": banner,
        "live": status == "LIVE",
        "days_accumulated": n_days,
        "target_days": AIS_TARGET_DAYS,
        "daily_files": days[-14:],  # tail only
        "n_tracked_mmsi": n_tracked,
        "progress_pct": round(100.0 * min(n_days, AIS_TARGET_DAYS) / AIS_TARGET_DAYS, 1),
        "links": {"full_dashboard": "history_dashboard.html"},
        # Distinguish empty archive from "zeros that look like data"
        "data_state": "no_data" if n_days == 0 else "partial_data" if n_days < AIS_TARGET_DAYS else "ready",
    }


def _parse_min_n_gates(gates: dict) -> dict[str, int]:
    """Extract numeric thresholds from forecast_ensemble model_min_n_gates text."""
    out = {"naive": 8, "arima": 40, "gbm": 60, "lstm": 300}
    for key, text in (gates or {}).items():
        m = re.search(r"(\d+)", str(text))
        if m:
            # prefer last number for 'horizon+1 = 8'
            nums = re.findall(r"(\d+)", str(text))
            if nums:
                out[key if key in out else key] = int(nums[-1] if "horizon" in str(text) else nums[0])
                if key == "naive":
                    out["naive"] = int(nums[-1])
                elif key.startswith("arima"):
                    out["arima"] = int(nums[0])
                elif key.startswith("gbm"):
                    out["gbm"] = int(nums[0])
                elif key.startswith("lstm"):
                    out["lstm"] = int(nums[0])
    return out


def collect_module3_forecast() -> dict[str, Any]:
    report = _read_json(FORECAST_JSON)
    if not report:
        return {
            "id": "forecast",
            "name": "FORECAST ENSEMBLE",
            "status": "NO_FORECAST",
            "status_label": "NO FORECAST",
            "tone": "red",
            "banner": "forecast_ensemble_report.json отсутствует — запустите forecast_ensemble.py",
            "live": False,
            "issued": False,
            "n_observations": 0,
            "min_n_gates": {"naive": 8, "arima": 40, "gbm": 60, "lstm": 300},
            "links": {"full_dashboard": "forecast_dashboard.html"},
        }

    fc = report.get("forecast") or {}
    issued = bool(fc.get("issued"))
    n_obs = int((report.get("training_sample") or {}).get("n_observations") or 0)
    gates = _parse_min_n_gates(report.get("model_min_n_gates") or {})
    cov = (report.get("training_sample") or {}).get("coverage_pct_vessels_mean")

    progress = {
        name: {
            "required": req,
            "current": n_obs,
            "pct": round(100.0 * min(n_obs, req) / req, 1) if req else 0.0,
            "met": n_obs >= req,
        }
        for name, req in gates.items()
    }

    if not issued:
        min_needed = min(gates.values()) if gates else 8
        return {
            "id": "forecast",
            "name": "FORECAST ENSEMBLE",
            "status": "NO_FORECAST",
            "status_label": "NO FORECAST",
            "tone": "red",
            "banner": (
                f"ПРОГНОЗ НЕ ВЫДАН — недостаточно данных "
                f"({n_obs} наблюдений, требуется минимум {min_needed} для baseline; "
                f"ARIMA≥{gates.get('arima')}, GBM≥{gates.get('gbm')}, LSTM≥{gates.get('lstm')})"
            ),
            "live": False,
            "issued": False,
            "n_observations": n_obs,
            "coverage_pct": cov,
            "min_n_gates": gates,
            "progress": progress,
            "reason": fc.get("reason"),
            "when_not_to_trust": report.get("when_not_to_trust") or [],
            "links": {"full_dashboard": "forecast_dashboard.html"},
            "data_state": "no_data" if n_obs == 0 else "insufficient_data",
        }

    return {
        "id": "forecast",
        "name": "FORECAST ENSEMBLE",
        "status": "LIVE",
        "status_label": "LIVE",
        "tone": "live",
        "banner": (
            f"Прогноз выдан · N={n_obs} · coverage≈{cov}% · "
            f"horizon=T+{report.get('horizon')}"
        ),
        "live": True,
        "issued": True,
        "n_observations": n_obs,
        "coverage_pct": cov,
        "min_n_gates": gates,
        "progress": progress,
        "forecast": {
            "dates": fc.get("dates"),
            "point": fc.get("point"),
            "interval": fc.get("interval"),
        },
        "history": report.get("history"),
        "feature_importance": report.get("feature_importance") or {},
        "walk_forward": report.get("walk_forward"),
        "links": {"full_dashboard": "forecast_dashboard.html"},
        "data_state": "ready",
    }


def collect_module4_causal(cfg: dict) -> dict[str, Any]:
    price_path = _resolve_price_path(cfg)
    causal = _read_json(CAUSAL_JSON) or {}
    disclaimer = causal.get("disclaimer") or (
        "DISCLAIMER — Granger 'causality' is statistical predictability, "
        "NOT physical causation. Report as 'X statistically predicts Y', never 'X causes Y'."
    )

    if price_path is None or not price_path.exists():
        return {
            "id": "causal",
            "name": "CAUSAL LINK · FLEET ↔ TTF/BRENT",
            "status": "AWAITING",
            "status_label": "AWAITING DATA SOURCE",
            "tone": "red",
            "banner": (
                "Загрузите файл цен TTF/Brent, укажите путь в config.yaml → "
                "paths.price_source (PRICE_SOURCE_PATH), перезапустите causal_analysis.py"
            ),
            "live": False,
            "price_source": _rel_path(price_path),
            "price_exists": False,
            "disclaimer": disclaimer,
            "instruction": (
                "1) Положите CSV/XLSX с колонками date + ttf/brent (или long: symbol+value)\n"
                "2) config.yaml → paths.price_source: input/prices.csv\n"
                "3) .\\venv\\Scripts\\python.exe causal_analysis.py"
            ),
            "data_state": "no_data",
        }

    # File exists — inspect causal report / intersection
    pairs = causal.get("pairs") or []
    min_n = int((causal.get("config") or {}).get("min_intersection_points") or 30)
    best_n = 0
    any_ok = False
    any_refused_overlap = False
    pair_summaries = []
    for p in pairs:
        n = int(p.get("n_intersection") or p.get("n_after_transform") or 0)
        best_n = max(best_n, n)
        st = p.get("status")
        if st == "ok":
            any_ok = True
        if st == "refused" and n < min_n:
            any_refused_overlap = True
        pair_summaries.append(
            {
                "pair": p.get("pair"),
                "status": st,
                "n_intersection": n,
                "message": p.get("message"),
                "summary_statements": (p.get("granger") or {}).get("summary_statements"),
            }
        )

    # If causal never run after file appeared
    if causal.get("status") == "refused" and "not found" in str(causal.get("message", "")).lower():
        # stale report from before file existed — treat as awaiting re-run? 
        # But file NOW exists — ask to re-run
        pass

    if any_ok:
        return {
            "id": "causal",
            "name": "CAUSAL LINK · FLEET ↔ TTF/BRENT",
            "status": "LIVE",
            "status_label": "LIVE",
            "tone": "live",
            "banner": "Тест Грейнджера выполнен · см. пары ниже (predicts ≠ causes)",
            "live": True,
            "price_source": _rel_path(price_path),
            "price_exists": True,
            "min_intersection_points": min_n,
            "best_intersection_n": best_n,
            "pairs": pair_summaries,
            "disclaimer": disclaimer,
            "data_state": "ready",
        }

    # File present but insufficient overlap or not yet successfully tested
    if best_n < min_n:
        return {
            "id": "causal",
            "name": "CAUSAL LINK · FLEET ↔ TTF/BRENT",
            "status": "INSUFFICIENT_OVERLAP",
            "status_label": "INSUFFICIENT OVERLAP",
            "tone": "amber",
            "banner": (
                f"Файл цен найден, но пересечение с DWT-flow < {min_n} точек "
                f"(факт пересечения: {best_n}). Причинный тест статистически несостоятелен."
                if price_path.exists()
                else "Недостаточно точек пересечения"
            ),
            "live": False,
            "price_source": _rel_path(price_path),
            "price_exists": True,
            "min_intersection_points": min_n,
            "best_intersection_n": best_n,
            "progress_pct": round(100.0 * min(best_n, min_n) / min_n, 1),
            "pairs": pair_summaries,
            "disclaimer": disclaimer,
            "note": (
                "Если файл только что добавлен — перезапустите causal_analysis.py. "
                "Также нужен ненулевой DWT-flow архив (Модуль 2)."
                if best_n == 0
                else None
            ),
            "data_state": "insufficient_data",
        }

    return {
        "id": "causal",
        "name": "CAUSAL LINK · FLEET ↔ TTF/BRENT",
        "status": "AWAITING",
        "status_label": "AWAITING RUN",
        "tone": "amber",
        "banner": "Файл цен есть, но успешный causal_report ещё не получен — перезапустите causal_analysis.py",
        "live": False,
        "price_source": _rel_path(price_path),
        "price_exists": True,
        "disclaimer": disclaimer,
        "data_state": "insufficient_data",
    }


def collect_module5_gas_weekly(cfg: dict) -> dict[str, Any]:
    """
    Module 5 — weekly paid gas-carrier poll.
    Consumes features/gas_carrier_weekly/mission_control_module.json written by
    weekly_gas_carrier_monitor.py (same module contract as modules 1–4).
    """
    module = _read_json(GAS_WEEKLY_MODULE_JSON)
    if isinstance(module, dict) and module.get("id") == "gas_weekly":
        return _attach_gas_universe_table(module, cfg)

    summary = _read_json(GAS_WEEKLY_SUMMARY_JSON)
    if not summary:
        base = {
            "id": "gas_weekly",
            "name": "GAS CARRIER WEEKLY · PAID AIS",
            "status": "AWAITING",
            "status_label": "AWAITING RUN",
            "tone": "amber",
            "banner": (
                "Еженедельный опрос газовозов ещё не запускался. "
                "Настройте PROVIDER_API_KEY / PROVIDER_COST_PER_CALL_USD и выполните "
                "weekly_gas_carrier_monitor.py (см. README_GAS_MONITOR.md)."
            ),
            "live": False,
            "metrics": {"top_n_target": _gas_top_n(cfg)},
            "tables": {},
            "links": {
                "summary": "features/gas_carrier_weekly/summary.json",
                "results": "features/gas_carrier_weekly/results.json",
            },
            "data_state": "no_data",
        }
        return _attach_gas_universe_table(base, cfg)

    # Partial artifacts without mission_control_module.json — degrade honestly
    coverage = float(summary.get("coverage_of_top_n_target_pct") or 0)
    trimmed = bool(summary.get("budget_trimmed"))
    base = {
        "id": "gas_weekly",
        "name": "GAS CARRIER WEEKLY · PAID AIS",
        "status": "BUDGET_TRIMMED" if trimmed and coverage < 100 else "STANDBY",
        "status_label": "BUDGET TRIMMED" if trimmed and coverage < 100 else "STANDBY",
        "tone": "amber",
        "banner": (
            f"Найден summary.json без mission_control_module.json. "
            f"Coverage {coverage}%. Перезапустите weekly_gas_carrier_monitor.py."
        ),
        "live": False,
        "metrics": summary,
        "tables": {},
        "links": {
            "summary": "features/gas_carrier_weekly/summary.json",
            "results": "features/gas_carrier_weekly/results.json",
        },
        "data_state": "insufficient_data",
        "coverage_of_top_n_target_pct": coverage,
        "budget_trimmed": trimmed,
    }
    return _attach_gas_universe_table(base, cfg)


def build_payload() -> dict[str, Any]:
    cfg = _load_cfg()

    modules = [
        collect_module1_fleet(),
        collect_module2_ais(),
        collect_module3_forecast(),
        collect_module4_causal(cfg),
        collect_module5_gas_weekly(cfg),
    ]
    active = sum(1 for m in modules if m.get("live"))
    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "system_title": "MISSION CONTROL — FLEET INTELLIGENCE SYSTEM",
        "aggregate": {
            "active_modules": active,
            "total_modules": TOTAL_MODULES,
            "label": f"{active} из {TOTAL_MODULES} модулей активны (LIVE)",
        },
        "modules": modules,
    }


def log_summary(payload: dict[str, Any]) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parts = []
    for m in payload["modules"]:
        mid = m["id"]
        st = m["status_label"]
        if mid == "ais":
            parts.append(f"Модуль 2: {st} ({m.get('days_accumulated', 0)}/{m.get('target_days', 30)} дней)")
        elif mid == "forecast":
            gates = m.get("min_n_gates") or {}
            ar = gates.get("arima", 40)
            parts.append(
                f"Модуль 3: {st} ({m.get('n_observations', 0)}/{ar} набл. для ARIMA-порога)"
            )
        elif mid == "causal":
            if not m.get("price_exists"):
                parts.append("Модуль 4: AWAITING (файл цен не найден)")
            else:
                parts.append(
                    f"Модуль 4: {st} (пересечение {m.get('best_intersection_n', 0)}/"
                    f"{m.get('min_intersection_points', 30)})"
                )
        elif mid == "gas_weekly":
            cov = (m.get("metrics") or {}).get("coverage_of_top_n_target_pct", m.get("coverage_of_top_n_target_pct"))
            parts.append(f"Модуль 5: {st} (coverage {cov if cov is not None else 'n/a'}%)")
        else:
            parts.append(f"Модуль 1: {st}")
    print(" | ".join(parts))
    print(f"Aggregate: {payload['aggregate']['label']}")


def write_publish_artifacts(payload: dict[str, Any]) -> None:
    safe = _sanitize_for_publish(payload)
    payload_json = json.dumps(safe, ensure_ascii=False, indent=2)
    assert_publish_safe(payload_json, context="mission_control_payload.json")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    PUBLISH_PAYLOAD_JSON.write_text(payload_json, encoding="utf-8")
    manifest = {
        "generated_at_utc": safe.get("generated_at_utc"),
        "publish_root": "output/",
        "safe_files": [
            "mission_control.html",
            "mission_control_payload.json",
            "dashboard.html",
            "osint_layers.html",
            "history_dashboard.html",
            "forecast_dashboard.html",
            "design_system.css",
            "dwt_filter_sort.js",
        ],
        "excluded_patterns": [".env", "features/", "*.py", "PROVIDER_*", "AISSTREAM_*"],
        "modules": [
            {"id": m.get("id"), "status": m.get("status"), "live": m.get("live")}
            for m in safe.get("modules", [])
        ],
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
    assert_publish_safe(manifest_json, context="publish_manifest.json")
    PUBLISH_MANIFEST_JSON.write_text(manifest_json, encoding="utf-8")


def build_html(payload: dict[str, Any], out_path: Path = OUT_HTML) -> Path:
    safe = _sanitize_for_publish(payload)
    html = _TEMPLATE.replace("__PAYLOAD__", json.dumps(safe, ensure_ascii=False))
    assert_publish_safe(html, context="mission_control.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main() -> int:
    payload = build_payload()
    log_summary(payload)
    write_publish_artifacts(payload)
    path = build_html(payload)
    print(f"Wrote {path}")
    print(f"Wrote {PUBLISH_PAYLOAD_JSON}")
    print(f"Wrote {PUBLISH_MANIFEST_JSON}")
    return 0


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Fleet Intelligence · Mission Control</title>
<link rel="stylesheet" href="design_system.css" />
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.8/css/jquery.dataTables.min.css" />
<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
<script src="https://cdn.datatables.net/1.13.8/js/jquery.dataTables.min.js"></script>
<style>
  html, body { height: 100%; }
  .mc-layout { display: grid; grid-template-columns: 220px 1fr; gap: 20px; }
  .mc-side { position: sticky; top: 72px; align-self: start; }
  .mc-side .fi-pill { width: 100%; text-align: left; margin-bottom: 8px; }
  .mc-telem { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }
  .mc-telem .fi-card { cursor: pointer; padding: 18px 20px; transition: transform 160ms ease; }
  .mc-telem .fi-card:hover { transform: scale(1.02); }
  .mc-telem .fi-card.active { outline: 2px solid var(--accent); }
  iframe.fi-frame { width: 100%; height: 62vh; border: 0; border-radius: 16px; background: var(--bg-elevated); }
  .tabs-row { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
  @media (max-width: 1100px) {
    .mc-layout { grid-template-columns: 1fr; }
    .mc-telem { grid-template-columns: 1fr 1fr; }
    .mc-side { position: static; display: flex; gap: 8px; overflow: auto; }
    .mc-side .fi-pill { width: auto; }
  }
  @media (max-width: 768px) { .mc-telem { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<a class="fi-skip" href="#content">Skip to content</a>
<header class="fi-nav no-print">
  <a class="fi-brand" href="mission_control.html"><span class="fi-brand-mark">FI</span> Fleet Intelligence</a>
  <div class="fi-nav-links" id="status-pills"></div>
  <button type="button" class="fi-theme-toggle" id="theme-toggle" aria-label="Toggle color theme">◐</button>
</header>

<div class="fi-page">
  <div class="fi-skeleton-wrap">
    <div class="fi-skeleton" style="height:88px;margin-bottom:16px"></div>
    <div class="fi-grid"><div class="fi-skeleton"></div><div class="fi-skeleton"></div><div class="fi-skeleton"></div><div class="fi-skeleton"></div></div>
  </div>

  <div class="fi-ready-wrap">
    <div style="margin-bottom:8px">
      <h1 class="fi-title" id="title">Mission Control</h1>
      <p class="fi-muted" id="stamp" style="margin:8px 0 0"></p>
    </div>
    <div class="fi-card" style="margin:16px 0 20px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:center">
      <div>
        <div class="fi-card-title">System status</div>
        <div class="fi-metric" id="agg" style="font-size:26px"></div>
        <p class="fi-muted" style="margin:6px 0 0">Neutral summary — waiting modules are not failures.</p>
      </div>
      <button type="button" class="fi-pill" id="btn-howto">Что нужно для активации остальных</button>
    </div>
    <details class="fi-details">
      <summary>Что нужно для активации остальных</summary>
      <ul>
        <li><strong>AIS Archive:</strong> создать <code>.env</code> с <code>AISSTREAM_API_KEY</code>, запустить <code>collector.py</code>, дождаться ~30 дней daily-снимков.</li>
        <li><strong>Forecast:</strong> нужны наблюдения DWT-flow (пороги MIN_N: naive / ARIMA 40 / GBM 60 / LSTM 300).</li>
        <li><strong>Causal:</strong> положить файл цен TTF/Brent в <code>paths.price_source</code> (по умолчанию <code>input/prices.csv</code>), затем <code>causal_analysis.py</code>.</li>
        <li><strong>Gas weekly:</strong> задать <code>PROVIDER_API_KEY</code> и <code>PROVIDER_COST_PER_CALL_USD</code>, запустить <code>weekly_gas_carrier_monitor.py</code> (бюджет $50/мес, честное урезание списка).</li>
      </ul>
    </details>

    <div class="mc-telem" id="telem" role="tablist" aria-label="Module status"></div>

    <div class="mc-layout">
      <nav class="mc-side no-print" id="nav" aria-label="Modules"></nav>
      <main id="content" tabindex="-1"></main>
    </div>
  </div>
</div>

<script>
const P = __PAYLOAD__;

(function themeInit(){
  const root = document.documentElement;
  const saved = localStorage.getItem("fi-theme");
  if(saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  document.getElementById("theme-toggle").onclick = () => {
    const cur = root.getAttribute("data-theme");
    const preferDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const nowDark = cur === "dark" || (!cur && preferDark);
    const next = nowDark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem("fi-theme", next);
  };
})();

function esc(s){ return String(s??"").replace(/[&<>"']/g, c=>({ "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;" }[c])); }
function fmtMetric(v, pending){
  if(pending || v === null || v === undefined || v === "") return '<span class="fi-muted" title="No data yet">—</span>';
  return esc(v);
}
function tone(t){ return t === "live" ? "live" : (t === "amber" || t === "wait") ? "wait" : (t === "red" || t === "danger") ? "wait" : "wait"; }
/* Map legacy red standby to wait (Apple orange), keep danger only if explicitly critical */
function statusTone(m){
  if(m.tone === "live" || m.status === "LIVE") return "live";
  if(m.status === "NO_FORECAST" || m.status === "STANDBY" || m.status === "AWAITING" || m.status === "INSUFFICIENT_OVERLAP" || m.status === "ACCUMULATING" || m.status === "BUDGET_TRIMMED" || m.status === "PARTIAL") return "wait";
  return m.live ? "live" : "wait";
}

document.getElementById("title").textContent = "Mission Control";
document.getElementById("stamp").textContent = "Updated " + P.generated_at_utc + " UTC";
document.getElementById("agg").textContent = P.aggregate.label;
document.getElementById("btn-howto").onclick = () => {
  const d = document.getElementById("howto");
  d.open = !d.open;
};

const telem = document.getElementById("telem");
const nav = document.getElementById("nav");
const pills = document.getElementById("status-pills");

P.modules.forEach((m, i) => {
  const t = statusTone(m);
  const card = document.createElement("button");
  card.type = "button";
  card.className = "fi-card";
  card.setAttribute("role", "tab");
  card.setAttribute("data-id", m.id);
  card.innerHTML = `<div class="fi-card-title">Module ${i+1}</div>
    <div style="font-weight:600;margin-bottom:10px">${esc(m.name)}</div>
    <div class="fi-status ${t}"><span class="fi-dot ${t}"></span>${esc(m.status_label)}</div>`;
  card.onclick = () => selectModule(m.id);
  telem.appendChild(card);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "fi-pill";
  btn.setAttribute("data-id", m.id);
  btn.textContent = (i+1) + ". " + m.name.split("·")[0].trim();
  btn.onclick = () => selectModule(m.id);
  nav.appendChild(btn);

  const pill = document.createElement("span");
  pill.className = `fi-status ${t}`;
  pill.style.marginLeft = "8px";
  pill.innerHTML = `<span class="fi-dot ${t}"></span><span style="font-size:12px">${esc(m.status_label)}</span>`;
  pills.appendChild(pill);
});

let activeId = P.modules[0].id;

function progressBlock(title, current, required, pct, met){
  const fillCls = met ? "done" : "";
  return `<div class="fi-progress"><div class="fi-progress-meta"><span>${esc(title)}</span><span>${current} / ${required}</span></div>
    <div class="fi-progress-track"><div class="fi-progress-fill ${fillCls}" style="width:${Math.min(100,pct)}%"></div></div></div>`;
}

function renderTable(id, rows){
  if(!rows || !rows.length) return `<p class="fi-muted">No rows to display (absence of records — not zeroed fake data).</p>`;
  const cols = Object.keys(rows[0]);
  const head = cols.map(c=>`<th>${esc(c)}</th>`).join("");
  const body = rows.map(r=>`<tr>${cols.map(c=>`<td>${esc(r[c])}</td>`).join("")}</tr>`).join("");
  return `<div class="fi-table-wrap"><table id="${id}" class="display" style="width:100%"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function selectModule(id){
  activeId = id;
  document.querySelectorAll(".mc-telem .fi-card, .mc-side .fi-pill").forEach(n => {
    const on = n.getAttribute("data-id") === id;
    n.classList.toggle("active", on);
    if(n.classList.contains("fi-pill")) n.setAttribute("aria-pressed", on ? "true" : "false");
  });
  renderModule(P.modules.find(m => m.id === id));
}

function renderModule(m){
  const root = document.getElementById("content");
  const t = statusTone(m);
  let html = `<div class="fi-banner ${t} ${m.id==="forecast" && !m.issued ? "hero":""}"><span class="fi-status ${t}"><span class="fi-dot ${t}"></span>${esc(m.status_label)}</span>
    <div style="margin-top:10px;font-size:16px;color:var(--text)">${esc(m.banner)}</div></div>`;

  if(m.id === "fleet"){
    const mt = m.metrics || {};
    html += `<div class="fi-grid">
      <div class="fi-card"><div class="fi-card-title">Vessels</div><div class="fi-metric">${esc(mt.vessel_count)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Non-vessel</div><div class="fi-metric">${esc(mt.non_vessel)}</div></div>
      <div class="fi-card"><div class="fi-card-title">IMO mismatch</div><div class="fi-metric">${esc(mt.imo_mismatch)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Identity conflicts</div><div class="fi-metric danger">${esc(mt.identity_conflicts)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Needs review</div><div class="fi-metric">${esc(mt.needs_review)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Invalid IMO</div><div class="fi-metric">${esc(mt.invalid_imo)}</div></div>
    </div>
    <div class="tabs-row">
      <button type="button" class="fi-pill active" data-t="identity">Identity conflicts</button>
      <button type="button" class="fi-pill" data-t="mismatch">IMO mismatch</button>
      <button type="button" class="fi-pill" data-t="review">Needs review (sample)</button>
      <button type="button" class="fi-pill" data-t="full">Full registry</button>
    </div>
    <div class="fi-card" id="fleet-panel"></div>`;
    root.innerHTML = html;
    const panel = document.getElementById("fleet-panel");
    const show = (key) => {
      if(key==="full"){
        if(!m.live){ panel.innerHTML = `<p class="fi-muted">Full registry unavailable — module not LIVE.</p>`; return; }
        panel.innerHTML = `<div class="fi-card-title">Full fleet dashboard</div><iframe class="fi-frame" title="Fleet dashboard" src="${esc(m.links.full_dashboard)}"></iframe>`;
        return;
      }
      const map = {identity:"identity_conflicts", mismatch:"imo_mismatch", review:"needs_review_sample"};
      panel.innerHTML = `<div class="fi-card-title">${key}</div>` + renderTable("dt-"+key, (m.tables||{})[map[key]]);
      const table = document.getElementById("dt-"+key);
      if(table && !$.fn.dataTable.isDataTable(table)) $(table).DataTable({pageLength:10, order:[]});
    };
    root.querySelectorAll(".tabs-row .fi-pill").forEach(btn => btn.onclick = () => {
      root.querySelectorAll(".tabs-row .fi-pill").forEach(x=>x.classList.remove("active"));
      btn.classList.add("active"); show(btn.getAttribute("data-t"));
    });
    show("identity");
    return;
  }

  if(m.id === "ais"){
    const pending = m.data_state === "no_data";
    html += progressBlock("Archive days", m.days_accumulated||0, m.target_days, m.progress_pct, m.days_accumulated>=m.target_days);
    html += `<div class="fi-grid">
      <div class="fi-card"><div class="fi-card-title">Days accumulated</div><div class="fi-metric wait">${fmtMetric(m.days_accumulated, pending)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Target for stable analytics</div><div class="fi-metric">${esc(m.target_days)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Tracked MMSI</div><div class="fi-metric">${fmtMetric(m.n_tracked_mmsi, pending && !m.n_tracked_mmsi)}</div></div>
    </div>`;
    if(m.data_state === "no_data"){
      html += `<div class="fi-card" style="margin-top:16px"><div class="fi-card-title">Data state</div>
        <p><strong>No data yet</strong> — the archive has not started. This is not “0 km sailed” and not an empty map with zeros.
        Activate <code>.env</code> + <code>collector.py</code>.</p></div>`;
    } else if(m.live){
      html += `<div class="fi-card" style="margin-top:16px"><div class="fi-card-title">AIS archive</div><iframe class="fi-frame" title="AIS archive" src="${esc(m.links.full_dashboard)}"></iframe></div>`;
    } else {
      html += `<div class="fi-card" style="margin-top:16px"><div class="fi-card-title">Partial archive</div>
        <p class="fi-muted">Accumulated ${esc(m.days_accumulated)} daily file(s). Module remains ACCUMULATING until ~${esc(m.target_days)} days.
        <a href="${esc(m.links.full_dashboard)}">Open history dashboard</a></p></div>`;
    }
    root.innerHTML = html; return;
  }

  if(m.id === "forecast"){
    if(!m.issued){
      html += `<div class="fi-card"><div class="fi-card-title">MIN_N gates — progress to model eligibility</div>`;
      const prog = m.progress || {};
      Object.keys(m.min_n_gates||{}).forEach(k=>{
        const p = prog[k] || {current:m.n_observations||0, required:m.min_n_gates[k], pct:0, met:false};
        html += progressBlock(k.toUpperCase(), p.current, p.required, p.pct, p.met);
      });
      html += `<p class="fi-muted">Current DWT-flow observations: <strong>${esc(m.n_observations)}</strong>. Forecast is not simulated. ${esc(m.reason||"")}</p></div>`;
    } else {
      html += `<div class="fi-grid">
        <div class="fi-card"><div class="fi-card-title">N train</div><div class="fi-metric">${esc(m.n_observations)}</div></div>
        <div class="fi-card"><div class="fi-card-title">Coverage %</div><div class="fi-metric">${esc(m.coverage_pct)}</div></div>
        <div class="fi-card"><div class="fi-card-title">Issued</div><div class="fi-metric success">TRUE</div></div>
      </div>
      <div class="fi-card" style="margin-top:16px"><iframe class="fi-frame" title="Forecast" src="${esc(m.links.full_dashboard)}"></iframe></div>`;
    }
    root.innerHTML = html; return;
  }

  if(m.id === "causal"){
    html += `<div class="fi-disclaimer"><strong>Disclaimer:</strong> ${esc(m.disclaimer)}</div>`;
    if(!m.price_exists){
      html += `<div class="fi-card" style="margin-top:16px"><div class="fi-card-title">How to activate</div>
        <pre style="white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;color:var(--text-secondary)">${esc(m.instruction||"")}</pre>
        <p class="fi-muted">PRICE_SOURCE_PATH: ${esc(m.price_source||"(not set)")}</p></div>`;
    } else if(m.status === "INSUFFICIENT_OVERLAP"){
      html += progressBlock("Intersection points", m.best_intersection_n||0, m.min_intersection_points||30, m.progress_pct||0, false);
      html += `<div class="fi-card"><p class="fi-muted">${esc(m.note||"")}<br/>File: ${esc(m.price_source)}</p></div>`;
    } else if(m.live){
      html += `<div class="fi-card" style="margin-top:16px"><div class="fi-card-title">Granger pair summaries</div>`;
      (m.pairs||[]).forEach(p=>{
        html += `<div style="margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid var(--separator)">
          <div style="font-weight:600">${esc(p.pair)} · ${esc(p.status)} · n=${esc(p.n_intersection)}</div>
          ${(p.summary_statements||[]).map(s=>`<div class="fi-muted">• ${esc(s)}</div>`).join("")}
        </div>`;
      });
      html += `</div>`;
    } else {
      html += `<div class="fi-card" style="margin-top:16px"><p class="fi-muted">Price file: ${esc(m.price_source)} · re-run causal_analysis.py</p></div>`;
    }
    root.innerHTML = html; return;
  }

  if(m.id === "gas_weekly"){
    const mt = m.metrics || {};
    const cov = mt.coverage_of_top_n_target_pct ?? m.coverage_of_top_n_target_pct ?? 0;
    html += progressBlock("Budget coverage of gas list", mt.selected_for_poll||0, mt.gas_carriers_total||0, cov, cov>=100);
    html += `<div class="fi-grid">
      <div class="fi-card"><div class="fi-card-title">Gas carriers</div><div class="fi-metric">${esc(mt.gas_carriers_total)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Polled this week</div><div class="fi-metric">${esc(mt.selected_for_poll)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Coverage %</div><div class="fi-metric ${cov<100?"wait":""}">${esc(cov)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Week spend est. USD</div><div class="fi-metric">${esc(mt.estimated_week_spend_usd)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Success</div><div class="fi-metric success">${esc(mt.success_count)}</div></div>
      <div class="fi-card"><div class="fi-card-title">Errors</div><div class="fi-metric">${esc(mt.error_count)}</div></div>
    </div>`;
    if(m.budget_trimmed || mt.budget_trimmed){
      html += `<div class="fi-card" style="margin-top:16px"><div class="fi-card-title">Honest budget gate</div>
        <p>Список урезан под weekly call budget (${esc(mt.weekly_call_budget)}). Фиктивные позиции не добавляются.</p></div>`;
    }
    html += `<div class="fi-card" style="margin-top:16px"><div class="fi-card-title">Poll sample</div>${renderTable("dt-gas-ok",(m.tables||{}).poll_sample)}</div>`;
    if((m.tables||{}).errors && (m.tables||{}).errors.length){
      html += `<div class="fi-card" style="margin-top:16px"><div class="fi-card-title">Errors</div>${renderTable("dt-gas-err",(m.tables||{}).errors)}</div>`;
    }
    root.innerHTML = html;
    ["dt-gas-ok","dt-gas-err"].forEach(id=>{
      const table = document.getElementById(id);
      if(table && !$.fn.dataTable.isDataTable(table)) $(table).DataTable({pageLength:10, order:[]});
    });
    return;
  }
  root.innerHTML = html;
}

selectModule(activeId);
document.body.classList.add("is-ready");
</script>
</body>
</html>
"""
