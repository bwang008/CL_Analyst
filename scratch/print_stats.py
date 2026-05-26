import sys
from pathlib import Path
import pandas as pd
import numpy as np
from scipy import stats
from scipy.signal import find_peaks

# Add project root to path
sys.path.append(str(Path("c:/Users/bwang/Documents/GitHub/CL_Analyst_Development")))
from scripts.plot_prediction_distributions import classify_distribution, infer_direction, find_prob_column

long_path = Path("data/predictions/oos_predictions_sweep_hs08_3x1_24h_20260523_2040_long_logloss.csv")
short_path = Path("data/predictions/oos_predictions_sweep_hs08_4x1_6h_20260523_2040_short_logloss.csv")

for path, thresh in [(long_path, 0.59), (short_path, 0.68)]:
    df = pd.read_csv(path)
    col = find_prob_column(df.columns.tolist())
    probs = df[col].dropna().values
    
    direction = infer_direction(col)
    shape = classify_distribution(probs)
    pct_above = (probs >= thresh).sum() / len(probs) * 100
    pct_below_sec = (probs <= 0.45).sum() / len(probs) * 100
    
    print(f"=== {path.name} ===")
    print(f"Direction: {direction}")
    print(f"Column: {col}")
    print(f"N: {len(probs):,}")
    print(f"Min: {probs.min():.6f}")
    print(f"Max: {probs.max():.6f}")
    print(f"Mean: {probs.mean():.6f}")
    print(f"Median: {np.median(probs):.6f}")
    print(f"Threshold >= {thresh:.2f}: {pct_above:.4f}%")
    print(f"Secondary <= 0.45: {pct_below_sec:.4f}%")
    print(f"Shape: {shape}")
    print()
