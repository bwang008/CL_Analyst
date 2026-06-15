import pandas as pd
import numpy as np

# Load master ledger
df = pd.read_parquet(r"C:\CL_Analyst_Data\data\processed\cl_continuous_master.parquet")

if 'DateTime' in df.columns:
    df.set_index('DateTime', inplace=True)

# Filter for Monday 02:00:00 UTC (which is Sunday 19:00 PST / 22:00 ET)
mask = (df.index.dayofweek == 0) & (df.index.hour == 2) & (df.index.minute == 0)
bars = df[mask]

print(f"Total Monday 02:00:00 bars: {len(bars)}")
print(f"Bars with Volume == 0: {(bars['Volume'] == 0).sum()}")
print(f"Average Volume: {bars['Volume'].mean():.1f}")
print("Sample of recent volumes:")
for dt, vol in bars['Volume'].tail(10).items():
    print(f"  {dt}: {vol}")
