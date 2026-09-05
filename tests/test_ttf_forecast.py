"""Smoke tests for TTF ingest + feature store."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def test_ttf_migrate_and_ingest(tmp_path: Path):
    from services.ttf_forecast.ingest_ttf import run_ingest
    from services.ttf_forecast.schema import migrate_ttf_schema

    db = tmp_path / "sentinel_ais.db"
    meta = migrate_ttf_schema(db)
    assert meta["ok"] is True
    assert "ttf_market_features" in meta["tables"]
    assert "ttf_predictions_store" in meta["tables"]

    result = run_ingest(db_path=db, days=365, force_synthetic=True)
    assert result["rows_upserted"] == 365
    assert result["price_last"] is not None


def test_ttf_feature_parquet(tmp_path: Path):
    from services.ttf_forecast.feature_engineering import run_feature_pipeline
    from services.ttf_forecast.ingest_ttf import run_ingest

    db = tmp_path / "sentinel_ais.db"
    out = tmp_path / "ttf_features.parquet"
    run_ingest(db_path=db, days=365, force_synthetic=True)
    meta = run_feature_pipeline(db_path=db, out_path=out, ensure_ingest=False)
    assert out.exists()
    assert out.stat().st_size > 0
    assert meta["rows"] > 200
    df = pd.read_parquet(out)
    assert "log_return" in df.columns
    assert "vol_30d" in df.columns
    assert "rsi_14" in df.columns
    assert "macd" in df.columns
    assert "lng_tanker_transit_index" in df.columns
    assert "log_return__robust" in df.columns
