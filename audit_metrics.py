import os
import pickle
import pandas as pd
import numpy as np
from sklearn.metrics import roc_auc_score, log_loss

def audit_model(direction, model_path, val_preds_path):
    print(f"==================================================")
    print(f"AUDITING {direction.upper()} MODEL")
    print(f"==================================================")
    
    # Load model
    with open(model_path, 'rb') as f:
        model = pickle.load(f)
        
    # Extract Feature Importance (Gain)
    fi = model.feature_importance(importance_type="gain")
    fi_names = model.feature_name()
    fi_df = pd.DataFrame({"feature": fi_names, "importance": fi})
    fi_df = fi_df.sort_values("importance", ascending=False)
    
    print("\n--- TOP 20 FEATURES BY GAIN ---")
    for idx, row in fi_df.head(20).iterrows():
        print(f"{row['feature']:<35} {row['importance']:.2f}")
        
    print("\n--- BOTTOM 20 FEATURES BY GAIN ---")
    for idx, row in fi_df.tail(20).iterrows():
        print(f"{row['feature']:<35} {row['importance']:.2f}")
        
    # Evaluate Validation Metrics
    val_df = pd.read_csv(val_preds_path, index_col=0, parse_dates=True)
    
    # Determine columns
    y_true = val_df['y_true']
    if direction == "long":
        probs = val_df['prob_Buy']
    else:
        probs = val_df['prob_Sell']
        
    # Filter out missing y_true (e.g. -1)
    valid_idx = y_true != -1
    y_true_valid = y_true[valid_idx]
    probs_valid = probs[valid_idx]
    
    if len(y_true_valid) > 0:
        roc_auc = roc_auc_score(y_true_valid, probs_valid)
        loss = log_loss(y_true_valid, probs_valid)
        print(f"\n--- VALIDATION SET METRICS ---")
        print(f"Base ROC-AUC: {roc_auc:.4f}")
        print(f"Logloss:      {loss:.4f}")
        print(f"Target Rate:  {y_true_valid.mean() * 100:.2f}%")
        print(f"Total Rows:   {len(y_true_valid)}")
    else:
        print("\n--- VALIDATION SET METRICS ---")
        print("No valid labels found for validation.")

base_dir = r"c:\Users\bwang\Documents\GitHub\CL_Analyst_Development\reports\hourset07_artifacts\canary_output"

audit_model(
    "long",
    os.path.join(base_dir, "final_long_model_logloss.pkl"),
    os.path.join(base_dir, "val_predictions_long_logloss.csv")
)
print("\n")
audit_model(
    "short",
    os.path.join(base_dir, "final_short_model_logloss.pkl"),
    os.path.join(base_dir, "val_predictions_short_logloss.csv")
)
