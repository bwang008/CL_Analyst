import pandas as pd
import pandas_ta as ta
import numpy as np

def compute_barrier(df, horizon, tp_atr_mult, sl_atr_mult, atr_period=14):
    close = df['Close'].values
    high_all = df['High'].values
    low_all = df['Low'].values
    atr = df[f'ATR_{atr_period}'].values
    n = len(df)
    
    long_labels = np.zeros(n, dtype=np.float64)
    short_labels = np.zeros(n, dtype=np.float64)
    
    for i in range(n - 1):
        if np.isnan(atr[i]) or atr[i] <= 0:
            continue
        entry = close[i]
        
        # LONG: TP above, SL below
        tp_barrier_long = entry + tp_atr_mult * atr[i]
        sl_barrier_long = entry - sl_atr_mult * atr[i]
        
        # SHORT: TP below, SL above
        tp_barrier_short = entry - tp_atr_mult * atr[i]
        sl_barrier_short = entry + sl_atr_mult * atr[i]
        
        end_idx = min(i + horizon, n)
        
        # LONG check (SL checked before TP)
        for j in range(i + 1, end_idx):
            if low_all[j] <= sl_barrier_long:
                break
            if high_all[j] >= tp_barrier_long:
                long_labels[i] = 1
                break
                
        # SHORT check (SL checked before TP)
        for j in range(i + 1, end_idx):
            if high_all[j] >= sl_barrier_short:
                break
            if low_all[j] <= tp_barrier_short:
                short_labels[i] = 1
                break

    long_labels[-horizon:] = np.nan
    short_labels[-horizon:] = np.nan
    
    return long_labels, short_labels

def main():
    path = "c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/data/processed/CL_HourSet_11.parquet"
    print(f"Loading data from {path}...")
    df = pd.read_parquet(path)
    
    atr_period = 14
    atr_col = f'ATR_{atr_period}'
    if atr_col not in df.columns:
        df[atr_col] = df.ta.atr(length=atr_period)
        
    configs = {
        1: [(2.0, 1.0), (3.0, 1.0), (4.0, 1.0)],
        3: [(2.0, 1.0), (3.0, 1.0), (4.0, 1.0)],
        36: [(3.0, 1.0), (4.0, 1.0), (5.0, 1.0), (6.0, 1.0), (6.0, 2.0), (8.0, 2.0), (8.0, 3.0)],
        48: [(3.0, 1.0), (4.0, 1.0), (5.0, 1.0), (6.0, 1.0), (6.0, 2.0), (8.0, 2.0), (8.0, 3.0)]
    }
    
    results = []
    
    for horizon, ratios in configs.items():
        for tp, sl in ratios:
            long_labels, short_labels = compute_barrier(df, horizon, tp, sl, atr_period)
            
            valid_long = long_labels[~np.isnan(long_labels)]
            valid_short = short_labels[~np.isnan(short_labels)]
            
            long_true = np.sum(valid_long == 1)
            long_false = np.sum(valid_long == 0)
            short_true = np.sum(valid_short == 1)
            short_false = np.sum(valid_short == 0)
            
            long_true_pct = (long_true / len(valid_long) * 100) if len(valid_long) > 0 else 0
            short_true_pct = (short_true / len(valid_short) * 100) if len(valid_short) > 0 else 0
            
            long_imb = long_false / long_true if long_true > 0 else float('inf')
            short_imb = short_false / short_true if short_true > 0 else float('inf')
            
            results.append({
                'Horizon': horizon,
                'TP': tp,
                'SL': sl,
                'Type': 'Long',
                'True%': round(long_true_pct, 2),
                'Imbalance': round(long_imb, 2)
            })
            results.append({
                'Horizon': horizon,
                'TP': tp,
                'SL': sl,
                'Type': 'Short',
                'True%': round(short_true_pct, 2),
                'Imbalance': round(short_imb, 2)
            })
            
    res_df = pd.DataFrame(results)
    print("\n--- Distribution Table ---")
    print(res_df.to_string(index=False))

if __name__ == "__main__":
    main()
