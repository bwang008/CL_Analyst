"""
Tests for ModelEvaluator metrics and exports.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.evaluator import ModelEvaluator


def _make_eval_df(n_rows: int = 6) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n_rows, freq="5min")
    df = pd.DataFrame(
        {
            "RAW_Close": np.full(n_rows, 100.0),
            "RAW_Future_High": np.full(n_rows, 108.0),
            "RAW_Future_Low": np.full(n_rows, 92.0),
            "TARGET_Direction": np.array([0, 1, 2, 0, 1, 2]),
        },
        index=dates,
    )
    return df


def test_evaluate_fold_basic_metrics():
    df = _make_eval_df()
    y_true = np.array([0, 1, 2, 0, 1, 2])
    y_pred = np.array([0, 1, 2, 1, 1, 0])

    evaluator = ModelEvaluator(threshold=0.08)
    result = evaluator.evaluate_fold(y_true=y_true, y_pred=y_pred, df_test=df)

    assert "accuracy" in result
    assert result["confusion_matrix"].shape == (3, 3)
    assert "actual_moves" in result
    assert set(result["actual_moves"].columns).issuperset(
        {"Actual_Up_Pct", "Actual_Down_Pct", "Predicted"}
    )


def test_actual_move_calculation_threshold_hits():
    df = _make_eval_df()
    y_pred = np.array([1, 1, 2, 0, 2, 0])

    evaluator = ModelEvaluator(threshold=0.08)
    moves = evaluator._calculate_actual_moves(df, y_pred)

    assert np.isclose(moves["Actual_Up_Pct"].iloc[0], 0.08)
    assert np.isclose(moves["Actual_Down_Pct"].iloc[0], 0.08)
    assert moves["Hit_Threshold_Up"].all()
    assert moves["Hit_Threshold_Down"].all()


def test_evaluate_fold_requires_raw_columns():
    df = pd.DataFrame({"RAW_Close": [100.0]}, index=pd.date_range("2024-01-01", periods=1, freq="5min"))
    evaluator = ModelEvaluator()
    with pytest.raises(ValueError):
        evaluator.evaluate_fold(np.array([0]), np.array([0]), df)


def test_export_predictions_and_save_report(tmp_path):
    df = _make_eval_df()
    y_true = np.array([0, 1, 2, 0, 1, 2])
    y_pred = np.array([0, 1, 2, 0, 1, 2])
    evaluator = ModelEvaluator(threshold=0.08)
    fold = evaluator.evaluate_fold(y_true, y_pred, df)

    report = evaluator.generate_report([fold])
    report_path = tmp_path / "report.json"
    csv_path = tmp_path / "preds.csv"

    evaluator.save_report(report, str(report_path))
    evaluator.export_predictions([fold], str(csv_path))

    assert report_path.exists()
    assert csv_path.exists()
