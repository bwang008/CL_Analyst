import pandas as pd
from src.data_processor import DataProcessor
import time

def main():
    print('Loading set_11...')
    start_time = time.time()
    df = pd.read_parquet('data/processed/CL_set_11.parquet')
    print(f'Loaded in {time.time() - start_time:.2f}s, shape: {df.shape}')
    
    dp = DataProcessor('data/cl-5m.csv', keep_ohlcv=True)
    
    print('Adding target...')
    start_time = time.time()
    df = dp.add_triple_barrier_target(
        df, prefix='TARGET_TRIPLE_1.5x0.75_12H', 
        tp_atr_mult=1.5, sl_atr_mult=0.75, 
        max_horizon=144, atr_period=14
    )
    print(f'Target added in {time.time() - start_time:.2f}s, new shape: {df.shape}')
    
    print('Saving...')
    start_time = time.time()
    df.to_parquet('data/processed/CL_set_11.parquet', engine='pyarrow')
    print(f'Saved in {time.time() - start_time:.2f}s')
    print('Done!')

if __name__ == "__main__":
    main()
