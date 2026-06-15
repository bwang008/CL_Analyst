import pandas as pd
import json
from agent.backtest_engine import BacktestEngine, load_ohlcv_dual, load_predictions

config_path = r"reports\sweep_hs11_3x1_6h_20260614_1556\registry\canary_output\sweep_hs11_3x1_6h_20260614_1556_logloss_opt_short_sharpe.json"
predictions_path = r"reports\sweep_hs11_3x1_6h_20260614_1556\registry\canary_output\oos_predictions_sweep_hs11_3x1_6h_20260614_1556_short_logloss.csv"
ohlcv_path = r"data\processed\CL_HourSet_11.parquet"

print("Loading data...")
df, ohlcv_exec = load_ohlcv_dual(ohlcv_path)
preds = load_predictions(predictions_path)

with open(config_path, "r") as f:
    config = json.load(f)

engine = BacktestEngine.from_config(config)

print(df[["High", "Low", "Close", "RAW_Close"]].head())

print("Running engine without ohlcv_exec_df...")
res = engine.run(preds, df)

print("Jan 3 01:00:", df.loc["2022-01-03 01:00:00", ["Open", "Close", "RAW_Close"]])
print("Jan 4 09:00:", df.loc["2022-01-04 09:00:00", ["Open", "Close", "RAW_Close"]])

for t in res.trades[:1]:
    print(f"Entry: {t.entry_dt} @ {t.entry_price:.2f} (ATR={t.atr_at_entry:.2f})")
    print(f"Exit:  {t.exit_dt} @ {t.exit_price:.2f} ({t.exit_reason}) -> PnL: {t.net_pnl_dollars:.2f}")
print(f"Trades: {res.trade_count}")
print(f"PnL: {res.total_pnl}")
print(f"Win Rate: {res.win_rate}")
print(f"PF: {res.profit_factor}")




