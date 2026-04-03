"""
HourSet_03 Local Retrain — Frozen Hyperparameters
Uses champion hyperparams from HourSet_02 PKLs to retrain on HourSet_03.
Generates new oos_predictions.csv for both long and short models.
"""

import json
import os
import sys
import numpy as np
import pandas as pd
import joblib
import lightgbm as lgb
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import src.util as util

# ---------------------------------------------------------------------------
# Focal loss (must match vm_e2e_pipeline.py exactly)
# ---------------------------------------------------------------------------
FOCAL_GAMMA = 2.0

def _sigmoid(x):
    x = np.clip(np.asarray(x, dtype=float), -60, 60)
    return 1.0 / (1.0 + np.exp(-x))

def focal_obj(preds, train_set):
    labels = train_set.get_label().astype(int)
    p = _sigmoid(preds)
    p_t = np.where(labels == 1, p, 1 - p)
    grad = (p - labels) * ((1 - p_t) ** FOCAL_GAMMA)
    hess = (p * (1 - p)) * ((1 - p_t) ** FOCAL_GAMMA)
    return grad, hess

def focal_eval(preds, val_set):
    labels = val_set.get_label().astype(int)
    p = _sigmoid(preds)
    p_t = np.where(labels == 1, p, 1 - p)
    loss = -((1 - p_t) ** FOCAL_GAMMA) * np.log(np.clip(p_t, 1e-7, 1.0))
    return "focal_loss", float(np.mean(loss)), False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_PATH = r"C:\CL_Analyst_Data\data\processed\cl-1h_bk_HourSet_03.parquet"
TRAIN_CUTOFF = pd.Timestamp("2022-01-01")
OUTPUT_BASE = "reports/canary/registry/canary_output/registry"

MODELS = {
    "long": {
        "experiment_id": "E2E_HourSet_03_long_average_precision",
        "target_name": "TARGET_TRIPLE_2p5x1_72H_LONG",
        "direction": "long",
        "balance_mode": "downsample",
        "params": {
            "use_time": False, "use_volatility": False, "use_momentum": True,
            "use_trend": True, "use_microstructure": False, "use_structure": False,
            "use_distribution": True, "use_exhaustion": False, "use_divergence": True,
            "use_macro_tech": True, "use_macro_external": True,
            "boosting_type": "gbdt", "num_leaves": 21, "min_child_samples": 265,
            "learning_rate": 0.009955276692030826, "feature_fraction": 0.526107441454962,
            "reg_alpha": 0.24927639658941683, "reg_lambda": 3.1566927993570597,
            "max_depth": 3, "min_gain_to_split": 1.7600716820772384,
            "path_smooth": 2.6316283326309917, "bagging_fraction": 0.386118818529592,
            "bagging_freq": 1, "n_estimators": 500,
        },
    },
    "short": {
        "experiment_id": "E2E_HourSet_03_short_logloss",
        "target_name": "TARGET_TRIPLE_2p5x1_120H_SHORT",
        "direction": "short",
        "balance_mode": "downsample",
        "params": {
            "use_time": False, "use_volatility": False, "use_momentum": False,
            "use_trend": True, "use_microstructure": False, "use_structure": False,
            "use_distribution": False, "use_exhaustion": True, "use_divergence": False,
            "use_macro_tech": True, "use_macro_external": False,
            "boosting_type": "gbdt", "num_leaves": 27, "min_child_samples": 26,
            "learning_rate": 0.01984465430649546, "feature_fraction": 0.41990251470987794,
            "reg_alpha": 0.017790700565472058, "reg_lambda": 0.01680235345790802,
            "max_depth": 4, "min_gain_to_split": 0.6301821388193342,
            "path_smooth": 8.447928139251882, "bagging_fraction": 0.5584128761973873,
            "bagging_freq": 6, "n_estimators": 500,
        },
    },
}

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

log = []
log.append("=" * 70)
log.append("HourSet_03 Local Retrain (Frozen Hyperparameters)")
log.append("=" * 70)
log.append(f"Data: {DATA_PATH}")
log.append(f"Train cutoff: {TRAIN_CUTOFF.date()}")

df = pd.read_parquet(DATA_PATH)
feature_cols = util.get_feature_columns(df)
df_train_full = df[df.index < TRAIN_CUTOFF].copy()
df_vault_full = df[df.index >= TRAIN_CUTOFF].copy()

log.append(f"Total rows: {len(df):,}")
log.append(f"Train (pre-cutoff): {len(df_train_full):,} rows -> {df_train_full.index.max().date()}")
log.append(f"Vault (post-cutoff): {len(df_vault_full):,} rows -> {df_vault_full.index.max().date()}")
log.append(f"Feature columns: {len(feature_cols)}")
log.append("")

# ---------------------------------------------------------------------------
# Train each model
# ---------------------------------------------------------------------------

results = {}

for role, cfg in MODELS.items():
    log.append("=" * 70)
    log.append(f"TRAINING: {cfg['experiment_id']}")
    log.append("=" * 70)

    params = dict(cfg["params"])
    target_name = cfg["target_name"]
    direction = cfg["direction"]
    balance_mode = cfg["balance_mode"]
    n_estimators = params.pop("n_estimators", 500)

    # Resolve target column
    target_col = util.get_target_column(df, target_name)
    log.append(f"Target column: {target_col}")

    # Drop NaN targets
    df_train = df_train_full.dropna(subset=[target_col]).copy()
    df_vault = df_vault_full.dropna(subset=[target_col]).copy()
    log.append(f"Train clean: {len(df_train):,} rows")
    log.append(f"Vault clean: {len(df_vault):,} rows")

    # Filter features by feature subset flags
    # Use util to filter feature cols if it supports subset flags, else use all
    # The feature subset flags (use_momentum, etc.) are embedded in params —
    # filter features to match what the frozen model was trained on (199 features)
    # by passing the subset flags to util if supported
    try:
        feature_cols_filtered = util.get_feature_columns(df, params=params)
        log.append(f"Feature cols (filtered): {len(feature_cols_filtered)}")
    except TypeError:
        feature_cols_filtered = feature_cols
        log.append(f"Feature cols (all, no filter support): {len(feature_cols_filtered)}")

    X_train = df_train[feature_cols_filtered]
    y_train = df_train[target_col].astype(int)

    # Downsample majority class
    if balance_mode == "downsample":
        X_train, y_train = util.downsample_majority(X_train, y_train, random_state=42)
        log.append(f"After downsample: {len(X_train):,} rows (pos={y_train.sum()}, neg={(y_train==0).sum()})")

    # Train/val split for early stopping (last 10%)
    val_split = int(len(X_train) * 0.9)
    X_tr, X_val = X_train.iloc[:val_split], X_train.iloc[val_split:]
    y_tr, y_val = y_train.iloc[:val_split], y_train.iloc[val_split:]

    # Build LGB params (remove feature subset flags)
    subset_keys = [k for k in params if k.startswith("use_")]
    lgb_params = {k: v for k, v in params.items() if k not in subset_keys}
    lgb_params["verbose"] = -1
    lgb_params["num_threads"] = 8
    lgb_params["metric"] = "None"
    lgb_params["objective"] = focal_obj

    log.append(f"LGB params: {json.dumps({k:v for k,v in lgb_params.items() if k not in ['objective']}, default=str)}")
    log.append(f"n_estimators: {n_estimators}")

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
    log.append(f"Trained: {best_iter} iterations")
    log.append(f"Model features: {model.num_feature()}")

    # Save PKL (dict format for compatibility)
    bundle_dir = os.path.join(OUTPUT_BASE, cfg["experiment_id"])
    os.makedirs(bundle_dir, exist_ok=True)
    pkl_path = os.path.join(bundle_dir, "final_model.pkl")
    payload = {
        "model": model,
        "feature_names": model.feature_name(),
        "n_features_in_": model.num_feature(),
        "params": {**cfg["params"], **lgb_params},
    }
    joblib.dump(payload, pkl_path)
    log.append(f"Saved PKL: {pkl_path}")

    # Generate OOS predictions on vault
    X_vault = df_vault[feature_cols_filtered]
    raw_pred = model.predict(X_vault)
    probs = _sigmoid(raw_pred)

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

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df_vault.columns:
            preds_df[col] = df_vault[col].values

    oos_path = os.path.join(bundle_dir, "oos_predictions.csv")
    preds_df.to_csv(oos_path)
    log.append(f"OOS predictions: {oos_path} ({len(preds_df):,} rows)")
    log.append(f"  date range: {preds_df.index.min()} -> {preds_df.index.max()}")
    log.append(f"  signals: {(probs >= 0.5).sum():,} / {len(probs):,}")

    # Save experiment config
    exp_config = {
        "experiment_id": cfg["experiment_id"],
        "strategy": f"e2e_alpha_factory_{direction}",
        "data_path": DATA_PATH,
        "target_name": target_name,
        "method": "frozen_retrain",
        "balance_mode": balance_mode,
        "train_cutoff_date": str(TRAIN_CUTOFF.date()),
        "model_params": cfg["params"],
        "provenance": {
            "source": "Frozen hyperparams from HourSet_02 Optuna champion",
            "base_experiment": f"E2E_HourSet_02_{direction}_{'average_precision' if direction == 'long' else 'logloss'}",
        },
    }
    with open(os.path.join(bundle_dir, "experiment_config.json"), "w") as f:
        json.dump(exp_config, f, indent=2, default=str)

    results[role] = {
        "oos_path": oos_path,
        "pkl_path": pkl_path,
        "n_rows_vault": len(preds_df),
        "n_signals": int((probs >= 0.5).sum()),
        "best_iter": best_iter,
    }
    log.append("")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
log.append("=" * 70)
log.append("RETRAIN COMPLETE")
log.append("=" * 70)
for role, r in results.items():
    log.append(f"{role.upper()}:")
    log.append(f"  OOS: {r['oos_path']}")
    log.append(f"  Vault rows: {r['n_rows_vault']:,} | Signals: {r['n_signals']:,} | Iters: {r['best_iter']}")
log.append("")
log.append("Next: Update hourly_ensemble_003.json and run backtest.")

report = "\n".join(log)
with open("tmp/retrain_log.txt", "w", encoding="utf-8") as f:
    f.write(report)
print("done -> tmp/retrain_log.txt")
print(f"Long OOS: {results['long']['oos_path']}")
print(f"Short OOS: {results['short']['oos_path']}")
