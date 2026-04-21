import pandas as pd
df=pd.read_parquet('C:/CL_Analyst_Data/data/processed/warm_start_cache_1h.parquet')
print(len(df))
if 'DateTime' in df: print(df['DateTime'].min(), df['DateTime'].max())
