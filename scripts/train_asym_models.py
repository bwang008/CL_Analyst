import json
import os
import sys

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import average_precision_score, log_loss

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src import util

def train_and_predict(data_path, train_cutoff_date, long_json_path, short_json_path, output_csv):
    print(f"Loading data from {data_path}...")
    df = pd.read_parquet(data_path)
    
    # Split train/oos based on cutoff
    df_train = df[df.index < train_cutoff_date].copy()
    df_oos = df[df.index >= train_cutoff_date].copy()
    
    feature_cols = util.get_feature_columns(df)
    
    print(f"Train rows: {len(df_train):,}, OOS rows: {len(df_oos):,}")
    
    # Init predictions dataframe
    predictions = pd.DataFrame(index=df_oos.index)
    
    for direction, json_path in [("LONG", long_json_path), ("SHORT", short_json_path)]:
        print(f"\nProcessing {direction} model...")
        with open(json_path, "r") as f:
            report = json.load(f)
            
        target_col = report["target_name"]
        params = report["model_params_for_experiment_runner"]
        
        # We need to remove objective/metric from params for lgb.train if using custom, 
        # but vm_e2e_pipeline passes them directly. Let's just use standard binary logloss.
        # Focal loss requires custom objective. We'll stick to binary for now, 
        # or use focal if `use_focal` is true. We'll use the LGBMLearner class from src!
        
        from src.LGBMLearner import LGBMLearner
        
        # Prepare train data
        df_train_sub = df_train.dropna(subset=[target_col])
        X_train = df_train_sub[feature_cols]
        y_vals = df_train_sub[target_col].values.astype(float)
        y_vals[np.isnan(y_vals)] = 0
        y_train = pd.Series(y_vals.astype(int), index=df_train_sub.index)
        
        print(f"  Downsampling majority class for {target_col}...")
        X_train_ds, y_train_ds = util.downsample_majority(X_train, y_train, random_state=42)
        
        print(f"  Training model with {len(X_train_ds)} samples...")
        learner = LGBMLearner(**params)
        learner.add_evidence(X_train_ds, y_train_ds)
        
        print(f"  Generating OOS predictions...")
        raw_preds = learner.model.predict(df_oos[feature_cols])
        
        # Apply sigmoid if focal loss output logits
        probs = np.asarray(raw_preds, dtype=float).ravel()
        if np.nanmin(probs) < 0.0 or np.nanmax(probs) > 1.0:
            probs = 1.0 / (1.0 + np.exp(-np.clip(probs, -60, 60)))
            
        prob_col = "prob_Buy" if direction == "LONG" else "prob_Sell"
        predictions[prob_col] = probs
        
        # Eval
        valid_idx = df_oos[target_col].notna()
        if valid_idx.sum() > 0:
            act_vals = df_oos.loc[valid_idx, target_col].values.astype(float)
            act_vals[np.isnan(act_vals)] = 0
            actuals = pd.Series(act_vals.astype(int), index=df_oos[valid_idx].index)
            
            ll = log_loss(actuals, probs[valid_idx])
            pr = average_precision_score(actuals, probs[valid_idx])
            print(f"  OOS Logloss: {ll:.4f}")
            print(f"  OOS PR-AUC:  {pr:.4f}")
            
    print(f"\nSaving predictions to {output_csv}...")
    predictions.to_csv(output_csv)
    print("Done!")

if __name__ == "__main__":
    train_and_predict(
        data_path="data/CL_set_11_asym.parquet",
        train_cutoff_date="2022-01-01",
        long_json_path="reports/canary_asym/optuna_best_params_long_average_precision.json",
        short_json_path="reports/canary_asym/optuna_best_params_short_average_precision.json",
        output_csv="reports/canary_asym/oos_predictions_asym.csv"
    )
