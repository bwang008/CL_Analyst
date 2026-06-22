import json
import pandas as pd
from agent.backtest_engine import BacktestEngine, load_predictions, load_ohlcv

config_path = "reports/batch_runs/batch_20260620_0028_HS13B_SCOUT/configs/TAG_Sharpe_E03_06202026.json"
data_path = "data/processed/CL_HourSet_13B.parquet"
exec_data_path = "C:\\CL_Analyst_Data\\data\\raw\\DataBentoSample\\CL_raw.parquet"

with open(config_path, "r") as f:
    cfg = json.load(f)

print("Loading data...")
ohlcv = load_ohlcv(data_path)
preds = load_predictions(cfg["models"]["long"]["predictions_path"])

print("Running backtest...")
bt = BacktestEngine.from_config(cfg, slippage_per_side=0.01)
result = bt.run(preds, ohlcv)

long_pnl = sum(t.net_pnl_dollars for t in result.trades if t.side == 1)
short_pnl = sum(t.net_pnl_dollars for t in result.trades if t.side == -1)
long_count = sum(1 for t in result.trades if t.side == 1)
short_count = sum(1 for t in result.trades if t.side == -1)

print(f"\n--- PNL Breakdown for Sharpe E03 ---")
print(f"Long Trades: {long_count}")
print(f"Long PnL: ${long_pnl:,.2f}")

print(f"Short Trades: {short_count}")
print(f"Short PnL: ${short_pnl:,.2f}")

print(f"Total PnL: ${result.total_pnl:,.2f}")
