import pandas as pd
import numpy as np
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.LGBMLearner import LGBMLearner

parquet_file = r"C:\CL_Analyst_Data\data\processed\CL_HourSet_09.parquet"
df = pd.read_parquet(parquet_file)

if 'DateTime' in df.columns:
    df = df.set_index('DateTime')

# Extract basic price columns for the output
# In HourSet datasets, the raw price columns are often 'Open', 'High', 'Low', 'Close', 'Volume'
# Wait, some are converted to log_ret. Let's see if 'Open' exists.
if 'Open' not in df.columns and 'RAW_Close' in df.columns:
    # Use RAW columns if needed
    pass

models_to_run = [
    {
        "model_path": r"C:\Users\bwang\Documents\GitHub\CL_Analyst_Development\scratch\artifacts\canary_output\registry\E2E_HourSet_09_long_logloss\final_model.pkl",
        "output_path": r"C:\Users\bwang\Documents\GitHub\CL_Analyst_Development\data\predictions\oos_predictions_sweep_hs09_3x1_24h_20260602_0330_long_logloss.csv",
        "prob_col": "prob_Buy"
    },
    {
        "model_path": r"C:\Users\bwang\Documents\GitHub\CL_Analyst_Development\scratch\artifacts\canary_output\registry\E2E_HourSet_09_short_average_precision\final_model.pkl",
        "output_path": r"C:\Users\bwang\Documents\GitHub\CL_Analyst_Development\data\predictions\oos_predictions_sweep_hs09_3x1_24h_20260602_0330_short_average_precision.csv",
        "prob_col": "prob_Sell"
    }
]

for cfg in models_to_run:
    print(f"Loading model: {cfg['model_path']}")
    learner = LGBMLearner()
    learner.load(cfg['model_path'])
    
    # Filter X
    feature_names = learner.feature_names
    # If some features are missing, this will fail, which is correct
    X = df[feature_names]
    
    print(f"Generating predictions for {len(X)} rows...")
    probs = learner.model.predict(X)
    
    out_df = pd.DataFrame(index=df.index)
    out_df['y_true'] = 0
    out_df[cfg['prob_col']] = probs
    out_df['prob_Hold'] = 1 - probs
    
    # Add open/high/low/close if available
    for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
        if col in df.columns:
            out_df[col] = df[col]
        elif f'RAW_{col}' in df.columns:
            out_df[col] = df[f'RAW_{col}']
        else:
            out_df[col] = 0.0

    if cfg['prob_col'] == 'prob_Buy':
        out_df['predicted'] = np.where(probs > 0.53, 'Buy', 'Hold')
    else:
        out_df['predicted'] = np.where(probs > 0.53, 'Sell', 'Hold')
    
    # Rearrange columns to match standard format
    cols = ['y_true', cfg['prob_col'], 'prob_Hold', 'predicted', 'Open', 'High', 'Low', 'Close', 'Volume']
    out_df = out_df[cols]
    
    print(f"Saving to {cfg['output_path']}")
    # Backup old file just in case
    if os.path.exists(cfg['output_path']):
        os.rename(cfg['output_path'], cfg['output_path'] + ".bak")
        
    out_df.to_csv(cfg['output_path'])
    print(f"Successfully saved {cfg['output_path']}")
