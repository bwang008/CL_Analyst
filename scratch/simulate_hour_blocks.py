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

def run_simulation():
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
    
    # 1. Base Backtest
    print("Running Baseline Backtest...")
    base_result = bt.run(preds, ohlcv_df, label="Baseline")
    base_trades = base_result.to_dataframe()
    base_trades["entry_time"] = pd.to_datetime(base_trades["entry_time"])
    base_trades["entry_hour"] = base_trades["entry_time"].dt.hour
    
    # We will simulate the impact of blocking specific entry hours post-hoc.
    # Note: Since allow_concurrent is False, blocking a trade at Hour 9 might let a trade be entered at Hour 10 or 11
    # which was previously blocked by the FSM being IN_POSITION or in COOLDOWN.
    # Therefore, a post-hoc analysis is an approximation, but running the actual backtest engine with the hours blocked
    # in the signal DataFrame is 100% accurate!
    # Let's run actual backtests with blocked hours in the signal DataFrame!
    
    # Test A: Block Hour 9 (Set signal probabilities to 0 at Hour 9)
    print("\n--- SIMULATION A: BLOCKING 9:00 AM BAR SIGNALS (Executed at 10:00 AM EST) ---")
    preds_no_h9 = preds.copy()
    preds_no_h9.loc[preds_no_h9.index.hour == 9, ["prob_Buy", "prob_Sell"]] = 0.0
    bt_no_h9 = BacktestEngine.from_config(strategy_cfg, slippage_per_side=slippage)
    res_no_h9 = bt_no_h9.run(preds_no_h9, ohlcv_df)
    trades_no_h9 = res_no_h9.to_dataframe()
    
    # Test B: Block Hour 8 (Set signals to 0 at Hour 8, executed at 9:00 AM EST)
    print("--- SIMULATION B: BLOCKING 8:00 AM BAR SIGNALS (Executed at 9:00 AM EST) ---")
    preds_no_h8 = preds.copy()
    preds_no_h8.loc[preds_no_h8.index.hour == 8, ["prob_Buy", "prob_Sell"]] = 0.0
    bt_no_h8 = BacktestEngine.from_config(strategy_cfg, slippage_per_side=slippage)
    res_no_h8 = bt_no_h8.run(preds_no_h8, ohlcv_df)
    trades_no_h8 = res_no_h8.to_dataframe()
    
    # Test C: Block Hour 10 (Set signals to 0 at Hour 10, executed at 11:00 AM EST)
    print("--- SIMULATION C: BLOCKING 10:00 AM BAR SIGNALS (Executed at 11:00 AM EST) ---")
    preds_no_h10 = preds.copy()
    preds_no_h10.loc[preds_no_h10.index.hour == 10, ["prob_Buy", "prob_Sell"]] = 0.0
    bt_no_h10 = BacktestEngine.from_config(strategy_cfg, slippage_per_side=slippage)
    res_no_h10 = bt_no_h10.run(preds_no_h10, ohlcv_df)
    trades_no_h10 = res_no_h10.to_dataframe()

    # Test D: Block Hour 11 (Set signals to 0 at Hour 11, executed at 12:00 PM EST)
    print("--- SIMULATION D: BLOCKING 11:00 AM BAR SIGNALS (Executed at 12:00 PM EST) ---")
    preds_no_h11 = preds.copy()
    preds_no_h11.loc[preds_no_h11.index.hour == 11, ["prob_Buy", "prob_Sell"]] = 0.0
    bt_no_h11 = BacktestEngine.from_config(strategy_cfg, slippage_per_side=slippage)
    res_no_h11 = bt_no_h11.run(preds_no_h11, ohlcv_df)
    trades_no_h11 = res_no_h11.to_dataframe()
    
    # Test E: Block Hour 9 and Hour 11 (Both toxic hours blocked)
    print("--- SIMULATION E: BLOCKING BOTH 9:00 AM AND 11:00 AM BAR SIGNALS ---")
    preds_no_h9_h11 = preds.copy()
    preds_no_h9_h11.loc[preds_no_h9_h11.index.hour.isin([9, 11]), ["prob_Buy", "prob_Sell"]] = 0.0
    bt_no_h9_h11 = BacktestEngine.from_config(strategy_cfg, slippage_per_side=slippage)
    res_no_h9_h11 = bt_no_h9_h11.run(preds_no_h9_h11, ohlcv_df)
    trades_no_h9_h11 = res_no_h9_h11.to_dataframe()

    print("\n==========================================================================================")
    print("                                SUMMARY OF SIMULATION RESULTS                             ")
    print("==========================================================================================")
    
    results_comparison = [
        {"Configuration": "Baseline (All Hours)", "Trades": len(base_trades), "Win Rate": f"{base_result.win_rate*100:.1f}%", "Net PnL": f"${base_result.total_pnl:,.2f}", "Max Drawdown": f"${base_result.max_drawdown:,.2f}", "Profit Factor": f"{base_result.profit_factor:.2f}"},
        {"Configuration": "Block 8:00 AM Bar (9 AM fill)", "Trades": len(trades_no_h8), "Win Rate": f"{res_no_h8.win_rate*100:.1f}%", "Net PnL": f"${res_no_h8.total_pnl:,.2f}", "Max Drawdown": f"${res_no_h8.max_drawdown:,.2f}", "Profit Factor": f"{res_no_h8.profit_factor:.2f}"},
        {"Configuration": "Block 9:00 AM Bar (10 AM fill)", "Trades": len(trades_no_h9), "Win Rate": f"{res_no_h9.win_rate*100:.1f}%", "Net PnL": f"${res_no_h9.total_pnl:,.2f}", "Max Drawdown": f"${res_no_h9.max_drawdown:,.2f}", "Profit Factor": f"{res_no_h9.profit_factor:.2f}"},
        {"Configuration": "Block 10:00 AM Bar (11 AM fill)", "Trades": len(trades_no_h10), "Win Rate": f"{res_no_h10.win_rate*100:.1f}%", "Net PnL": f"${res_no_h10.total_pnl:,.2f}", "Max Drawdown": f"${res_no_h10.max_drawdown:,.2f}", "Profit Factor": f"{res_no_h10.profit_factor:.2f}"},
        {"Configuration": "Block 11:00 AM Bar (12 PM fill)", "Trades": len(trades_no_h11), "Win Rate": f"{res_no_h11.win_rate*100:.1f}%", "Net PnL": f"${res_no_h11.total_pnl:,.2f}", "Max Drawdown": f"${res_no_h11.max_drawdown:,.2f}", "Profit Factor": f"{res_no_h11.profit_factor:.2f}"},
        {"Configuration": "Block both 9:00 AM and 11:00 AM Bars", "Trades": len(trades_no_h9_h11), "Win Rate": f"{res_no_h9_h11.win_rate*100:.1f}%", "Net PnL": f"${res_no_h9_h11.total_pnl:,.2f}", "Max Drawdown": f"${res_no_h9_h11.max_drawdown:,.2f}", "Profit Factor": f"{res_no_h9_h11.profit_factor:.2f}"},
    ]
    
    comp_df = pd.DataFrame(results_comparison)
    print(comp_df.to_markdown(index=False))

if __name__ == "__main__":
    run_simulation()
