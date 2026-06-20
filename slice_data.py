import pandas as pd
import numpy as np
import os

data_path = r"C:\CL_Analyst_Data\data\processed\CL_HourSet_13B.parquet"
output_path = r"C:\CL_Analyst_Data\data\processed\CL_HourSet_13B_holdout_subset.parquet"

df = pd.read_parquet(data_path)

# Find the index position of the first bar in 2026
target_date = pd.to_datetime("2026-01-01")

# Get the first index that is >= 2026-01-01
mask = df.index >= target_date
first_2026_idx = np.where(mask)[0][0]

# We need 2200 bars before this
start_idx = first_2026_idx - 2200

# Ensure we don't go negative
if start_idx < 0:
    start_idx = 0

subset = df.iloc[start_idx:]
subset.to_parquet(output_path)

print(f"Original length: {len(df)}")
print(f"Target date 2026-01-01 starts at position: {first_2026_idx}")
print(f"Subset start position: {start_idx}")
print(f"Subset length: {len(subset)}")
print(f"Subset date range: {subset.index[0]} -> {subset.index[-1]}")
