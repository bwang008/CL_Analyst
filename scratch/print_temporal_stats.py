import sys
from pathlib import Path
import pandas as pd
import numpy as np

# Load the datasets
long_path = Path("data/predictions/oos_predictions_sweep_hs08_3x1_24h_20260523_2040_long_logloss.csv")
short_path = Path("data/predictions/oos_predictions_sweep_hs08_4x1_6h_20260523_2040_short_logloss.csv")

for path, thresh, label in [(long_path, 0.59, "Long Model (prob_Buy)"), (short_path, 0.68, "Short Model (prob_Sell)")]:
    df = pd.read_csv(path)
    # Find probability column
    col = "prob_Buy" if "long" in path.name else "prob_Sell"
    df["DateTime"] = pd.to_datetime(df["DateTime"])
    df["hour"] = df["DateTime"].dt.hour
    df["day_name"] = df["DateTime"].dt.day_name()
    
    signals = df[df[col] >= thresh]
    
    print(f"=== {label} (Thresh: {thresh}) ===")
    print(f"Total signals: {len(signals)} out of {len(df)} bars ({len(signals)/len(df)*100:.2f}%)")
    
    # Hourly distribution of signals
    hourly_sig = signals.groupby("hour").size()
    hourly_total = df.groupby("hour").size()
    peak_hour = hourly_sig.idxmax() if len(hourly_sig) > 0 else "N/A"
    print(f"Peak Signal Hour: {peak_hour}:00 UTC with {hourly_sig.get(peak_hour, 0)} signals")
    
    print("Hourly signal count (UTC):")
    h_str = []
    for h in sorted(hourly_sig.index):
        rate = hourly_sig[h] / hourly_total[h] * 100
        h_str.append(f"{h:02d}:00 ({hourly_sig[h]} sigs, {rate:.1f}%)")
    print(", ".join(h_str[:8]))
    print(", ".join(h_str[8:16]))
    print(", ".join(h_str[16:]))
    
    # Day of week distribution
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    dow_sig = signals.groupby("day_name").size()
    dow_total = df.groupby("day_name").size()
    print("Day of Week signal count:")
    dow_str = []
    for d in dow_order:
        if d in dow_sig.index:
            rate = dow_sig[d] / dow_total[d] * 100
            dow_str.append(f"{d[:3]}: {dow_sig[d]} ({rate:.1f}%)")
        else:
            dow_str.append(f"{d[:3]}: 0 (0.0%)")
    print(", ".join(dow_str))
    print()
