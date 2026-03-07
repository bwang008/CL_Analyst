"""
Generate prediction probabilities from a saved LGBM model.

Modes:
  1. Default (original): Load saved model, score entire dataset.
  2. --oos-start-date:    Load saved model, but only output predictions
                          AFTER the given date (filters output only; model
                          was still trained on earlier data).
  3. --train-cutoff:      Retrain a fresh model on data BEFORE cutoff,
                          then predict ONLY on data AFTER cutoff.
                          Requires --model-config (registry config.json)
                          to read hyperparameters & target.
                          Produces true out-of-sample predictions.
"""

from __future__ import annotations

import argparse
import json
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
        description="Generate prediction probabilities from a saved model"
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
    # ---- NEW: OOS filtering ----
    parser.add_argument(
        "--oos-start-date", default=None,
        help="Only output predictions after this date (YYYY-MM-DD). "
             "Uses the saved model unchanged."
    )
    # ---- NEW: True OOS via retrain ----
    parser.add_argument(
        "--train-cutoff", default=None,
        help="Train a fresh model on data before this date (YYYY-MM-DD) "
             "and predict only on data after it. Requires --model-config."
    )
    parser.add_argument(
        "--model-config", default=None,
        help="Path to registry config.json (for --train-cutoff mode). "
             "Reads target_name and model_params from it."
    )
    args = parser.parse_args()

    # Resolve paths via CL_DATA_ROOT fallback
    from src.data_paths import resolve_cli_path
    args.model_path = resolve_cli_path(args.model_path)
    args.data_path = resolve_cli_path(args.data_path)
    args.output = resolve_cli_path(args.output)
    if args.model_config:
        args.model_config = resolve_cli_path(args.model_config)

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

    # =========================================================================
    # Mode 3: True OOS via retrain
    # =========================================================================
    if args.train_cutoff:
        cutoff = pd.Timestamp(args.train_cutoff)
        print(f"\n=== RETRAIN MODE: cutoff = {cutoff.date()} ===")

        if not args.model_config:
            # Try to infer config path from model-path
            model_dir = os.path.dirname(args.model_path)
            cfg_path = os.path.join(model_dir, "config.json")
            if os.path.exists(cfg_path):
                args.model_config = cfg_path
                print(f"Auto-detected config: {cfg_path}")
            else:
                raise ValueError(
                    "--train-cutoff requires --model-config (registry config.json) "
                    "or config.json in the same directory as --model-path"
                )

        with open(args.model_config, "r", encoding="utf-8") as f:
            config = json.load(f)

        target_name = config["target_name"]
        model_params = dict(config["model_params"])
        balance_mode = config.get("source_experiment_record", {}).get(
            "changes", {}
        ).get("balance_mode", "downsample")

        print(f"Target: {target_name}")
        print(f"Balance: {balance_mode}")
        print(f"Params: num_leaves={model_params.get('num_leaves')}, "
              f"n_estimators={model_params.get('n_estimators')}, "
              f"lr={model_params.get('learning_rate')}")

        # Split at cutoff
        train_df = df[df.index < cutoff].copy()
        test_df = df[df.index >= cutoff].copy()
        print(f"\nTrain: {len(train_df):,} rows  "
              f"({train_df.index.min().date()} to {train_df.index.max().date()})")
        print(f"Test:  {len(test_df):,} rows  "
              f"({test_df.index.min().date()} to {test_df.index.max().date()})")

        if len(test_df) == 0:
            raise ValueError(f"No data after cutoff {cutoff.date()}")

        X_train, y_train = util.get_X_y(train_df, target_name=target_name)
        X_test = test_df[feature_cols]

        # Drop NaN targets
        if y_train.isna().any():
            mask = ~y_train.isna()
            X_train = X_train.loc[mask]
            y_train = y_train.loc[mask]
            print(f"Dropped {(~mask).sum():,} NaN target rows from train")

        # Downsample if needed
        if balance_mode == "downsample":
            n_before = len(X_train)
            X_train, y_train = util.downsample_majority(
                X_train, y_train, random_state=42
            )
            print(f"Downsampled: {n_before:,} -> {len(X_train):,} rows")

        # Train fresh model
        print("\nTraining fresh model...")
        model = LGBMLearner(**model_params)
        model.add_evidence(X_train, y_train)
        print("Training complete.")

        # Predict on test only
        raw_pred = model.model.predict(X_test)
        probs = _to_probability(raw_pred)

        out = pd.DataFrame(index=test_df.index)
        out[args.prob_col] = probs
        out.to_csv(args.output)

        # Summary stats
        print(f"\n=== OOS PREDICTION SUMMARY ===")
        print(f"Rows: {len(out):,}")
        print(f"Mean prob: {probs.mean():.4f}")
        print(f"Std:       {probs.std():.4f}")
        for t in [0.50, 0.60, 0.70, 0.80]:
            n = (probs >= t).sum()
            print(f"  >= {t}: {n:>7,} signals ({n/len(probs)*100:.1f}%)")
        print(f"\nSaved to {args.output}")
        return

    # =========================================================================
    # Mode 1 & 2: Use saved model
    # =========================================================================
    if not os.path.exists(args.model_path):
        raise FileNotFoundError(f"Model not found: {args.model_path}")

    learner = LGBMLearner()
    learner.load(args.model_path)

    # Determine prediction range
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
