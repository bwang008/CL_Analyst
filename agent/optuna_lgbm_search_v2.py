"""
Optuna LightGBM Hyperparameter Search v2 — Walk-Forward Bake-Off.

E2E Alpha Factory Edition:
- Supports three ML-metric modes via --ml-metric for bake-off comparison:
    logloss           — average binary_logloss – best-calibrated probabilities
    f0.5              — average F-Beta(0.5) – emphasizes precision over recall
    average_precision — PR-AUC – rewards ranking highly confident positives
- Also supports sharpe (requires --strategy-config).
- Binary classification with focal loss (consistent with production).
- Configurable LGB num_threads via --num-threads (default: 8, auto-set by shell script).
- Wider search ranges with boosting_type (gbdt/goss) and path_smooth.
- Persists study to SQLite for visualization and resume.
- Does NOT touch the final OOS holdout (2022-2026). That set is reserved
  for a single untouched evaluation after the search completes.

Architecture (Quant Desk Standard):
  Phase 1 (this script): Optuna finds best LightGBM hyperparams via sampled
           WF folds. The "Brain" optimization.
  Phase 2 (strategy_optimizer.py): Threshold/strategy sweep on vault set
           or last gym slice. The "Trigger" optimization.
  Phase 3 (experiment_runner.py or vm_e2e_pipeline.py): Train final model on
           all pre-cutoff data, one-shot evaluation on untouched OOS.

Usage:
    # Run the logloss bake-off (recommended, 12 workers on 96-core VM)
    python agent/optuna_lgbm_search_v2.py \\
        --target TARGET_TRIPLE_2x1_24H_LONG \\
        --data /home/bwang/data/CL_set_09.parquet \\
        --ml-metric logloss --n-trials 200 --n-jobs 12

    # Full bake-off: run all three metrics and compare
    python agent/optuna_lgbm_search_v2.py --ml-metric logloss --n-trials 200 ...
    python agent/optuna_lgbm_search_v2.py --ml-metric f0.5 --n-trials 200 ...
    python agent/optuna_lgbm_search_v2.py --ml-metric average_precision --n-trials 200 ...

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import sys
import time
import gc
import threading
from collections import Counter
from pathlib import Path
from typing import Optional

# CPU count for dynamic thread allocation
_CPU_COUNT = os.cpu_count() or 4

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import lightgbm as lgb
import optuna
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    fbeta_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)

import src.util as util


# ---------------------------------------------------------------------------
# Memory diagnostics
# ---------------------------------------------------------------------------

def _get_rss_gb() -> float:
    """Get current process RSS in GB. Uses /proc on Linux, psutil fallback."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / (1024 * 1024)  # kB → GB
    except FileNotFoundError:
        pass
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024**3)
    except ImportError:
        return -1.0

_peak_rss_gb = 0.0
_rss_lock = threading.Lock()
_worker_id = 0  # Set by --worker-id CLI arg
_total_trials = 0  # Set by main() for status file writes

def _wprint(*args, **kwargs):
    """Print with [W{id}] prefix for multi-process log disambiguation."""
    prefix = f"[W{_worker_id}]" if _worker_id > 0 else ""
    msg = " ".join(str(a) for a in args)
    if prefix:
        msg = f"{prefix} {msg}"
    print(msg, **kwargs)

def _log_mem(label: str, **kwargs) -> None:
    """Log RSS with a label. Thread-safe peak tracking."""
    global _peak_rss_gb
    rss = _get_rss_gb()
    with _rss_lock:
        if rss > _peak_rss_gb:
            _peak_rss_gb = rss
        peak = _peak_rss_gb
    parts = " ".join(f"{k}={v}" for k, v in kwargs.items())
    tid = threading.current_thread().name
    wtag = f"[W{_worker_id}] " if _worker_id > 0 else ""
    print(f"{wtag}[MEM] {label} RSS={rss:.2f}GB peak={peak:.2f}GB thread={tid} {parts}", flush=True)

def _write_worker_status(trials_done: int, total_trials: int, status: str = "running", best_score: float = None, target: str = "", metric: str = "") -> None:
    """Write per-worker status file for monitor aggregation."""
    if _worker_id <= 0:
        return
    import json as _json
    status_path = f"/tmp/worker_W{_worker_id}_status.json"
    try:
        data = {"trials_done": trials_done, "total_trials": total_trials, "status": status, "pid": os.getpid()}
        if best_score is not None:
            data["best_score"] = best_score
        if target:
            data["target"] = target
        if metric:
            data["metric"] = metric
        with open(status_path, "w") as f:
            _json.dump(data, f)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Experiment log helpers (inlined to avoid heavy experiment_runner imports)
# ---------------------------------------------------------------------------

_EXPERIMENT_LOG_PATH = os.path.join(PROJECT_ROOT, "agent", "experiment_log.json")


def load_experiment_log():
    """Load the experiment log, or create a fresh one."""
    if os.path.exists(_EXPERIMENT_LOG_PATH):
        with open(_EXPERIMENT_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"experiments": []}


def generate_experiment_id(log_data):
    """Generate the next experiment ID."""
    existing = log_data.get("experiments", [])
    return f"EXP-{len(existing) + 1:03d}"


def _append_to_log(record):
    """Append an experiment record to the log file."""
    log_data = load_experiment_log()
    log_data["experiments"].append(record)
    os.makedirs(os.path.dirname(_EXPERIMENT_LOG_PATH), exist_ok=True)
    with open(_EXPERIMENT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, default=str)

optuna.logging.set_verbosity(optuna.logging.WARNING)


# ---------------------------------------------------------------------------
# Walk-forward fold generation
# ---------------------------------------------------------------------------


def walk_forward_folds(
    n_total: int,
    min_train: int = 8640,
    fold_size: int = 8640,
    purge: int = 576,
) -> list[tuple[int, int, int, int]]:
    """Generate walk-forward expanding-window fold indices.

    Returns list of (train_start, train_end, test_start, test_end).
    """
    folds = []
    test_start = min_train + purge
    while test_start + fold_size <= n_total:
        train_end = test_start - purge
        test_end = test_start + fold_size
        folds.append((0, train_end, test_start, test_end))
        test_start += fold_size
    return folds


def sample_folds(
    folds: list[tuple],
    max_folds: int = 10,
    sample_step: int = 10,
) -> list[tuple]:
    """Sample folds for speed: every Nth fold, minimum 3."""
    sampled = folds[::sample_step] if len(folds) > sample_step else folds
    if len(sampled) < 3:
        sampled = folds[:5] if len(folds) >= 5 else folds
    if len(sampled) > max_folds:
        # Evenly space if too many
        step = max(1, len(sampled) // max_folds)
        sampled = sampled[::step][:max_folds]
    return sampled


# ---------------------------------------------------------------------------
# Focal loss objective
# ---------------------------------------------------------------------------

FOCAL_GAMMA = 2.0


def _sigmoid(x: np.ndarray) -> np.ndarray:
    x = np.clip(np.asarray(x, dtype=float), -60, 60)
    return 1.0 / (1.0 + np.exp(-x))


def focal_obj(preds: np.ndarray, train_set: lgb.Dataset):
    """Focal loss gradient/hessian for binary classification."""
    labels = train_set.get_label().astype(int)
    p = _sigmoid(preds)
    p_t = np.where(labels == 1, p, 1 - p)
    grad = (p - labels) * ((1 - p_t) ** FOCAL_GAMMA)
    hess = (p * (1 - p)) * ((1 - p_t) ** FOCAL_GAMMA)
    return grad, hess


def focal_eval(preds: np.ndarray, val_set: lgb.Dataset):
    """Focal loss eval metric for early stopping (matches focal_obj).

    Returns (name, value, is_higher_better) for LightGBM custom eval.
    Lower focal loss = better, so is_higher_better=False.
    """
    labels = val_set.get_label().astype(int)
    p = _sigmoid(preds)
    p_t = np.where(labels == 1, p, 1 - p)
    # Focal loss: -alpha_t * (1 - p_t)^gamma * log(p_t)
    loss = -((1 - p_t) ** FOCAL_GAMMA) * np.log(np.clip(p_t, 1e-7, 1.0))
    return "focal_loss", float(np.mean(loss)), False


# ---------------------------------------------------------------------------
# Asymmetric loss objective (Experiment 1: Hyper-Conservative FP penalty)
# ---------------------------------------------------------------------------

ASYMMETRIC_FP_WEIGHT = 5.0  # False Positive penalty multiplier


def asymmetric_obj(preds: np.ndarray, train_set: lgb.Dataset):
    """Asymmetric binary cross-entropy that penalizes False Positives 5× more.

    When truth=0 but model predicts 1 (FP), the gradient is scaled by
    fp_weight, mathematically forcing the model toward higher precision.
    """
    labels = train_set.get_label().astype(int)
    p = _sigmoid(preds)
    w = np.where(labels == 0, ASYMMETRIC_FP_WEIGHT, 1.0)
    grad = w * (p - labels)
    hess = w * p * (1 - p)
    return grad, hess


def asymmetric_eval(preds: np.ndarray, val_set: lgb.Dataset):
    """Asymmetric loss eval metric for early stopping (matches asymmetric_obj).

    Returns (name, value, is_higher_better) for LightGBM custom eval.
    Lower = better, so is_higher_better=False.
    """
    labels = val_set.get_label().astype(int)
    p = _sigmoid(preds)
    w = np.where(labels == 0, ASYMMETRIC_FP_WEIGHT, 1.0)
    # Weighted binary cross-entropy
    loss = -w * (labels * np.log(np.clip(p, 1e-7, 1.0))
                 + (1 - labels) * np.log(np.clip(1 - p, 1e-7, 1.0)))
    return "asymmetric_loss", float(np.mean(loss)), False


# Objective registry — maps CLI name to (obj_fn, eval_fn) pair
OBJECTIVE_REGISTRY = {
    "focal": (focal_obj, focal_eval),
    "asymmetric": (asymmetric_obj, asymmetric_eval),
}


# ---------------------------------------------------------------------------
# Sharpe helper
# ---------------------------------------------------------------------------


def compute_sharpe(equity_curve: list[float], bars_per_year: int = 105120) -> float:
    """Annualized Sharpe ratio from bar-by-bar equity curve."""
    if len(equity_curve) < 2:
        return 0.0
    returns = np.diff(equity_curve)
    std = np.std(returns)
    if std == 0:
        return 0.0
    return float(np.mean(returns) / std * np.sqrt(bars_per_year))


# ---------------------------------------------------------------------------
# Per-fold evaluation functions
# ---------------------------------------------------------------------------


def evaluate_fold_f1(y_true: np.ndarray, probs: np.ndarray) -> float:
    """F1 score for binary Buy signal."""
    preds = (probs >= 0.5).astype(int)
    return float(f1_score(y_true, preds, zero_division=0))


def evaluate_fold_fbeta05(y_true: np.ndarray, probs: np.ndarray) -> float:
    """F-Beta(0.5) — emphasizes precision over recall."""
    preds = (probs >= 0.5).astype(int)
    return float(fbeta_score(y_true, preds, beta=0.5, zero_division=0))


def evaluate_fold_logloss(y_true: np.ndarray, probs: np.ndarray) -> float:
    """Negative binary log-loss (higher is better for Optuna maximize)."""
    # Clip to avoid log(0)
    probs_clipped = np.clip(probs, 1e-7, 1 - 1e-7)
    return -float(log_loss(y_true, probs_clipped))


def evaluate_fold_avg_precision(y_true: np.ndarray, probs: np.ndarray) -> float:
    """Average Precision (PR-AUC) — rewards ranking highly confident positive signals.

    Higher is better (directly compatible with Optuna maximize direction).
    """
    if len(np.unique(y_true)) < 2:
        return 0.0  # Skip degenerate folds with single class
    return float(average_precision_score(y_true, probs))


def evaluate_fold_sharpe(
    probs: np.ndarray,
    fold_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    strategy_cfg: dict,
    prob_col: str,
    ohlcv_exec_df: pd.DataFrame | None = None,
) -> float:
    """Run BacktestEngine on the fold's validation predictions → Sharpe."""
    from agent.backtest_engine import BacktestEngine

    # Build signals
    signals = pd.DataFrame(index=fold_df.index)
    signals[prob_col] = probs

    # Slice OHLCV to the fold period
    fold_start = fold_df.index.min()
    fold_end = fold_df.index.max()
    ohlcv_slice = ohlcv_df[(ohlcv_df.index >= fold_start) & (ohlcv_df.index <= fold_end)]

    if len(ohlcv_slice) < 100:
        return 0.0

    ohlcv_exec_slice = None
    if ohlcv_exec_df is not None:
        ohlcv_exec_slice = ohlcv_exec_df[(ohlcv_exec_df.index >= fold_start) & (ohlcv_exec_df.index <= fold_end)]

    try:
        engine = BacktestEngine.from_config(strategy_cfg)
        result = engine.run(signals, ohlcv_slice, ohlcv_exec_df=ohlcv_exec_slice)
        sharpe = compute_sharpe(result.equity_curve)
        # Reject if too few trades for statistical significance
        if result.trade_count < 10:
            return 0.0
        return sharpe
    except Exception:
        return 0.0


METRIC_EVALUATORS = {
    "f1": evaluate_fold_f1,
    "f0.5": evaluate_fold_fbeta05,
    "logloss": evaluate_fold_logloss,
    "average_precision": evaluate_fold_avg_precision,
    # "sharpe" handled separately (needs extra data)
}


# ---------------------------------------------------------------------------
# Optuna Objective
# ---------------------------------------------------------------------------


def make_objective(
    X: pd.DataFrame,
    y: pd.Series,
    df_gym: pd.DataFrame,
    folds: list[tuple],
    ml_metric: str,
    target_name: str,
    balance_mode: str = "downsample",
    ohlcv_gym: pd.DataFrame | None = None,
    ohlcv_exec_gym: pd.DataFrame | None = None,
    strategy_cfg: dict | None = None,
    n_jobs: int = 1,
    num_threads: int = 8,
    max_depth_range: tuple[int, int] = (4, 10),
    num_leaves_range: tuple[int, int] = (15, 90),
    max_n_estimators: int = 3000,
    early_stopping_rounds: int = 100,
    objective_type: str = "focal",
    use_buckets: bool = False,
    learning_rate_range: tuple[float, float] = (0.005, 0.02),
    min_child_samples_range: tuple[int, int] = (150, 400),
    feature_fraction_range: tuple[float, float] = (0.3, 1.0),
):
    """Create the Optuna objective closure.

    Trains on sampled WF folds, evaluates with the chosen metric.
    Returns a closure that Optuna calls with each trial.
    """
    feature_cols = list(X.columns)

    # Determine prob column for sharpe mode
    if target_name.endswith("_LONG"):
        prob_col = "prob_Buy"
    elif target_name.endswith("_SHORT"):
        prob_col = "prob_Sell"
    else:
        prob_col = "prob_Signal"

    def objective(trial: optuna.Trial) -> float:
        _log_mem("trial_start", trial=trial.number)

        # ---- Feature Bucket Selection ----
        if use_buckets:
            from src.features.feature_buckets import (
                TOGGLEABLE_BUCKETS, get_active_features,
            )
            active_buckets = {"core"}  # always on
            for bucket in TOGGLEABLE_BUCKETS:
                if trial.suggest_categorical(f"use_{bucket}", [True, False]):
                    active_buckets.add(bucket)
            trial_features = get_active_features(feature_cols, active_buckets)
            trial.set_user_attr("active_buckets", sorted(active_buckets))
            trial.set_user_attr("n_active_features", len(trial_features))
        else:
            trial_features = feature_cols

        # ---- Suggest hyperparameters (E2E Alpha Factory wide ranges) ----
        boosting_type = trial.suggest_categorical("boosting_type", ["gbdt", "goss"])

        params = {
            "boosting_type": boosting_type,
            "verbose": -1,
            "num_threads": num_threads,
            "num_leaves": trial.suggest_int("num_leaves", num_leaves_range[0], num_leaves_range[1]),
            "min_child_samples": trial.suggest_int("min_child_samples", min_child_samples_range[0], min_child_samples_range[1]),
            "learning_rate": trial.suggest_float("learning_rate", learning_rate_range[0], learning_rate_range[1], log=True),
            "feature_fraction": trial.suggest_float("feature_fraction", feature_fraction_range[0], feature_fraction_range[1]),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.01, 1.0, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 0.01, 10.0, log=True),
            "max_depth": trial.suggest_int("max_depth", max_depth_range[0], max_depth_range[1]),
            "min_gain_to_split": trial.suggest_float("min_gain_to_split", 0.0, 2.0),
            "path_smooth": trial.suggest_float("path_smooth", 0.0, 10.0),
        }
        
        # Hyperparameter Tuning: Tune scale_pos_weight around the baseline estimate
        imbalance_multiplier = trial.suggest_float("scale_pos_weight_mult", 0.8, 1.2)

        # GOSS uses gradient-based sampling — bagging params are ignored
        if boosting_type == "gbdt":
            params["bagging_fraction"] = trial.suggest_float("bagging_fraction", 0.3, 1.0)
            params["bagging_freq"] = trial.suggest_int("bagging_freq", 1, 7)

        n_estimators = trial.suggest_int("n_estimators", 500, max_n_estimators, step=100)
        lookback_window_years = 100  # 100 years effectively disables lookback

        # Disable built-in metric — we use custom eval for early stopping
        params["metric"] = "None"

        # Custom loss objective goes in params (LightGBM 4.x API)
        obj_fn, eval_fn = OBJECTIVE_REGISTRY[objective_type]
        params["objective"] = obj_fn

        # ---- Evaluate across sampled folds ----
        fold_scores = []
        fold_details = []

        for fold_idx, (train_start, train_end, test_start, test_end) in enumerate(folds):
            X_train = X[trial_features].iloc[train_start:train_end]
            y_train = y.iloc[train_start:train_end]
            X_test = X[trial_features].iloc[test_start:test_end]
            y_test = y.iloc[test_start:test_end]

            # Apply lookback window
            # X.index is the chronologically ordered DatetimeIndex
            train_end_time = X.index[train_end - 1]
            lookback_start_time = train_end_time - pd.DateOffset(years=lookback_window_years)
            mask = X_train.index >= lookback_start_time
            X_train = X_train.loc[mask]
            y_train = y_train.loc[mask]

            # Apply class balancing
            if balance_mode == "downsample":
                pass  # Downsampling is disabled in favor of dynamic scale_pos_weight

            # Split training into train/val for early stopping (chronological)
            val_frac = 0.1
            val_split = int(len(X_train) * (1 - val_frac))
            X_tr, X_val = X_train.iloc[:val_split], X_train.iloc[val_split:]
            y_tr, y_val = y_train.iloc[:val_split], y_train.iloc[val_split:]

            # Dynamic Class Weighting: Calculate per-fold
            n_neg = (y_tr == 0).sum()
            n_pos = (y_tr == 1).sum()
            estimated_weight = n_neg / n_pos if n_pos > 0 else 1.0
            params["scale_pos_weight"] = estimated_weight * imbalance_multiplier

            # Train with early stopping
            # free_raw_data=False: avoids C++ deallocation race when multiple
            # Optuna worker threads build/destroy Datasets concurrently.
            # Memory cost is negligible (~0.7 GB total for all workers).
            train_data = lgb.Dataset(X_tr, label=y_tr, free_raw_data=False)
            val_data = lgb.Dataset(X_val, label=y_val, reference=train_data, free_raw_data=False)

            callbacks = [
                lgb.log_evaluation(period=0),  # silent
                lgb.early_stopping(stopping_rounds=early_stopping_rounds, verbose=False),
            ]

            try:
                model = lgb.train(
                    params,
                    train_data,
                    num_boost_round=n_estimators,
                    valid_sets=[val_data],
                    valid_names=["val"],
                    feval=eval_fn,
                    callbacks=callbacks,
                )
            except Exception:
                continue

            # Track actual iterations used
            best_iter = getattr(model, "best_iteration", n_estimators)

            # Predict probabilities
            raw_pred = model.predict(X_test)
            probs = _sigmoid(raw_pred)

            y_true = y_test.values.astype(int)

            # Evaluate based on chosen metric
            if ml_metric == "sharpe":
                fold_test_df = df_gym.iloc[test_start:test_end]
                score = evaluate_fold_sharpe(
                    probs, fold_test_df, ohlcv_gym, strategy_cfg, prob_col, ohlcv_exec_df=ohlcv_exec_gym
                )
            else:
                evaluator = METRIC_EVALUATORS[ml_metric]
                score = evaluator(y_true, probs)

            fold_scores.append(score)

            # Track per-fold ML metrics
            preds_binary = (probs >= 0.5).astype(int)
            fold_f1 = f1_score(y_true, preds_binary, zero_division=0)
            fold_prec = precision_score(y_true, preds_binary, zero_division=0)
            fold_f05 = fbeta_score(y_true, preds_binary, beta=0.5, zero_division=0)
            probs_clipped = np.clip(probs, 1e-7, 1 - 1e-7)
            fold_logloss = log_loss(y_true, probs_clipped)
            try:
                fold_roc_auc = roc_auc_score(y_true, probs)
            except ValueError:
                fold_roc_auc = 0.5

            fold_details.append({
                "f1": fold_f1,
                "precision": fold_prec,
                "f0.5": fold_f05,
                "logloss": fold_logloss,
                "roc_auc": fold_roc_auc,
                "score": score,
                "best_iteration": best_iter,
                "n_estimators_budget": n_estimators,
            })

            # Free model + datasets to prevent memory accumulation across folds
            del model, train_data, val_data, X_tr, X_val, y_tr, y_val
            gc.collect()
            _log_mem("fold_cleanup", trial=trial.number, fold=fold_idx)

        if not fold_scores:
            return -999.0

        avg_score = float(np.mean(fold_scores))

        # Log intermediate metrics
        avg_f1 = float(np.mean([d["f1"] for d in fold_details]))
        avg_prec = float(np.mean([d["precision"] for d in fold_details]))
        avg_f05 = float(np.mean([d["f0.5"] for d in fold_details]))
        avg_logloss = float(np.mean([d["logloss"] for d in fold_details]))
        avg_roc_auc = float(np.mean([d["roc_auc"] for d in fold_details]))

        iterations = [d["best_iteration"] for d in fold_details]
        trial.set_user_attr("avg_f1", round(avg_f1, 4))
        trial.set_user_attr("avg_precision", round(avg_prec, 4))
        trial.set_user_attr("avg_f0.5", round(avg_f05, 4))
        trial.set_user_attr("avg_logloss", round(avg_logloss, 4))
        trial.set_user_attr("avg_roc_auc", round(avg_roc_auc, 4))

        trial.set_user_attr("n_folds_evaluated", len(fold_scores))
        trial.set_user_attr("fold_scores", [round(s, 4) for s in fold_scores])
        trial.set_user_attr("std_score", round(float(np.std(fold_scores)), 4))
        # Early stopping iteration tracking
        trial.set_user_attr("n_estimators_budget", n_estimators)
        trial.set_user_attr("avg_iterations", round(float(np.mean(iterations)), 1))
        trial.set_user_attr("min_iterations", int(np.min(iterations)))
        trial.set_user_attr("max_iterations", int(np.max(iterations)))
        trial.set_user_attr("fold_iterations", iterations)
        trial.set_user_attr(
            "early_stopped_folds",
            sum(1 for d in fold_details if d["best_iteration"] < d["n_estimators_budget"]),
        )

        return avg_score

    # Wrap objective to force garbage collection between trials
    def objective_with_cleanup(trial: optuna.Trial) -> float:
        try:
            return objective(trial)
        finally:
            gc.collect()
            _log_mem("trial_end", trial=trial.number)
            try:
                best_val = trial.study.best_value
            except ValueError:
                best_val = None
            _write_worker_status(trial.number + 1, _total_trials, "running", best_val, target_name, ml_metric)

    return objective_with_cleanup


# ---------------------------------------------------------------------------
# Main search driver
# ---------------------------------------------------------------------------


def run_search(
    data_path: str,
    target_name: str,
    ml_metric: str = "logloss",
    n_trials: int = 100,
    n_jobs: int = 1,
    balance_mode: str = "downsample",
    strategy_config_path: str | None = None,
    train_cutoff_date: str | None = None,
    gym_fraction: float = 0.85,
    study_name: str | None = None,
    db_dir: str = "models/optuna_studies",
    num_threads: int = 8,
    max_depth_range: tuple[int, int] = (4, 10),
    num_leaves_range: tuple[int, int] = (15, 90),
    max_n_estimators: int = 3000,
    early_stopping_rounds: int = 100,
    max_folds: int = 10,
    objective_type: str = "focal",
    use_buckets: bool = False,
    learning_rate_range: tuple[float, float] = (0.005, 0.02),
    min_child_samples_range: tuple[int, int] = (150, 400),
    feature_fraction_range: tuple[float, float] = (0.3, 1.0),
    exec_ohlcv_path: str | None = None,
):
    """Run the Walk-Forward Optuna search (Phase 1: Brain Optimization).

    Args:
        data_path: Path to processed parquet with features and targets.
        target_name: Target column (e.g. TARGET_TRIPLE_2x1_24H_LONG).
        ml_metric: Optimization metric — 'f1', 'f0.5', 'logloss', or 'sharpe'.
        n_trials: Number of Optuna trials.
        balance_mode: Class balancing ('downsample' or 'none').
        strategy_config_path: Strategy JSON config (required for --ml-metric sharpe).
        train_cutoff_date: Optional date cutoff. If set, gym = data before cutoff.
                           If not set, gym = first gym_fraction of data.
        gym_fraction: Fraction of data to use as gym (ignored if cutoff set).
        study_name: Optuna study name (auto-generated if None).
        db_dir: Directory for SQLite study persistence.
    """
    start_time = time.perf_counter()

    # Validate sharpe mode requires strategy config
    if ml_metric == "sharpe" and not strategy_config_path:
        raise ValueError("--strategy-config is required when --ml-metric is 'sharpe'")

    # Derive direction tag
    if target_name.endswith("_LONG"):
        direction_tag = "long"
    elif target_name.endswith("_SHORT"):
        direction_tag = "short"
    else:
        direction_tag = "multi"

    if study_name is None:
        study_name = f"wf_v2_{direction_tag}_{ml_metric}"

    # Enforce minimum trials when buckets are active (2^11 search space)
    if use_buckets:
        from src.features.feature_buckets import BUCKET_MIN_TRIALS, TOGGLEABLE_BUCKETS
        if n_trials < BUCKET_MIN_TRIALS:
            print(f"  [BUCKET MODE] Raising n_trials from {n_trials} to {BUCKET_MIN_TRIALS} "
                  f"(2^{len(TOGGLEABLE_BUCKETS)} categorical combos require TPE warm-up)")
            n_trials = BUCKET_MIN_TRIALS

    print("=" * 70)
    print("OPTUNA LIGHTGBM SEARCH v2 — WALK-FORWARD BAKE-OFF")
    print("=" * 70)
    print(f"  Target:          {target_name}")
    print(f"  ML metric:       {ml_metric}")
    print(f"  Objective:       {objective_type}")
    print(f"  Buckets:         {'ON (feature group toggles)' if use_buckets else 'OFF (all features)'}")
    print(f"  Trials:          {n_trials}")
    print(f"  Workers:         {n_jobs}  (LGB threads/worker: {num_threads})")
    print(f"  Balance:         {balance_mode}")
    if train_cutoff_date:
        print(f"  Cutoff:          {train_cutoff_date} (date-based gym)")
    else:
        print(f"  Gym fraction:    {gym_fraction:.0%}")
    print(f"  Study:           {study_name}")
    print("=" * 70)

    # ---- Enable crash diagnostics ----
    faulthandler.enable()  # Prints C-level traceback on SIGSEGV (exit 139)
    try:
        import signal
        faulthandler.register(signal.SIGUSR1)  # kill -USR1 <pid> → dump all threads
    except (AttributeError, OSError):
        pass  # SIGUSR1 not available on Windows

    # ---- Load data ----
    print("\n[1/4] Loading data...")
    df = pd.read_parquet(data_path)
    feature_cols = util.get_feature_columns(df)
    target_col = util.get_target_column(df, target_name)
    df = df.dropna(subset=[target_col])

    # ---- Filter Features if Strategy Config provided ----
    strategy_cfg = None
    if strategy_config_path:
        import fnmatch
        with open(strategy_config_path) as f:
            strategy_cfg = json.load(f)
        
        if strategy_cfg.get("features"):
            filtered_cols = []
            for col in feature_cols:
                if any(fnmatch.fnmatch(col, pat) for pat in strategy_cfg["features"]):
                    filtered_cols.append(col)
            print(f"  [ISOLATION STUDY] Filtering from {len(feature_cols)} to {len(set(filtered_cols))} features based on strategy config.")
            feature_cols = list(set(filtered_cols))

    # Split into gym (for WF folds) and vault (untouched holdout)
    if train_cutoff_date:
        cutoff = pd.Timestamp(train_cutoff_date)
        df_gym = df[df.index < cutoff].copy()
        df_vault = df[df.index >= cutoff].copy()
        print(f"  Using date cutoff: {train_cutoff_date}")
    else:
        n_vault = int(len(df) * (1 - gym_fraction))
        df_gym = df.iloc[:len(df) - n_vault].copy()
        df_vault = df.iloc[len(df) - n_vault:].copy()
        print(f"  Using {gym_fraction:.0%} gym / {1-gym_fraction:.0%} vault split")

    X = df_gym[feature_cols].astype("float32")
    y = df_gym[target_col].astype(int)
    n_gym = len(X)
    gym_index = X.index  # lightweight reference for fold time display

    print(f"  Gym:   {n_gym:,} rows ({gym_index.min()} → {gym_index.max()})")
    print(f"  Vault: {len(df_vault):,} rows ({df_vault.index.min()} → {df_vault.index.max()}) [UNTOUCHED]")
    print(f"  Features: {len(feature_cols)} (float32)")
    print(f"  Target distribution (gym): {y.value_counts().to_dict()}")

    # Free the full gym DataFrame and vault (vault only used for print above)
    del df, df_gym, df_vault
    gc.collect()
    _log_mem("after_data_load")

    # ---- Generate and sample WF folds ----
    print("\n[2/4] Generating walk-forward folds...")
    all_folds = walk_forward_folds(n_gym)
    folds = sample_folds(all_folds, max_folds=max_folds)
    print(f"  Total folds: {len(all_folds)}  |  Sampled: {len(folds)}")

    # Show fold time ranges
    for i, (ts, te, vs, ve) in enumerate(folds):
        train_end_dt = gym_index[min(te - 1, n_gym - 1)]
        val_start_dt = gym_index[min(vs, n_gym - 1)]
        val_end_dt = gym_index[min(ve - 1, n_gym - 1)]
        print(f"    Fold {i}: train→{train_end_dt.date()} | val {val_start_dt.date()}→{val_end_dt.date()}")

    # ---- Load OHLCV if sharpe mode ----
    ohlcv_gym = None
    ohlcv_exec_gym = None
    if ml_metric == "sharpe":
        print("\n[2b/4] Loading OHLCV + strategy config for sharpe evaluation...")
        from agent.backtest_engine import load_ohlcv_dual
        ohlcv_full, ohlcv_exec_full = load_ohlcv_dual(data_path) if exec_ohlcv_path is None else load_ohlcv_dual(data_path)
        if exec_ohlcv_path:
            _, ohlcv_exec_full = load_ohlcv_dual(exec_ohlcv_path)
            
        if train_cutoff_date:
            ohlcv_gym = ohlcv_full[ohlcv_full.index < cutoff].copy()
            if ohlcv_exec_full is not None:
                ohlcv_exec_gym = ohlcv_exec_full[ohlcv_exec_full.index < cutoff].copy()
        else:
            ohlcv_gym = ohlcv_full.iloc[:n_gym].copy()
            if ohlcv_exec_full is not None:
                ohlcv_exec_gym = ohlcv_exec_full.iloc[:n_gym].copy()
        print(f"  OHLCV gym: {len(ohlcv_gym):,} bars")

        if not strategy_cfg:
            with open(strategy_config_path) as f:
                strategy_cfg = json.load(f)
        print(f"  Strategy: {strategy_cfg.get('nickname', Path(strategy_config_path).stem)}")

    # ---- Run Optuna ----
    print(f"\n[3/4] Running Optuna search ({n_trials} trials, {len(folds)} folds each)...")

    os.makedirs(db_dir, exist_ok=True)
    if os.name == 'nt':
        db_path = os.path.join(db_dir, f"{study_name}.db")
        storage = f"sqlite:///{db_path}"
    else:
        journal_path = os.path.join(db_dir, f"{study_name}.journal")
        storage = optuna.storages.JournalStorage(
            optuna.storages.journal.JournalFileBackend(journal_path),
        )

    study = optuna.create_study(
        study_name=study_name,
        storage=storage,
        direction="maximize",
        load_if_exists=True,
    )

    objective = make_objective(
        X=X,
        y=y,
        df_gym=None,  # freed for memory; only needed in sharpe mode
        folds=folds,
        ml_metric=ml_metric,
        target_name=target_name,
        balance_mode=balance_mode,
        ohlcv_gym=ohlcv_gym,
        ohlcv_exec_gym=ohlcv_exec_gym,
        strategy_cfg=strategy_cfg,
        n_jobs=n_jobs,
        num_threads=num_threads,
        max_depth_range=max_depth_range,
        num_leaves_range=num_leaves_range,
        max_n_estimators=max_n_estimators,
        early_stopping_rounds=early_stopping_rounds,
        objective_type=objective_type,
        use_buckets=use_buckets,
        learning_rate_range=learning_rate_range,
        min_child_samples_range=min_child_samples_range,
        feature_fraction_range=feature_fraction_range,
    )

    # Progress callback
    trial_start_time = time.perf_counter()

    def progress_callback(study: optuna.Study, trial: optuna.trial.FrozenTrial):
        if trial.state != optuna.trial.TrialState.COMPLETE:
            return
        n_done = len([t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE])
        elapsed = time.perf_counter() - trial_start_time
        avg_time = elapsed / n_done if n_done > 0 else 0
        remaining = avg_time * (n_trials - n_done)

        score = trial.value if trial.value is not None else 0
        std = trial.user_attrs.get("std_score", 0)

        if n_done % 10 == 0 or n_done <= 3:
            avg_iters = trial.user_attrs.get('avg_iterations', 0)
            budget = trial.user_attrs.get('n_estimators_budget', 0)
            es_folds = trial.user_attrs.get('early_stopped_folds', 0)
            n_folds_eval = trial.user_attrs.get('n_folds_evaluated', 0)
            print(
                f"  Trial {n_done:>4}/{n_trials}  "
                f"{ml_metric}={score:>8.4f} (±{std:.4f})  "
                f"F1={trial.user_attrs.get('avg_f1', 0):.4f}  "
                f"iters={avg_iters:.0f}/{budget} "
                f"(ES:{es_folds}/{n_folds_eval})  "
                f"[{elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining]"
            )
            best = study.best_trial
            print(
                f"         Best: {ml_metric}={best.value:.4f}  "
                f"F1={best.user_attrs.get('avg_f1', 0):.4f}  "
                f"(trial #{best.number})"
            )

    import logging
    log_path = os.path.join(db_dir, f"{study_name}_errors.log")
    file_handler = logging.FileHandler(log_path, mode="a")
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("optuna").addHandler(file_handler)

    try:
        study.optimize(
            objective,
            n_trials=n_trials,
            n_jobs=n_jobs,
            callbacks=[progress_callback],
            show_progress_bar=True,
        )
    except Exception as e:
        print(f"\n{'='*70}")
        print(f"STUDY CRASHED: {type(e).__name__}: {e}")
        print(f"{'='*70}")
        import traceback
        traceback.print_exc()
        with open(log_path, "a") as f:
            f.write(f"\n{'='*70}\n")
            f.write(f"STUDY CRASHED: {type(e).__name__}: {e}\n")
            traceback.print_exc(file=f)
        print(f"\nError log saved to: {log_path}")
        print(f"Completed trials are preserved in the DB. Restart to resume.")
        raise  # Stop the process

    elapsed = time.perf_counter() - start_time

    # ---- Results ----
    best = study.best_trial

    print("\n" + "=" * 70)
    print(f"SEARCH COMPLETE — {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print("=" * 70)
    print(f"\nBest Trial #{best.number}:")
    print(f"  {ml_metric}:       {best.value:.4f}")
    print(f"  Avg F0.5:      {best.user_attrs.get('avg_f0.5', 0):.4f}")
    print(f"  Avg Precision: {best.user_attrs.get('avg_precision', 0):.4f}")
    print(f"  Avg ROC-AUC:   {best.user_attrs.get('avg_roc_auc', 0):.4f}")
    print(f"  Avg Logloss:   {best.user_attrs.get('avg_logloss', 0):.4f}")
    print(f"  Fold StdDev:   {best.user_attrs.get('std_score', 0):.4f}")
    print(f"  Folds used:    {best.user_attrs.get('n_folds_evaluated', 0)}")
    print(f"  Avg Iters:     {best.user_attrs.get('avg_iterations', 0):.0f}"
          f" / {best.user_attrs.get('n_estimators_budget', 0)} budget")
    print(f"  ES Folds:      {best.user_attrs.get('early_stopped_folds', 0)}"
          f" / {best.user_attrs.get('n_folds_evaluated', 0)} stopped early")
    print(f"\nBest Hyperparameters:")
    for k, v in best.params.items():
        print(f"  {k}: {v}")

    # ---- Save best params ----
    print(f"\n[4/4] Saving results...")
    os.makedirs("reports", exist_ok=True)
    params_path = os.path.join("reports", f"optuna_best_params_{direction_tag}_{ml_metric}.json")

    best_params_payload = {
        "study_name": study_name,
        "ml_metric": ml_metric,
        "target_name": target_name,
        "data_path": data_path,
        "train_cutoff_date": train_cutoff_date,
        "n_trials": n_trials,
        "n_folds": len(folds),
        "best_trial_number": best.number,
        "best_score": round(best.value, 4),
        "best_hyperparameters": dict(best.params),
        "best_metrics": {
            "optimization_metric": ml_metric,
            "optimization_score": round(best.value, 4),
            "avg_f1": best.user_attrs.get("avg_f1"),
            "avg_precision": best.user_attrs.get("avg_precision"),
            "fold_std": best.user_attrs.get("std_score"),
            "fold_scores": best.user_attrs.get("fold_scores"),
        },
        "model_params_for_experiment_runner": {
            **{k: v for k, v in best.params.items()},
            "objective": "binary",
            "use_focal": True,
            "metric": "binary_logloss",
            "validation_fraction": 0.1,
            "class_weight": None,
        },
        "wall_time_seconds": round(elapsed, 1),
    }
    with open(params_path, "w") as f:
        json.dump(best_params_payload, f, indent=2)
    print(f"  Best params: {params_path}")

    # ---- Save all trials CSV ----
    csv_path = os.path.join("reports", f"optuna_trials_{direction_tag}_{ml_metric}.csv")
    rows = []
    for trial in study.trials:
        if trial.state != optuna.trial.TrialState.COMPLETE:
            continue
        _skip_keys = {"fold_scores", "fold_iterations"}
        row = {
            "trial_number": trial.number,
            "score": trial.value,
            **trial.params,
            **{k: v for k, v in trial.user_attrs.items() if k not in _skip_keys},
        }
        rows.append(row)
    if rows:
        csv_df = pd.DataFrame(rows).sort_values("score", ascending=False)
        csv_df.to_csv(csv_path, index=False)
        print(f"  Trials CSV: {csv_path} ({len(csv_df)} trials)")

    # ---- Log to experiment log ----
    log = load_experiment_log()
    exp_id = generate_experiment_id(log)
    experiment_record = {
        "id": exp_id,
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "strategy": f"optuna_v2_{direction_tag}_{ml_metric}",
        "hypothesis": (
            f"WF bake-off: Optuna search optimizing {ml_metric} "
            f"on sampled WF folds for {direction_tag} target (set_07)"
        ),
        "changes": {
            "optuna_v2": True,
            "ml_metric": ml_metric,
            "n_trials": n_trials,
            "n_folds": len(folds),
            "best_params": dict(best.params),
        },
        "config": {
            "data_path": data_path,
            "target_name": target_name,
            "method": "optuna_v2_wf_bakeoff",
            "balance_mode": balance_mode,
            "train_cutoff_date": train_cutoff_date,
            "gym_fraction": gym_fraction if not train_cutoff_date else None,
        },
        "metrics": {
            f"best_{ml_metric}": round(best.value, 4),
            "best_avg_f1": best.user_attrs.get("avg_f1"),
            "best_avg_precision": best.user_attrs.get("avg_precision"),
            "fold_std": best.user_attrs.get("std_score"),
            "wall_time_seconds": round(elapsed, 1),
        },
        "verdict": "search_complete",
    }
    _append_to_log(experiment_record)
    print(f"  Logged as {exp_id}")

    if os.name != 'nt':
        print(f"\n  Study journal: {journal_path}")
    print(f"  Resume: --study-name {study_name}")
    print(f"\n  NEXT STEP: Run Phase 2 threshold optimization, then Phase 3 OOS test")
    print(f"  See: python agent/strategy_optimizer.py --help")

    return best.params, best.value


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Optuna LightGBM Search v2 — Walk-Forward Bake-Off. "
            "Phase 1: finds optimal hyperparams via sampled WF fold evaluation. "
            "Supports f1/f0.5/logloss/sharpe metric bake-off."
        )
    )
    parser.add_argument(
        "--target", required=True,
        help="Target column (e.g. TARGET_TRIPLE_2x1_24H_LONG)",
    )
    parser.add_argument(
        "--data", required=True,
        help="Path to processed parquet dataset",
    )
    parser.add_argument(
        "--ml-metric",
        choices=["f1", "f0.5", "logloss", "average_precision", "sharpe"],
        default="logloss",
        help="Optimization metric: logloss, f0.5, average_precision, f1, or sharpe (default: logloss)",
    )
    parser.add_argument(
        "--n-trials", type=int, default=100,
        help="Number of Optuna trials (default: 100)",
    )
    parser.add_argument(
        "--balance-mode", default="downsample",
        choices=["downsample", "none"],
        help="Class balancing mode (default: downsample)",
    )
    parser.add_argument(
        "--strategy-config", default=None,
        help="Strategy JSON config (required for --ml-metric sharpe)",
    )
    parser.add_argument(
        "--train-cutoff-date", default=None,
        help="Date cutoff for gym/vault split (YYYY-MM-DD). "
             "If not set, uses --gym-fraction.",
    )
    parser.add_argument(
        "--gym-fraction", type=float, default=0.85,
        help="Fraction of data for gym if no cutoff date (default: 0.85)",
    )
    parser.add_argument(
        "--study-name", default=None,
        help="Optuna study name (for resume). Auto-generated if not provided.",
    )
    parser.add_argument(
        "--db-dir", default="models/optuna_studies",
        help="Directory for SQLite study persistence (default: models/optuna_studies)",
    )
    parser.add_argument(
        "--n-jobs", type=int, default=1,
        help="Number of parallel Optuna workers (default: 1). "
             "LightGBM num_threads auto-scales to cpu_count/n_jobs.",
    )
    parser.add_argument(
        "--max-depth-range", type=int, nargs=2, default=[4, 10],
        metavar=("MIN", "MAX"),
        help="Search range for max_depth (default: 4 10)",
    )
    parser.add_argument(
        "--num-leaves-range", type=int, nargs=2, default=[15, 90],
        metavar=("MIN", "MAX"),
        help="Search range for num_leaves (default: 15 90)",
    )
    parser.add_argument(
        "--max-n-estimators", type=int, default=3000,
        help="Maximum n_estimators for search (default: 3000)",
    )
    parser.add_argument(
        "--early-stopping-rounds", type=int, default=100,
        help="Early stopping rounds (default: 100)",
    )
    parser.add_argument(
        "--learning-rate-range", type=float, nargs=2, default=[0.005, 0.02],
        metavar=("MIN", "MAX"),
        help="Search range for learning_rate (default: 0.005 0.02)",
    )
    parser.add_argument(
        "--min-child-samples-range", type=int, nargs=2, default=[150, 400],
        metavar=("MIN", "MAX"),
        help="Search range for min_child_samples (default: 150 400)",
    )
    parser.add_argument(
        "--feature-fraction-range", type=float, nargs=2, default=[0.4, 0.8],
        metavar=("MIN", "MAX"),
        help="Search range for feature_fraction (default: 0.4 0.8)",
    )
    parser.add_argument(
        "--num-threads", type=int, default=8,
        help="LightGBM num_threads per worker (default: 8). "
             "Total cores used = n_jobs × num_threads.",
    )
    parser.add_argument(
        "--max-folds", type=int, default=10,
        help="Maximum number of walk-forward folds to sample (default: 10). "
             "Fewer folds = faster searches with slightly noisier estimates.",
    )
    parser.add_argument(
        "--worker-id", type=int, default=0,
        help="Worker ID for process-level parallelism (default: 0 = single process). "
             "When >0, all output is prefixed with [W{id}] for log disambiguation.",
    )
    parser.add_argument(
        "--objective", default="focal",
        choices=list(OBJECTIVE_REGISTRY.keys()),
        help="LightGBM objective function: 'focal' (default) or 'asymmetric' "
             "(5× FP penalty for hyper-conservative precision).",
    )
    parser.add_argument(
        "--use-buckets", action="store_true", default=False,
        help="Enable Feature Bucket Architecture: Optuna toggles feature groups "
             "(volatility, momentum, macro, etc.) as categorical hyperparameters. "
             "Automatically enforces minimum 150 trials for TPE convergence.",
    )
    parser.add_argument(
        "--exec-data", default=None,
        help="Optional: path to raw unadjusted execution data for wallet/fills",
    )
    args = parser.parse_args()

    # Set global worker ID and total trials for logging
    global _worker_id, _total_trials
    _worker_id = args.worker_id
    _total_trials = args.n_trials

    run_search(
        data_path=args.data,
        target_name=args.target,
        ml_metric=args.ml_metric,
        n_trials=args.n_trials,
        n_jobs=args.n_jobs,
        balance_mode=args.balance_mode,
        strategy_config_path=args.strategy_config,
        train_cutoff_date=args.train_cutoff_date,
        gym_fraction=args.gym_fraction,
        study_name=args.study_name,
        db_dir=args.db_dir,
        num_threads=args.num_threads,
        max_depth_range=tuple(args.max_depth_range),
        num_leaves_range=tuple(args.num_leaves_range),
        max_n_estimators=args.max_n_estimators,
        early_stopping_rounds=args.early_stopping_rounds,
        max_folds=args.max_folds,
        objective_type=args.objective,
        use_buckets=args.use_buckets,
        learning_rate_range=tuple(args.learning_rate_range),
        min_child_samples_range=tuple(args.min_child_samples_range),
        feature_fraction_range=tuple(args.feature_fraction_range),
        exec_ohlcv_path=args.exec_data,
    )


if __name__ == "__main__":
    main()
