"""
Optuna LightGBM Hyperparameter Search v2 — Walk-Forward Bake-Off.

Upgrades over v1:
- Supports four ML-metric modes via --ml-metric for bake-off comparison:
    f1      — average Buy F1 across WF folds (v1 baseline)
    f0.5    — average F-Beta(0.5) – emphasizes precision over recall
    logloss — average binary_logloss – produces best-calibrated probabilities
    sharpe  — average Sharpe ratio from BacktestEngine on each fold's OOS
- Binary classification with focal loss (consistent with production).
- Persists study to SQLite for visualization and resume.
- Does NOT touch the final OOS holdout (2022-2026). That set is reserved
  for a single untouched evaluation after the search completes.

Architecture (Quant Desk Standard):
  Phase 1 (this script): Optuna finds best LightGBM hyperparams via sampled
           WF folds. The "Brain" optimization.
  Phase 2 (strategy_optimizer.py): Threshold/strategy sweep on vault set
           or last gym slice. The "Trigger" optimization.
  Phase 3 (experiment_runner.py): Train final model on all pre-cutoff data,
           one-shot evaluation on untouched OOS.

Usage:
    # Run the logloss bake-off (recommended)
    python agent/optuna_lgbm_search_v2.py \\
        --target TARGET_TRIPLE_2x1_24H_LONG \\
        --data C:\\CL_Analyst_Data\\data\\processed\\CL_set_07.parquet \\
        --ml-metric logloss --n-trials 100

    # Full bake-off: run all four and compare
    python agent/optuna_lgbm_search_v2.py --ml-metric f1 --n-trials 100 ...
    python agent/optuna_lgbm_search_v2.py --ml-metric f0.5 --n-trials 100 ...
    python agent/optuna_lgbm_search_v2.py --ml-metric logloss --n-trials 100 ...
    python agent/optuna_lgbm_search_v2.py --ml-metric sharpe --n-trials 100 \\
        --strategy-config configs/strategies/ensemble2_alt.json ...

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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
    f1_score,
    fbeta_score,
    log_loss,
    precision_score,
    recall_score,
)

import src.util as util

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


def evaluate_fold_sharpe(
    probs: np.ndarray,
    fold_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    strategy_cfg: dict,
    prob_col: str,
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

    try:
        engine = BacktestEngine.from_config(strategy_cfg)
        result = engine.run(signals, ohlcv_slice)
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
    strategy_cfg: dict | None = None,
    n_jobs: int = 1,
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
        # ---- Suggest hyperparameters ----
        params = {
            "boosting_type": "gbdt",
            "verbose": -1,
            "num_threads": max(1, _CPU_COUNT // n_jobs),
            "num_leaves": trial.suggest_int("num_leaves", 15, 63),
            "min_child_samples": trial.suggest_int("min_child_samples", 50, 300),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
            "feature_fraction": trial.suggest_float("feature_fraction", 0.4, 0.9),
            "bagging_fraction": trial.suggest_float("bagging_fraction", 0.4, 0.9),
            "bagging_freq": trial.suggest_int("bagging_freq", 1, 7),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.1, 10.0, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 0.1, 10.0, log=True),
            "max_depth": trial.suggest_int("max_depth", 3, 8),
            "min_gain_to_split": trial.suggest_float("min_gain_to_split", 0.0, 2.0),
        }
        n_estimators = trial.suggest_int("n_estimators", 500, 2000, step=100)

        # Disable built-in metric — we use focal_eval for early stopping
        params["metric"] = "None"

        # Custom focal loss objective goes in params (LightGBM 4.x API)
        params["objective"] = focal_obj

        # ---- Evaluate across sampled folds ----
        fold_scores = []
        fold_details = []

        for fold_idx, (train_start, train_end, test_start, test_end) in enumerate(folds):
            X_train = X.iloc[train_start:train_end]
            y_train = y.iloc[train_start:train_end]
            X_test = X.iloc[test_start:test_end]
            y_test = y.iloc[test_start:test_end]

            # Apply class balancing
            if balance_mode == "downsample":
                try:
                    X_train, y_train = util.downsample_majority(
                        X_train, y_train, random_state=42 + fold_idx
                    )
                except ValueError:
                    continue  # Skip degenerate fold

            # Split training into train/val for early stopping (chronological)
            val_frac = 0.1
            val_split = int(len(X_train) * (1 - val_frac))
            X_tr, X_val = X_train.iloc[:val_split], X_train.iloc[val_split:]
            y_tr, y_val = y_train.iloc[:val_split], y_train.iloc[val_split:]

            # Train with early stopping
            train_data = lgb.Dataset(X_tr, label=y_tr)
            val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

            callbacks = [
                lgb.log_evaluation(period=0),  # silent
                lgb.early_stopping(stopping_rounds=100, verbose=False),
            ]

            try:
                model = lgb.train(
                    params,
                    train_data,
                    num_boost_round=n_estimators,
                    valid_sets=[val_data],
                    valid_names=["val"],
                    feval=focal_eval,
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
                    probs, fold_test_df, ohlcv_gym, strategy_cfg, prob_col
                )
            else:
                evaluator = METRIC_EVALUATORS[ml_metric]
                score = evaluator(y_true, probs)

            fold_scores.append(score)

            # Track per-fold ML metrics
            preds_binary = (probs >= 0.5).astype(int)
            fold_f1 = f1_score(y_true, preds_binary, zero_division=0)
            fold_prec = precision_score(y_true, preds_binary, zero_division=0)
            fold_details.append({
                "f1": fold_f1,
                "precision": fold_prec,
                "score": score,
                "best_iteration": best_iter,
                "n_estimators_budget": n_estimators,
            })

        if not fold_scores:
            return -999.0

        avg_score = float(np.mean(fold_scores))

        # Log intermediate metrics
        avg_f1 = float(np.mean([d["f1"] for d in fold_details]))
        avg_prec = float(np.mean([d["precision"] for d in fold_details]))
        iterations = [d["best_iteration"] for d in fold_details]
        trial.set_user_attr("avg_f1", round(avg_f1, 4))
        trial.set_user_attr("avg_precision", round(avg_prec, 4))
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

    return objective


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

    print("=" * 70)
    print("OPTUNA LIGHTGBM SEARCH v2 — WALK-FORWARD BAKE-OFF")
    print("=" * 70)
    print(f"  Target:          {target_name}")
    print(f"  ML metric:       {ml_metric}")
    print(f"  Trials:          {n_trials}")
    print(f"  Workers:         {n_jobs}  (LGB threads/worker: {max(1, _CPU_COUNT // n_jobs)})")
    print(f"  Balance:         {balance_mode}")
    if train_cutoff_date:
        print(f"  Cutoff:          {train_cutoff_date} (date-based gym)")
    else:
        print(f"  Gym fraction:    {gym_fraction:.0%}")
    print(f"  Study:           {study_name}")
    print("=" * 70)

    # ---- Load data ----
    print("\n[1/4] Loading data...")
    df = pd.read_parquet(data_path)
    feature_cols = util.get_feature_columns(df)
    target_col = util.get_target_column(df, target_name)
    df = df.dropna(subset=[target_col])

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

    X = df_gym[feature_cols]
    y = df_gym[target_col].astype(int)

    print(f"  Gym:   {len(df_gym):,} rows ({df_gym.index.min()} → {df_gym.index.max()})")
    print(f"  Vault: {len(df_vault):,} rows ({df_vault.index.min()} → {df_vault.index.max()}) [UNTOUCHED]")
    print(f"  Features: {len(feature_cols)}")
    print(f"  Target distribution (gym): {y.value_counts().to_dict()}")

    # ---- Generate and sample WF folds ----
    print("\n[2/4] Generating walk-forward folds...")
    all_folds = walk_forward_folds(len(df_gym))
    folds = sample_folds(all_folds)
    print(f"  Total folds: {len(all_folds)}  |  Sampled: {len(folds)}")

    # Show fold time ranges
    for i, (ts, te, vs, ve) in enumerate(folds):
        train_end_dt = df_gym.index[min(te - 1, len(df_gym) - 1)]
        val_start_dt = df_gym.index[min(vs, len(df_gym) - 1)]
        val_end_dt = df_gym.index[min(ve - 1, len(df_gym) - 1)]
        print(f"    Fold {i}: train→{train_end_dt.date()} | val {val_start_dt.date()}→{val_end_dt.date()}")

    # ---- Load OHLCV if sharpe mode ----
    ohlcv_gym = None
    strategy_cfg = None
    if ml_metric == "sharpe":
        print("\n[2b/4] Loading OHLCV + strategy config for sharpe evaluation...")
        from agent.backtest_engine import load_ohlcv
        ohlcv_full = load_ohlcv(data_path)
        if train_cutoff_date:
            ohlcv_gym = ohlcv_full[ohlcv_full.index < cutoff].copy()
        else:
            ohlcv_gym = ohlcv_full.iloc[:len(df) - n_vault].copy()
        print(f"  OHLCV gym: {len(ohlcv_gym):,} bars")

        with open(strategy_config_path) as f:
            strategy_cfg = json.load(f)
        print(f"  Strategy: {strategy_cfg.get('nickname', Path(strategy_config_path).stem)}")

    # ---- Run Optuna ----
    print(f"\n[3/4] Running Optuna search ({n_trials} trials, {len(folds)} folds each)...")

    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, f"{study_name}.db")
    storage = optuna.storages.RDBStorage(
        url=f"sqlite:///{db_path}",
        engine_kwargs={"connect_args": {"timeout": 30}},
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
        df_gym=df_gym,
        folds=folds,
        ml_metric=ml_metric,
        target_name=target_name,
        balance_mode=balance_mode,
        ohlcv_gym=ohlcv_gym,
        strategy_cfg=strategy_cfg,
        n_jobs=n_jobs,
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
    print(f"  Avg F1:        {best.user_attrs.get('avg_f1', 0):.4f}")
    print(f"  Avg Precision: {best.user_attrs.get('avg_precision', 0):.4f}")
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

    print(f"\n  Study DB: {db_path}")
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
        choices=["f1", "f0.5", "logloss", "sharpe"],
        default="logloss",
        help="Optimization metric: f1, f0.5, logloss, or sharpe (default: logloss)",
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
    args = parser.parse_args()

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
    )


if __name__ == "__main__":
    main()
