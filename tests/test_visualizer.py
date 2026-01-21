"""
Tests for SignalVisualizer plotting utilities.
"""

import os
import sys

import numpy as np
import pandas as pd

# Ensure headless backend before matplotlib is imported by visualizer
os.environ.setdefault("MPLBACKEND", "Agg")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.visualizer import SignalVisualizer


def _make_plot_df(n_rows: int = 40) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n_rows, freq="5min")
    close = np.linspace(100, 102, n_rows)
    df = pd.DataFrame(
        {
            "RAW_Close": close,
            "RAW_Future_High": close + 1.0,
            "RAW_Future_Low": close - 1.0,
        },
        index=dates,
    )
    return df


def test_plot_signals_saves_file(tmp_path):
    df = _make_plot_df()
    preds = np.zeros(len(df), dtype=int)
    preds[::5] = 1
    preds[::7] = 2

    output_path = tmp_path / "signals.png"
    viz = SignalVisualizer()
    fig = viz.plot_signals(df, preds, output_path=str(output_path))

    assert fig is not None
    assert output_path.exists()


def test_plot_fold_summary_saves_file(tmp_path):
    fold_results = [
        {
            "y_true": np.array([0, 1, 2, 0]),
            "y_pred": np.array([0, 1, 1, 0]),
            "train_size": 10,
            "test_size": 4,
        },
        {
            "y_true": np.array([0, 1, 2, 2]),
            "y_pred": np.array([0, 2, 2, 2]),
            "train_size": 14,
            "test_size": 4,
        },
    ]

    output_path = tmp_path / "summary.png"
    viz = SignalVisualizer()
    fig = viz.plot_fold_summary(fold_results, output_path=str(output_path))

    assert fig is not None
    assert output_path.exists()


def test_plot_actual_moves_saves_file(tmp_path):
    df = _make_plot_df(20)
    preds = np.array([0, 1, 2, 0, 1] * 4)
    actual_moves = pd.DataFrame(
        {
            "Predicted": preds,
            "Actual_Up_Pct": np.random.rand(len(preds)) / 10,
            "Actual_Down_Pct": np.random.rand(len(preds)) / 10,
        },
        index=df.index,
    )

    output_path = tmp_path / "moves.png"
    viz = SignalVisualizer()
    fig = viz.plot_actual_moves(actual_moves, output_path=str(output_path))

    assert fig is not None
    assert output_path.exists()
