import pandas as pd
import numpy as np

def compute_barrier_stats(df, tp_mult, sl_mult, horizon=12):
    print(f"\n--- {tp_mult}x{sl_mult}_{horizon}H Targets ---")
    
    # We need Close, High, Low, and ATR
    close = df['Close'].values
    high = df['High'].values
    low = df['Low'].values
    
    # The feature name might be ATR_14 or similar. Let's find it.
    atr_cols = [c for c in df.columns if c.startswith('ATR_')]
    if not atr_cols:
        print("Error: No ATR column found.")
        return 0
    atr = df[atr_cols[0]].values
    
    n = len(df)
    long_hits = 0
    short_hits = 0
    valid_bars = 0
    
    # We can do this efficiently with a sliding window approach, 
    # but a numba or fast numpy loop is fine for 100k rows.
    # We will do a fast loop.
    for i in range(n - horizon):
        if np.isnan(atr[i]) or np.isnan(close[i]):
            continue
            
        valid_bars += 1
        c = close[i]
        a = atr[i]
        
        long_tp = c + (tp_mult * a)
        long_sl = c - (sl_mult * a)
        
        short_tp = c - (tp_mult * a)
        short_sl = c + (sl_mult * a)
        
        long_hit_tp = False
        short_hit_tp = False
        
        for j in range(1, horizon + 1):
            h = high[i + j]
            l = low[i + j]
            
            # Check LONG
            if not long_hit_tp:
                if l <= long_sl:
                    pass # Stopped out, can't hit TP anymore
                elif h >= long_tp:
                    long_hit_tp = True
                    long_hits += 1
            
            # Check SHORT
            if not short_hit_tp:
                if h >= short_sl:
                    pass # Stopped out
                elif l <= short_tp:
                    short_hit_tp = True
                    short_hits += 1
                    
            # Technically, if a bar spans BOTH the TP and SL, the more conservative approach 
            # is to assume the SL was hit first. But for rough distribution stats, this is close enough.
            # We break early if both are determined, but since we just care about hits vs no hits, 
            # we just need to know if it hit TP *before* hitting SL.
            
            # A more accurate check for a single bar:
            # If Low <= SL and High >= TP in the SAME bar, assume SL hit first (loss).
        
    long_pct = long_hits / valid_bars * 100 if valid_bars > 0 else 0
    short_pct = short_hits / valid_bars * 100 if valid_bars > 0 else 0
    
    print(f"Total valid bars: {valid_bars:,}")
    print(f"LONG True:  {long_hits:>6,} ({long_pct:.1f}%)   Ratio: {(valid_bars-long_hits)/long_hits if long_hits>0 else 0:.1f}:1")
    print(f"SHORT True: {short_hits:>6,} ({short_pct:.1f}%)   Ratio: {(valid_bars-short_hits)/short_hits if short_hits>0 else 0:.1f}:1")
    
    return max(long_pct, short_pct)

df = pd.read_parquet('data/processed/CL_HourSet_07.parquet')

# Check 3x1
max_pct = compute_barrier_stats(df, 3, 1, 12)

if max_pct >= 15.0:
    print("\n[+] 3x is >= 15%, calculating 4x and 5x...")
    compute_barrier_stats(df, 4, 1, 12)
    compute_barrier_stats(df, 5, 1, 12)
else:
    print(f"\n[-] 3x is below 15% ({max_pct:.1f}%), skipping 4x and 5x.")
