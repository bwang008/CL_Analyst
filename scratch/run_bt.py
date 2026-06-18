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

# Emulate livetest_engine with --warmup-bars 2200
df_replay_start = df.iloc[2200].name; preds = preds.loc[preds.index >= df_replay_start]


bt = BacktestEngine.from_config(cfg)
res = bt.run(preds, df)

trades_df = res.to_dataframe()
trades_df.to_csv('reports/backtest_trades_patched.csv', index=False)
print(f'Done exporting {len(trades_df)} trades to reports/backtest_trades_patched.csv')
