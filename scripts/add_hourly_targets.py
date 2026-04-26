import pandas as pd
from src.data_processor import DataProcessor
import time
import os

def main():
    print('Loading HourSet_04...')
    start_time = time.time()
    input_file = r'C:\CL_Analyst_Data\data\processed\cl-1h_bk_HourSet_04.parquet'
    output_file = r'C:\CL_Analyst_Data\data\processed\cl-1h_bk_HourSet_05.parquet'
    
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")
        
    df = pd.read_parquet(input_file)
    print(f'Loaded in {time.time() - start_time:.2f}s, shape: {df.shape}')
    
    # Instantiate DataProcessor. We can pass a dummy file path since we're just using its methods.
    dp = DataProcessor('data/dummy.csv', keep_ohlcv=True)
    
    horizons = {
        '3H': 3,
        '6H': 6,
        '12H': 12
    }
    
    atr_period = 14
    
    # ── Set 1: 1x ATR TP, 0.5x ATR SL ──────────────────────────
    tp_mult_1 = 1.0
    sl_mult_1 = 0.5
    tag_1 = '1x0.5'
    
    for label, max_bars in horizons.items():
        prefix = f'TARGET_TRIPLE_{tag_1}_{label}'
        print(f'Adding target {prefix} (TP={tp_mult_1}, SL={sl_mult_1}, horizon={max_bars})...')
        start_t = time.time()
        df = dp.add_triple_barrier_target(
            df, 
            prefix=prefix, 
            tp_atr_mult=tp_mult_1, 
            sl_atr_mult=sl_mult_1, 
            max_horizon=max_bars, 
            atr_period=atr_period
        )
        print(f'  Target {prefix} added in {time.time() - start_t:.2f}s')

    # ── Set 2: 1x ATR TP, 2x ATR SL ────────────────────────────
    tp_mult_2 = 1.0
    sl_mult_2 = 2.0
    tag_2 = '1x2'
    
    for label, max_bars in horizons.items():
        prefix = f'TARGET_TRIPLE_{tag_2}_{label}'
        print(f'Adding target {prefix} (TP={tp_mult_2}, SL={sl_mult_2}, horizon={max_bars})...')
        start_t = time.time()
        df = dp.add_triple_barrier_target(
            df, 
            prefix=prefix, 
            tp_atr_mult=tp_mult_2, 
            sl_atr_mult=sl_mult_2, 
            max_horizon=max_bars, 
            atr_period=atr_period
        )
        print(f'  Target {prefix} added in {time.time() - start_t:.2f}s')

    print(f'New dataset shape: {df.shape}')
    
    print(f'Saving to {output_file}...')
    start_time = time.time()
    df.to_parquet(output_file, engine='pyarrow')
    print(f'Saved in {time.time() - start_time:.2f}s')
    
    # Print out ALL target columns for verification
    all_targets = sorted([c for c in df.columns if c.startswith('TARGET_TRIPLE')])
    print(f'\nAll Triple Barrier Target Columns ({len(all_targets)}):')
    for t in all_targets:
        print(f'  {t}')
    print('Done!')

if __name__ == "__main__":
    main()
