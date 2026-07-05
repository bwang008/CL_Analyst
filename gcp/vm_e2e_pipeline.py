"""
E2E Alpha Factory Pipeline - VM Orchestrator.

Runs after Optuna search completes. Handles:
  1. Extract best_params from each Optuna study (.db)
  2. Train final LightGBM models on full train split -> save .pkl
  3. Generate OOS predictions on vault/test data
  4. Run BacktestEngine on predictions -> save backtest reports
  5. Package all artifacts -> zip -> upload to GCS -> shutdown VM

This script is self-contained and runs on the GCP VM using only
packages available in the optuna-env (lightgbm, optuna, pandas,
scikit-learn, numpy, pyarrow, sqlalchemy).

Usage:
    python gcp/vm_e2e_pipeline.py \
        --data /home/bwang/data/CL_set_09.parquet \
        --train-cutoff-date 2022-01-01 \
        --strategy-config configs/strategies/ensemble4.json \
        --db-dir models/optuna_studies \
        --output-dir production_output

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
import optuna
from sklearn.metrics import (
    average_precision_score,
    classification_report,
    f1_score,
    fbeta_score,
    log_loss,
    precision_score,
    recall_score,
)

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import src.util as util
# T6 (t6-config-generator-fix_07052026_0043): single-source dataset-tag
# derivation. Identity of this function is test-pinned against the generator's
# import — do NOT re-implement the stripping inline.
from src.core.dataset_tag import derive_dataset_tag


# ---------------------------------------------------------------------------
# Focal loss (must match optuna_lgbm_search_v2.py exactly)
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
    """Focal loss eval metric for early stopping."""
    labels = val_set.get_label().astype(int)
    p = _sigmoid(preds)
    p_t = np.where(labels == 1, p, 1 - p)
    loss = -((1 - p_t) ** FOCAL_GAMMA) * np.log(np.clip(p_t, 1e-7, 1.0))
    return "focal_loss", float(np.mean(loss)), False


# ---------------------------------------------------------------------------
# Study extraction
# ---------------------------------------------------------------------------


def extract_best_params(db_path: str, study_name: str) -> dict:
    """Load an Optuna study from JournalStorage and extract the best trial params."""
    # Support both journal (new) and SQLite (legacy)
    if db_path.endswith(".journal"):
        storage = optuna.storages.JournalStorage(
            optuna.storages.journal.JournalFileBackend(db_path),
        )
    else:
        storage = optuna.storages.RDBStorage(
            url=f"sqlite:///{db_path}",
            engine_kwargs={"connect_args": {"timeout": 10}},
        )
    study = optuna.load_study(study_name=study_name, storage=storage)
    best = study.best_trial

    params = dict(best.params)
    print(f"  Best trial #{best.number}: score={best.value:.4f}")
    print(f"  Params: {json.dumps(params, indent=4, default=str)}")
    return params


# ---------------------------------------------------------------------------
# Model training
# ---------------------------------------------------------------------------


def train_final_model(
    df_train: pd.DataFrame,
    feature_cols: list[str],
    target_col: str,
    params: dict,
    balance_mode: str = "downsample",
    output_path: str = "final_model.pkl",
    random_seed: int = 42,
) -> lgb.Booster:
    """Train a final LightGBM model on the full train split and save .pkl."""

    lookback_window_years = params.pop("lookback_window_years", 100)  # Default 100 disables lookback
    end_time = df_train.index.max()
    start_time = end_time - pd.DateOffset(years=lookback_window_years)
    print(f"  Applying {lookback_window_years}-year lookback window (>= {start_time.date()})")
    df_train = df_train[df_train.index >= start_time]

    X = df_train[feature_cols]
    y = df_train[target_col].astype(int)

    # Apply class balancing
    if balance_mode == "downsample":
        X, y = util.downsample_majority(X, y, random_state=random_seed)
        print(f"  After downsample: {len(X):,} rows")

    # Split for early stopping (last 10%)
    val_split = int(len(X) * 0.9)
    X_tr, X_val = X.iloc[:val_split], X.iloc[val_split:]
    y_tr, y_val = y.iloc[:val_split], y.iloc[val_split:]

    # Extract model hyperparams (remove non-LGB keys)
    lgb_params = {k: v for k, v in params.items()}
    n_estimators = lgb_params.pop("n_estimators", 1500)
    lgb_params["verbose"] = -1
    lgb_params["num_threads"] = 8
    lgb_params["metric"] = "None"
    lgb_params["objective"] = focal_obj
    # --- Reproducibility: route all LGBM RNG through the shared seed ---
    # NOTE: full bit-for-bit determinism ALSO requires a fixed thread count.
    # deterministic=True + force_col_wise=True remove most nondeterminism, but a
    # residual multi-thread nondeterminism risk remains if num_threads changes
    # between runs (here num_threads is pinned to 8, so runs are reproducible).
    lgb_params["seed"] = random_seed
    lgb_params["bagging_seed"] = random_seed
    lgb_params["feature_fraction_seed"] = random_seed
    lgb_params["deterministic"] = True
    lgb_params["force_col_wise"] = True

    train_data = lgb.Dataset(X_tr, label=y_tr)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

    callbacks = [
        lgb.log_evaluation(period=0),
        lgb.early_stopping(stopping_rounds=100, verbose=False),
    ]

    model = lgb.train(
        lgb_params,
        train_data,
        num_boost_round=n_estimators,
        valid_sets=[val_data],
        valid_names=["val"],
        feval=focal_eval,
        callbacks=callbacks,
    )

    best_iter = getattr(model, "best_iteration", n_estimators)
    print(f"  Trained: {best_iter} iterations (budget: {n_estimators})")

    # Save as pickle (compatible with existing registry format)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        pickle.dump(model, f)
    print(f"  Saved: {output_path} ({os.path.getsize(output_path) / 1024:.0f} KB)")

    return model


# ---------------------------------------------------------------------------
# OOS Prediction
# ---------------------------------------------------------------------------


def generate_oos_predictions(
    model: lgb.Booster,
    df_vault: pd.DataFrame,
    feature_cols: list[str],
    target_col: str,
    direction: str,
    output_path: str,
) -> pd.DataFrame:
    """Generate OOS predictions and save to CSV."""

    X_vault = df_vault[feature_cols]
    raw_pred = model.predict(X_vault)
    probs = _sigmoid(raw_pred)

    # Build predictions DataFrame (matches registry format)
    preds_df = pd.DataFrame(index=df_vault.index)
    preds_df["y_true"] = df_vault[target_col].fillna(-1).astype(int)

    if direction == "long":
        preds_df["prob_Buy"] = probs
        preds_df["prob_Hold"] = 1.0 - probs
        preds_df["predicted"] = np.where(probs >= 0.5, "Buy", "Hold")
    else:
        preds_df["prob_Sell"] = probs
        preds_df["prob_Hold"] = 1.0 - probs
        preds_df["predicted"] = np.where(probs >= 0.5, "Sell", "Hold")

    # Add OHLCV columns needed for backtesting
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df_vault.columns:
            preds_df[col] = df_vault[col].values

    # Add ATR if available
    atr_cols = [c for c in df_vault.columns if c.startswith("VOL_ATR")]
    if atr_cols:
        preds_df["ATR_14"] = df_vault[atr_cols[0]].values

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    preds_df.to_csv(output_path)
    print(f"  OOS predictions: {output_path} ({len(preds_df):,} rows)")
    return preds_df


# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------


def _normalize_exec_index(exec_df: pd.DataFrame, source_path: str) -> pd.DataFrame:
    """Ensure the execution frame is DatetimeIndex-ed.

    Non-CL <SYM>_raw.parquet files carry an int64 RangeIndex with DateTime as a
    column; reindexing that against the signals' DatetimeIndex silently yields
    all-NaN exec columns -> 0 trades / $0 in every baseline backtest (the
    ES/ZC zero-metrics bug). Raise on anything unresolvable — no silent
    fallback.
    """
    if isinstance(exec_df.index, pd.DatetimeIndex):
        return exec_df
    if "DateTime" in exec_df.columns:
        exec_df = exec_df.set_index(pd.to_datetime(exec_df["DateTime"])).drop(columns=["DateTime"])
        exec_df.index.name = "DateTime"
        print(f"  Exec data index normalized from 'DateTime' column: "
              f"{exec_df.index.min()} -> {exec_df.index.max()} ({source_path})")
        return exec_df
    # Only string/object indexes are safe to coerce: pd.to_datetime on an
    # int64 index "succeeds" as nanoseconds-since-1970 — garbage dates that
    # reintroduce the silent all-NaN reindex.
    if exec_df.index.dtype == object:
        try:
            exec_df.index = pd.to_datetime(exec_df.index)
            print(f"  Exec data index coerced to DatetimeIndex: "
                  f"{exec_df.index.min()} -> {exec_df.index.max()} ({source_path})")
            return exec_df
        except (ValueError, TypeError, OverflowError):
            pass
    raise ValueError(
        f"Execution data at {source_path} has neither a DatetimeIndex nor a "
        f"'DateTime' column (index dtype: {exec_df.index.dtype}). Refusing to "
        f"reindex against a DatetimeIndex — that silently produces all-NaN "
        f"exec columns and zeroed backtests."
    )


def run_backtest(
    predictions_df: pd.DataFrame,
    strategy_cfg: dict,
    direction: str,
    report_path: str,
    exec_data_path: str | None = None,
    overrides: dict | None = None,
) -> dict:
    """Run BacktestEngine on OOS predictions and save report."""
    from agent.backtest_engine import BacktestEngine

    overrides = overrides or {}
    engine = BacktestEngine.from_config(strategy_cfg, **overrides)

    # Build signal columns for the engine
    signals = predictions_df.copy()

    if exec_data_path:
        if str(exec_data_path).endswith('.parquet'):
            exec_df = pd.read_parquet(exec_data_path)
        else:
            exec_df = pd.read_csv(exec_data_path, index_col=0, parse_dates=True)
        exec_df = _normalize_exec_index(exec_df, exec_data_path)
    else:
        exec_df = predictions_df

    result = engine.run(signals, exec_df)

    # Compile report
    report = {
        "direction": direction,
        "trade_count": result.trade_count,
        "total_pnl": round(result.total_pnl, 2),
        "win_rate": round(result.win_rate * 100, 2),
        "profit_factor": round(result.profit_factor, 4),
        "max_drawdown": round(result.max_drawdown, 2),
        "exit_distribution": result.exit_distribution,
    }

    # Write human-readable report
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w") as f:
        f.write(f"{'='*60}\n")
        f.write(f"BACKTEST REPORT — {direction.upper()}\n")
        f.write(f"{'='*60}\n\n")
        f.write(f"Strategy Config: {strategy_cfg.get('nickname', 'unknown')}\n")
        f.write(f"TP/SL ATR Mult:  {strategy_cfg.get('tp_atr_mult')}/{strategy_cfg.get('sl_atr_mult')}\n")
        f.write(f"Consec. Signal:  {strategy_cfg.get('consecutive_signal_threshold', 0)}\n\n")
        f.write(f"Trade Count:     {report['trade_count']}\n")
        f.write(f"Total PnL:       ${report['total_pnl']:,.2f}\n")
        f.write(f"Win Rate:        {report['win_rate']:.1f}%\n")
        f.write(f"Profit Factor:   {report['profit_factor']:.4f}\n")
        f.write(f"Max Drawdown:    ${report['max_drawdown']:,.2f}\n\n")
        f.write(f"Exit Distribution:\n")
        for reason, data in report.get("exit_distribution", {}).items():
            f.write(f"  {reason}: {data['count']} ({data['pct']:.1f}%)\n")

    print(f"  Backtest report: {report_path}")
    print(f"    Trades: {report['trade_count']}, WR: {report['win_rate']:.1f}%, "
          f"PF: {report['profit_factor']:.4f}, PnL: ${report['total_pnl']:,.2f}")

    return report


# ---------------------------------------------------------------------------
# Registry bundle creation
# ---------------------------------------------------------------------------


def create_registry_bundle(
    bundle_dir: str,
    model: lgb.Booster,
    model_path: str,
    predictions_path: str,
    backtest_report_path: str,
    params: dict,
    metric_name: str,
    target_name: str,
    direction: str,
    data_path: str,
    train_cutoff_date: str,
    study_name: str,
) -> str:
    """Create a registry-compatible bundle directory."""

    os.makedirs(bundle_dir, exist_ok=True)

    # Copy model
    shutil.copy2(model_path, os.path.join(bundle_dir, "final_model.pkl"))

    # Copy predictions
    shutil.copy2(predictions_path, os.path.join(bundle_dir, "oos_predictions.csv"))

    # Copy backtest report
    shutil.copy2(backtest_report_path, os.path.join(bundle_dir, "backtest_report.txt"))

    # Create experiment_config.json
    exp_config = {
        "experiment_id": os.path.basename(bundle_dir),
        "strategy": f"e2e_alpha_factory_{direction}_{metric_name}",
        "hypothesis": f"E2E Alpha Factory: {direction} model optimized for {metric_name} on set_09",
        "data_path": data_path,
        "target_name": target_name,
        "method": "walk_forward",
        "balance_mode": "downsample",
        "train_cutoff_date": train_cutoff_date,
        "model_params": params,
        "optuna_provenance": {
            "source": f"Optuna study: {study_name}",
            "note": "E2E Alpha Factory production run on set_09 (MACRO lookahead fix)",
        },
    }
    with open(os.path.join(bundle_dir, "experiment_config.json"), "w") as f:
        json.dump(exp_config, f, indent=2, default=str)

    # Create config.json (full metadata)
    config = {
        "experiment_id": os.path.basename(bundle_dir),
        "strategy": f"e2e_alpha_factory_{direction}_{metric_name}",
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "target_name": target_name,
        "type": direction.capitalize(),
        "dataset_path": data_path,
        "model_params": params,
    }
    with open(os.path.join(bundle_dir, "config.json"), "w") as f:
        json.dump(config, f, indent=2, default=str)

    # Feature importance
    try:
        fi = model.feature_importance(importance_type="gain")
        fi_names = model.feature_name()
        fi_df = pd.DataFrame({"feature": fi_names, "importance": fi})
        fi_df = fi_df.sort_values("importance", ascending=False)
        fi_df.to_csv(os.path.join(bundle_dir, "feature_importance.csv"), index=False)
    except Exception:
        pass

    print(f"  Registry bundle: {bundle_dir}/")
    return bundle_dir


# ---------------------------------------------------------------------------
# Artifact packaging
# ---------------------------------------------------------------------------


def package_artifacts(output_dir: str, zip_path: str) -> str:
    """Zip all production artifacts."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(output_dir):
            for f in files:
                file_path = os.path.join(root, f)
                arcname = os.path.relpath(file_path, os.path.dirname(output_dir))
                zf.write(file_path, arcname)

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"  Packaged: {zip_path} ({size_mb:.1f} MB)")
    return zip_path


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    data_path: str,
    train_cutoff_date: str,
    strategy_config_path: str,
    symbol: str = "CL",
    db_dir: str = "models/optuna_studies",
    output_dir: str = "production_output",
    gcs_bucket: str = "gs://cltrainer-optuna-results",
    gcs_prefix: str = "",
    shutdown_after: bool = False,
    metrics: list[str] | None = None,
    targets: list[str] | None = None,
    study_prefix: str | None = None,
    holdout_cutoff_date: str | None = None,
    opt_trials: int = 100,
    exec_data_path: str | None = None,
    slippage_per_side: float | None = None,
    random_seed: int = 42,
):
    """Run the full E2E pipeline."""

    start_time = time.perf_counter()

    # Reproducibility: seed the process-level numpy RNG at this entrypoint.
    np.random.seed(random_seed)

    if metrics is None:
        metrics = ["logloss", "average_precision"]

    if targets is None:
        targets = [
            ("TARGET_TRIPLE_2x1_24H_LONG", "long"),
            ("TARGET_TRIPLE_2x1_24H_SHORT", "short"),
        ]
    else:
        targets = [(t, "long" if t.endswith("_LONG") else "short") for t in targets]

    print("=" * 70)
    print("[E2E] ALPHA FACTORY PIPELINE")
    print("=" * 70)
    print(f"  Data:        {data_path}")
    print(f"  Cutoff:      {train_cutoff_date}")
    print(f"  Strategy:    {strategy_config_path}")
    print(f"  Metrics:     {metrics}")
    print(f"  Targets:     {[t[0] for t in targets]}")
    print(f"  Output:      {output_dir}")
    print("=" * 70)

    # ---- Load strategy config ----
    with open(strategy_config_path) as f:
        strategy_cfg = json.load(f)
    print(f"\nStrategy: {strategy_cfg.get('nickname', 'unknown')}")
    print(f"  TP/SL: {strategy_cfg.get('tp_atr_mult')}/{strategy_cfg.get('sl_atr_mult')} ATR")
    print(f"  Consecutive Signal: {strategy_cfg.get('consecutive_signal_threshold', 0)}")

    # ---- Load data ----
    print("\n[1/5] Loading data...")
    df = pd.read_parquet(data_path)
    feature_cols = util.get_feature_columns(df)
    
    
    cutoff = pd.Timestamp(train_cutoff_date)

    if holdout_cutoff_date is None:
        holdout_cutoff = cutoff + pd.DateOffset(years=1)
    else:
        holdout_cutoff = pd.Timestamp(holdout_cutoff_date)

    assert cutoff < holdout_cutoff, f"train_cutoff_date ({cutoff.date()}) must be before holdout_cutoff_date ({holdout_cutoff.date()})"

    # --- Data split logic ---
    # DEFAULT (2-way): Train = all data before cutoff. Vault = all data >= cutoff.
    # OPT-IN (3-way): Only activated when --holdout-cutoff-date is explicitly passed.
    #   Train = before cutoff. Val = [cutoff, holdout_cutoff). Vault = >= holdout_cutoff.
    if holdout_cutoff_date is None:
        # Original 2-way split — maximises training data, vault = full OOS
        df_train = df[df.index < cutoff].copy()
        df_val = None
        df_vault = df[df.index >= cutoff].copy()
        print(f"  Split mode:  2-way (default)")
        print(f"  Train:   {len(df_train):,} rows -> {df_train.index.max().date()}")
        print(f"  Vault:   {len(df_vault):,} rows -> {df_vault.index.max().date()}")
    else:
        # Explicit 3-way split — execution optimizer runs on val, backtest on vault
        df_train = df[df.index < cutoff].copy()
        df_val = df[(df.index >= cutoff) & (df.index < holdout_cutoff)].copy()
        df_vault = df[df.index >= holdout_cutoff].copy()
        print(f"  Split mode:  3-way (holdout_cutoff_date={holdout_cutoff.date()})")
        print(f"  Train:   {len(df_train):,} rows -> {df_train.index.max().date()}")
        print(f"  Val:     {len(df_val):,} rows -> {df_val.index.max().date()}")
        print(f"  Holdout: {len(df_vault):,} rows -> {df_vault.index.max().date()}")
    print(f"  Features: {len(feature_cols)}")

    # Drop rows with NaN targets from all active splits
    for target_name, _ in targets:
        target_col = util.get_target_column(df, target_name)
        df_train = df_train.dropna(subset=[target_col])
        if df_val is not None:
            df_val = df_val.dropna(subset=[target_col])
        df_vault = df_vault.dropna(subset=[target_col])
    print(f"  Train (clean): {len(df_train):,} rows")
    if df_val is not None:
        print(f"  Val (clean):   {len(df_val):,} rows")
    print(f"  Vault (clean): {len(df_vault):,} rows")

    os.makedirs(output_dir, exist_ok=True)
    all_bundles = []
    all_reports = {}

    # ---- Process each target × metric combination ----
    for target_name, direction in targets:
        target_col = util.get_target_column(df, target_name)

        for metric_name in metrics:
            combo_name = f"{direction}_{metric_name}"
            print(f"\n{'='*60}")
            print(f"PROCESSING: {combo_name.upper()}")
            print(f"{'='*60}")

            # Step 2: Extract best params from Optuna study
            print(f"\n[2/5] Extracting best params for {combo_name}...")

            # Build list of candidate study names (most specific -> least specific)
            candidate_names = []
            if study_prefix:
                # Canary/prefixed runs use: prefix_direction_metric
                candidate_names.append(f"{study_prefix}_{direction}_{metric_name}")
                # Also try the prefix alone (legacy)
                candidate_names.append(study_prefix)
            candidate_names.extend([
                f"e2e_{direction}_{metric_name}",
                f"wf_v2_{direction}_{metric_name}",
                f"production_{direction}_{metric_name}",
            ])

            # Try each candidate — check .journal first (new), then .db (legacy)
            db_path = None
            study_name = None
            for name in candidate_names:
                for ext in [".journal", ".db"]:
                    path = os.path.join(db_dir, f"{name}{ext}")
                    if os.path.exists(path):
                        db_path = path
                        study_name = name
                        break
                if db_path:
                    break

            # Ultimate fallback: scan directory for any .journal/.db containing direction and metric
            if db_path is None:
                for ext in [".journal", ".db"]:
                    for f_name in os.listdir(db_dir):
                        if f_name.endswith(ext) and direction in f_name and metric_name in f_name:
                            db_path = os.path.join(db_dir, f_name)
                            study_name = f_name.replace(ext, "")
                            print(f"  Found via directory scan: {f_name}")
                            break
                    if db_path:
                        break

            if db_path is None:
                print(f"  WARNING: No study DB found for {combo_name}, skipping.")
                print(f"    Searched names: {candidate_names}")
                print(f"    Directory: {db_dir}")
                continue

            params = extract_best_params(db_path, study_name)

            # Step 3: Train final model
            print(f"\n[3/5] Training final model for {combo_name}...")
            model_filename = f"final_{direction}_model_{metric_name}.pkl"
            model_path = os.path.join(output_dir, model_filename)
            model = train_final_model(
                df_train=df_train,
                feature_cols=feature_cols,
                target_col=target_col,
                params=params,
                balance_mode="downsample",
                output_path=model_path,
                random_seed=random_seed,
            )

            fmt_prefix = study_prefix if study_prefix else "baseline"
            if fmt_prefix and fmt_prefix.startswith("scout_hs"):
                parts = fmt_prefix.split("_", 2)
                if len(parts) >= 3:
                    fmt_prefix = f"{parts[1]}_{parts[0]}_{parts[2]}"

            # Step 4: Generate predictions
            # -- Validation predictions (only in 3-way split mode) --
            if df_val is not None:
                print(f"\n[4a/5] Generating Validation predictions for {combo_name}...")
                val_preds_path = os.path.join(output_dir, f"val_predictions_{fmt_prefix}_{combo_name}.csv")
                generate_oos_predictions(
                    model=model,
                    df_vault=df_val,
                    feature_cols=feature_cols,
                    target_col=target_col,
                    direction=direction,
                    output_path=val_preds_path,
                )
                print(f"       Generating Holdout/OOS predictions for {combo_name}...")
            else:
                print(f"\n[4a/5] Generating OOS predictions for {combo_name}...")

            preds_path = os.path.join(output_dir, f"oos_predictions_{fmt_prefix}_{combo_name}.csv")
            preds_df = generate_oos_predictions(
                model=model,
                df_vault=df_vault,
                feature_cols=feature_cols,
                target_col=target_col,
                direction=direction,
                output_path=preds_path,
            )

            # Step 4b: Run backtest
            print(f"\n[4b/5] Running backtest for {combo_name}...")
            bt_report_path = os.path.join(output_dir, f"backtest_report_{combo_name}.txt")
            overrides = {}
            if slippage_per_side is not None:
                overrides["slippage_per_side"] = slippage_per_side
            # Per-symbol economics: baseline metrics feed pair selection —
            # they must use the instrument's dollars-per-point, not CL's 1000.
            from src.core.instrument_master import dollars_per_point
            overrides["contract_multiplier"] = dollars_per_point(symbol)

            report = run_backtest(
                predictions_df=preds_df,
                strategy_cfg=strategy_cfg,
                direction=direction,
                report_path=bt_report_path,
                exec_data_path=exec_data_path,
                overrides=overrides,
            )
            all_reports[combo_name] = report

            # Derive dataset tag from data filename via the shared helper
            # (strips legacy "bk_" / modern "{symbol}_" prefixes — T6 single source)
            data_basename = os.path.splitext(os.path.basename(data_path))[0]
            dataset_tag = derive_dataset_tag(data_basename, symbol)

            bundle_name = f"E2E_{dataset_tag}_{direction}_{metric_name}"
            bundle_dir = os.path.join(output_dir, "registry", bundle_name)
            create_registry_bundle(
                bundle_dir=bundle_dir,
                model=model,
                model_path=model_path,
                predictions_path=preds_path,
                backtest_report_path=bt_report_path,
                params=params,
                metric_name=metric_name,
                target_name=target_name,
                direction=direction,
                data_path=data_path,
                train_cutoff_date=train_cutoff_date,
                study_name=study_name,
            )
            all_bundles.append(bundle_dir)

    # ---- Summary of individual backtests ----
    elapsed = time.perf_counter() - start_time
    print(f"\n{'='*70}")
    print(f"INDIVIDUAL BACKTESTS COMPLETE - {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"{'='*70}")
    print(f"\nPer-Model Backtest Summary:")
    print(f"{'-'*60}")
    print(f"{'Model':<30} {'Trades':>7} {'WR':>6} {'PF':>8} {'PnL':>12}")
    print(f"{'-'*60}")
    for name, rpt in all_reports.items():
        print(f"{name:<30} {rpt['trade_count']:>7} {rpt['win_rate']:>5.1f}% "
              f"{rpt['profit_factor']:>8.4f} ${rpt['total_pnl']:>10,.2f}")
    print(f"{'-'*60}")

    # ---- Step 4c: Ensemble backtest (combine long + short per metric) ----
    # Group OOS predictions by metric so we can create ensemble configs
    ensemble_reports = {}
    preds_by_metric = {}  # metric -> {direction -> predictions_path}
    val_preds_by_metric = {}
    for target_name, direction in targets:
        for metric_name in metrics:
            combo_name = f"{direction}_{metric_name}"
            
            fmt_prefix = study_prefix if study_prefix else "baseline"
            if fmt_prefix and fmt_prefix.startswith("scout_hs"):
                parts = fmt_prefix.split("_", 2)
                if len(parts) >= 3:
                    fmt_prefix = f"{parts[1]}_{parts[0]}_{parts[2]}"
                    
            preds_path = os.path.join(output_dir, f"oos_predictions_{fmt_prefix}_{combo_name}.csv")
            val_preds_path = os.path.join(output_dir, f"val_predictions_{fmt_prefix}_{combo_name}.csv")
            if os.path.exists(preds_path):
                if metric_name not in preds_by_metric:
                    preds_by_metric[metric_name] = {}
                    val_preds_by_metric[metric_name] = {}
                preds_by_metric[metric_name][direction] = preds_path
                val_preds_by_metric[metric_name][direction] = val_preds_path

    # For each metric that has both long + short, create an ensemble config and run combined backtest
    for metric_name, direction_paths in preds_by_metric.items():
        if "long" not in direction_paths or "short" not in direction_paths:
            print(f"\n  Skipping ensemble for {metric_name} — missing {'long' if 'long' not in direction_paths else 'short'} model")
            continue

        print(f"\n{'='*60}")
        print(f"[4c/5] ENSEMBLE BACKTEST — {metric_name.upper()}")
        print(f"{'='*60}")

        # Create ensemble config from the base strategy config
        ensemble_cfg = dict(strategy_cfg)
        # T6: stamp the run symbol — kills the CL-base execution_symbol leak
        # into sweep_{metric}.json and TopKTracker candidates (value-preserving
        # for CL runs, where symbol == "CL" == the base config's value).
        ensemble_cfg["execution_symbol"] = symbol
        # Derive dataset tag via the shared helper (T6 single source)
        data_basename = os.path.splitext(os.path.basename(data_path))[0]
        dataset_tag = derive_dataset_tag(data_basename, symbol)

        ensemble_cfg["models"] = {
            "long": {
                "symbol": symbol,
                "experiment_id": f"E2E_{dataset_tag}_long_{metric_name}",
                "predictions_path": f"data/predictions/{os.path.basename(direction_paths['long'])}",
                "threshold": strategy_cfg.get("models", {}).get("long", {}).get("threshold", 0.60),
            },
            "short": {
                "symbol": symbol,
                "experiment_id": f"E2E_{dataset_tag}_short_{metric_name}",
                "predictions_path": f"data/predictions/{os.path.basename(direction_paths['short'])}",
                "threshold": strategy_cfg.get("models", {}).get("short", {}).get("threshold", 0.60),
            },
        }
        ensemble_cfg["nickname"] = f"E2E_{dataset_tag}_{metric_name}_ensemble"

        ohlcv = pd.read_parquet(data_path) # Load here since optimizer and backtest needs it

        # Auto-optimize strategy parameters on Validation set (3-way split mode only)
        val_preds_exist = (
            metric_name in val_preds_by_metric
            and "long" in val_preds_by_metric[metric_name]
            and "short" in val_preds_by_metric[metric_name]
            and os.path.exists(val_preds_by_metric[metric_name]["long"])
            and os.path.exists(val_preds_by_metric[metric_name]["short"])
        )
        if opt_trials > 0 and val_preds_exist:
            val_long_df = pd.read_csv(val_preds_by_metric[metric_name]["long"], index_col=0, parse_dates=True)
            val_short_df = pd.read_csv(val_preds_by_metric[metric_name]["short"], index_col=0, parse_dates=True)

            val_long_prob_col = [c for c in val_long_df.columns if "buy" in c.lower() or "Buy" in c][0]
            val_short_prob_col = [c for c in val_short_df.columns if "sell" in c.lower() or "Sell" in c][0]
            
            val_long_probs = val_long_df[[val_long_prob_col]].rename(columns={val_long_prob_col: "prob_Buy"})
            val_short_probs = val_short_df[[val_short_prob_col]].rename(columns={val_short_prob_col: "prob_Sell"})
            val_preds_merged = val_long_probs.join(val_short_probs, how="outer").fillna(0.0)

            val_merged_path = os.path.join(output_dir, f"_val_merged_{metric_name}.csv")
            val_preds_merged.to_csv(val_merged_path)

            # Write temporary ensemble config
            cfg_filename = f"{fmt_prefix}_{metric_name}.json"
            ensemble_cfg_path = os.path.join(output_dir, cfg_filename)
            
            # Use absolute paths for the VM run so the optimizer doesn't fail
            import copy
            temp_cfg = copy.deepcopy(ensemble_cfg)
            if "long" in temp_cfg and "models" in temp_cfg["long"]:
                temp_cfg["long"]["models"][0]["predictions_path"] = direction_paths['long']
                temp_cfg["short"]["models"][0]["predictions_path"] = direction_paths['short']
            else:
                temp_cfg["models"]["long"]["predictions_path"] = direction_paths['long']
                temp_cfg["models"]["short"]["predictions_path"] = direction_paths['short']

            with open(ensemble_cfg_path, "w") as f:
                json.dump(temp_cfg, f, indent=2)

            from agent.strategy_optimizer import run_optimization
            print(f"\n  Running autonomous execution optimization ({opt_trials} trials)...")
            best_cfg, best_result = run_optimization(
                config_path=ensemble_cfg_path,
                n_trials=opt_trials,
                predictions_path=val_merged_path,
                ohlcv_path=data_path,
                exec_ohlcv_path=exec_data_path,
                slippage_per_side=slippage_per_side
            )

            long_preds_rel = f"data/predictions/{os.path.basename(direction_paths['long'])}"
            short_preds_rel = f"data/predictions/{os.path.basename(direction_paths['short'])}"

            # Handle Top-K Candidates
            candidates_dir = "configs/strategies/candidates"
            if os.path.exists(candidates_dir):
                for filename in os.listdir(candidates_dir):
                    if filename.endswith(".json"):
                        filepath = os.path.join(candidates_dir, filename)
                        with open(filepath, 'r') as f:
                            candidate_cfg = json.load(f)
                        
                        # Re-inject relative paths for Windows compatibility
                        if "long" in candidate_cfg and "models" in candidate_cfg["long"]:
                            candidate_cfg["long"]["models"][0]["predictions_path"] = long_preds_rel
                        if "short" in candidate_cfg and "models" in candidate_cfg["short"]:
                            candidate_cfg["short"]["models"][0]["predictions_path"] = short_preds_rel
                        
                        if "models" in candidate_cfg:
                            if "long" in candidate_cfg["models"]:
                                candidate_cfg["models"]["long"]["predictions_path"] = long_preds_rel
                            if "short" in candidate_cfg["models"]:
                                candidate_cfg["models"]["short"]["predictions_path"] = short_preds_rel

                        # Save into output_dir with metric_name in the filename
                        final_name = f"{fmt_prefix}_{metric_name}_{filename}"
                        final_dest = os.path.join(output_dir, final_name)
                        with open(final_dest, 'w') as f:
                            json.dump(candidate_cfg, f, indent=4)
                
                shutil.rmtree(candidates_dir)

            # Clean up the single _opt files generated by the optimizer
            if is_tiered := (best_cfg.get("execution_class") == "TieredEnsembleStrategy"):
                for side in ["long", "short"]:
                    side_cfg_path = ensemble_cfg_path.replace(".json", f"_opt_{side}.json")
                    if os.path.exists(side_cfg_path): os.remove(side_cfg_path)
            if os.path.exists(ensemble_cfg_path.replace(".json", "_opt.json")):
                os.remove(ensemble_cfg_path.replace(".json", "_opt.json"))

            # Save the unoptimized base config for the backtest baseline
            with open(ensemble_cfg_path, "w") as f:
                json.dump(ensemble_cfg, f, indent=2)

            print(f"  Ensemble base config: {ensemble_cfg_path}")
            print(f"    Long:  {direction_paths['long']}")
            print(f"    Short: {direction_paths['short']}")
            
            ensemble_cfg = best_cfg
            # Note: We continue to run the baseline backtest with the unoptimized config
            # because the optimizer has already provided the optimized metrics. We keep
            # the standard logging.

        else:
            cfg_filename = f"{fmt_prefix}_{metric_name}.json"
            ensemble_cfg_path = os.path.join(output_dir, cfg_filename)
            with open(ensemble_cfg_path, "w") as f:
                json.dump(ensemble_cfg, f, indent=2, default=str)
            print(f"  Ensemble config: {ensemble_cfg_path}")
            print(f"    Long:  {direction_paths['long']}")
            print(f"    Short: {direction_paths['short']}")

        # Run combined backtest via BacktestEngine
        try:
            from agent.backtest_engine import BacktestEngine, format_report

            overrides = {}
            if slippage_per_side is not None:
                overrides["slippage_per_side"] = slippage_per_side
            from src.core.instrument_master import dollars_per_point
            overrides["contract_multiplier"] = dollars_per_point(symbol)

            bt = BacktestEngine.from_config(ensemble_cfg, **overrides)

            # Load and merge long/short OOS predictions directly from CSVs
            long_df = pd.read_csv(direction_paths["long"], index_col=0, parse_dates=True)
            short_df = pd.read_csv(direction_paths["short"], index_col=0, parse_dates=True)

            # Extract prob columns and merge
            long_prob_col = [c for c in long_df.columns if "buy" in c.lower() or "Buy" in c]
            short_prob_col = [c for c in short_df.columns if "sell" in c.lower() or "Sell" in c]
            if not long_prob_col or not short_prob_col:
                print(f"  ✗ Cannot find prob columns: long={list(long_df.columns)}, short={list(short_df.columns)}")
                continue
            long_probs = long_df[[long_prob_col[0]]].rename(columns={long_prob_col[0]: "prob_Buy"})
            short_probs = short_df[[short_prob_col[0]]].rename(columns={short_prob_col[0]: "prob_Sell"})
            preds = long_probs.join(short_probs, how="outer").fillna(0.0)
            print(f"    Merged: {len(preds):,} rows ({preds['prob_Buy'].gt(0).sum():,} buy, {preds['prob_Sell'].gt(0).sum():,} sell)")

            # Run backtest
            if exec_data_path:
                if str(exec_data_path).endswith('.parquet'):
                    exec_ohlcv = pd.read_parquet(exec_data_path)
                else:
                    exec_ohlcv = pd.read_csv(exec_data_path, index_col=0, parse_dates=True)
                exec_ohlcv = _normalize_exec_index(exec_ohlcv, exec_data_path)
            else:
                exec_ohlcv = ohlcv

            result = bt.run(preds, exec_ohlcv, label=f"Ensemble ({metric_name})")

            # Generate report
            ensemble_report_path = os.path.join(output_dir, f"ensemble_backtest_{metric_name}.txt")
            report_text = format_report(
                result,
                config=ensemble_cfg,
                predictions_path=f"long: {direction_paths['long']}, short: {direction_paths['short']}",
                data_path=data_path,
            )
            with open(ensemble_report_path, "w", encoding="utf-8") as rf:
                rf.write(report_text + "\n")

            ens_report = {
                "direction": "ensemble",
                "metric": metric_name,
                "trade_count": result.trade_count,
                "total_pnl": round(result.total_pnl, 2),
                "win_rate": round(result.win_rate * 100, 2),
                "profit_factor": round(result.profit_factor, 4),
                "max_drawdown": round(result.max_drawdown, 2),
            }
            ensemble_reports[f"ensemble_{metric_name}"] = ens_report
            all_reports[f"ensemble_{metric_name}"] = ens_report

            print(f"    Trades: {result.trade_count}, WR: {result.win_rate*100:.1f}%, "
                  f"PF: {result.profit_factor:.4f}, PnL: ${result.total_pnl:,.2f}")
            print(f"  Report: {ensemble_report_path}")

        except Exception as e:
            print(f"  ✗ Ensemble backtest failed for {metric_name}: {e}")
            import traceback
            traceback.print_exc()

    # ---- Final summary including ensembles ----
    elapsed = time.perf_counter() - start_time
    print(f"\n{'='*70}")
    print(f"E2E PIPELINE COMPLETE - {elapsed/60:.1f} min")
    print(f"{'='*70}")
    print(f"\nFull Backtest Summary (Individual + Ensemble):")
    print(f"{'-'*60}")
    print(f"{'Model':<30} {'Trades':>7} {'WR':>6} {'PF':>8} {'PnL':>12}")
    print(f"{'-'*60}")
    for name, rpt in all_reports.items():
        print(f"{name:<30} {rpt['trade_count']:>7} {rpt['win_rate']:>5.1f}% "
              f"{rpt['profit_factor']:>8.4f} ${rpt['total_pnl']:>10,.2f}")
    print(f"{'-'*60}")

    # Save summary JSON
    summary_path = os.path.join(output_dir, "pipeline_summary.json")
    with open(summary_path, "w") as f:
        json.dump({
            "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
            "data_path": data_path,
            "train_cutoff_date": train_cutoff_date,
            "strategy_config": strategy_config_path,
            "wall_time_seconds": round(elapsed, 1),
            "backtest_results": all_reports,
            "registry_bundles": all_bundles,
        }, f, indent=2, default=str)

    # ---- Step 5: Package and upload ----
    print(f"\n[5/5] Packaging artifacts...")

    # Copy study DBs into output
    studies_dest = os.path.join(output_dir, "optuna_studies")
    os.makedirs(studies_dest, exist_ok=True)
    for f_name in os.listdir(db_dir):
        if f_name.endswith(".db"):
            src = os.path.join(db_dir, f_name)
            shutil.copy2(src, os.path.join(studies_dest, f_name))

    # Copy strategy config
    shutil.copy2(strategy_config_path, os.path.join(output_dir, "strategy_config.json"))

    # Zip everything
    zip_path = os.path.join(os.path.dirname(output_dir), "production_artifacts.zip")
    package_artifacts(output_dir, zip_path)

    # Upload to GCS
    gcs_dest = f"{gcs_bucket}/{gcs_prefix}/production" if gcs_prefix else f"{gcs_bucket}/production"
    zip_name = f"{gcs_prefix}_artifacts.zip" if gcs_prefix else "production_artifacts.zip"
    gcs_root_dest = f"{gcs_bucket}/{gcs_prefix}" if gcs_prefix else gcs_bucket
    
    print(f"\n  Uploading to {gcs_dest}/...")
    
    is_sandbox = os.environ.get("IS_LOCAL_SANDBOX", "False").lower() == "true"
    if is_sandbox:
        def mock_gcs_path(gcs_uri: str) -> str:
            if not gcs_uri.startswith("gs://"): return gcs_uri
            parts = gcs_uri[5:].split("/", 1)
            path = parts[1] if len(parts) > 1 else ""
            mock_base = os.path.join(os.getcwd(), "tests", "sandbox", "mock_gcs")
            final_path = os.path.join(mock_base, path)
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            return final_path
            
        try:
            zip_mock_dest = mock_gcs_path(f"{gcs_dest}/{zip_name}")
            shutil.copy(zip_path, zip_mock_dest)
            
            summary_mock_dest = mock_gcs_path(f"{gcs_root_dest}/pipeline_summary.json")
            shutil.copy(summary_path, summary_mock_dest)
            
            print(f"  ✓ Copied to Sandbox Mock GCS")
        except Exception as e:
            print(f"  ✗ Sandbox mock copy failed: {e}")
    else:
        try:
            subprocess.run(
                ["gsutil", "cp", zip_path, f"{gcs_dest}/{zip_name}"],
                check=True,
                capture_output=True,
                text=True,
            )
            # Also upload pipeline_summary.json separately to the root prefix for the orchestrator
            subprocess.run(
                ["gsutil", "cp", summary_path, f"{gcs_root_dest}/pipeline_summary.json"],
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"  ✓ Uploaded to GCS")
        except Exception as e:
            print(f"  ✗ GCS upload failed: {e}")
            print(f"    Artifacts are still available locally at: {zip_path}")

    # Shutdown VM if requested
    if shutdown_after:
        print(f"\n  Shutting down VM...")
        try:
            subprocess.run(
                ["sudo", "shutdown", "-h", "now"],
                check=False,
            )
        except Exception:
            print("  (shutdown command sent)")

    print(f"\n{'='*70}")
    print("DONE. All artifacts saved.")
    print(f"  Local:  {output_dir}/")
    print(f"  Zip:    {zip_path}")
    print(f"  GCS:    {gcs_dest}/")
    print(f"{'='*70}")

    return all_reports


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="E2E Alpha Factory Pipeline — train final models + backtest after Optuna"
    )
    parser.add_argument("--master-config", required=True, help="Path to Master Config JSON (e.g., configs/sweeps/sweep_es_v1.json)")
    parser.add_argument("--db-dir", default="models/optuna_studies", help="Dir with Optuna .db files")
    parser.add_argument("--output-dir", default="production_output", help="Output directory")
    parser.add_argument("--gcs-bucket", default="gs://cltrainer-optuna-results", help="GCS bucket")
    parser.add_argument("--shutdown", action="store_true", help="Shutdown VM after completion")
    parser.add_argument(
        "--metrics", nargs="+",
        default=["logloss", "average_precision"],
        help="Metrics to process (default: logloss average_precision)",
    )
    parser.add_argument(
        "--study-prefix",
        default=None,
        help="Study name prefix used by Optuna search (for DB file matching)",
    )
    parser.add_argument(
        "--gcs-prefix",
        default="",
        help="GCS subfolder prefix (e.g. 'canary'). If set, uploads to bucket/prefix/ instead of bucket/",
    )
    parser.add_argument(
        "--opt-trials", type=int, default=100,
        help="Optuna trials for execution param optimization (0 to skip)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse config, resolve paths, and exit successfully without running pipeline",
    )
    parser.add_argument(
        "--random-seed", type=int, default=42,
        help="Shared random seed for reproducibility (downsampling, LGBM RNG, "
             "numpy). Default: 42.",
    )

    args = parser.parse_args()
    
    import json
    import os
    from pathlib import Path
    
    config_path = Path(args.master_config)
    if not config_path.exists():
        raise FileNotFoundError(f"MasterConfig not found at {config_path}")
        
    with open(config_path, "r") as f:
        raw_json = json.load(f)
        
    from src.config.schemas import MasterConfig
    try:
        master_config = MasterConfig(**raw_json)
    except Exception as e:
        raise ValueError(f"Configuration Validation Error:\n{e}")
        
    if not master_config.training_workflow:
        raise ValueError("MasterConfig must define a training_workflow for this script.")
    if not master_config.execution_workflow:
        raise ValueError("MasterConfig must define an execution_workflow for this script.")

    # Derive data path from data_workflow dataset_version
    from src.data_paths import get_data_root
    raw_version = master_config.data_workflow.dataset_version
    if raw_version.upper().startswith(master_config.symbol.upper()):
        dataset_name = f"{raw_version}.parquet"
    else:
        dataset_name = f"{master_config.symbol}_{raw_version}.parquet"
        
    data_path = str(get_data_root() / "processed" / dataset_name)
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"Dataset not found at derived path: {data_path}")

    # Resolve Slippage.
    # ABSOLUTE price-units per side, taken directly from the manifest (single source of
    # truth) — identical semantics to the post-optimizer stage. For CL, 0.01 == 1 tick.
    # (Historically this was base_ticks * slippage_multiplier, which disagreed with the
    # post-opt stage's raw-absolute use of the same field and caused the -$2.5M PnL bug.)
    from src.core.instrument_master import get_instrument
    instrument = get_instrument(master_config.symbol)
    one_tick = instrument.slippage_ticks * instrument.tick_size
    resolved_slippage = master_config.execution_workflow.slippage_per_side

    targets_arg = master_config.training_workflow.target_columns

    print(f"============================================================")
    print(f" E2E Pipeline (Parsed MasterConfig)")
    print(f"============================================================")
    print(f"  Symbol:     {master_config.symbol}")
    print(f"  Data Path:  {data_path}")
    print(f"  Strategy:   {master_config.execution_workflow.strategy_config_path}")
    print(f"  Targets:    {targets_arg}")
    print(f"  Slippage:   {resolved_slippage} points/side (absolute, from manifest slippage_per_side; 1 tick = {one_tick})")
    print(f"============================================================")

    if args.dry_run:
        print("Dry run successful. Exiting.")
        return

    run_pipeline(
        data_path=str(data_path),
        train_cutoff_date=master_config.training_workflow.train_cutoff_date,
        strategy_config_path=master_config.execution_workflow.strategy_config_path,
        symbol=master_config.symbol,
        db_dir=args.db_dir,
        output_dir=args.output_dir,
        gcs_bucket=args.gcs_bucket,
        gcs_prefix=args.gcs_prefix,
        shutdown_after=args.shutdown,
        metrics=args.metrics,
        targets=targets_arg,
        study_prefix=args.study_prefix,
        holdout_cutoff_date=master_config.training_workflow.holdout_cutoff_date,
        opt_trials=args.opt_trials,
        exec_data_path=master_config.execution_workflow.execution_data_path,
        slippage_per_side=resolved_slippage,
        random_seed=args.random_seed,
    )


if __name__ == "__main__":
    main()
