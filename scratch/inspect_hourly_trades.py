import os
import sys
import json
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent.backtest_engine import BacktestEngine, _resolve_prob_column
from src.data_paths import resolve_cli_path

def load_predictions(path):
    path = resolve_cli_path(path)
    df = pd.read_csv(path)
    if "DateTime" in df.columns:
        df["DateTime"] = pd.to_datetime(df["DateTime"])
        df = df.set_index("DateTime")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime")
    elif df.index.name != "DateTime":
        df.index = pd.to_datetime(df.iloc[:, 0])
        df.index.name = "DateTime"
        df = df.iloc[:, 1:]
    return df

def run_backtest_and_inspect():
    config_path = "configs/strategies/HourSet_08_Ensemble_03_05242026.json"
    data_path = "C:\\CL_Analyst_Data\\data\\processed\\CL_HourSet_08.parquet"
    slippage = 0.01

    with open(config_path) as f:
        strategy_cfg = json.load(f)

    bt = BacktestEngine.from_config(strategy_cfg, slippage_per_side=slippage)

    models_cfg = strategy_cfg.get("models", {})
    long_preds_path = resolve_cli_path(models_cfg.get("long", {}).get("predictions_path"))
    short_preds_path = resolve_cli_path(models_cfg.get("short", {}).get("predictions_path"))
    
    long_df = load_predictions(long_preds_path)
    short_df = load_predictions(short_preds_path)
    
    long_col = _resolve_prob_column(long_df, "buy")
    short_col = _resolve_prob_column(short_df, "sell")
    
    long_probs = long_df[[long_col]].rename(columns={long_col: "prob_Buy"})
    short_probs = short_df[[short_col]].rename(columns={short_col: "prob_Sell"})
    preds = long_probs.join(short_probs, how="outer").fillna(0.0)
    
    ohlcv_df = pd.read_parquet(data_path)
    
    result = bt.run(preds, ohlcv_df, label="Historical")
    trades_df = result.to_dataframe()
    
    trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
    trades_df["entry_hour"] = trades_df["entry_time"].dt.hour
    
    print("\n--- INDIVIDUAL TRADES ENTERED AT HOUR 8, 9, 10, 11 ---")
    target_hours = [8, 9, 10, 11]
    filtered_trades = trades_df[trades_df["entry_hour"].isin(target_hours)].copy()
    
    # Sort by entry time
    filtered_trades = filtered_trades.sort_values("entry_time")
    
    print(f"Total trades entered during these hours: {len(filtered_trades)}")
    
    # Group by hour and print summary
    summary = filtered_trades.groupby("entry_hour").agg(
        trade_count=("net_pnl_dollars", "count"),
        winning_trades=("net_pnl_dollars", lambda x: (x > 0).sum()),
        losing_trades=("net_pnl_dollars", lambda x: (x < 0).sum()),
        total_pnl=("net_pnl_dollars", "sum"),
        avg_pnl=("net_pnl_dollars", "mean")
    ).reset_index()
    print("\nSummary by hour:")
    print(summary.to_string(index=False))
    
    print("\nSample of Trades entered at 9 AM (Hour 9):")
    h9_trades = filtered_trades[filtered_trades["entry_hour"] == 9]
    print(h9_trades[["entry_time", "signal_side", "entry_price", "exit_time", "exit_price", "exit_reason", "net_pnl_dollars"]].head(15).to_string(index=False))
    
    print("\nSample of Trades entered at 10 AM (Hour 10):")
    h10_trades = filtered_trades[filtered_trades["entry_hour"] == 10]
    print(h10_trades[["entry_time", "signal_side", "entry_price", "exit_time", "exit_price", "exit_reason", "net_pnl_dollars"]].head(15).to_string(index=False))
    
    print("\nSample of Trades entered at 11 AM (Hour 11):")
    h11_trades = filtered_trades[filtered_trades["entry_hour"] == 11]
    print(h11_trades[["entry_time", "signal_side", "entry_price", "exit_time", "exit_price", "exit_reason", "net_pnl_dollars"]].head(15).to_string(index=False))
    
    # Let's check how the hourly timestamps relate to market sessions.
    # In CL futures, standard pit open starts at 09:00:00 EST. 
    # Let's print a few rows of ohlcv_df around 09:00 and 10:00 on a specific day to see what the values look like.
    sample_day = "2025-05-13" # A Tuesday
    print(f"\nOHLCV bars on {sample_day}:")
    day_ohlcv = ohlcv_df[ohlcv_df.index.strftime('%Y-%m-%d') == sample_day]
    print(day_ohlcv[["Open", "High", "Low", "Close", "Volume"]])

if __name__ == "__main__":
    run_backtest_and_inspect()
