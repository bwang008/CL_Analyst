"""
Generate prediction probabilities from a saved LGBM model.

This is a pure scoring utility — it does NOT train models.
For OOS training with a date cutoff, use experiment_runner.py --train-cutoff-date.

Modes:
  1. Default: Load saved model, score entire dataset.
  2. --oos-start-date: Load saved model, but only output predictions
                       AFTER the given date (filters output only).
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import src.util as util
from src.LGBMLearner import LGBMLearner


def _sigmoid(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    x = np.clip(x, -60, 60)
    return 1.0 / (1.0 + np.exp(-x))


def _to_probability(pred: np.ndarray) -> np.ndarray:
    p = np.asarray(pred, dtype=float).ravel()
    if np.nanmin(p) < 0.0 or np.nanmax(p) > 1.0:
        return _sigmoid(p)
    return p


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate prediction probabilities from a saved model. "
                    "For OOS training, use experiment_runner.py --train-cutoff-date."
    )
    parser.add_argument(
        "--model-path", required=True, help="Path to final_model.pkl"
    )
    parser.add_argument(
        "--data-path", required=True,
        help="Processed parquet/csv with features"
    )
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument(
        "--prob-col", default="prob_Buy",
        help="Probability column name (default: prob_Buy)"
    )
    parser.add_argument(
        "--oos-start-date", default=None,
        help="Only output predictions after this date (YYYY-MM-DD). "
             "Uses the saved model unchanged."
    )
    args = parser.parse_args()

    # Resolve paths via CL_DATA_ROOT fallback
    from src.data_paths import resolve_cli_path
    args.model_path = resolve_cli_path(args.model_path)
    args.data_path = resolve_cli_path(args.data_path)
    args.output = resolve_cli_path(args.output)
    # No model_config to resolve anymore

    # ---- Load data ----
    if args.data_path.endswith(".parquet"):
        df = pd.read_parquet(args.data_path)
    else:
        df = pd.read_csv(args.data_path, index_col=0, parse_dates=True)

    # Ensure DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        if "DateTime" in df.columns:
            df = df.set_index("DateTime")
        else:
            df.index = pd.to_datetime(df.index)

    feature_cols = util.get_feature_columns(df)
    print(f"Loaded {len(df):,} rows  |  {len(feature_cols)} features")
    print(f"Date range: {df.index.min()} to {df.index.max()}")

    # ---- Load model ----
    if not os.path.exists(args.model_path):
        raise FileNotFoundError(f"Model not found: {args.model_path}")

    learner = LGBMLearner()
    learner.load(args.model_path)

    # ---- Narrow features to model's training set ----
    if learner.feature_names is not None:
        model_feats = learner.feature_names
        available = [f for f in model_feats if f in feature_cols]
        dropped = set(feature_cols) - set(available)
        extra = set(model_feats) - set(feature_cols)
        if extra:
            raise ValueError(
                f"Dataset is missing {len(extra)} features the model expects: "
                f"{sorted(extra)[:10]}..."
            )
        if dropped:
            print(f"  [INFO] Dropping {len(dropped)} dataset features not in model")
        feature_cols = available
        print(f"  Using {len(feature_cols)} model features for scoring")

    # ---- Determine prediction range ----
    if args.oos_start_date:
        oos_start = pd.Timestamp(args.oos_start_date)
        score_df = df[df.index >= oos_start]
        print(f"\n=== OOS FILTER MODE: only scoring after {oos_start.date()} ===")
        print(f"Scoring {len(score_df):,} / {len(df):,} rows")
    else:
        score_df = df
        print(f"\n=== FULL DATASET MODE (original behavior) ===")

    X = score_df[feature_cols]
    raw_pred = learner.model.predict(X)
    probs = _to_probability(raw_pred)

    out = pd.DataFrame(index=score_df.index)
    out[args.prob_col] = probs
    out.to_csv(args.output)
    print(f"Saved {len(out):,} predictions to {args.output}")


if __name__ == "__main__":
    main()
