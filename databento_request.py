import pandas as pd
import databento as db
import numpy as np

def back_adjust_continuous_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Post-processing function to parse Databento's raw CSV format, detect rollovers 
    via instrument_id, convert fixed-precision integers to dollar decimals, 
    and apply backward Ratio Scaling to the historical OHLC prices.
    """
    if df.empty or 'instrument_id' not in df.columns:
        return df
        
    df_adj = df.copy()
    
    # Convert Unix nanosecond timestamps to human-readable datetime
    if 'ts_event' in df_adj.columns:
        df_adj['ts_event'] = pd.to_datetime(df_adj['ts_event'], unit='ns', utc=True)
    
    # 1. Convert fixed-precision Databento integers to standard dollar decimals (divide by 1e9)
    ohlc_cols = ['open', 'high', 'low', 'close']
    for col in ohlc_cols:
        if col in df_adj.columns:
            df_adj[col] = df_adj[col] / 1e9
            
    # 2. Detect contract rollovers using instrument_id
    # True when the instrument_id changes from the previous row
    df_adj['is_roll'] = (df_adj['instrument_id'] != df_adj['instrument_id'].shift(1)) & (df_adj['instrument_id'].shift(1).notna())
    
    # 3. Calculate Ratio Multiplier at each rollover
    # Factor = Open of new contract / Close of old contract
    df_adj['roll_factor'] = 1.0
    
    # We only apply the factor on the exact row where the roll happens
    roll_mask = df_adj['is_roll']
    df_adj.loc[roll_mask, 'roll_factor'] = df_adj.loc[roll_mask, 'open'] / df_adj['close'].shift(1)[roll_mask]
    
    # 4. Calculate Cumulative Ratio Factor (Backward)
    # Because we anchor to the CURRENT live price, the newest data has a multiplier of 1.0.
    # We must propagate the multiplier BACKWARDS into history.
    df_adj['roll_factor'] = df_adj['roll_factor'].replace([np.inf, -np.inf], 1.0).fillna(1.0)
    
    # We use cumprod backwards
    df_adj['cum_factor'] = df_adj['roll_factor'].iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
    
    # 5. Apply Ratio Scaling to all OHLC historical prices
    for col in ohlc_cols:
        if col in df_adj.columns:
            df_adj[col] = df_adj[col] * df_adj['cum_factor']
            
    # 6. Clean up temporary columns
    df_adj = df_adj.drop(columns=['is_roll', 'roll_factor', 'cum_factor'])
    
    return df_adj

def main():
    """
    Submit a batch download request to Databento for historical futures data.
    """
    client = db.Historical("db-rn44nxsG5jfyNvhWrebEhHyQCsRed")
    
    # Fetch the earliest available start date from metadata
    dataset_range = client.metadata.get_dataset_range(dataset="GLBX.MDP3")
    earliest_start = dataset_range['start'][:10]
    today_date = pd.Timestamp.today().strftime('%Y-%m-%d')
    
    print(f"Requesting full history from {earliest_start} to {today_date}...")
    
    try:
        # Submit batch job according to exact specifications
        job = client.batch.submit_job(
            dataset="GLBX.MDP3",
            symbols="CL.v.0",
            stype_in="continuous",
            schema="ohlcv-1h",
            start=earliest_start,
            end=today_date,
            encoding="csv",
            split_duration="none",
            compression="none"
        )
        
        print("Batch job submitted successfully!")
        print(f"Job Details: {job}")
        
    except db.BentoError as e:
        print(f"Databento API Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    main()
