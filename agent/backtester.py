"""
Event-driven Backtester for CL_Analyst.

Simulates trading based on vault predictions and raw price data.
Calculates key performance metrics: Sharpe Ratio, Profit Factor, etc.

Usage:
    python agent/backtester.py --predictions reports/vault_predictions.csv
"""

import os
import sys
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

def run_backtest(
    predictions_path,
    data_path="data/processed/CL_set_05.parquet",
    tp_mult=2.0,
    sl_mult=1.0,
    max_horizon=288,
    prob_threshold=0.5,
):
    """
    Run backtest on predictions.
    
    Args:
        predictions_path: Path to CSV with model predictions.
        data_path: Path to full parquet for price data.
        tp_mult: Take profit ATR multiplier.
        sl_mult: Stop loss ATR multiplier.
        max_horizon: Max bars to hold.
        prob_threshold: Minimum probability to take a Buy signal.
    """
    print(f"Loading predictions from {predictions_path}...")
    preds = pd.read_csv(predictions_path, index_col=0, parse_dates=True)
    
    print(f"Loading price data from {data_path} for simulation...")
    full_df = pd.read_parquet(data_path)
    
    # Feature columns for ATR (need 'Close', 'High', 'Low')
    # If the parquet doesn't have 'High'/'Low', we'll need the raw CSV.
    # But set_05 should have them if we didn't drop them in cleanup.
    # Actually cleanup drops raw columns. We need the raw data.
    
    raw_path = os.path.join(PROJECT_ROOT, "data", "raw", "CL.csv")
    from src.data_processor import DataProcessor
    dp = DataProcessor(input_path=raw_path)
    raw = dp.load_data()
    
    # Align indices
    common_idx = preds.index.intersection(raw.index)
    preds = preds.loc[common_idx]
    
    # Identify signals
    # If 'prob_Buy' exists, use threshold. Otherwise use 'Predicted'
    if 'prob_Buy' in preds.columns:
        signals = preds[preds['prob_Buy'] >= prob_threshold]
    else:
        signals = preds[preds['Predicted'] == 1]
    
    print(f"Found {len(signals)} Buy signals out of {len(preds)} bars.")
    
    trades = []
    
    # We need ATR for each signal
    # Calculate ATR on raw data
    raw_copy = raw.copy()
    raw_copy['tr'] = np.maximum(
        raw_copy['High'] - raw_copy['Low'],
        np.maximum(
            (raw_copy['High'] - raw_copy['Close'].shift(1)).abs(),
            (raw_copy['Low'] - raw_copy['Close'].shift(1)).abs()
        )
    )
    raw_copy['atr'] = raw_copy['tr'].rolling(14).mean()
    
    for dt, signal in signals.iterrows():
        entry_price = raw.at[dt, 'Close']
        atr = raw_copy.at[dt, 'atr']
        
        if np.isnan(atr):
            continue
            
        tp_price = entry_price + tp_mult * atr
        sl_price = entry_price - sl_mult * atr
        
        # Look forward in raw data
        future_data = raw.loc[dt:].iloc[1:max_horizon+1]
        
        exit_price = None
        exit_dt = None
        reason = "Timeout"
        
        for fdt, row in future_data.iterrows():
            if row['High'] >= tp_price:
                exit_price = tp_price
                exit_dt = fdt
                reason = "TP"
                break
            if row['Low'] <= sl_price:
                exit_price = sl_price
                exit_dt = fdt
                reason = "SL"
                break
        
        if exit_price is None and len(future_data) > 0:
            exit_dt = future_data.index[-1]
            exit_price = future_data.at[exit_dt, 'Close']
            reason = "Timeout"
            
        if exit_price is not None:
            pnl = exit_price - entry_price
            trades.append({
                'entry_dt': dt,
                'exit_dt': exit_dt,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl': pnl,
                'pnl_pct': pnl / entry_price,
                'reason': reason,
                'duration': (exit_dt - dt).total_seconds() / 300, # bars
            })
            
    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        print("No trades simulated.")
        return
        
    # Metrics
    win_rate = (trades_df['pnl'] > 0).mean()
    avg_pnl = trades_df['pnl_pct'].mean()
    profit_factor = trades_df[trades_df['pnl'] > 0]['pnl'].sum() / abs(trades_df[trades_df['pnl'] < 0]['pnl'].sum())
    
    # Cumulative PNL
    trades_df['cum_pnl'] = (1 + trades_df['pnl_pct']).cumprod()
    
    print("\n" + "="*40)
    print("BACKTEST RESULTS")
    print("="*40)
    print(f"Total Trades:   {len(trades_df)}")
    print(f"Win Rate:       {win_rate:.1%}")
    print(f"Avg PnL %:      {avg_pnl:.2%}")
    print(f"Profit Factor:  {profit_factor:.2f}")
    print(f"TP Hits:        {(trades_df['reason'] == 'TP').sum()}")
    print(f"SL Hits:        {(trades_df['reason'] == 'SL').sum()}")
    print(f"Timeouts:       {(trades_df['reason'] == 'Timeout').sum()}")
    print("="*40)
    
    # Simple Sharpe (on trade returns)
    if len(trades_df) > 1:
        sharpe = (trades_df['pnl_pct'].mean() / trades_df['pnl_pct'].std()) * np.sqrt(252) # Scaled for equity-like trades
        print(f"Scaled Sharpe:  {sharpe:.2f}")
    
    # Save results
    report_path = os.path.join(PROJECT_ROOT, "reports", "backtest_results.csv")
    trades_df.to_csv(report_path)
    print(f"Saved trade log to {report_path}")
    
    # Plot PNL
    plt.figure(figsize=(10, 6))
    plt.plot(trades_df['entry_dt'], trades_df['cum_pnl'])
    plt.title("Cumulative Equity Curve (Vault)")
    plt.grid(True)
    plt.savefig(os.path.join(PROJECT_ROOT, "reports", "equity_curve.png"))
    print("Saved equity curve to reports/equity_curve.png")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", default="reports/vault_predictions.csv")
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()
    
    run_backtest(args.predictions, prob_threshold=args.threshold)
