"""
Probability Threshold Sweep for CL_Analyst.

Trains the best model config (Triple Barrier 2x1 24h), gets probability
predictions on the vault set, and sweeps thresholds to find optimal
precision-recall tradeoff.

Usage:
    python agent/threshold_sweep.py
"""

import os
import sys
import json
import time
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import lightgbm as lgb
from src import util
from agent.experiment_runner import (
    load_experiment_log,
    generate_experiment_id,
    _append_to_log,
)


def run_threshold_sweep(
    data_path="data/processed/CL_set_05.parquet",
    target_name="TARGET_TRIPLE_2x1_24H_LONG",
    balance_mode="downsample",
    thresholds=None,
    model_params=None,
):
    """
    Train model on gym set, get probabilities on vault set,
    sweep thresholds to find optimal Buy confidence cutoff.
    """
    if thresholds is None:
        thresholds = np.arange(0.05, 0.95, 0.05).tolist()

    start_time = time.perf_counter()

    print("=" * 70)
    print("PROBABILITY THRESHOLD SWEEP")
    print(f"  Target: {target_name}")
    print(f"  Balance: {balance_mode}")
    print(f"  Thresholds: {len(thresholds)} values [{thresholds[0]:.2f} to {thresholds[-1]:.2f}]")
    print("=" * 70)

    # --- Step 1: Load data ---
    df = pd.read_parquet(data_path)
    feature_cols = util.get_feature_columns(df)
    target_col = util.get_target_column(df, target_name=target_name)
    print(f"Loaded {len(df):,} rows, {len(feature_cols)} features, target: {target_col}")

    # Drop rows where target is NaN
    df = df.dropna(subset=[target_col])

    # --- Step 2: Split gym/vault (same as train_and_evaluate) ---
    holdout_pct = 0.15
    n_total = len(df)
    n_vault = int(n_total * holdout_pct)
    n_gym = n_total - n_vault

    df_gym = df.iloc[:n_gym].copy()
    df_vault = df.iloc[n_gym:].copy()
    print(f"Gym: {len(df_gym):,} rows, Vault: {len(df_vault):,} rows")

    X_gym = df_gym[feature_cols]
    y_gym = df_gym[target_col].astype(int)
    X_vault = df_vault[feature_cols]
    y_vault = df_vault[target_col].astype(int)

    # --- Step 3: Apply class balancing ---
    if balance_mode == "downsample":
        # Downsample majority class to match minority
        from collections import Counter
        class_counts = Counter(y_gym)
        min_count = min(class_counts.values())
        print(f"Original class distribution: {dict(class_counts)}")
        print(f"Downsampling all classes to {min_count}")

        indices = []
        for cls in class_counts:
            cls_idx = np.where(y_gym.values == cls)[0]
            selected = np.random.RandomState(42).choice(cls_idx, size=min_count, replace=False)
            indices.extend(selected)
        indices = sorted(indices)
        X_gym = X_gym.iloc[indices]
        y_gym = y_gym.iloc[indices]
        print(f"After downsample: {Counter(y_gym)}")

    # --- Step 4: Train LightGBM ---
    default_params = {
        "objective": "multiclass",
        "num_class": len(y_gym.unique()),
        "metric": "multi_logloss",
        "boosting_type": "gbdt",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
        "n_estimators": 500,
        "min_child_samples": 100,
    }
    if model_params:
        default_params.update(model_params)

    n_est = default_params.pop("n_estimators", 500)

    print(f"\nTraining LightGBM ({n_est} rounds)...")
    train_data = lgb.Dataset(X_gym, y_gym)
    model = lgb.train(default_params, train_data, num_boost_round=n_est)

    # --- Step 5: Get probabilities on vault ---
    proba = model.predict(X_vault)  # shape: (n_samples, n_classes)
    print(f"Vault probabilities shape: {proba.shape}")

    # Determine class mapping: find which column index == Buy (class 1)
    classes = sorted(y_gym.unique())
    buy_class_idx = classes.index(1) if 1 in classes else None
    if buy_class_idx is None:
        print("[ERROR] Buy class (1) not found in training labels")
        return []

    buy_probs = proba[:, buy_class_idx]
    actual_buy = (y_vault.values == 1).astype(int)
    total_actual_buy = actual_buy.sum()

    print(f"Vault: {len(y_vault)} samples, {total_actual_buy} actual Buys ({total_actual_buy/len(y_vault)*100:.1f}%)")
    print(f"Buy probability stats: mean={buy_probs.mean():.4f}, median={np.median(buy_probs):.4f}, "
          f"max={buy_probs.max():.4f}, min={buy_probs.min():.4f}")

    # --- Step 6: Sweep thresholds ---
    print("\n" + "=" * 80)
    print(f"{'Threshold':>10} | {'Precision':>10} | {'Recall':>10} | {'F1':>10} | {'N_Buy':>8} | {'%_Pred':>8} | {'TP':>6} | {'FP':>6}")
    print("-" * 80)

    sweep_results = []
    best_f1 = 0
    best_threshold = 0

    for th in thresholds:
        pred_buy = (buy_probs >= th).astype(int)
        n_pred = pred_buy.sum()
        pct_pred = n_pred / len(y_vault)

        tp = ((pred_buy == 1) & (actual_buy == 1)).sum()
        fp = ((pred_buy == 1) & (actual_buy == 0)).sum()
        fn = ((pred_buy == 0) & (actual_buy == 1)).sum()

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

        sweep_results.append({
            "threshold": round(th, 3),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "n_predictions": int(n_pred),
            "pct_predictions": round(pct_pred, 4),
            "tp": int(tp),
            "fp": int(fp),
        })

        marker = " ***" if f1 > best_f1 else ""
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = th

        print(f"{th:>10.3f} | {precision:>10.4f} | {recall:>10.4f} | {f1:>10.4f} | {n_pred:>8} | {pct_pred:>8.1%} | {tp:>6} | {fp:>6}{marker}")

    print("-" * 80)
    print(f"\nBest threshold: {best_threshold:.3f} (F1={best_f1:.4f})")

    # Also find best precision at >50% recall
    high_recall_results = [r for r in sweep_results if r["recall"] >= 0.50 and r["n_predictions"] > 0]
    if high_recall_results:
        best_prec_at_recall = max(high_recall_results, key=lambda x: x["precision"])
        print(f"Best precision with recall>=50%: threshold={best_prec_at_recall['threshold']:.3f}, "
              f"prec={best_prec_at_recall['precision']:.4f}, rec={best_prec_at_recall['recall']:.4f}")

    # Find threshold with precision >= 60%
    high_prec_results = [r for r in sweep_results if r["precision"] >= 0.60 and r["n_predictions"] > 0]
    if high_prec_results:
        best_recall_at_prec = max(high_prec_results, key=lambda x: x["recall"])
        print(f"Best recall with precision>=60%: threshold={best_recall_at_prec['threshold']:.3f}, "
              f"prec={best_recall_at_prec['precision']:.4f}, rec={best_recall_at_prec['recall']:.4f}")

    print("=" * 80)

    elapsed = time.perf_counter() - start_time

    # --- Step 7: Save results ---
    sweep_path = os.path.join("reports", "threshold_sweep.json")
    sweep_data = {
        "target": target_name,
        "balance_mode": balance_mode,
        "n_vault_samples": len(y_vault),
        "n_actual_buy": int(total_actual_buy),
        "best_threshold": round(best_threshold, 3),
        "best_f1": round(best_f1, 4),
        "wall_time_seconds": round(elapsed, 1),
        "results": sweep_results,
    }
    with open(sweep_path, "w") as f:
        json.dump(sweep_data, f, indent=2)
    print(f"\nSaved sweep results to {sweep_path}")

    # --- Step 8: Log best result ---
    log = load_experiment_log()
    exp_id = generate_experiment_id(log)
    best_result = next(r for r in sweep_results if r["threshold"] == round(best_threshold, 3))

    experiment_record = {
        "id": exp_id,
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "strategy": "S3a",
        "hypothesis": f"Probability threshold sweep on Triple Barrier 2x1 24h (best threshold={best_threshold:.3f})",
        "changes": {
            "threshold_sweep": True,
            "best_prob_threshold": round(best_threshold, 3),
            "sweep_range": [round(thresholds[0], 2), round(thresholds[-1], 2)],
        },
        "config": {
            "data_path": data_path,
            "target_name": target_name,
            "method": "threshold_sweep",
            "balance_mode": balance_mode,
        },
        "metrics": {
            "signal_precision_buy": best_result["precision"],
            "signal_recall_buy": best_result["recall"],
            "signal_f1_buy": best_result["f1"],
            "optimal_threshold": round(best_threshold, 3),
            "n_predictions_at_optimal": best_result["n_predictions"],
            "pct_predictions_at_optimal": best_result["pct_predictions"],
            "wall_time_seconds": round(elapsed, 1),
        },
        "verdict": "promising" if best_result["f1"] > 0.10 else "no_improvement",
    }
    _append_to_log(experiment_record)
    print(f"Logged as {exp_id}")

    return sweep_results


if __name__ == "__main__":
    run_threshold_sweep()
