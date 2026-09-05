"""
TTF forecasting schema extension for sentinel_ais.db.

Tables:
  - ttf_market_features   daily market + exogenous feature store
  - ttf_predictions_store probabilistic forecast archive
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "история1" / "sentinel_ais.db"

TTF_MARKET_FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS ttf_market_features (
    timestamp INTEGER PRIMARY KEY,
    price_ttf_eur_mwh REAL NOT NULL,
    volume REAL,
    lng_flow_rate_eia REAL,
    shadow_tanker_density_gulf INTEGER,
    weather_degree_days REAL,
    temp_anomaly_europe REAL,
    storage_fill_level_pct REAL
)
"""

TTF_PREDICTIONS_STORE_DDL = """
CREATE TABLE IF NOT EXISTS ttf_predictions_store (
    model_id TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    target_date INTEGER NOT NULL,
    price_p10 REAL,
    price_p50 REAL,
    price_p90 REAL,
    top_prob_pct REAL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (model_id, horizon_days, target_date, created_at)
)
"""

TTF_TABLES = (
    "ttf_market_features",
    "ttf_predictions_store",
)

INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_ttf_pred_target ON ttf_predictions_store(target_date)",
    "CREATE INDEX IF NOT EXISTS idx_ttf_pred_model ON ttf_predictions_store(model_id, horizon_days)",
)


def resolve_db(explicit: Optional[Path | str] = None) -> Path:
    if explicit:
        return Path(explicit)
    for cand in (
        DEFAULT_DB,
        ROOT / "sentinel_ais.db",
        Path("/opt/oracle1001/ais_ingest/история1/sentinel_ais.db"),
        Path("/opt/oracle1001/analytical_engine/история1/sentinel_ais.db"),
    ):
        if cand.parent.exists():
            return cand
    return DEFAULT_DB


def migrate_ttf_schema(db_path: Optional[Path | str] = None) -> dict:
    """Idempotent DDL migration for TTF feature / prediction stores."""
    path = resolve_db(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute(TTF_MARKET_FEATURES_DDL)
        conn.execute(TTF_PREDICTIONS_STORE_DDL)
        for ddl in INDEX_DDL:
            conn.execute(ddl)
        conn.commit()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ttf_%'"
            )
        }
        return {
            "db": str(path),
            "ok": all(t in tables for t in TTF_TABLES),
            "tables": sorted(tables),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    print(migrate_ttf_schema())
