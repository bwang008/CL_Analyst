"""
Model Sanitization Script
Converts legacy pickled LightGBM models into native .txt representations.
This strips all Python namespace dependencies and Immunizes the production
engine from Custom Objective Pickling Errors.
"""

import os
import sys
import numpy as np
import joblib

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

# 1. Define dummy focal_obj so Joblib can unpack the infected .pkl files
FOCAL_GAMMA = 2.0

def _sigmoid(x):
    x = np.clip(np.asarray(x, dtype=float), -60, 60)
    return 1.0 / (1.0 + np.exp(-x))

def focal_obj(preds, train_set):
    """Dummy scope target for unpickling"""
    pass

def sanitize_model(pkl_path):
    if not os.path.exists(pkl_path):
        print(f"File not found: {pkl_path}")
        return
        
    print(f"Loading {pkl_path}...")
    try:
        data = joblib.load(pkl_path)
    except Exception as e:
        print(f"Failed to load {pkl_path}: {e}")
        return
        
    if isinstance(data, dict) and 'model' in data:
        model = data['model']
    else:
        model = data

    pure_txt_path = pkl_path.replace(".pkl", "_pure.txt")
    print(f"Saving native booster to {pure_txt_path}...")
    model.save_model(pure_txt_path)
    print("Sanitization complete for this model.\n")

if __name__ == "__main__":
    base_dir = "reports/canary/registry/canary_output/registry"
    
    models = [
        "E2E_HourSet_03_long_average_precision",
        "E2E_HourSet_03_short_logloss"
    ]
    
    for model_dir in models:
        pkl_path = os.path.join(base_dir, model_dir, "final_model.pkl")
        sanitize_model(pkl_path)
