import pandas as pd
from pathlib import Path

root = Path(r'C:\CL_Analyst_Data')
seed_1h = root / 'data' / 'processed' / 'cl-1h_bk_HourSet_02.parquet'
cache_1h = root / 'data' / 'processed' / 'warm_start_cache_1h.parquet'

if seed_1h.exists():
    df = pd.read_parquet(seed_1h)
    min_date = df['DateTime'].min() if 'DateTime' in df.columns else df.index.min()
    max_date = df['DateTime'].max() if 'DateTime' in df.columns else df.index.max()
    print(f'Seed 1h exists: {len(df)} rows, min: {min_date}, max: {max_date}')
else:
    print('Seed 1h missing')

if cache_1h.exists():
    dfc = pd.read_parquet(cache_1h)
    min_date_c = dfc['DateTime'].min() if 'DateTime' in dfc.columns else dfc.index.min()
    max_date_c = dfc['DateTime'].max() if 'DateTime' in dfc.columns else dfc.index.max()
    print(f'Cache 1h exists: {len(dfc)} rows, min: {min_date_c}, max: {max_date_c}')
else:
    print('Cache 1h missing')
