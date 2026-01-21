"""
Tests for walk-forward splitter and validation utilities.

Focus:
- Holdout (gym/vault) split sizing
- Expanding window fold indices with purge gap
- Feature/target extraction excludes RAW_ columns
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.walk_forward import WalkForwardSplitter


def _make_processed_df(n_rows: int = 30) -> pd.DataFrame:
    """Create a small processed-style DataFrame."""
    dates = pd.date_range("2024-01-01", periods=n_rows, freq="5min")
    df = pd.DataFrame(
        {
            "Feature_A": np.linspace(0, 1, n_rows),
            "Feature_B": np.linspace(1, 0, n_rows),
            "RAW_Close": np.full(n_rows, 100.0),
            "RAW_Future_High": np.full(n_rows, 108.0),
            "RAW_Future_Low": np.full(n_rows, 92.0),
            "TARGET_Direction": np.random.choice([0, 1, 2], size=n_rows),
        },
        index=dates,
    )
    return df


def test_get_holdout_sizes():
    df = _make_processed_df(20)
    splitter = WalkForwardSplitter(
        holdout_pct=0.2, purge_bars=1, min_train_bars=5, fold_size_bars=3
    )

    gym_df, vault_df = splitter.get_holdout(df)

    assert len(gym_df) == 16
    assert len(vault_df) == 4
    assert gym_df.index.max() < vault_df.index.min()


def test_split_generates_folds_with_purge():
    df = _make_processed_df(25)
    splitter = WalkForwardSplitter(
        holdout_pct=0.2, purge_bars=1, min_train_bars=5, fold_size_bars=4
    )

    gym_df, _ = splitter.get_holdout(df)
    folds = list(splitter.split(gym_df))

    assert len(folds) >= 2
    for train_idx, test_idx in folds:
        assert train_idx.max() < test_idx.min()
        assert (test_idx.min() - train_idx.max()) >= splitter.purge_bars


def test_get_fold_data_excludes_raw_columns():
    df = _make_processed_df(20)
    splitter = WalkForwardSplitter(
        holdout_pct=0.2, purge_bars=1, min_train_bars=5, fold_size_bars=3
    )
    gym_df, _ = splitter.get_holdout(df)
    train_idx, test_idx = next(splitter.split(gym_df))

    X_train, y_train, X_test, y_test, df_test = splitter.get_fold_data(
        gym_df, train_idx, test_idx
    )

    assert "RAW_Close" not in X_train.columns
    assert "RAW_Future_High" not in X_train.columns
    assert "RAW_Future_Low" not in X_train.columns
    assert "TARGET_Direction" not in X_train.columns
    assert len(X_train) == len(y_train)
    assert len(X_test) == len(y_test)
    assert df_test.index.equals(gym_df.iloc[test_idx].index)
