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
    commission_per_side=2.50,
    slippage_per_side=0.03,
    contract_multiplier=1000,
):
    """
    Run backtest on predictions.
    
    Args:
        predictions_path: Path to CSV with model predictions.
        data_path: Path to full parquet for price data.
        tp_mult: Take profit ATR multiplier.
        sl_mult: Stop loss ATR multiplier.
        max_horizon: Max bars to hold.
        prob_threshold: Minimum probability to take a signal.
        commission_per_side: Flat commission per side, in dollars.
        slippage_per_side: Slippage penalty per side, in price units.
        contract_multiplier: Dollar value multiplier per 1.0 price move.
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
    
    # Identify signals and map each to a trade side (+1 long, -1 short)
    signal_sides = pd.Series(index=preds.index, dtype="float64")
    if 'prob_Buy' in preds.columns and 'prob_Sell' in preds.columns:
        buy_mask = preds['prob_Buy'] >= prob_threshold
        sell_mask = preds['prob_Sell'] >= prob_threshold
        signal_sides[buy_mask] = 1
        signal_sides[sell_mask] = -1
        conflict_mask = buy_mask & sell_mask
        if conflict_mask.any():
            buy_dominates = preds.loc[conflict_mask, 'prob_Buy'] >= preds.loc[conflict_mask, 'prob_Sell']
            signal_sides.loc[conflict_mask] = np.where(buy_dominates, 1, -1)
    elif 'prob_Buy' in preds.columns:
        signal_sides[preds['prob_Buy'] >= prob_threshold] = 1
    elif 'prob_Sell' in preds.columns:
        signal_sides[preds['prob_Sell'] >= prob_threshold] = -1
    elif 'Predicted' in preds.columns:
        if preds['Predicted'].dtype == object:
            pred_lower = preds['Predicted'].astype(str).str.lower()
            signal_sides[pred_lower == 'buy'] = 1
            signal_sides[pred_lower == 'sell'] = -1
        else:
            signal_sides[preds['Predicted'] == 1] = 1
            signal_sides[preds['Predicted'] == -1] = -1

    signal_sides = signal_sides.dropna().astype(int)
    signals = preds.loc[signal_sides.index].copy()
    signals['side'] = signal_sides
    
    n_long = int((signals['side'] == 1).sum())
    n_short = int((signals['side'] == -1).sum())
    print(f"Found {len(signals)} signals out of {len(preds)} bars ({n_long} long, {n_short} short).")
    
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
    
    def apply_slippage(price, order_side):
        if order_side == "Buy":
            return price + slippage_per_side
        return price - slippage_per_side

    for dt, signal in signals.iterrows():
        side = int(signal['side'])
        entry_price = raw.at[dt, 'Close']
        atr = raw_copy.at[dt, 'atr']
        
        if np.isnan(atr):
            continue
            
        if side == 1:
            tp_price = entry_price + tp_mult * atr
            sl_price = entry_price - sl_mult * atr
            entry_order_side = "Buy"
            exit_order_side = "Sell"
        else:
            tp_price = entry_price - tp_mult * atr
            sl_price = entry_price + sl_mult * atr
            entry_order_side = "Sell"
            exit_order_side = "Buy"
        
        # Look forward in raw data
        future_data = raw.loc[dt:].iloc[1:max_horizon+1]
        
        exit_price = None
        exit_dt = None
        reason = "Timeout"
        
        for fdt, row in future_data.iterrows():
            if side == 1:
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
            else:
                if row['Low'] <= tp_price:
                    exit_price = tp_price
                    exit_dt = fdt
                    reason = "TP"
                    break
                if row['High'] >= sl_price:
                    exit_price = sl_price
                    exit_dt = fdt
                    reason = "SL"
                    break
        
        if exit_price is None and len(future_data) > 0:
            exit_dt = future_data.index[-1]
            exit_price = future_data.at[exit_dt, 'Close']
            reason = "Timeout"
            
        if exit_price is not None:
            entry_fill = apply_slippage(entry_price, entry_order_side)
            exit_fill = apply_slippage(exit_price, exit_order_side)
            gross_pnl_price = side * (exit_fill - entry_fill)
            gross_pnl_dollars = gross_pnl_price * contract_multiplier
            commission_dollars = 2 * commission_per_side
            net_pnl_dollars = gross_pnl_dollars - commission_dollars
            trades.append({
                'entry_dt': dt,
                'exit_dt': exit_dt,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'entry_fill': entry_fill,
                'exit_fill': exit_fill,
                'side': side,
                'gross_pnl_price': gross_pnl_price,
                'gross_pnl_dollars': gross_pnl_dollars,
                'commission_dollars': commission_dollars,
                'pnl': net_pnl_dollars,
                'pnl_pct': net_pnl_dollars / (entry_fill * contract_multiplier),
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
    gross_profits = trades_df[trades_df['pnl'] > 0]['pnl'].sum()
    gross_losses = abs(trades_df[trades_df['pnl'] < 0]['pnl'].sum())
    profit_factor = gross_profits / gross_losses if gross_losses > 0 else np.inf
    total_net_pnl = trades_df['pnl'].sum()
    
    # Cumulative PNL
    trades_df['cum_pnl'] = trades_df['pnl'].cumsum()
    
    print("\n" + "="*40)
    print("BACKTEST RESULTS")
    print("="*40)
    print(f"Total Trades:   {len(trades_df)}")
    print(f"Friction:       commission=${commission_per_side:.2f}/side, slippage=${slippage_per_side:.2f}/side")
    print(f"Win Rate:       {win_rate:.1%}")
    print(f"Avg PnL %:      {avg_pnl:.2%}")    
    print(f"Profit Factor:  {profit_factor:.2f}")
    print(f"Total Net PnL:  ${total_net_pnl:,.2f}")
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
    parser.add_argument("--commission-per-side", type=float, default=2.50)
    parser.add_argument("--slippage-per-side", type=float, default=0.03)
    parser.add_argument("--contract-multiplier", type=float, default=1000.0)
    args = parser.parse_args()
    
    run_backtest(
        args.predictions,
        prob_threshold=args.threshold,
        commission_per_side=args.commission_per_side,
        slippage_per_side=args.slippage_per_side,
        contract_multiplier=args.contract_multiplier,
    )
