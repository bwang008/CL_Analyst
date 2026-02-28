"""
Generate prediction probabilities from a saved LGBM model.
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
    parser = argparse.ArgumentParser(description="Generate prediction probabilities from a saved model")
    parser.add_argument("--model-path", required=True, help="Path to final_model.pkl")
    parser.add_argument("--data-path", required=True, help="Processed parquet/csv with features")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--prob-col", default="prob_Buy", help="Probability column name")
    args = parser.parse_args()

    if not os.path.exists(args.model_path):
        raise FileNotFoundError(f"Model not found: {args.model_path}")

    if args.data_path.endswith(".parquet"):
        df = pd.read_parquet(args.data_path)
    else:
        df = pd.read_csv(args.data_path, index_col=0, parse_dates=True)

    feature_cols = util.get_feature_columns(df)
    X = df[feature_cols]

    learner = LGBMLearner()
    learner.load(args.model_path)
    # Use raw model prediction to preserve probabilities
    raw_pred = learner.model.predict(X)
    probs = _to_probability(raw_pred)

    out = pd.DataFrame(index=df.index)
    out[args.prob_col] = probs
    out.to_csv(args.output)
    print(f"Saved predictions to {args.output}")


if __name__ == "__main__":
    main()
