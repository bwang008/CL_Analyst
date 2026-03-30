import os
import sys
import numpy as np
import pandas as pd
import itertools
from collections import defaultdict

def compute_atr(df, period=14):
    high = df['High'].values
    low = df['Low'].values
    close = df['Close'].values
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr1 = high - low
    tr2 = np.abs(high - prev_close)
    tr3 = np.abs(low - prev_close)
    tr = np.maximum(tr1, np.maximum(tr2, tr3))
    atr = pd.Series(tr).rolling(window=period).mean().values
    return atr

def run_sweep(csv_path, side, f):
    f.write(f"\n### {side} Model Top 5\n\n")
    df = pd.read_csv(csv_path)
    
    if 'ATR_14' not in df.columns:
        df['ATR_14'] = compute_atr(df, 14)
        
    df = df.dropna(subset=['ATR_14']).reset_index(drop=True)
    
    high = df['High'].values
    low = df['Low'].values
    close = df['Close'].values
    atr = df['ATR_14'].values
    n = len(df)
    
    prob_col = 'prob_Buy' if side == "LONG" else 'prob_Sell'
    probs = df[prob_col].values
    
    thresholds = np.arange(0.50, 0.80, 0.05)
    tp_mults = np.arange(1.0, 5.5, 0.5)
    sl_mults = np.arange(0.5, 3.5, 0.5)
    
    results = []
    
    for th in thresholds:
        triggers = probs >= th
        for tp_m in tp_mults:
            for sl_m in sl_mults:
                trades = 0
                win_count = 0
                total_pnl = 0.0
                i = 0
                while i < n - 1:
                    if triggers[i]:
                        entry_price = close[i]
                        entry_atr = atr[i]
                        
                        if side == "LONG":
                            tp_price = entry_price + (tp_m * entry_atr)
                            sl_price = entry_price - (sl_m * entry_atr)
                        else:
                            tp_price = entry_price - (tp_m * entry_atr)
                            sl_price = entry_price + (sl_m * entry_atr)
                            
                        j = i + 1
                        trade_pnl = 0.0
                        exit_found = False
                        
                        while j < n:
                            bar_h = high[j]
                            bar_l = low[j]
                            bar_c = close[j]
                            
                            if side == "LONG":
                                hit_tp = bar_h >= tp_price
                                hit_sl = bar_l <= sl_price
                                if hit_sl and hit_tp:
                                    trade_pnl = sl_price - entry_price
                                    exit_found = True
                                elif hit_sl:
                                    trade_pnl = sl_price - entry_price
                                    exit_found = True
                                elif hit_tp:
                                    trade_pnl = tp_price - entry_price
                                    exit_found = True
                            else:
                                hit_tp = bar_l <= tp_price
                                hit_sl = bar_h >= sl_price
                                if hit_sl and hit_tp:
                                    trade_pnl = entry_price - sl_price
                                    exit_found = True
                                elif hit_sl:
                                    trade_pnl = entry_price - sl_price
                                    exit_found = True
                                elif hit_tp:
                                    trade_pnl = entry_price - tp_price
                                    exit_found = True
                                    
                            if exit_found:
                                i = j
                                break
                            j += 1
                            
                        if not exit_found:
                            if side == "LONG":
                                trade_pnl = close[-1] - entry_price
                            else:
                                trade_pnl = entry_price - close[-1]
                            i = n
                            break
                            
                        trades += 1
                        total_pnl += trade_pnl
                        if trade_pnl > 0:
                            win_count += 1
                    else:
                        i += 1
                        
                if trades >= 25:
                    win_rate = win_count / trades if trades > 0 else 0
                    results.append({
                        'Threshold': round(th, 2),
                        'TP (ATR)': round(tp_m, 1),
                        'SL (ATR)': round(sl_m, 1),
                        'Trades': trades,
                        'WinRate(%)': round(win_rate * 100, 1),
                        'Total PnL': round(total_pnl, 2)
                    })
                    
    results_df = pd.DataFrame(results)
    if len(results_df) > 0:
        results_df = results_df.sort_values('Total PnL', ascending=False).head(5)
        f.write(results_df.to_string(index=False) + "\n")
    else:
        f.write("No combinations yielded >= 25 trades.\n")

if __name__ == "__main__":
    long_csv = "reports/sweep/production/unpacked/canary_output/registry/E2E_HourSet_02_long_logloss/oos_predictions.csv"
    short_csv = "models/registry/HourSet_02_2p5x1_120H_short_logloss/oos_predictions.csv"
    
    out_path = "artifacts/threshold_sweep_results.md"
    os.makedirs("artifacts", exist_ok=True)
    with open(out_path, "w") as f:
        f.write("# Local Threshold Sweeper Results\n")
        
        run_sweep(long_csv, "LONG", f)
        run_sweep(short_csv, "SHORT", f)
        
    print(f"Results written to {out_path}")
