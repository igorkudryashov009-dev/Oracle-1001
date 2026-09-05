"""
Purged Walk-Forward / Group TimeSeriesSplit for TTF ML.

Prevents leakage from overlapping label horizons by purging a gap
(default 14 days) between train and test folds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np


@dataclass
class FoldSpec:
    fold_id: int
    train_idx: np.ndarray
    test_idx: np.ndarray
    purge_idx: np.ndarray
    train_time_range: tuple[int, int]
    test_time_range: tuple[int, int]


class PurgedWalkForwardSplit:
    """
    Expanding (or sliding) walk-forward CV with purge window.

    Parameters
    ----------
    n_splits : int
        Number of test folds (default 5).
    purge_days : int
        Gap (in samples for daily bars ≈ days) removed between last train
        observation and first test observation.
    embargo_days : int
        Extra samples dropped from train that fall immediately after test
        (Lopez de Prado embargo). Default 0 for pure walk-forward.
    min_train_size : int
        Minimum training observations before first fold.
    test_size : int | None
        Fixed test length; if None, remaining span is split evenly.
    """

    def __init__(
        self,
        n_splits: int = 5,
        purge_days: int = 14,
        embargo_days: int = 0,
        min_train_size: int = 90,
        test_size: Optional[int] = None,
    ):
        if n_splits < 2:
            raise ValueError("n_splits must be ≥ 2")
        self.n_splits = int(n_splits)
        self.purge_days = int(purge_days)
        self.embargo_days = int(embargo_days)
        self.min_train_size = int(min_train_size)
        self.test_size = test_size

    def split(
        self,
        X,
        y=None,
        groups=None,
        timestamps: Optional[np.ndarray] = None,
    ) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """Yield (train_idx, test_idx) integer index arrays."""
        for fold in self.split_detailed(X, y=y, groups=groups, timestamps=timestamps):
            yield fold.train_idx, fold.test_idx

    def split_detailed(
        self,
        X,
        y=None,
        groups=None,
        timestamps: Optional[np.ndarray] = None,
    ) -> Iterator[FoldSpec]:
        n = len(X) if not hasattr(X, "shape") else int(X.shape[0])
        if n < self.min_train_size + self.purge_days + self.n_splits:
            raise ValueError(
                f"Insufficient samples for purged WF: n={n}, "
                f"need ≥{self.min_train_size + self.purge_days + self.n_splits}"
            )

        if timestamps is None:
            timestamps = np.arange(n)
        else:
            timestamps = np.asarray(timestamps)

        # Reserve final contiguous block for successive test folds
        usable = n - self.min_train_size - self.purge_days
        if usable < self.n_splits:
            raise ValueError("Not enough span for requested n_splits after purge/min_train")

        test_size = self.test_size or max(14, usable // self.n_splits)
        # Place folds from earliest test window to latest (walk-forward)
        first_test_start = self.min_train_size + self.purge_days
        # Ensure last fold fits
        max_start = n - test_size
        if max_start < first_test_start:
            raise ValueError("test_size too large for series length")

        starts = np.linspace(first_test_start, max_start, self.n_splits).astype(int)
        # de-duplicate starts
        starts = np.unique(starts)
        while len(starts) < self.n_splits and starts[-1] + 1 <= max_start:
            starts = np.append(starts, starts[-1] + 1)
        starts = starts[: self.n_splits]

        for fold_id, test_start in enumerate(starts):
            test_end = min(test_start + test_size, n)
            test_idx = np.arange(test_start, test_end)

            # Train = everything before purge gap
            train_end = test_start - self.purge_days
            if train_end <= 0:
                continue
            train_idx = np.arange(0, train_end)

            # Embargo: drop train points that are within embargo after test_end
            # (relevant for overlapping label horizons in non-strict WF)
            purge_mask = np.zeros(n, dtype=bool)
            purge_lo = max(0, train_end)
            purge_hi = test_start
            purge_mask[purge_lo:purge_hi] = True
            if self.embargo_days > 0:
                emb_hi = min(n, test_end + self.embargo_days)
                purge_mask[test_end:emb_hi] = True
                train_idx = train_idx[~np.isin(train_idx, np.arange(test_end, emb_hi))]

            purge_idx = np.where(purge_mask)[0]
            if len(train_idx) < self.min_train_size // 2 or len(test_idx) < 5:
                continue

            yield FoldSpec(
                fold_id=fold_id,
                train_idx=train_idx,
                test_idx=test_idx,
                purge_idx=purge_idx,
                train_time_range=(int(timestamps[train_idx[0]]), int(timestamps[train_idx[-1]])),
                test_time_range=(int(timestamps[test_idx[0]]), int(timestamps[test_idx[-1]])),
            )

    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        return self.n_splits


def assert_no_leakage(train_idx: np.ndarray, test_idx: np.ndarray, purge_days: int) -> bool:
    """Return True if min(test) - max(train) >= purge_days."""
    if len(train_idx) == 0 or len(test_idx) == 0:
        return False
    gap = int(np.min(test_idx) - np.max(train_idx))
    return gap >= purge_days
