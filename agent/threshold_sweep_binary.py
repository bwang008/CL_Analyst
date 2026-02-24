"""
Binary probability threshold sweep for CL_Analyst.

This is used for binary targets like:
- TARGET_TRIPLE_2x1_24H_LONG
- TARGET_TRIPLE_2x1_24H_SHORT

It mirrors the pipeline's (current) nested holdout behavior:
1) Outer holdout: df -> outer_gym (85%), outer_holdout (15%)  [outer holdout is ignored here]
2) Inner holdout: outer_gym -> inner_gym (85%), inner_vault (15%)

The sweep is performed on inner_vault to match `reports/vault_metrics.json`/`vault_predictions.csv`.

Important:
- When using custom focal objective in LightGBM, Booster.predict() may output logits.
  We detect that and apply sigmoid to recover probabilities.

Usage:
    python agent/threshold_sweep_binary.py --target TARGET_TRIPLE_2x1_24H_SHORT
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from src import util
from src.LGBMLearner import LGBMLearner
from agent.experiment_runner import load_experiment_log, generate_experiment_id, _append_to_log


def _sigmoid(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    x = np.clip(x, -60, 60)  # numerical safety
    return 1.0 / (1.0 + np.exp(-x))


def _to_probability(pred: np.ndarray) -> np.ndarray:
    """
    LightGBM predict() returns probability for built-in binary objective, but can return logits
    for custom objectives. If values fall outside [0,1], assume logits and apply sigmoid.
    """
    p = np.asarray(pred, dtype=float).ravel()
    if np.nanmin(p) < 0.0 or np.nanmax(p) > 1.0:
        return _sigmoid(p)
    return p


def run_threshold_sweep_binary(
    data_path: str,
    target_name: str,
    balance_mode: str = "downsample",
    thresholds: list[float] | None = None,
    model_params: dict | None = None,
    prob_col_name: str | None = None,
    output_json: str | None = None,
    output_predictions_csv: str | None = None,
) -> dict:
    if thresholds is None:
        thresholds = np.arange(0.05, 0.95, 0.05).tolist()

    start_time = time.perf_counter()

    df = pd.read_parquet(data_path)
    feature_cols = util.get_feature_columns(df)
    target_col = util.get_target_column(df, target_name=target_name)
    df = df.dropna(subset=[target_col]).copy()

    n_total = len(df)
    n_outer_holdout = int(n_total * 0.15)
    outer_gym = df.iloc[: n_total - n_outer_holdout].copy()

    n_inner = len(outer_gym)
    n_inner_vault = int(n_inner * 0.15)
    inner_gym = outer_gym.iloc[: n_inner - n_inner_vault].copy()
    inner_vault = outer_gym.iloc[n_inner - n_inner_vault :].copy()

    X_gym = inner_gym[feature_cols]
    y_gym = inner_gym[target_col].astype(int)
    X_vault = inner_vault[feature_cols]
    y_vault = inner_vault[target_col].astype(int)

    if balance_mode == "downsample":
        X_gym, y_gym = util.downsample_majority(X_gym, y_gym, random_state=42)

    if model_params is None:
        model_params = {
            "num_leaves": 31,
            "min_child_samples": 166,
            "learning_rate": 0.05242702195760322,
            "feature_fraction": 0.6940065346564026,
            "bagging_fraction": 0.6483459770074159,
            "bagging_freq": 1,
            "reg_alpha": 2.737488884954343,
            "reg_lambda": 7.378557513409711,
            "max_depth": 4,
            "min_gain_to_split": 0.9901794009928347,
            "n_estimators": 1000,
            "objective": "binary",
            "use_focal": True,
            "metric": "binary_logloss",
            "class_weight": None,
        }

    if prob_col_name is None:
        prob_col_name = "prob_Sell" if target_name.endswith("_SHORT") else "prob_Buy"

    if output_json is None:
        output_json = os.path.join("reports", "threshold_sweep_binary.json")
    if output_predictions_csv is None:
        output_predictions_csv = os.path.join("reports", "threshold_sweep_binary_predictions.csv")

    print("=" * 70)
    print("BINARY PROBABILITY THRESHOLD SWEEP")
    print(f"  Data:   {data_path}")
    print(f"  Target: {target_name}")
    print(f"  Balance: {balance_mode}")
    print(f"  Inner vault samples: {len(y_vault):,}")
    print(f"  Thresholds: {len(thresholds)} values [{thresholds[0]:.2f} to {thresholds[-1]:.2f}]")
    print("=" * 70)

    model = LGBMLearner(**model_params)
    model.add_evidence(X_gym, y_gym)

    raw_pred = model.model.predict(X_vault)
    probs = _to_probability(raw_pred)

    actual_pos = (y_vault.values == 1).astype(int)
    total_actual_pos = int(actual_pos.sum())

    print(
        f"Vault positives: {total_actual_pos:,} / {len(actual_pos):,} "
        f"({(total_actual_pos/len(actual_pos)):.1%})"
    )
    print(
        f"Score stats: raw[min={float(np.min(np.asarray(raw_pred))):.4f}, "
        f"max={float(np.max(np.asarray(raw_pred))):.4f}] -> "
        f"prob[min={float(probs.min()):.4f}, max={float(probs.max()):.4f}]"
    )

    best_f1 = -1.0
    best_threshold = thresholds[0]
    sweep_rows: list[dict] = []

    for th in thresholds:
        pred_pos = (probs >= th).astype(int)
        tp = int(((pred_pos == 1) & (actual_pos == 1)).sum())
        fp = int(((pred_pos == 1) & (actual_pos == 0)).sum())
        fn = int(((pred_pos == 0) & (actual_pos == 1)).sum())

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        n_pred = int(pred_pos.sum())
        sweep_rows.append(
            {
                "threshold": round(float(th), 3),
                "precision": round(float(precision), 4),
                "recall": round(float(recall), 4),
                "f1": round(float(f1), 4),
                "n_predictions": n_pred,
                "pct_predictions": round(float(n_pred / len(actual_pos)), 4),
                "tp": tp,
                "fp": fp,
            }
        )

        if f1 > best_f1:
            best_f1 = f1
            best_threshold = th

    elapsed = time.perf_counter() - start_time

    sweep_data = {
        "target": target_name,
        "data_path": data_path,
        "balance_mode": balance_mode,
        "n_vault_samples": int(len(actual_pos)),
        "n_actual_positive": int(total_actual_pos),
        "prob_col_name": prob_col_name,
        "best_threshold": round(float(best_threshold), 3),
        "best_f1": round(float(best_f1), 4),
        "wall_time_seconds": round(float(elapsed), 1),
        "results": sweep_rows,
    }

    os.makedirs(os.path.dirname(output_json) or ".", exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(sweep_data, f, indent=2)
    print(f"Saved sweep JSON to {output_json}")

    # Export probabilities for backtesting (inner vault window)
    pred_out = pd.DataFrame(index=inner_vault.index)
    pred_out[prob_col_name] = probs
    pred_out.to_csv(output_predictions_csv)
    print(f"Saved predictions CSV to {output_predictions_csv}")

    # Log as an experiment record (S3a style)
    log = load_experiment_log()
    exp_id = generate_experiment_id(log)
    best_row = next(r for r in sweep_rows if r["threshold"] == round(float(best_threshold), 3))
    _append_to_log(
        {
            "id": exp_id,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "strategy": "S3a",
            "hypothesis": f"Binary probability threshold sweep ({target_name}) best={best_threshold:.3f}",
            "changes": {
                "threshold_sweep": True,
                "best_prob_threshold": round(float(best_threshold), 3),
                "sweep_range": [round(float(thresholds[0]), 2), round(float(thresholds[-1]), 2)],
            },
            "config": {
                "data_path": data_path,
                "target_name": target_name,
                "method": "threshold_sweep_binary",
                "balance_mode": balance_mode,
            },
            "metrics": {
                "signal_precision_pos": best_row["precision"],
                "signal_recall_pos": best_row["recall"],
                "signal_f1_pos": best_row["f1"],
                "optimal_threshold": best_row["threshold"],
                "n_predictions_at_optimal": best_row["n_predictions"],
                "pct_predictions_at_optimal": best_row["pct_predictions"],
                "wall_time_seconds": round(float(elapsed), 1),
            },
            "verdict": "promising" if best_row["f1"] > 0.10 else "no_improvement",
        }
    )
    print(f"Logged sweep as {exp_id}")

    return sweep_data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-path", default="data/processed/CL_set_06_shortfix.parquet")
    parser.add_argument("--target", default="TARGET_TRIPLE_2x1_24H_SHORT")
    parser.add_argument("--balance-mode", default="downsample")
    parser.add_argument("--output-json", default="reports/threshold_sweep_short.json")
    parser.add_argument("--output-predictions", default="reports/short_sniper_predictions.csv")
    args = parser.parse_args()

    run_threshold_sweep_binary(
        data_path=args.data_path,
        target_name=args.target,
        balance_mode=args.balance_mode,
        output_json=args.output_json,
        output_predictions_csv=args.output_predictions,
    )


if __name__ == "__main__":
    main()

