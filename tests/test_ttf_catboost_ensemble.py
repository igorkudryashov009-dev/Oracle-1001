"""Tests for purged CV + CatBoost ensemble scaffolding."""

from __future__ import annotations

import numpy as np
import pandas as pd

from services.ttf_forecast.cross_validation import (
    PurgedWalkForwardSplit,
    assert_no_leakage,
)


def test_purged_walk_forward_gap():
    n = 250
    X = np.zeros((n, 3))
    splitter = PurgedWalkForwardSplit(n_splits=5, purge_days=14, min_train_size=80, test_size=20)
    folds = list(splitter.split_detailed(X))
    assert len(folds) >= 3
    for f in folds:
        assert assert_no_leakage(f.train_idx, f.test_idx, 14)
        assert np.min(f.test_idx) - np.max(f.train_idx) >= 14


def test_price_range_labels():
    from services.ttf_forecast.catboost_model import price_range_label

    assert price_range_label(65).startswith("A_")
    assert price_range_label(71).startswith("B_")
    assert price_range_label(78).startswith("C_")
    assert price_range_label(90).startswith("D_")


def test_weighted_merge_monotone():
    from services.ttf_forecast.ensemble_aggregator import weighted_merge

    parts = {
        "catboost": {"p10": 68.0, "p50": 72.0, "p90": 78.0},
        "markov": {"p10": 69.0, "p50": 73.0, "p90": 80.0},
        "spectral": {"p10": 67.0, "p50": 71.5, "p90": 76.0},
        "elliott": {"p10": 70.0, "p50": 72.5, "p90": 79.0},
    }
    m = weighted_merge(parts)
    assert m["p10"] <= m["p50"] <= m["p90"]
    assert 60.0 <= m["p10"] <= 95.0
    assert abs(m["p50"] - 72.0) < 3.0