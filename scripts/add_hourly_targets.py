import pandas as pd
from src.data_processor import DataProcessor
import time
import os

def main():
    print('Loading HourSet_03...')
    start_time = time.time()
    input_file = r'C:\CL_Analyst_Data\data\processed\cl-1h_bk_HourSet_03.parquet'
    output_file = r'C:\CL_Analyst_Data\data\processed\cl-1h_bk_HourSet_04.parquet'
    
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
    
    tp_mult = 2.0
    sl_mult = 1.0
    atr_period = 14
    
    for label, max_bars in horizons.items():
        print(f'Adding target TARGET_TRIPLE_2x1_{label}...')
        start_t = time.time()
        df = dp.add_triple_barrier_target(
            df, 
            prefix=f'TARGET_TRIPLE_2x1_{label}', 
            tp_atr_mult=tp_mult, 
            sl_atr_mult=sl_mult, 
            max_horizon=max_bars, 
            atr_period=atr_period
        )
        print(f'  Target {label} added in {time.time() - start_t:.2f}s')

    print(f'New dataset shape: {df.shape}')
    
    print(f'Saving to {output_file}...')
    start_time = time.time()
    df.to_parquet(output_file, engine='pyarrow')
    print(f'Saved in {time.time() - start_time:.2f}s')
    
    # Print out the new target columns
    new_targets = [c for c in df.columns if 'TARGET_TRIPLE_2x1' in c]
    print(f'New Target Columns: {new_targets}')
    print('Done!')

if __name__ == "__main__":
    main()
