from agent.backtest_engine import BacktestEngine
import pandas as pd
import json

with open('configs/strategies/HS11_Prod_Ensemble_E01_06162026.json', 'r') as f:
    cfg = json.load(f)

p1 = pd.read_csv('reports/sweep_hs11_3x1_24h_20260615_0622/registry/canary_output/extended_predictions_long_ap.csv', index_col=0, parse_dates=True)
p2 = pd.read_csv('reports/sweep_hs11_3x1_6h_20260615_0622/registry/canary_output/extended_predictions_short_ll.csv', index_col=0, parse_dates=True)

long_col = [c for c in p1.columns if 'prob' in c.lower()][0]
short_col = [c for c in p2.columns if 'prob' in c.lower()][0]

preds = pd.DataFrame({
    'prob_Buy': p1[long_col],
    'prob_Sell': p2[short_col]
})

df = pd.read_parquet('C:\\CL_Analyst_Data\\data\\processed\\CL_HourSet_11_tiny.parquet')

# Apply same slicing
df = df.iloc[15000:]
preds = preds.loc[preds.index >= df.index[0]]

# Print indicator values exactly at the time we care about
ts1 = pd.Timestamp("2026-04-30 04:00:00").tz_localize("America/New_York")
ts2 = pd.Timestamp("2026-04-30 05:00:00").tz_localize("America/New_York")

print("At 04:00:", df.loc[ts1]['atr']) if ts1 in df.index else print("Missing")
print("At 05:00:", df.loc[ts2]['atr']) if ts2 in df.index else print("Missing")
