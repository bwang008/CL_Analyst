import time
import numpy as np
import pandas as pd
import pandas_ta as ta
import sys
import os

# Add src to path so we can import _rolling_slope_r2_numba
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from src.features.alpha_factory import _rolling_slope_r2_numba

def run_comparison():
    # 1. Generate test data
    np.random.seed(42)
    n_rows = 5000
    window = 1000 # Large window to show performance diff, but small enough to not hang forever
    
    print(f"Generating test data: {n_rows} rows, window: {window} bars...")
    prices = 100.0 + np.cumsum(np.random.randn(n_rows))
    volumes = np.random.randint(100, 1000, size=n_rows)
    
    close = pd.Series(prices)
    volume = pd.Series(volumes)
    
    # Calculate OBV
    obv = ta.obv(close, volume)
    
    # 2. Time pandas_ta.linreg
    print("\n--- Running pandas_ta.linreg ---")
    start_time = time.perf_counter()
    # Note: ta.linreg returns a pandas Series if slope=True and it's a single return, 
    # but internally it does list comprehension over sliding_window_view
    ta_slope = ta.linreg(obv, length=window, slope=True)
    ta_time = time.perf_counter() - start_time
    print(f"pandas_ta wall clock time: {ta_time:.4f} seconds")
    
    # 3. Time Numba implementation
    print("\n--- Running _rolling_slope_r2_numba ---")
    # First run includes Numba compilation overhead, so we do a tiny warmup run
    _ = _rolling_slope_r2_numba(obv.to_numpy()[:window+2], window)
    
    start_time = time.perf_counter()
    obv_arr = obv.to_numpy(dtype=np.float64)
    numba_slope, _ = _rolling_slope_r2_numba(obv_arr, window)
    numba_time = time.perf_counter() - start_time
    print(f"Numba wall clock time: {numba_time:.4f} seconds")
    
    # 4. Compare parity
    print("\n--- Checking Parity ---")
    ta_slope_arr = ta_slope.to_numpy() if isinstance(ta_slope, pd.Series) else ta_slope.iloc[:, 0].to_numpy()
    
    # Ignore initial NaNs
    valid_mask = ~np.isnan(numba_slope) & ~np.isnan(ta_slope_arr)
    valid_count = valid_mask.sum()
    
    diffs = np.abs(ta_slope_arr[valid_mask] - numba_slope[valid_mask])
    max_diff = np.max(diffs)
    
    print(f"Compared {valid_count} valid windows.")
    print(f"Max absolute difference: {max_diff:.10e}")
    
    if max_diff < 1e-8:
        print("✅ Parity verified! Outputs are effectively identical.")
    else:
        print("❌ Parity failed! Significant difference found.")
        
    print(f"\nSpeedup Factor: {ta_time / numba_time:.2f}x faster")

if __name__ == "__main__":
    run_comparison()
