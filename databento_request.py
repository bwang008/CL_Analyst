import pandas as pd
import databento as db

def back_adjust_continuous_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Post-processing function to detect contract rollovers, calculate the 
    absolute difference price gaps, and back-adjust historical OHLC prices 
    to create a smooth continuous series.
    
    Assumes the DataFrame contains 'symbol', 'open', 'high', 'low', and 'close' columns.
    """
    # Ensure we have data and the required symbol column
    if df.empty or 'symbol' not in df.columns:
        return df
        
    # Create a copy to avoid modifying the original dataframe in-place
    df_adj = df.copy()
    
    # Detect contract rollovers: True when the symbol changes from the previous row
    # We ignore the first row by ensuring the previous symbol is not NaN
    df_adj['is_roll'] = (df_adj['symbol'] != df_adj['symbol'].shift(1)) & (df_adj['symbol'].shift(1).notna())
    
    # Calculate the price gap at each rollover
    # Gap = Open price of the new contract - Close price of the old contract
    df_adj['gap'] = 0.0
    df_adj.loc[df_adj['is_roll'], 'gap'] = df_adj['open'] - df_adj['close'].shift(1)
    
    # To back-adjust, we need to apply the sum of all future gaps to historical prices.
    # 1. Reverse the gaps, calculate cumulative sum, and reverse back.
    # 2. Shift by -1 so the current contract doesn't include its own rollover gap.
    # 3. Fill the newest contract's NaN with 0.
    df_adj['adjustment'] = df_adj['gap'].iloc[::-1].cumsum().iloc[::-1].shift(-1).fillna(0)
    
    # Apply the additive adjustment to all OHLC columns
    ohlc_cols = ['open', 'high', 'low', 'close']
    for col in ohlc_cols:
        if col in df_adj.columns:
            df_adj[col] = df_adj[col] + df_adj['adjustment']
            
    # Clean up temporary columns used for calculation
    df_adj = df_adj.drop(columns=['is_roll', 'gap', 'adjustment'])
    
    return df_adj

def main():
    """
    Submit a batch download request to Databento for historical futures data.
    """
    # Initialize client with placeholder API key
    client = db.Historical("db-rn44nxsG5jfyNvhWrebEhHyQCsRed")
    
    print("Submitting Databento batch job for CL.v.0...")
    
    try:
        # Submit batch job according to exact specifications
        job = client.batch.submit_job(
            dataset="GLBX.MDP3",
            symbols="CL.v.0",
            stype_in="continuous",
            schema="ohlcv-1h",
            start="2026-03-01",
            end="2026-06-13",
            encoding="csv",
            split_duration="none",
            compression="none"
        )
        
        print("Batch job submitted successfully!")
        print(f"Job Details: {job}")
        
        # Note: The data would normally be downloaded once the job completes,
        # and then passed to the back_adjust_continuous_data() function like so:
        # 
        # client.batch.download(job_id=job['id'], output_dir='data/')
        # df = pd.read_csv('data/job_file.csv')
        # adjusted_df = back_adjust_continuous_data(df)
        
    except db.BentoError as e:
        # Databento-specific exceptions (e.g., authentication, invalid parameters)
        print(f"Databento API Error: {e}")
    except Exception as e:
        # General network timeouts or other unexpected exceptions
        print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    main()
