"""
Optuna Hyperparameter Search for CL_Analyst.

Optimizes LightGBM parameters using walk-forward cross-validation
with the best target configuration (Triple Barrier 2x1 24h).

Constraints (per implementation plan):
- num_leaves <= 31
- min_child_samples: 50-200
- Objective: average Buy F1 across all WF folds

Usage:
    python agent/optuna_lgbm_search.py
    python agent/optuna_lgbm_search.py --n-trials 50
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
import optuna
from sklearn.metrics import f1_score, precision_score, recall_score
from collections import Counter

from src import util
from agent.experiment_runner import (
    load_experiment_log,
    generate_experiment_id,
    _append_to_log,
)

optuna.logging.set_verbosity(optuna.logging.WARNING)


def walk_forward_folds(n_total, min_train=8640, fold_size=8640, purge=576):
    """Generate walk-forward expanding window fold indices."""
    folds = []
    test_start = min_train + purge
    while test_start + fold_size <= n_total:
        train_end = test_start - purge
        test_end = test_start + fold_size
        folds.append((0, train_end, test_start, test_end))
        test_start += fold_size
    return folds


def objective(trial, X, y, feature_cols, folds, balance_mode="downsample"):
    """Optuna objective: average Buy F1 across walk-forward folds."""

    params = {
        "objective": "multiclass",
        "num_class": len(np.unique(y)),
        "metric": "multi_logloss",
        "boosting_type": "gbdt",
        "verbose": -1,

        # Constrained search space
        "num_leaves": trial.suggest_int("num_leaves", 8, 31),
        "min_child_samples": trial.suggest_int("min_child_samples", 50, 200),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
        "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
        "bagging_freq": trial.suggest_int("bagging_freq", 1, 10),
        "reg_alpha": trial.suggest_float("reg_alpha", 1e-8, 10.0, log=True),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-8, 10.0, log=True),
        "max_depth": trial.suggest_int("max_depth", 3, 8),
        "min_gain_to_split": trial.suggest_float("min_gain_to_split", 0.0, 1.0),
    }

    n_estimators = trial.suggest_int("n_estimators", 200, 1000, step=100)

    # Sample a subset of folds for speed (use every 10th fold)
    sampled_folds = folds[::10] if len(folds) > 10 else folds
    if len(sampled_folds) < 3:
        sampled_folds = folds[:5] if len(folds) >= 5 else folds

    fold_f1s = []
    for fold_idx, (train_start, train_end, test_start, test_end) in enumerate(sampled_folds):
        X_train = X.iloc[train_start:train_end]
        y_train = y.iloc[train_start:train_end]
        X_test = X.iloc[test_start:test_end]
        y_test = y.iloc[test_start:test_end]

        # Apply downsampling to training set
        if balance_mode == "downsample":
            class_counts = Counter(y_train)
            min_count = min(class_counts.values())
            if min_count > 0:
                indices = []
                for cls in class_counts:
                    cls_idx = np.where(y_train.values == cls)[0]
                    selected = np.random.RandomState(42 + fold_idx).choice(
                        cls_idx, size=min_count, replace=False
                    )
                    indices.extend(selected)
                indices = sorted(indices)
                X_train = X_train.iloc[indices]
                y_train = y_train.iloc[indices]

        train_data = lgb.Dataset(X_train, y_train)
        model = lgb.train(params, train_data, num_boost_round=n_estimators)

        # Predict with probability threshold at 0.45 (best from sweep)
        proba = model.predict(X_test)
        classes = sorted(np.unique(y))
        buy_idx = classes.index(1) if 1 in classes else None
        if buy_idx is None:
            continue

        buy_probs = proba[:, buy_idx]
        preds = np.zeros(len(y_test), dtype=int)
        preds[buy_probs >= 0.45] = 1

        # Calculate Buy F1
        actual_buy = (y_test.values == 1).astype(int)
        pred_buy = (preds == 1).astype(int)

        tp = ((pred_buy == 1) & (actual_buy == 1)).sum()
        fp = ((pred_buy == 1) & (actual_buy == 0)).sum()
        fn = ((pred_buy == 0) & (actual_buy == 1)).sum()

        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        fold_f1s.append(f1)

    if not fold_f1s:
        return 0.0

    return np.mean(fold_f1s)


def run_optuna_search(
    data_path="data/processed/CL_set_05.parquet",
    target_name="TARGET_TRIPLE_2x1_24H_LONG",
    balance_mode="downsample",
    n_trials=30,
):
    """Run Optuna search and log the best result."""
    start_time = time.perf_counter()

    print("=" * 70)
    print("OPTUNA HYPERPARAMETER SEARCH")
    print(f"  Target: {target_name}")
    print(f"  Balance: {balance_mode}")
    print(f"  Trials: {n_trials}")
    print("=" * 70)

    # Load data
    df = pd.read_parquet(data_path)
    feature_cols = util.get_feature_columns(df)
    target_col = util.get_target_column(df, target_name=target_name)
    df = df.dropna(subset=[target_col])

    # Use only gym set (85%)
    n_vault = int(len(df) * 0.15)
    df_gym = df.iloc[:len(df) - n_vault]

    X = df_gym[feature_cols]
    y = df_gym[target_col].astype(int)

    print(f"Gym set: {len(df_gym):,} rows, {len(feature_cols)} features")

    # Generate WF folds
    folds = walk_forward_folds(len(df_gym))
    print(f"Walk-forward folds: {len(folds)} total")

    # Run Optuna
    study = optuna.create_study(direction="maximize")
    study.optimize(
        lambda trial: objective(trial, X, y, feature_cols, folds, balance_mode),
        n_trials=n_trials,
        show_progress_bar=True,
    )

    best_params = study.best_params
    best_score = study.best_value
    n_estimators = best_params.pop("n_estimators", 500)

    print(f"\nBest trial score (avg Buy F1): {best_score:.4f}")
    print(f"Best parameters:")
    for k, v in best_params.items():
        print(f"  {k}: {v}")
    print(f"  n_estimators: {n_estimators}")

    # --- Evaluate best params on vault set ---
    print("\n" + "=" * 70)
    print("EVALUATING BEST PARAMS ON VAULT SET")
    print("=" * 70)

    df_vault = df.iloc[len(df) - n_vault:]
    X_vault = df_vault[feature_cols]
    y_vault = df_vault[target_col].astype(int)

    # Train on full gym with best params + downsampling
    class_counts = Counter(y)
    min_count = min(class_counts.values())
    indices = []
    for cls in class_counts:
        cls_idx = np.where(y.values == cls)[0]
        selected = np.random.RandomState(42).choice(cls_idx, size=min_count, replace=False)
        indices.extend(selected)
    X_train = X.iloc[sorted(indices)]
    y_train = y.iloc[sorted(indices)]

    lgb_params = {
        "objective": "multiclass",
        "num_class": len(y.unique()),
        "metric": "multi_logloss",
        "boosting_type": "gbdt",
        "verbose": -1,
    }
    lgb_params.update({k: v for k, v in best_params.items()})

    train_data = lgb.Dataset(X_train, y_train)
    model = lgb.train(lgb_params, train_data, num_boost_round=n_estimators)

    proba = model.predict(X_vault)
    classes = sorted(y.unique())
    buy_idx = classes.index(1)

    buy_probs = proba[:, buy_idx]
    actual_buy = (y_vault.values == 1).astype(int)

    # Evaluate at multiple thresholds
    for th in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
        pred = (buy_probs >= th).astype(int)
        tp = ((pred == 1) & (actual_buy == 1)).sum()
        fp = ((pred == 1) & (actual_buy == 0)).sum()
        fn = ((pred == 0) & (actual_buy == 1)).sum()
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        print(f"  th={th:.2f}: prec={prec:.4f} rec={rec:.4f} f1={f1:.4f} n_pred={pred.sum()}")

    # Use threshold 0.45 for logging (best from sweep)
    pred_045 = (buy_probs >= 0.45).astype(int)
    tp = ((pred_045 == 1) & (actual_buy == 1)).sum()
    fp = ((pred_045 == 1) & (actual_buy == 0)).sum()
    fn = ((pred_045 == 0) & (actual_buy == 1)).sum()
    vault_prec = tp / (tp + fp) if (tp + fp) > 0 else 0
    vault_rec = tp / (tp + fn) if (tp + fn) > 0 else 0
    vault_f1 = 2 * vault_prec * vault_rec / (vault_prec + vault_rec) if (vault_prec + vault_rec) > 0 else 0

    elapsed = time.perf_counter() - start_time

    print(f"\nVault results at 0.45 threshold:")
    print(f"  Precision: {vault_prec:.4f}")
    print(f"  Recall: {vault_rec:.4f}")
    print(f"  F1: {vault_f1:.4f}")
    print(f"  Wall time: {elapsed:.1f}s")

    # Log result
    log = load_experiment_log()
    exp_id = generate_experiment_id(log)

    experiment_record = {
        "id": exp_id,
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "strategy": "S3b",
        "hypothesis": "Optuna hyperparameter search with constrained space improves Buy F1 on Triple Barrier target",
        "changes": {
            "optuna_search": True,
            "n_trials": n_trials,
            "best_params": {**best_params, "n_estimators": n_estimators},
        },
        "config": {
            "data_path": data_path,
            "target_name": target_name,
            "method": "optuna_search",
            "balance_mode": balance_mode,
        },
        "metrics": {
            "optuna_best_cv_f1": round(best_score, 4),
            "signal_precision_buy": round(vault_prec, 4),
            "signal_recall_buy": round(vault_rec, 4),
            "signal_f1_buy": round(vault_f1, 4),
            "prob_threshold_used": 0.45,
            "wall_time_seconds": round(elapsed, 1),
        },
        "verdict": "promising" if vault_f1 > 0.50 else ("improvement" if vault_f1 > 0.30 else "no_improvement"),
    }
    _append_to_log(experiment_record)
    print(f"\nLogged as {exp_id}")

    # Save best params
    params_path = os.path.join("reports", "optuna_best_params.json")
    with open(params_path, "w") as f:
        json.dump({
            "best_params": {**best_params, "n_estimators": n_estimators},
            "best_cv_f1": round(best_score, 4),
            "vault_f1": round(vault_f1, 4),
            "vault_precision": round(vault_prec, 4),
            "vault_recall": round(vault_rec, 4),
        }, f, indent=2)
    print(f"Saved best params to {params_path}")

    return best_params, best_score


if __name__ == "__main__":
    n_trials = 30
    if "--n-trials" in sys.argv:
        idx = sys.argv.index("--n-trials")
        if idx + 1 < len(sys.argv):
            n_trials = int(sys.argv[idx + 1])

    run_optuna_search(n_trials=n_trials)
