import json
import os
import sys
import pandas as pd

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import src.util as util
from gcp.vm_e2e_pipeline import train_final_model, generate_oos_predictions

def main():
    print("Loading params from Exp 3 JSON...")
    with open("reports/overnight_alpha/reports/optuna_best_params_multi_logloss.json") as f:
        data = json.load(f)
    
    # We want just the lgbm parameters
    params = data.get("best_hyperparameters", {})
    # Default to 500
    if "n_estimators" not in params:
        params["n_estimators"] = 500
        
    print(f"Optimal Params: {params}")

    print("Loading data...")
    df = pd.read_parquet("data/processed/cl-5m_bk_set_11_vol.parquet")
    feature_cols = util.get_feature_columns(df)
    target_col = "TARGET_VOL_EXPANSION"

    # Match the cutoff used in the experiment
    cutoff = pd.Timestamp("2022-01-01")
    df_train = df[df.index < cutoff].copy()
    df_vault = df[df.index >= cutoff].copy()
    
    # Drop NaNs
    df_train = df_train.dropna(subset=[target_col])
    df_vault = df_vault.dropna(subset=[target_col])

    print(f"Train rows: {len(df_train):,}, Vault rows: {len(df_vault):,}")

    model_path = "models/registry/VOLATILITY_EXPANSION_overnight/final_model.pkl"
    preds_path = "models/registry/VOLATILITY_EXPANSION_overnight/oos_predictions.csv"
    
    print("Training Volatility model on Train split...")
    model = train_final_model(
        df_train=df_train,
        feature_cols=feature_cols,
        target_col=target_col,
        params=params,
        balance_mode="downsample",
        output_path=model_path
    )

    print("Generating Vault predictions...")
    preds_df = generate_oos_predictions(
        model=model,
        df_vault=df_vault,
        feature_cols=feature_cols,
        target_col=target_col,
        direction="long",  # We use 'long' so it outputs prob_Buy
        output_path=preds_path
    )
    
    print("DONE. Predictions generated at:", preds_path)

if __name__ == "__main__":
    main()
