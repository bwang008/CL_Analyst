import pandas as pd
import numpy as np

df = pd.read_parquet(r"data\processed\CL_HourSet_11.parquet")
print("Data shape:", df.shape)

short_cols = [c for c in df.columns if "SHORT" in c]
long_cols = [c for c in df.columns if "LONG" in c]

print("Short distributions:")
for c in short_cols:
    print(c, dict(df[c].value_counts(dropna=False)))

print("Long distributions:")
for c in long_cols:
    print(c, dict(df[c].value_counts(dropna=False)))

# Check some examples where SHORT is 1
if len(short_cols) > 0:
    target = short_cols[0]
    print(f"\nAnalyzing {target}:")
    idx = df.index[df[target] == 1][:5]
    for i in idx:
        print(f"Index {i}: Close={df.loc[i, 'Close']:.2f}, High={df.loc[i, 'High']:.2f}, Low={df.loc[i, 'Low']:.2f}")
